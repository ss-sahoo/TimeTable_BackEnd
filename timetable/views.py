from __future__ import annotations

"""
Simple JSON APIs to expose timetable data for the optimisation code.

These views do NOT implement the optimisation algorithm themselves.
They only:
1) Read from Django models
2) Build plain Python dicts/lists in the shapes your Python code expects
3) Return them as JSON so you can plug them directly into your optimiser
"""

from django.http import JsonResponse, Http404
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime, timedelta
from django.conf import settings

from accounts.models import Center, Batch, User as AccountUser
from .optimization import build_full_payload, DAY_MAP_SHORT
from .models import Timetable, DaySlot, TimetableHoliday, TeacherSlotAvailability, BatchFacultyLoad, FixedSlot, TimetableEntry, TimetableBatch
from django.db.models import Q
from .genetic_algorithm import check_timetable_feasibility_from_start, generate_random_timetable, generate_new_fixed_slots, run_genetic_algorithm, fitness_score, check_constraints
from .algorithm_adapter import convert_teachers_to_algorithm_format, convert_batches_to_algorithm_format
from django.db import transaction


@require_GET
def timetable_payload(request, timetable_id: str):
    """
    GET /api/timetables/<timetable_id>/payload/

    Returns JSON:
    {
      "available_slots": {...},
      "teachers": [...],
      "batches": {...},
      "fixed_slots": {...}
    }

    You can feed this directly to your optimisation code
    instead of reading from Excel.
    """

    try:
        payload = build_full_payload(timetable_id)
    except Exception as exc:  # pragma: no cover - simple error wrapper
        raise Http404(str(exc))

    return JsonResponse(payload, json_dumps_params={"indent": 2})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_timetable_with_slots(request):
    """
    Create a Timetable with weekly slots and holidays.

    Allowed roles:
    - ADMIN (center admins): center is taken from request.user.center
    - SUPER_ADMIN: must provide center_name in payload

    Example payload:
    {
      "center_name": "Allen - Jaipur Center",  # required only for SUPER_ADMIN
      "name": "JEE Main 2025 Schedule",  # optional timetable name
      "from_date": "2025-01-01",
      "to_date": "2025-03-31",
      "free_classes_count": 3,
      "weekly_slots": {
        "mon": [
          {"code": "m1", "start": "08:00", "end": "09:30", "is_free_class": false},
          {"code": "m2", "start": "09:40", "end": "11:10", "is_free_class": false}
        ],
        "tue": [
          {"code": "tu1", "start": "08:00", "end": "09:30", "is_free_class": false}
        ]
      },
      "holidays": [
        {"date": "2025-01-26", "description": "Republic Day"},
        {"date": "2025-03-08", "description": "Internal Exam"}
      ]
    }
    """

    user = request.user
    # Make role check case-insensitive to handle both 'admin'/'ADMIN' and 'super_admin'/'SUPER_ADMIN'
    user_role_lower = user.role.lower() if user.role else ''
    if user_role_lower not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can create timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )

    data = request.data

    center = None
    if user_role_lower == AccountUser.ROLE_ADMIN.lower():
        center = user.center
        if not center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:  # SUPER_ADMIN
        center_id = data.get("center_id")
        center_name = data.get("center_name")
        
        if center_id:
            try:
                center = Center.objects.get(id=center_id)
            except Center.DoesNotExist:
                return Response(
                    {"detail": f"Center with ID '{center_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except (ValueError, TypeError):
                 return Response(
                    {"detail": f"Invalid center_id format: '{center_id}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif center_name:
            try:
                center = Center.objects.get(name=center_name)
            except Center.DoesNotExist:
                return Response(
                    {"detail": f"Center '{center_name}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except Center.MultipleObjectsReturned:
                return Response(
                    {
                        "detail": (
                            f"Multiple centers found with name '{center_name}'. "
                            "Please use center_id instead."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            return Response(
                {"detail": "center_id or center_name is required for Super Admin."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    from_date_str = data.get("from_date")
    to_date_str = data.get("to_date")
    free_classes_count = data.get("free_classes_count", 0)
    weekly_slots = data.get("weekly_slots", {})
    holidays = data.get("holidays", [])

    if not from_date_str or not to_date_str:
        return Response(
            {"detail": "from_date and to_date are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        to_date = datetime.strptime(to_date_str, "%Y-%m-%d").date()
    except ValueError:
        return Response(
            {"detail": "from_date and to_date must be in YYYY-MM-DD format."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if from_date > to_date:
        return Response(
            {"detail": "from_date cannot be after to_date."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Create timetable
    timetable = Timetable.objects.create(
        center=center,
        name=data.get("name", ""),
        from_date=from_date,
        to_date=to_date,
        free_classes_count=free_classes_count,
        description=data.get("description", ""),
    )

    # Map legacy weekly day keys to DaySlot constants
    day_map = {
        "mon": DaySlot.MONDAY,
        "tue": DaySlot.TUESDAY,
        "wed": DaySlot.WEDNESDAY,
        "thu": DaySlot.THURSDAY,
        "fri": DaySlot.FRIDAY,
        "sat": DaySlot.SATURDAY,
        "sun": DaySlot.SUNDAY,
    }

    # Helper to compute weekday constant from an actual date
    weekday_constants = [
        DaySlot.MONDAY,
        DaySlot.TUESDAY,
        DaySlot.WEDNESDAY,
        DaySlot.THURSDAY,
        DaySlot.FRIDAY,
        DaySlot.SATURDAY,
        DaySlot.SUNDAY,
    ]

    total_days = (to_date - from_date).days + 1

    created_slots = 0
    for day_key, slots in weekly_slots.items():
        key_lower = str(day_key).lower()

        # Mode 1: legacy weekly timetable using mon/tue/...
        if key_lower in day_map:
            day_const = day_map[key_lower]
            day_index = None
            actual_date = None
        # Mode 2: date-based timetable using D1, D2, ... within [from_date, to_date]
        elif key_lower.startswith("d") and len(key_lower) > 1 and key_lower[1:].isdigit():
            idx = int(key_lower[1:])
            if idx < 1:
                # Skip invalid day indices (must be >= 1)
                continue
            if idx > total_days:
                # Skip days outside timetable range
                continue
            day_index = idx
            actual_date = from_date + timedelta(days=idx - 1)
            weekday_idx = actual_date.weekday()  # 0 = Monday
            day_const = weekday_constants[weekday_idx]
        else:
            # Unknown day key; skip
            continue

        slot_number = 0
        for slot in slots:
            slot_number += 1
            code = slot.get("code") or f"{day_key}{slot_number}"
            start_str = slot.get("start")
            end_str = slot.get("end")
            is_free = bool(slot.get("is_free_class", False))

            if not start_str or not end_str:
                continue

            try:
                start_time = datetime.strptime(start_str, "%H:%M").time()
                end_time = datetime.strptime(end_str, "%H:%M").time()
            except ValueError:
                continue

            DaySlot.objects.create(
                timetable=timetable,
                day=day_const,
                day_index=day_index,
                actual_date=actual_date,
                slot_code=code,
                slot_number=slot_number,
                start_time=start_time,
                end_time=end_time,
                is_free_class=is_free,
            )
            created_slots += 1

    # Create holidays
    created_holidays = 0
    for h in holidays:
        date_str = h.get("date")
        description = h.get("description", "")
        if not date_str:
            continue
        try:
            h_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        TimetableHoliday.objects.create(
            timetable=timetable,
            date=h_date,
            description=description,
            is_full_day=True,
        )
        created_holidays += 1

    return Response(
        {
            "message": "Timetable created successfully.",
            "timetable_id": str(timetable.id),
            "name": timetable.name,
            "center": center.name,
            "from_date": str(from_date),
            "to_date": str(to_date),
            "slots_created": created_slots,
            "holidays_created": created_holidays,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def update_timetable(request, timetable_id: str):
    """
    Update an existing Timetable.
    
    PUT/PATCH /api/timetable/admin/timetables/<timetable_id>/update/

    Allowed roles:
    - ADMIN (center admins): can only update their center's timetables
    - SUPER_ADMIN: can update any timetable

    Example payload:
    {
      "from_date": "2025-01-01",        # Optional
      "to_date": "2025-03-31",          # Optional
      "free_classes_count": 3,          # Optional
      "description": "Updated desc",    # Optional
      "name": "Timetable Name",         # Optional
      "weekly_slots": {...},            # Optional - replaces all slots if provided
      "holidays": [...]                 # Optional - replaces all holidays if provided
    }
    
    Note: To activate/deactivate a timetable, use the dedicated endpoints:
    - POST /api/timetable/admin/timetables/<id>/activate/
    - POST /api/timetable/admin/timetables/<id>/deactivate/
    """
    user = request.user
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can update timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Check permissions: Admin can only update their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only update timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )

    data = request.data

    # Update basic fields if provided
    if "from_date" in data:
        try:
            timetable.from_date = datetime.strptime(data["from_date"], "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "from_date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if "to_date" in data:
        try:
            timetable.to_date = datetime.strptime(data["to_date"], "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "to_date must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if timetable.from_date > timetable.to_date:
        return Response(
            {"detail": "from_date cannot be after to_date."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if "free_classes_count" in data:
        timetable.free_classes_count = data["free_classes_count"]

    if "name" in data:
        timetable.name = data["name"]

    if "description" in data:
        timetable.description = data["description"]

    # Note: is_active should only be changed via activate/deactivate endpoints
    # if "is_active" in data:
    #     timetable.is_active = data["is_active"]

    timetable.save()

    # Update slots if provided
    slots_updated = 0
    if "weekly_slots" in data:
        weekly_slots = data["weekly_slots"]
        
        # Delete existing slots
        DaySlot.objects.filter(timetable=timetable).delete()

        # Map legacy weekly day keys to DaySlot constants
        day_map = {
            "mon": DaySlot.MONDAY,
            "tue": DaySlot.TUESDAY,
            "wed": DaySlot.WEDNESDAY,
            "thu": DaySlot.THURSDAY,
            "fri": DaySlot.FRIDAY,
            "sat": DaySlot.SATURDAY,
            "sun": DaySlot.SUNDAY,
        }

        weekday_constants = [
            DaySlot.MONDAY,
            DaySlot.TUESDAY,
            DaySlot.WEDNESDAY,
            DaySlot.THURSDAY,
            DaySlot.FRIDAY,
            DaySlot.SATURDAY,
            DaySlot.SUNDAY,
        ]

        total_days = (timetable.to_date - timetable.from_date).days + 1

        for day_key, slots in weekly_slots.items():
            key_lower = str(day_key).lower()

            if key_lower in day_map:
                day_const = day_map[key_lower]
                day_index = None
                actual_date = None
            elif key_lower.startswith("d") and len(key_lower) > 1 and key_lower[1:].isdigit():
                idx = int(key_lower[1:])
                if idx < 1 or idx > total_days:
                    continue
                day_index = idx
                actual_date = timetable.from_date + timedelta(days=idx - 1)
                weekday_idx = actual_date.weekday()
                day_const = weekday_constants[weekday_idx]
            else:
                continue

            slot_number = 0
            for slot in slots:
                slot_number += 1
                code = slot.get("code") or f"{day_key}{slot_number}"
                start_str = slot.get("start")
                end_str = slot.get("end")
                is_free = bool(slot.get("is_free_class", False))

                if not start_str or not end_str:
                    continue

                try:
                    start_time = datetime.strptime(start_str, "%H:%M").time()
                    end_time = datetime.strptime(end_str, "%H:%M").time()
                except ValueError:
                    continue

                DaySlot.objects.create(
                    timetable=timetable,
                    day=day_const,
                    day_index=day_index,
                    actual_date=actual_date,
                    slot_code=code,
                    slot_number=slot_number,
                    start_time=start_time,
                    end_time=end_time,
                    is_free_class=is_free,
                )
                slots_updated += 1

    # Update holidays if provided
    holidays_updated = 0
    if "holidays" in data:
        holidays = data["holidays"]
        
        # Delete existing holidays
        TimetableHoliday.objects.filter(timetable=timetable).delete()

        for h in holidays:
            date_str = h.get("date")
            description = h.get("description", "")
            if not date_str:
                continue
            try:
                h_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            TimetableHoliday.objects.create(
                timetable=timetable,
                date=h_date,
                description=description,
                is_full_day=True,
            )
            holidays_updated += 1

    response_data = {
        "message": "Timetable updated successfully.",
        "timetable_id": str(timetable.id),
        "center": timetable.center.name,
        "from_date": str(timetable.from_date),
        "to_date": str(timetable.to_date),
        "free_classes_count": timetable.free_classes_count,
        "is_active": timetable.is_active,
    }

    if "weekly_slots" in data:
        response_data["slots_updated"] = slots_updated
    if "holidays" in data:
        response_data["holidays_updated"] = holidays_updated

    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_timetable(request, timetable_id: str):
    """
    Get timetable details by ID.
    
    GET /api/timetables/<timetable_id>/
    
    Returns:
    {
      "id": "uuid",
      "center": "Allen - Jaipur Center",
      "from_date": "2025-01-01",
      "to_date": "2025-03-31",
      "free_classes_count": 3,
      "is_active": true,
      "description": "...",
      "created_at": "...",
      "updated_at": "..."
    }
    """
    try:
        timetable = Timetable.objects.get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only see their center's timetables
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if user.center != timetable.center:
            return Response(
                {"detail": "You don't have permission to view this timetable."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    return Response(
        {
            "id": str(timetable.id),
            "name": timetable.name,
            "center": timetable.center.name,
            "center_id": str(timetable.center.id),
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "free_classes_count": timetable.free_classes_count,
            "is_active": timetable.is_active,
            "description": timetable.description,
            "created_at": timetable.created_at.isoformat(),
            "updated_at": timetable.updated_at.isoformat(),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_timetable_slots(request, timetable_id: str):
    """
    Get all slots for a timetable, organized by day.
    
    GET /api/timetables/<timetable_id>/slots/
    
    Returns:
    {
      "timetable_id": "uuid",
      "timetable": "Allen - Jaipur Center - 2025-01-01 to 2025-03-31",
      "slots": {
        "mon": [
          {
            "id": "uuid",
            "code": "m1",
            "slot_number": 1,
            "start_time": "08:00",
            "end_time": "09:30",
            "is_free_class": false
          },
          ...
        ],
        "tue": [...],
        ...
      },
      "total_slots": 10
    }
    """
    try:
        timetable = Timetable.objects.get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if user.center != timetable.center:
            return Response(
                {"detail": "You don't have permission to view this timetable."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all slots for this timetable
    day_slots = DaySlot.objects.filter(timetable=timetable).order_by("day_index", "day", "slot_number")

    # Organize by logical day key
    slots_by_day = {}
    
    total_slots = 0
    for slot in day_slots:
        # If day_index is set, expose D1, D2, ...; otherwise fall back to weekly day keys
        if slot.day_index:
            day_key = f"d{slot.day_index}"
        else:
            if slot.day == DaySlot.MONDAY:
                day_key = "mon"
            elif slot.day == DaySlot.TUESDAY:
                day_key = "tue"
            elif slot.day == DaySlot.WEDNESDAY:
                day_key = "wed"
            elif slot.day == DaySlot.THURSDAY:
                day_key = "thu"
            elif slot.day == DaySlot.FRIDAY:
                day_key = "fri"
            elif slot.day == DaySlot.SATURDAY:
                day_key = "sat"
            elif slot.day == DaySlot.SUNDAY:
                day_key = "sun"
            else:
                continue

        slots_by_day.setdefault(day_key, [])
        slots_by_day[day_key].append({
            "id": str(slot.id),
            "code": slot.slot_code,
            "slot_number": slot.slot_number,
            "start_time": slot.start_time.strftime("%H:%M"),
            "end_time": slot.end_time.strftime("%H:%M"),
            "is_free_class": slot.is_free_class,
            "day_index": slot.day_index,
            "actual_date": str(slot.actual_date) if slot.actual_date else None,
        })
        total_slots += 1
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "slots": slots_by_day,
            "total_slots": total_slots,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_free_classes_count(request, timetable_id: str):
    """
    Admin sets the number of free classes for a timetable.
    
    Payload:
    {
        "free_classes_count": 3
    }
    
    Returns:
    {
        "message": "Free classes count updated successfully.",
        "timetable_id": "uuid",
        "free_classes_count": 3
    }
    """
    user = request.user
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can set free classes count."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        timetable = Timetable.objects.select_related('center').get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": f"Timetable with id '{timetable_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only update timetables in their center
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": f"You can only update timetables in your center '{user.center.name}'."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    free_classes_count = request.data.get("free_classes_count")
    
    if free_classes_count is None:
        return Response(
            {"detail": "free_classes_count is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        free_classes_count = int(free_classes_count)
        if free_classes_count < 0:
            return Response(
                {"detail": "free_classes_count must be a non-negative integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except (ValueError, TypeError):
        return Response(
            {"detail": "free_classes_count must be a valid integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Update free classes count
    timetable.free_classes_count = free_classes_count
    timetable.save()
    
    # Update all DaySlots to reflect the new free classes count
    # Mark first N slots as free classes for each day
    from django.db import transaction
    with transaction.atomic():
        # Get all unique days in this timetable
        days = DaySlot.objects.filter(timetable=timetable).values_list('day', flat=True).distinct()
        
        for day in days:
            # Get all slots for this day, ordered by slot_number
            slots = DaySlot.objects.filter(timetable=timetable, day=day).order_by('slot_number')
            
            for idx, slot in enumerate(slots):
                # Mark first free_classes_count slots as free
                slot.is_free_class = (idx < free_classes_count)
                slot.save()
    
    return Response(
        {
            "message": "Free classes count updated successfully.",
            "timetable_id": str(timetable.id),
            "free_classes_count": timetable.free_classes_count,
            "center": timetable.center.name,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_free_classes_count(request, timetable_id: str):
    """
    Get the number of free classes for a timetable.
    
    Returns:
    {
        "timetable_id": "uuid",
        "free_classes_count": 3,
        "center": "Allen - Jaipur Center"
    }
    """
    try:
        timetable = Timetable.objects.select_related('center').get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": f"Timetable with id '{timetable_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only view timetables in their center
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": f"You can only view timetables in your center '{user.center.name}'."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "free_classes_count": timetable.free_classes_count,
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_timetables(request):
    """
    List all timetables (filtered by center for Admin).
    
    GET /api/timetables/
    GET /api/timetables/?center_id=uuid  (for Super Admin)
    GET /api/timetables/?center_name=Allen - Jaipur Center  (for Super Admin)
    
    Returns:
    {
      "timetables": [
        {
          "id": "uuid",
          "center": "Allen - Jaipur Center",
          "from_date": "2025-01-01",
          "to_date": "2025-03-31",
          "is_active": true,
          "slots_count": 10,
          "holidays_count": 2
        },
        ...
      ],
      "total": 5
    }
    """
    user = request.user
    
    # Filter by center based on role
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        timetables = Timetable.objects.filter(center=user.center)
    elif user.role.lower() == AccountUser.ROLE_SUPER_ADMIN.lower():
        # Super Admin can filter by center_id, center_name, or see all
        center_id = request.query_params.get("center_id")
        center_name = request.query_params.get("center_name")
        
        if center_id:
            try:
                center = Center.objects.get(id=center_id)
                timetables = Timetable.objects.filter(center=center)
            except Center.DoesNotExist:
                return Response(
                    {"detail": f"Center with id '{center_id}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        elif center_name:
            try:
                center = Center.objects.get(name=center_name)
                timetables = Timetable.objects.filter(center=center)
            except Center.DoesNotExist:
                return Response(
                    {"detail": f"Center '{center_name}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        else:
            timetables = Timetable.objects.all()
    elif user.role.lower() in ('teacher',):
        # Teachers see timetables for their center
        if user.center:
            timetables = Timetable.objects.filter(center=user.center)
        else:
            timetables = Timetable.objects.none()
    elif user.role.lower() in ('student',):
        # Students see timetables for their center
        if user.center:
            timetables = Timetable.objects.filter(center=user.center)
        else:
            # Try to find center from enrolled batches
            from accounts.models import Enrollment
            enrolled_centers = Enrollment.objects.filter(
                student=user,
                status=Enrollment.STATUS_ACTIVE
            ).values_list('batch__center', flat=True).distinct()
            if enrolled_centers:
                timetables = Timetable.objects.filter(center_id__in=enrolled_centers)
            else:
                timetables = Timetable.objects.none()
    else:
        return Response(
            {"detail": "You don't have permission to view timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Order by date (newest first)
    timetables = timetables.order_by("-from_date", "-to_date")
    
    # Build response
    timetable_list = []
    for tt in timetables:
        slots_count = DaySlot.objects.filter(timetable=tt).count()
        holidays_count = TimetableHoliday.objects.filter(timetable=tt).count()
        
        timetable_list.append({
            "id": str(tt.id),
            "name": tt.name,
            "center": tt.center.name,
            "from_date": str(tt.from_date),
            "to_date": str(tt.to_date),
            "free_classes_count": tt.free_classes_count,
            "is_active": tt.is_active,
            "description": tt.description,
            "slots_count": slots_count,
            "holidays_count": holidays_count,
            "created_at": tt.created_at.isoformat(),
        })
    
    return Response(
        {
            "timetables": timetable_list,
            "total": len(timetable_list),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_teacher_slot_availability(request):
    """
    Admin sets teacher availability for a specific slot in a timetable.
    
    By default, teachers are available. Admin can toggle availability.
    When a teacher is marked as available in one timetable for a slot,
    they become unavailable for the same time slot in other overlapping timetables
    within the same center.
    
    Payload:
    {
        "timetable_id": "uuid",
        "day_slot_id": "uuid",
        "teacher_code": "AK-CAP",  # or teacher username/email
        "is_available": true  # or false
    }
    
    Returns:
    {
        "message": "Teacher availability updated successfully.",
        "teacher": "AK-CAP",
        "slot_code": "m1",
        "day": "Monday",
        "is_available": true
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can set teacher availability."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # If Admin, ensure they can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    timetable_id = request.data.get("timetable_id")
    day_slot_id = request.data.get("day_slot_id")
    teacher_identifier = request.data.get("teacher_code") or request.data.get("teacher_username") or request.data.get("teacher_email")
    is_available = request.data.get("is_available")
    
    if not timetable_id or not day_slot_id or not teacher_identifier:
        return Response(
            {"detail": "timetable_id, day_slot_id, and teacher_code/username/email are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    if is_available is None:
        return Response(
            {"detail": "is_available (true/false) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get day slot
    try:
        day_slot = DaySlot.objects.get(id=day_slot_id, timetable=timetable)
    except DaySlot.DoesNotExist:
        return Response(
            {"detail": "Day slot not found in this timetable."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Get teacher
    try:
        from django.db.models import Q
        teacher = AccountUser.objects.get(
            Q(role=AccountUser.ROLE_TEACHER) &
            (Q(teacher_code__iexact=teacher_identifier) |
             Q(username__iexact=teacher_identifier) |
             Q(email__iexact=teacher_identifier))
        )
    except AccountUser.DoesNotExist:
        return Response(
            {"detail": f"Teacher with code/username/email '{teacher_identifier}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except AccountUser.MultipleObjectsReturned:
        return Response(
            {"detail": f"Multiple teachers found with identifier '{teacher_identifier}'. Please use teacher_code."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Create or update TeacherSlotAvailability
    try:
        from django.db import transaction
        with transaction.atomic():
            availability, created = TeacherSlotAvailability.objects.get_or_create(
                timetable=timetable,
                teacher=teacher,
                day_slot=day_slot,
                defaults={"is_available": is_available}
            )
            
            if not created:
                availability.is_available = is_available
                # Call full_clean to trigger validation
                availability.full_clean()
                availability.save()
            
            # Map day constant to display name
            day_display_map = {
                DaySlot.MONDAY: "Monday",
                DaySlot.TUESDAY: "Tuesday",
                DaySlot.WEDNESDAY: "Wednesday",
                DaySlot.THURSDAY: "Thursday",
                DaySlot.FRIDAY: "Friday",
                DaySlot.SATURDAY: "Saturday",
                DaySlot.SUNDAY: "Sunday",
            }
            
            return Response(
                {
                    "message": "Teacher availability updated successfully.",
                    "teacher": teacher.teacher_code or teacher.username,
                    "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip(),
                    "slot_code": day_slot.slot_code,
                    "day": day_display_map.get(day_slot.day, day_slot.day),
                    "start_time": str(day_slot.start_time),
                    "end_time": str(day_slot.end_time),
                    "is_available": availability.is_available,
                },
                status=status.HTTP_200_OK,
            )
    except Exception as e:
        # Check if it's a ValidationError from clean()
        from django.core.exceptions import ValidationError
        if isinstance(e, ValidationError):
            return Response(
                {"detail": str(e.message_dict.get("is_available", [str(e)])[0])},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"detail": f"Error updating teacher availability: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_teacher_availability(request, timetable_id: str):
    """
    Get teacher availability for all slots in a timetable.
    
    Returns availability organized by slot code, showing which teachers
    are available/unavailable for each slot.
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "slots": [
            {
                "slot_id": "uuid",
                "slot_code": "m1",
                "day": "Monday",
                "start_time": "08:00:00",
                "end_time": "09:30:00",
                "teachers": [
                    {
                        "teacher_code": "AK-CAP",
                        "teacher_name": "A K",
                        "is_available": true
                    }
                ]
            }
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view teacher availability."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only view their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all day slots for this timetable
    day_slots = DaySlot.objects.filter(timetable=timetable).order_by("day", "slot_number")
    
    # Get all teachers in the center
    center = timetable.center
    teachers = AccountUser.objects.filter(
        role=AccountUser.ROLE_TEACHER,
        center=center
    ).order_by("teacher_code", "username")
    
    # Get all teacher slot availabilities for this timetable
    availabilities = TeacherSlotAvailability.objects.filter(
        timetable=timetable
    ).select_related("teacher", "day_slot")
    
    # Create a map: (day_slot_id, teacher_id) -> is_available
    availability_map = {}
    for av in availabilities:
        key = (av.day_slot_id, av.teacher_id)
        availability_map[key] = av.is_available
    
    # Map day constant to display name
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    # Build response
    slots_data = []
    for slot in day_slots:
        teachers_data = []
        for teacher in teachers:
            # Default is available (True) if not explicitly set
            key = (slot.id, teacher.id)
            is_available = availability_map.get(key, True)
            
            teachers_data.append({
                "teacher_code": teacher.teacher_code or teacher.username,
                "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip() or teacher.username,
                "teacher_id": str(teacher.id),
                "is_available": is_available,
            })
        
        slots_data.append({
            "slot_id": str(slot.id),
            "slot_code": slot.slot_code,
            "day": day_display_map.get(slot.day, slot.day),
            "start_time": str(slot.start_time),
            "end_time": str(slot.end_time),
            "is_free_class": slot.is_free_class,
            "teachers": teachers_data,
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "slots": slots_data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_available_teachers_for_slot(request, timetable_id: str, slot_id: str = None):
    """
    Get all teachers available for a specific slot.
    Shows which teachers can be assigned to this slot.
    
    GET /api/timetable/timetables/<timetable_id>/slots/<slot_id>/available-teachers/
    GET /api/timetable/timetables/<timetable_id>/available-teachers/?slot_code=d1_s1
    
    Returns:
    {
        "timetable_id": "uuid",
        "slot": {
            "slot_id": "uuid",
            "slot_code": "d1_s1",
            "day": "Friday",
            "date": "2025-12-19",
            "start_time": "08:00:00",
            "end_time": "09:00:00"
        },
        "available_teachers": [
            {
                "teacher_id": "uuid",
                "teacher_code": "TCH-CENT-230",
                "teacher_name": "Trushank Lohar",
                "subjects": "Physics, Chemistry",
                "is_available": true,
                "is_busy": false
            }
        ],
        "unavailable_teachers": [
            {
                "teacher_id": "uuid",
                "teacher_code": "TCH-CENT-501",
                "teacher_name": "Radha Rath",
                "subjects": "Biology",
                "is_available": false,
                "reason": "Marked unavailable by admin"
            },
            {
                "teacher_id": "uuid",
                "teacher_code": "TCH-CENT-802",
                "teacher_name": "Teacher Name",
                "subjects": "Physics",
                "is_available": true,
                "is_busy": true,
                "reason": "Busy in another timetable",
                "busy_in": {
                    "timetable": "Other Timetable",
                    "batch": "Batch30",
                    "subject": "Physics",
                    "time": "08:00-09:00"
                }
            }
        ],
        "total_available": 5,
        "total_unavailable": 2
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view teacher availability."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get slot_code from query params if slot_id not provided
    slot_code = request.query_params.get("slot_code")
    
    # Get the specific slot
    try:
        if slot_id:
            day_slot = DaySlot.objects.get(id=slot_id, timetable=timetable)
        elif slot_code:
            day_slot = DaySlot.objects.get(slot_code=slot_code, timetable=timetable)
        else:
            return Response(
                {"detail": "Either slot_id in URL or slot_code query param is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except DaySlot.DoesNotExist:
        return Response(
            {"detail": "Slot not found in this timetable."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Get all teachers in the center (exclude FREE teachers)
    center = timetable.center
    teachers = AccountUser.objects.filter(
        role=AccountUser.ROLE_TEACHER,
        center=center
    ).exclude(
        teacher_code__istartswith="FREE"
    ).order_by("teacher_code", "username")
    
    # Get availability records for this slot
    availabilities = TeacherSlotAvailability.objects.filter(
        timetable=timetable,
        day_slot=day_slot
    ).select_related("teacher")
    
    # Create availability map: teacher_id -> is_available
    availability_map = {av.teacher_id: av.is_available for av in availabilities}
    
    # Get busy info from other timetables
    teacher_ids = [t.id for t in teachers]
    
    other_entries = TimetableEntry.objects.filter(
        teacher_id__in=teacher_ids
    ).exclude(
        day_slot__timetable=timetable
    ).select_related('day_slot', 'teacher', 'batch')
    
    # Build busy slots list
    busy_slots = []
    for entry in other_entries:
        if entry.day_slot.actual_date and entry.teacher_id:
            busy_slots.append({
                "teacher_id": entry.teacher_id,
                "date": entry.day_slot.actual_date,
                "start_time": entry.day_slot.start_time,
                "end_time": entry.day_slot.end_time,
                "info": {
                    "timetable": str(entry.day_slot.timetable),
                    "batch": entry.batch.code if entry.batch else None,
                    "subject": entry.subject,
                    "time": f"{entry.day_slot.start_time.strftime('%H:%M')}-{entry.day_slot.end_time.strftime('%H:%M')}",
                }
            })
    
    def check_time_overlap(start1, end1, start2, end2):
        return start1 < end2 and start2 < end1
    
    def find_busy_info(teacher_id, slot_date, slot_start, slot_end):
        if not slot_date:
            return None
        for busy in busy_slots:
            if (busy["teacher_id"] == teacher_id and 
                busy["date"] == slot_date and
                check_time_overlap(slot_start, slot_end, busy["start_time"], busy["end_time"])):
                return busy["info"]
        return None
    
    # Day display map
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    available_teachers = []
    
    # Add Free Class options
    if timetable.free_classes_count > 0:
        for i in range(1, timetable.free_classes_count + 1):
             available_teachers.append({
                "teacher_id": f"FREE-{i}",
                "teacher_code": f"FREE{i}",
                "teacher_name": f"Free Class {i}",
                "subjects": "FREE",
                "is_available": True,
                "is_busy": False,
            })

    unavailable_teachers = []
    
    for teacher in teachers:
        # Check admin availability setting (default True if not set)
        is_available = availability_map.get(teacher.id, True)
        
        # Check if busy in another timetable
        busy_info = find_busy_info(
            teacher.id,
            day_slot.actual_date,
            day_slot.start_time,
            day_slot.end_time
        )
        is_busy = busy_info is not None
        
        teacher_data = {
            "teacher_id": str(teacher.id),
            "teacher_code": teacher.teacher_code or teacher.username,
            "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip() or teacher.username,
            "subjects": teacher.teacher_subjects or "",
            "is_available": is_available,
            "is_busy": is_busy,
        }
        
        if is_available and not is_busy:
            available_teachers.append(teacher_data)
        else:
            if not is_available:
                teacher_data["reason"] = "Marked unavailable by admin"
            elif is_busy:
                teacher_data["reason"] = "Busy in another timetable"
                teacher_data["busy_in"] = busy_info
            unavailable_teachers.append(teacher_data)
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "slot": {
                "slot_id": str(day_slot.id),
                "slot_code": day_slot.slot_code,
                "day": day_display_map.get(day_slot.day, day_slot.day),
                "date": str(day_slot.actual_date) if day_slot.actual_date else None,
                "start_time": str(day_slot.start_time),
                "end_time": str(day_slot.end_time),
            },
            "available_teachers": available_teachers,
            "unavailable_teachers": unavailable_teachers,
            "total_available": len(available_teachers),
            "total_unavailable": len(unavailable_teachers),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_teacher_wise_availability(request, timetable_id: str):
    """
    Get teacher availability organized by teacher, then by day.
    
    Returns availability organized by teacher, with slots grouped by day
    (Friday slots, Saturday slots, etc.)
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "center": "Center Name",
        "from_date": "2025-12-19",
        "to_date": "2025-12-29",
        "teachers": [
            {
                "teacher_code": "TCH-CENT-230",
                "teacher_name": "Trushank Lohar",
                "teacher_id": "34",
                "days": [
                    {
                        "day": "Friday",
                        "day_number": 1,
                        "date": "2025-12-19",
                        "slots": [
                            {
                                "slot_id": "2dd8001f-0b71-44b3-a28a-d780bc0b643f",
                                "slot_code": "d1_s1",
                                "start_time": "08:00:00",
                                "end_time": "09:00:00",
                                "is_free_class": false,
                                "is_available": true
                            },
                            {
                                "slot_id": "ef44b599-50e0-48ad-b19f-f405f01002ca",
                                "slot_code": "d1_s2",
                                "start_time": "09:00:00",
                                "end_time": "10:00:00",
                                "is_free_class": false,
                                "is_available": true
                            }
                        ]
                    },
                    {
                        "day": "Saturday",
                        "day_number": 2,
                        "date": "2025-12-20",
                        "slots": [
                            {
                                "slot_id": "abc123...",
                                "slot_code": "d2_s1",
                                "start_time": "08:00:00",
                                "end_time": "09:00:00",
                                "is_free_class": false,
                                "is_available": false
                            }
                        ]
                    }
                ]
            }
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view teacher availability."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only view their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all day slots for this timetable
    day_slots = DaySlot.objects.filter(timetable=timetable).order_by("day", "slot_number")
    
    # Get all teachers in the center (exclude FREE teachers)
    center = timetable.center
    teachers = AccountUser.objects.filter(
        role=AccountUser.ROLE_TEACHER,
        center=center
    ).exclude(
        teacher_code__istartswith="FREE"  # Exclude FREE1, FREE2, etc.
    ).order_by("teacher_code", "username")
    
    # Get all teacher slot availabilities for this timetable
    availabilities = TeacherSlotAvailability.objects.filter(
        timetable=timetable
    ).select_related("teacher", "day_slot")
    
    # Create a map: (day_slot_id, teacher_id) -> is_available
    availability_map = {}
    for av in availabilities:
        key = (av.day_slot_id, av.teacher_id)
        availability_map[key] = av.is_available
    
    # Get all TimetableEntry for teachers in OTHER timetables to check if busy
    # We need to find entries where teacher has class at same date and overlapping time
    teacher_ids = [t.id for t in teachers]
    
    # Get all entries from OTHER timetables for these teachers
    other_entries = TimetableEntry.objects.filter(
        teacher_id__in=teacher_ids
    ).exclude(
        day_slot__timetable=timetable  # Exclude current timetable
    ).select_related('day_slot', 'teacher', 'batch')
    
    # Create a list of busy slots: (teacher_id, date, start_time, end_time, info)
    # We'll check for time overlap, not exact match
    busy_slots = []
    for entry in other_entries:
        if entry.day_slot.actual_date:
            busy_slots.append({
                "teacher_id": entry.teacher_id,
                "date": entry.day_slot.actual_date,
                "start_time": entry.day_slot.start_time,
                "end_time": entry.day_slot.end_time,
                "info": {
                    "timetable": str(entry.day_slot.timetable),
                    "batch": entry.batch.code if entry.batch else None,
                    "subject": entry.subject,
                    "time": f"{entry.day_slot.start_time.strftime('%H:%M')}-{entry.day_slot.end_time.strftime('%H:%M')}",
                }
            })
    
    def check_time_overlap(start1, end1, start2, end2):
        """Check if two time ranges overlap."""
        # Times overlap if: start1 < end2 AND start2 < end1
        return start1 < end2 and start2 < end1
    
    def find_busy_info(teacher_id, slot_date, slot_start, slot_end):
        """Find if teacher is busy at given date/time (with overlap check)."""
        for busy in busy_slots:
            if (busy["teacher_id"] == teacher_id and 
                busy["date"] == slot_date and
                check_time_overlap(slot_start, slot_end, busy["start_time"], busy["end_time"])):
                return busy["info"]
        return None
    
    # Map day constant to display name
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    # Build response organized by teacher, then by day
    teachers_data = []
    for teacher in teachers:
        # Group slots by day_index (or actual_date) to keep each date separate
        days_data = {}
        for slot in day_slots:
            # Default is available (True) if not explicitly set
            key = (slot.id, teacher.id)
            is_available = availability_map.get(key, True)
            
            # Check if teacher is busy (has class in another timetable at overlapping time)
            is_busy = False
            busy_info = None
            if slot.actual_date:
                busy_info = find_busy_info(
                    teacher.id,
                    slot.actual_date,
                    slot.start_time,
                    slot.end_time
                )
                if busy_info:
                    is_busy = True
            
            day_name = day_display_map.get(slot.day, slot.day)
            slot_data = {
                "slot_id": str(slot.id),
                "slot_code": slot.slot_code,
                "start_time": str(slot.start_time),
                "end_time": str(slot.end_time),
                "is_free_class": slot.is_free_class,
                "is_available": is_available,
                "is_busy": is_busy,
            }
            
            if busy_info:
                slot_data["busy_in"] = busy_info
            
            # Use day_index as key to keep each date separate
            day_key = slot.day_index or (str(slot.actual_date) if slot.actual_date else day_name)
            
            if day_key not in days_data:
                days_data[day_key] = {
                    "day": day_name,
                    "day_number": slot.day_index or (list(day_display_map.values()).index(day_name) + 1 if day_name in day_display_map.values() else 0),
                    "date": str(slot.actual_date) if slot.actual_date else None,
                    "slots": []
                }
            days_data[day_key]["slots"].append(slot_data)
        
        # Convert to sorted list by day_number
        days_list = sorted(days_data.values(), key=lambda x: x["day_number"])
        
        teachers_data.append({
            "teacher_code": teacher.teacher_code or teacher.username,
            "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip() or teacher.username,
            "teacher_id": str(teacher.id),
            "days": days_list,
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "teachers": teachers_data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def assign_batch_to_timetable(request):
    """
    Admin assigns a batch to a timetable.
    This makes the batch available for all slots in the timetable.
    
    Payload:
    {
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1"
    }
    
    Returns:
    {
        "message": "Batch assigned to timetable successfully.",
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "batch_name": "Super 30 - Batch A (2025)"
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can assign batches to timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    timetable_id = request.data.get("timetable_id")
    batch_code = request.data.get("batch_code")
    
    if not timetable_id or not batch_code:
        return Response(
            {"detail": "timetable_id and batch_code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get batch
    try:
        batch = Batch.objects.select_related("program", "center").get(code=batch_code)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with code '{batch_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Verify batch belongs to the same center as timetable
    # Check direct center field (batch.center) since Program doesn't have center
    if batch.center and batch.center != timetable.center:
        return Response(
            {"detail": f"Batch '{batch_code}' does not belong to the same center as the timetable."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Create or get TimetableBatch assignment
    timetable_batch, created = TimetableBatch.objects.get_or_create(
        timetable=timetable,
        batch=batch,
    )
    
    return Response(
        {
            "message": "Batch assigned to timetable successfully." if created else "Batch was already assigned to this timetable.",
            "timetable_id": str(timetable.id),
            "batch_code": batch.code,
            "batch_name": batch.name,
            "batch_id": str(batch.id),
            "already_assigned": not created,
        },
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def assign_teacher_to_batch(request):
    """
    Admin assigns a teacher to a batch in a timetable with lecture constraints.
    Subject is automatically taken from teacher's teacher_subjects field.
    
    Values are batch-specific: Each batch-teacher combination has its own values.
    If the same teacher is added to a different batch, admin must provide new values.
    
    Payload:
    {
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "teacher_code": "AK-CAP",
        "min_lectures_per_day": 1,
        "max_lectures_per_day": 2,
        "max_lectures_per_week": 10,
        "total_lectures": 20
    }
    
    Returns:
    {
        "message": "Teacher assigned to batch successfully.",
        "teacher_code": "AK-CAP",
        "teacher_name": "A K",
        "subject": "Physics",
        "batch_code": "HDTN-1A-ZA1",
        "min_lectures_per_day": 1,
        "max_lectures_per_day": 2,
        "max_lectures_per_week": 10,
        "total_lectures": 20
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can assign teachers to batches."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    timetable_id = request.data.get("timetable_id")
    batch_code = request.data.get("batch_code")
    teacher_code = request.data.get("teacher_code")
    subject_name = request.data.get("subject_name")  # For FREE periods without teacher
    
    # Get values from request, default to 0 if not provided
    min_lectures_per_day = request.data.get("min_lectures_per_day", 0)
    max_lectures_per_day = request.data.get("max_lectures_per_day", 0)
    max_lectures_per_week = request.data.get("max_lectures_per_week", 0)
    total_lectures = request.data.get("total_lectures", 0)  # Default to 0
    
    if not timetable_id or not batch_code:
        return Response(
            {"detail": "timetable_id and batch_code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Either teacher_code or subject_name must be provided
    if not teacher_code and not subject_name:
        return Response(
            {"detail": "Either teacher_code or subject_name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get batch
    try:
        batch = Batch.objects.select_related("program", "center").get(code=batch_code)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with code '{batch_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Verify batch belongs to the same center as timetable
    # Check direct center field (batch.center) since Program doesn't have center
    if batch.center and batch.center != timetable.center:
        return Response(
            {"detail": f"Batch '{batch_code}' does not belong to the same center as the timetable."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Handle FREE period (special case - no teacher, just a subject like "Exam")
    # Can be triggered by:
    # 1. teacher_code starting with "FREE" (e.g., "FREE1", "FREE2")
    # 2. teacher_code is null/empty and subject_name is provided
    is_free_period = False
    free_code = None
    
    if teacher_code and str(teacher_code).upper().startswith("FREE"):
        is_free_period = True
        free_code = str(teacher_code).upper()
    elif not teacher_code and subject_name:
        # No teacher_code but subject_name provided - treat as FREE period
        is_free_period = True
        free_code = str(subject_name).upper()
    
    teacher = None
    
    if is_free_period:
        # FREE, FREE1, FREE2, etc. are treated like "Exam" - no teacher, just a subject
        free_code = teacher_code.upper()  # Normalize to uppercase
        
        # Validate lecture constraints
        if min_lectures_per_day < 0:
            return Response(
                {"detail": "min_lectures_per_day must be >= 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if max_lectures_per_day < 0:
            return Response(
                {"detail": "max_lectures_per_day must be >= 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if max_lectures_per_week < 0:
            return Response(
                {"detail": "max_lectures_per_week must be >= 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if total_lectures < 0:
            return Response(
                {"detail": "total_lectures must be >= 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Create or update BatchFacultyLoad with teacher=None
        try:
            with transaction.atomic():
                # Check if FREE entry already exists for this batch
                faculty_load = BatchFacultyLoad.objects.filter(
                    timetable=timetable,
                    batch=batch,
                    teacher__isnull=True,
                    subject_name__iexact=free_code
                ).first()
                
                if faculty_load:
                    # Update existing
                    faculty_load.min_lectures_per_day = min_lectures_per_day
                    faculty_load.max_lectures_per_day = max_lectures_per_day
                    faculty_load.max_lectures_per_week = max_lectures_per_week
                    faculty_load.total_lectures = total_lectures
                    faculty_load.save()
                    created = False
                else:
                    # Create new
                    faculty_load = BatchFacultyLoad.objects.create(
                        timetable=timetable,
                        batch=batch,
                        teacher=None,
                        subject_name=free_code,
                        min_lectures_per_day=min_lectures_per_day,
                        max_lectures_per_day=max_lectures_per_day,
                        max_lectures_per_week=max_lectures_per_week,
                        total_lectures=total_lectures,
                    )
                    created = True
                
                action = "created" if created else "updated"
                
                return Response(
                    {
                        "message": f"FREE period assigned to batch successfully ({action}).",
                        "teacher_code": None,
                        "teacher_name": None,
                        "teacher_id": None,
                        "subject": free_code,
                        "is_free": True,
                        "batch_code": batch.code,
                        "batch_name": batch.name,
                        "min_lectures_per_day": faculty_load.min_lectures_per_day,
                        "max_lectures_per_day": faculty_load.max_lectures_per_day,
                        "max_lectures_per_week": faculty_load.max_lectures_per_week,
                        "total_lectures": faculty_load.total_lectures,
                    },
                    status=status.HTTP_200_OK,
                )
        except Exception as e:
            return Response(
                {"detail": f"Error assigning FREE period to batch: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Regular teacher handling
    try:
        from django.db.models import Q
        teacher = AccountUser.objects.get(
            Q(role=AccountUser.ROLE_TEACHER) &
            (Q(teacher_code__iexact=teacher_code) |
             Q(username__iexact=teacher_code) |
             Q(email__iexact=teacher_code))
        )
    except AccountUser.DoesNotExist:
        return Response(
            {"detail": f"Teacher with code '{teacher_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except AccountUser.MultipleObjectsReturned:
        return Response(
            {"detail": f"Multiple teachers found with code '{teacher_code}'. Please use unique teacher_code."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Verify teacher belongs to the same center
    if teacher.center != timetable.center:
        return Response(
            {"detail": f"Teacher '{teacher_code}' does not belong to the same center as the timetable."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate lecture constraints
    if min_lectures_per_day < 0:
        return Response(
            {"detail": "min_lectures_per_day must be >= 0."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if max_lectures_per_day < 0:
        return Response(
            {"detail": "max_lectures_per_day must be >= 0."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if max_lectures_per_week < 0:
        return Response(
            {"detail": "max_lectures_per_week must be >= 0."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if min_lectures_per_day > max_lectures_per_day and max_lectures_per_day > 0:
        return Response(
            {"detail": "min_lectures_per_day cannot be greater than max_lectures_per_day."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if total_lectures < 0:
        return Response(
            {"detail": "total_lectures must be >= 0."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Create or update BatchFacultyLoad for regular teacher
    try:
        with transaction.atomic():
            faculty_load, created = BatchFacultyLoad.objects.update_or_create(
                timetable=timetable,
                teacher=teacher,
                batch=batch,
                defaults={
                    "subject_name": teacher.teacher_subjects or "",
                    "min_lectures_per_day": min_lectures_per_day,
                    "max_lectures_per_day": max_lectures_per_day,
                    "max_lectures_per_week": max_lectures_per_week,
                    "total_lectures": total_lectures,
                }
            )
            
            subject = teacher.teacher_subjects or "Not specified"
            action = "created" if created else "updated"
            teacher_display_name = teacher.teacher_code or teacher.username
            
            return Response(
                {
                    "message": f"Teacher assigned to batch successfully ({action}).",
                    "teacher_code": teacher_display_name,
                    "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip() or teacher_display_name,
                    "teacher_id": str(teacher.id),
                    "subject": subject,
                    "is_free": False,
                    "batch_code": batch.code,
                    "batch_name": batch.name,
                    "min_lectures_per_day": faculty_load.min_lectures_per_day,
                    "max_lectures_per_day": faculty_load.max_lectures_per_day,
                    "max_lectures_per_week": faculty_load.max_lectures_per_week,
                    "total_lectures": faculty_load.total_lectures,
                },
                status=status.HTTP_200_OK,
            )
    except Exception as e:
        return Response(
            {"detail": f"Error assigning teacher to batch: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def remove_batch_from_timetable(request):
    """
    Admin removes a batch from a timetable.
    This also removes all teacher assignments (BatchFacultyLoad) for this batch in this timetable.
    
    Payload:
    {
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1"
    }
    
    Returns:
    {
        "message": "Batch removed from timetable successfully.",
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "teachers_removed": 3
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can remove batches from timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    timetable_id = request.data.get("timetable_id")
    batch_code = request.data.get("batch_code")
    
    if not timetable_id or not batch_code:
        return Response(
            {"detail": "timetable_id and batch_code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get batch
    try:
        batch = Batch.objects.get(code=batch_code)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with code '{batch_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if batch is assigned to this timetable
    try:
        timetable_batch = TimetableBatch.objects.get(timetable=timetable, batch=batch)
    except TimetableBatch.DoesNotExist:
        return Response(
            {"detail": f"Batch '{batch_code}' is not assigned to this timetable."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Remove all teacher assignments for this batch in this timetable
    teachers_removed = BatchFacultyLoad.objects.filter(
        timetable=timetable,
        batch=batch
    ).delete()[0]
    
    # Remove all timetable entries for this batch
    entries_removed = TimetableEntry.objects.filter(
        day_slot__timetable=timetable,
        batch=batch
    ).delete()[0]
    
    # Remove all fixed slots for this batch
    fixed_slots_removed = FixedSlot.objects.filter(
        timetable=timetable,
        batch=batch
    ).delete()[0]
    
    # Remove the batch assignment
    timetable_batch.delete()
    
    return Response(
        {
            "message": "Batch removed from timetable successfully.",
            "timetable_id": str(timetable.id),
            "batch_code": batch.code,
            "batch_name": batch.name,
            "teachers_removed": teachers_removed,
            "entries_removed": entries_removed,
            "fixed_slots_removed": fixed_slots_removed,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def remove_teacher_from_batch(request):
    """
    Admin removes a teacher assignment from a batch in a timetable.
    
    For regular teachers:
    Payload:
    {
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "teacher_code": "AK-CAP"
    }
    
    For FREE periods (FREE, FREE1, FREE2, etc.):
    Payload:
    {
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "teacher_code": "FREE2"  // or "subject_name": "FREE2"
    }
    
    Returns:
    {
        "message": "Teacher removed from batch successfully.",
        "timetable_id": "uuid",
        "batch_code": "HDTN-1A-ZA1",
        "teacher_code": "AK-CAP"
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can remove teachers from batches."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    timetable_id = request.data.get("timetable_id")
    batch_code = request.data.get("batch_code")
    teacher_code = request.data.get("teacher_code")
    subject_name = request.data.get("subject_name")  # Alternative for FREE periods
    
    if not timetable_id or not batch_code:
        return Response(
            {"detail": "timetable_id and batch_code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    if not teacher_code and not subject_name:
        return Response(
            {"detail": "teacher_code or subject_name is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get batch
    try:
        batch = Batch.objects.get(code=batch_code)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with code '{batch_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if this is a FREE period removal
    is_free_period = False
    free_code = None
    
    if teacher_code and str(teacher_code).upper().startswith("FREE"):
        is_free_period = True
        free_code = str(teacher_code).upper()
    elif subject_name and str(subject_name).upper().startswith("FREE"):
        is_free_period = True
        free_code = str(subject_name).upper()
    
    if is_free_period:
        # Handle FREE period removal
        try:
            faculty_load = BatchFacultyLoad.objects.get(
                timetable=timetable,
                batch=batch,
                teacher__isnull=True,
                subject_name__iexact=free_code
            )
        except BatchFacultyLoad.DoesNotExist:
            return Response(
                {"detail": f"FREE period '{free_code}' is not assigned to batch '{batch_code}' in this timetable."},
                status=status.HTTP_404_NOT_FOUND,
            )
        
        # Remove timetable entries for this FREE period in this batch (if any)
        entries_removed = TimetableEntry.objects.filter(
            day_slot__timetable=timetable,
            batch=batch,
            teacher__isnull=True,
            subject_name__iexact=free_code
        ).delete()[0]
        
        # Remove fixed slots for this FREE period in this batch (if any)
        fixed_slots_removed = FixedSlot.objects.filter(
            timetable=timetable,
            batch=batch,
            teacher__isnull=True,
            subject_name__iexact=free_code
        ).delete()[0]
        
        # Remove the faculty load assignment
        faculty_load.delete()
        
        return Response(
            {
                "message": f"FREE period '{free_code}' removed from batch successfully.",
                "timetable_id": str(timetable.id),
                "batch_code": batch.code,
                "batch_name": batch.name,
                "teacher_code": None,
                "teacher_name": None,
                "subject": free_code,
                "is_free": True,
                "entries_removed": entries_removed,
                "fixed_slots_removed": fixed_slots_removed,
            },
            status=status.HTTP_200_OK,
        )
    
    # Regular teacher handling
    # Get teacher
    try:
        teacher = AccountUser.objects.get(
            Q(teacher_code__iexact=teacher_code) |
            Q(username__iexact=teacher_code)
        )
    except AccountUser.DoesNotExist:
        return Response(
            {"detail": f"Teacher with code '{teacher_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except AccountUser.MultipleObjectsReturned:
        # If multiple found, try to get the one in the same center
        teacher = AccountUser.objects.filter(
            Q(teacher_code__iexact=teacher_code) |
            Q(username__iexact=teacher_code),
            center=timetable.center
        ).first()
        if not teacher:
            return Response(
                {"detail": f"Multiple teachers found with code '{teacher_code}'. Please use unique teacher_code."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Check if teacher is assigned to this batch in this timetable
    try:
        faculty_load = BatchFacultyLoad.objects.get(
            timetable=timetable,
            batch=batch,
            teacher=teacher
        )
    except BatchFacultyLoad.DoesNotExist:
        return Response(
            {"detail": f"Teacher '{teacher_code}' is not assigned to batch '{batch_code}' in this timetable."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Remove timetable entries for this teacher in this batch
    entries_removed = TimetableEntry.objects.filter(
        day_slot__timetable=timetable,
        batch=batch,
        teacher=teacher
    ).delete()[0]
    
    # Remove fixed slots for this teacher in this batch
    fixed_slots_removed = FixedSlot.objects.filter(
        timetable=timetable,
        batch=batch,
        teacher=teacher
    ).delete()[0]
    
    # Remove the faculty load assignment
    faculty_load.delete()
    
    return Response(
        {
            "message": "Teacher removed from batch successfully.",
            "timetable_id": str(timetable.id),
            "batch_code": batch.code,
            "batch_name": batch.name,
            "teacher_code": teacher.teacher_code or teacher.username,
            "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip(),
            "entries_removed": entries_removed,
            "fixed_slots_removed": fixed_slots_removed,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_timetable_batch_assignments(request, timetable_id: str):
    """
    Get all batch assignments and their teachers for a timetable.
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "batches": [
            {
                "batch_code": "HDTN-1A-ZA1",
                "batch_name": "Super 30 - Batch A (2025)",
                "teachers": [
                    {
                        "teacher_code": "AK-CAP",
                        "teacher_name": "A K",
                        "subject": "Physics",
                        "min_lectures_per_day": 1,
                        "max_lectures_per_day": 3,
                        "total_lectures": 20,
                    }
                ]
            }
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view batch assignments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only view their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all batches assigned to this timetable (from TimetableBatch)
    timetable_batches = TimetableBatch.objects.filter(
        timetable=timetable
    ).select_related("batch").order_by("batch__code")
    
    # Get all BatchFacultyLoad entries for this timetable
    faculty_loads = BatchFacultyLoad.objects.filter(
        timetable=timetable
    ).select_related("batch", "teacher").order_by("batch__code", "teacher__teacher_code", "teacher__username")
    
    # Initialize batches_dict with all assigned batches (even without teachers)
    batches_dict = {}
    for tb in timetable_batches:
        batch = tb.batch
        batches_dict[batch.code] = {
            "batch_code": batch.code,
            "batch_name": batch.name,
            "batch_id": str(batch.id),
            "teachers": [],
        }
    
    # Add teacher assignments (include FREE periods)
    for load in faculty_loads:
        batch_code = load.batch.code
        
        # If batch not in dict (assigned via old method without TimetableBatch), add it
        if batch_code not in batches_dict:
            batches_dict[batch_code] = {
                "batch_code": batch_code,
                "batch_name": load.batch.name,
                "batch_id": str(load.batch.id),
                "teachers": [],
            }
        
        # Handle null teacher (FREE periods)
        if load.teacher is None:
            # FREE period - no teacher
            batches_dict[batch_code]["teachers"].append({
                "teacher_code": None,
                "teacher_name": None,
                "teacher_id": None,
                "subject": load.subject_name or "FREE",
                "is_free": True,
                "min_lectures_per_day": load.min_lectures_per_day,
                "max_lectures_per_day": load.max_lectures_per_day,
                "max_lectures_per_week": load.max_lectures_per_week,
                "total_lectures": load.total_lectures,
            })
        else:
            # Regular teacher
            subject = load.subject_name or load.teacher.teacher_subjects or "Not specified"
            teacher_code = load.teacher.teacher_code or load.teacher.username
            teacher_name = f"{load.teacher.first_name} {load.teacher.last_name}".strip() or teacher_code
            
            batches_dict[batch_code]["teachers"].append({
                "teacher_code": teacher_code,
                "teacher_name": teacher_name,
                "teacher_id": str(load.teacher.id),
                "subject": subject,
                "is_free": False,
                "min_lectures_per_day": load.min_lectures_per_day,
                "max_lectures_per_day": load.max_lectures_per_day,
                "max_lectures_per_week": load.max_lectures_per_week,
                "total_lectures": load.total_lectures,
            })
    
    batches_list = list(batches_dict.values())
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "batches": batches_list,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_batch_wise_slots(request, timetable_id: str, batch_id: str = None):
    """
    Get all slots organized by batch for a timetable.
    Shows each assigned batch with all available slots and their status.
    
    GET /api/timetable/timetables/<timetable_id>/batch-wise-slots/
    GET /api/timetable/timetables/<timetable_id>/batch-wise-slots/<batch_id>/
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "center": "Center Name",
        "from_date": "2025-12-19",
        "to_date": "2025-12-29",
        "batches": [
            {
                "batch_code": "BATCH-001",
                "batch_name": "JEE Batch 1",
                "batch_id": "uuid",
                "days": [
                    {
                        "day": "Friday",
                        "day_number": 1,
                        "date": "2025-12-19",
                        "slots": [
                            {
                                "slot_id": "uuid",
                                "slot_code": "d1_s1",
                                "start_time": "08:00:00",
                                "end_time": "09:00:00",
                                "is_free_class": false,
                                "is_assigned": true,
                                "subject": "Physics",
                                "teacher_code": "TCH-001",
                                "teacher_name": "Teacher Name",
                                "is_fixed": false
                            }
                        ]
                    }
                ]
            }
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view batch-wise slots."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all day slots for this timetable
    day_slots = DaySlot.objects.filter(timetable=timetable).order_by("day_index", "slot_number")
    
    # Get batches - filter by batch_id if provided
    if batch_id:
        timetable_batches = TimetableBatch.objects.filter(
            timetable=timetable,
            batch_id=batch_id
        ).select_related("batch")
        
        if not timetable_batches.exists():
            return Response(
                {"detail": f"Batch not found or not assigned to this timetable."},
                status=status.HTTP_404_NOT_FOUND,
            )
    else:
        timetable_batches = TimetableBatch.objects.filter(
            timetable=timetable
        ).select_related("batch").order_by("batch__code")
    
    # Get all timetable entries (slot assignments)
    entries = TimetableEntry.objects.filter(
        day_slot__timetable=timetable
    ).select_related("day_slot", "batch", "teacher")
    
    # Get all fixed slots
    fixed_slots = FixedSlot.objects.filter(
        timetable=timetable
    ).select_related("day_slot", "batch", "teacher")
    
    # Create entry map: (day_slot_id, batch_id) -> entry
    entry_map = {}
    for entry in entries:
        key = (entry.day_slot_id, entry.batch_id)
        entry_map[key] = entry
    
    # Create fixed slot map: (day_slot_id, batch_id) -> fixed_slot
    fixed_map = {}
    for fs in fixed_slots:
        key = (fs.day_slot_id, fs.batch_id)
        fixed_map[key] = fs
    
    # Map day constant to display name
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    # Build response organized by batch, then by day
    batches_data = []
    for tb in timetable_batches:
        batch = tb.batch
        
        # Group slots by day
        days_data = {}
        for slot in day_slots:
            day_name = day_display_map.get(slot.day, slot.day)
            day_key = slot.day_index or day_name
            
            # Check if this slot has an entry for this batch
            entry_key = (slot.id, batch.id)
            entry = entry_map.get(entry_key)
            fixed = fixed_map.get(entry_key)
            
            slot_data = {
                "slot_id": str(slot.id),
                "slot_code": slot.slot_code,
                "start_time": str(slot.start_time),
                "end_time": str(slot.end_time),
                "is_free_class": slot.is_free_class,
                "is_assigned": entry is not None or fixed is not None,
                "subject": None,
                "teacher_code": None,
                "teacher_name": None,
                "is_fixed": fixed is not None,
            }
            
            # Add assignment details if exists
            if fixed:
                slot_data["subject"] = fixed.subject
                if fixed.teacher:
                    slot_data["teacher_code"] = fixed.teacher.teacher_code or fixed.teacher.username
                    slot_data["teacher_name"] = f"{fixed.teacher.first_name} {fixed.teacher.last_name}".strip() or slot_data["teacher_code"]
                slot_data["is_fixed"] = True
            elif entry:
                slot_data["subject"] = entry.subject
                if entry.teacher:
                    slot_data["teacher_code"] = entry.teacher.teacher_code or entry.teacher.username
                    slot_data["teacher_name"] = f"{entry.teacher.first_name} {entry.teacher.last_name}".strip() or slot_data["teacher_code"]
            
            if day_key not in days_data:
                days_data[day_key] = {
                    "day": day_name,
                    "day_number": slot.day_index or (list(day_display_map.values()).index(day_name) + 1 if day_name in day_display_map.values() else 0),
                    "date": str(slot.actual_date) if slot.actual_date else None,
                    "slots": []
                }
            days_data[day_key]["slots"].append(slot_data)
        
        # Convert to sorted list by day_number
        days_list = sorted(days_data.values(), key=lambda x: x["day_number"])
        
        batches_data.append({
            "batch_code": batch.code,
            "batch_name": batch.name,
            "batch_id": str(batch.id),
            "days": days_list,
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "batches": batches_data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def assign_fixed_slot(request):
    """
    Admin assigns a fixed slot for a batch. Can be a teacher class, exam, or free period.
    This locks the slot so optimization cannot change it.
    
    Payload:
    {
        "timetable_id": "uuid",
        "slot_code": "d1_s1",           # or use day_slot_id
        "day_slot_id": "uuid",          # alternative to slot_code
        "batch_code": "BATCH-001",
        "teacher_code": "TCH-CENT-230", # Optional - omit for Exam/Free
        "subject": "Physics"            # Required - can be "Exam", "Free Period", etc.
    }
    
    Examples:
    1. Teacher class:
       {"timetable_id": "...", "slot_code": "d1_s1", "batch_code": "BATCH-001", 
        "teacher_code": "TCH-CENT-230", "subject": "Physics"}
    
    2. Exam (no teacher):
       {"timetable_id": "...", "slot_code": "d1_s1", "batch_code": "BATCH-001", 
        "subject": "Exam"}
    
    3. Free Period (no teacher):
       {"timetable_id": "...", "slot_code": "d1_s1", "batch_code": "BATCH-001", 
        "subject": "Free Period"}
    
    4. Free teacher (special):
       {"timetable_id": "...", "slot_code": "d1_s1", "batch_code": "BATCH-001", 
        "teacher_code": "FREE", "subject": "Free"}
    
    Returns:
    {
        "message": "Fixed slot assigned successfully.",
        "slot_code": "d1_s1",
        "day": "Friday",
        "batch_code": "BATCH-001",
        "teacher_code": "TCH-CENT-230",  # null if no teacher
        "subject": "Physics",
        "is_locked": true
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can assign fixed slots."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    timetable_id = request.data.get("timetable_id")
    slot_code = request.data.get("slot_code")
    day_slot_id = request.data.get("day_slot_id")
    batch_code = request.data.get("batch_code")
    teacher_code = request.data.get("teacher_code")
    subject = request.data.get("subject", "")
    
    if not timetable_id or not batch_code:
        return Response(
            {"detail": "timetable_id and batch_code are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    if not slot_code and not day_slot_id:
        return Response(
            {"detail": "Either slot_code or day_slot_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get day slot
    try:
        if day_slot_id:
            day_slot = DaySlot.objects.get(id=day_slot_id, timetable=timetable)
        else:
            day_slot = DaySlot.objects.get(slot_code=slot_code, timetable=timetable)
    except DaySlot.DoesNotExist:
        return Response(
            {"detail": f"Slot with code '{slot_code}' not found in this timetable."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except DaySlot.MultipleObjectsReturned:
        return Response(
            {"detail": f"Multiple slots found with code '{slot_code}'. Please use day_slot_id instead."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get batch
    try:
        batch = Batch.objects.select_related("program", "center").get(code=batch_code)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with code '{batch_code}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Verify batch belongs to the same center as timetable
    # Check direct center field (batch.center) since Program doesn't have center
    if batch.center and batch.center != timetable.center:
        return Response(
            {"detail": f"Batch '{batch_code}' does not belong to the same center as the timetable."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get teacher (if provided)
    teacher = None
    if teacher_code:
        # Handle FREE teacher (special case)
        # We NO LONGER create a dummy user. We just set teacher=None and subject="Free Class X"
        is_free_teacher = teacher_code.upper().startswith("FREE")
        
        if is_free_teacher:
            teacher = None
            # If subject not provided, use a default like "Free Class"
            if not subject:
                # Try to extract number if possible, e.g. FREE1 -> Free Class 1
                try:
                    num = teacher_code.upper().replace("FREE", "")
                    if num.isdigit():
                         subject = f"Free Class {num}"
                    else:
                         subject = "Free Class"
                except:
                    subject = "Free Class"
        else:
            try:
                from django.db.models import Q
                teacher = AccountUser.objects.get(
                    Q(role=AccountUser.ROLE_TEACHER) &
                    (Q(teacher_code__iexact=teacher_code) |
                     Q(username__iexact=teacher_code) |
                     Q(email__iexact=teacher_code))
                )
            except AccountUser.DoesNotExist:
                return Response(
                    {"detail": f"Teacher with code '{teacher_code}' not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            
            # Verify teacher belongs to the same center
            if teacher.center != timetable.center:
                return Response(
                    {"detail": f"Teacher '{teacher_code}' does not belong to the same center as the timetable."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
        # If subject not provided, get from teacher
        if not subject and teacher:
            if is_free_teacher:
                subject = "FREE"
            else:
                subject = teacher.teacher_subjects or ""
    
    # Create or update FixedSlot
    try:
        from django.db import transaction
        with transaction.atomic():
            fixed_slot, created = FixedSlot.objects.update_or_create(
                timetable=timetable,
                day_slot=day_slot,
                batch=batch,
                defaults={
                    "teacher": teacher,
                    "subject": subject,
                    "is_locked": True,
                }
            )
            
            # Map day constant to display name
            day_display_map = {
                DaySlot.MONDAY: "Monday",
                DaySlot.TUESDAY: "Tuesday",
                DaySlot.WEDNESDAY: "Wednesday",
                DaySlot.THURSDAY: "Thursday",
                DaySlot.FRIDAY: "Friday",
                DaySlot.SATURDAY: "Saturday",
                DaySlot.SUNDAY: "Sunday",
            }
            
            action = "created" if created else "updated"
            return Response(
                {
                    "message": f"Fixed slot assigned successfully ({action}).",
                    "slot_code": day_slot.slot_code,
                    "day": day_display_map.get(day_slot.day, day_slot.day),
                    "start_time": str(day_slot.start_time),
                    "end_time": str(day_slot.end_time),
                    "batch_code": batch.code,
                    "batch_name": batch.name,
                    "teacher_code": teacher.teacher_code if teacher else None,
                    "teacher_name": f"{teacher.first_name} {teacher.last_name}".strip() if teacher else None,
                    "subject": subject or "Free / Exam",
                    "is_locked": fixed_slot.is_locked,
                },
                status=status.HTTP_200_OK,
            )
    except Exception as e:
        return Response(
            {"detail": f"Error assigning fixed slot: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_fixed_slots(request, timetable_id: str):
    """
    Get all fixed slots for a timetable.
    
    GET /api/timetable/timetables/<timetable_id>/fixed-slots/
    GET /api/timetable/timetables/<timetable_id>/fixed-slots/?batch_code=BATCH-001
    GET /api/timetable/timetables/<timetable_id>/fixed-slots/?batch_id=uuid
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "fixed_slots": [
            {
                "id": "uuid",
                "slot_code": "m1",
                "day": "Monday",
                "start_time": "08:00:00",
                "end_time": "09:30:00",
                "batch_code": "HDTN-1A-ZA1",
                "batch_name": "Super 30 - Batch A (2025)",
                "teacher_code": "AK-CAP",
                "teacher_name": "A K",
                "subject": "Physics",
                "is_locked": true
            }
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can view fixed slots."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only view their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only view timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get filter params
    batch_code = request.query_params.get("batch_code")
    batch_id = request.query_params.get("batch_id")
    
    # Get all fixed slots for this timetable
    fixed_slots_qs = FixedSlot.objects.filter(
        timetable=timetable
    ).select_related("day_slot", "batch", "teacher")
    
    # Apply batch filter if provided
    if batch_id:
        fixed_slots_qs = fixed_slots_qs.filter(batch_id=batch_id)
    elif batch_code:
        fixed_slots_qs = fixed_slots_qs.filter(batch__code=batch_code)
    
    fixed_slots = fixed_slots_qs.order_by("day_slot__day_index", "day_slot__slot_number", "batch__code")
    
    # Map day constant to display name
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    fixed_slots_data = []
    for fixed_slot in fixed_slots:
        fixed_slots_data.append({
            "id": str(fixed_slot.id),
            "slot_code": fixed_slot.day_slot.slot_code,
            "day": day_display_map.get(fixed_slot.day_slot.day, fixed_slot.day_slot.day),
            "start_time": str(fixed_slot.day_slot.start_time),
            "end_time": str(fixed_slot.day_slot.end_time),
            "batch_code": fixed_slot.batch.code,
            "batch_name": fixed_slot.batch.name,
            "teacher_code": fixed_slot.teacher.teacher_code if fixed_slot.teacher else None,
            "teacher_name": f"{fixed_slot.teacher.first_name} {fixed_slot.teacher.last_name}".strip() if fixed_slot.teacher else None,
            "subject": fixed_slot.subject or "Free / Exam",
            "is_locked": fixed_slot.is_locked,
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "center": timetable.center.name,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "fixed_slots": fixed_slots_data,
            "total": len(fixed_slots_data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def update_fixed_slot(request, fixed_slot_id: str):
    """
    Update an existing fixed slot.
    Admin can change teacher, subject, or lock status of a fixed slot.
    
    Payload:
    {
        "teacher_code": "NEW-TEACHER",  # Optional - change teacher
        "subject": "New Subject",        # Optional - change subject
        "is_locked": false               # Optional - unlock the slot
    }
    
    Returns:
    {
        "message": "Fixed slot updated successfully.",
        "id": "uuid",
        "slot_code": "m1",
        "day": "Monday",
        "batch_code": "HDTN-1A-ZA1",
        "teacher_code": "NEW-TEACHER",
        "subject": "New Subject",
        "is_locked": false
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can update fixed slots."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get fixed slot
    try:
        fixed_slot = FixedSlot.objects.select_related(
            "timetable", "timetable__center", "day_slot", "batch", "teacher"
        ).get(id=fixed_slot_id)
    except FixedSlot.DoesNotExist:
        return Response(
            {"detail": "Fixed slot not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    timetable = fixed_slot.timetable
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage fixed slots in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get update data
    teacher_code = request.data.get("teacher_code")
    subject = request.data.get("subject")
    is_locked = request.data.get("is_locked")
    
    # Track what was updated
    updated_fields = []
    
    # Update teacher if provided
    if teacher_code is not None:
        if teacher_code == "":
            # Remove teacher (make it a free slot)
            fixed_slot.teacher = None
            updated_fields.append("teacher")
        else:
            # Handle FREE teacher (special case)
            is_free_teacher = teacher_code.upper().startswith("FREE")
            
            if is_free_teacher:
                free_code = teacher_code.upper()
                try:
                    teacher = AccountUser.objects.get(
                        role=AccountUser.ROLE_TEACHER,
                        center=timetable.center,
                        teacher_code__iexact=free_code
                    )
                except AccountUser.DoesNotExist:
                    # Create new FREE teacher
                    teacher = AccountUser.objects.create_user(
                        username=free_code,
                        teacher_code=free_code,
                        role=AccountUser.ROLE_TEACHER,
                        center=timetable.center,
                        first_name="FREE",
                        last_name=free_code.replace("FREE", "").strip() or "",
                        teacher_subjects="FREE",
                        password="FREE_TEACHER_PLACEHOLDER",
                    )
            else:
                try:
                    from django.db.models import Q
                    teacher = AccountUser.objects.get(
                        Q(role=AccountUser.ROLE_TEACHER) &
                        (Q(teacher_code__iexact=teacher_code) |
                         Q(username__iexact=teacher_code) |
                         Q(email__iexact=teacher_code))
                    )
                except AccountUser.DoesNotExist:
                    return Response(
                        {"detail": f"Teacher with code '{teacher_code}' not found."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                
                # Verify teacher belongs to the same center
                if teacher.center != timetable.center:
                    return Response(
                        {"detail": f"Teacher '{teacher_code}' does not belong to the same center as the timetable."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            
            fixed_slot.teacher = teacher
            updated_fields.append("teacher")
            
            # Auto-update subject from teacher if subject not explicitly provided
            if subject is None and teacher:
                if is_free_teacher:
                    fixed_slot.subject = "FREE"
                else:
                    fixed_slot.subject = teacher.teacher_subjects or fixed_slot.subject
                updated_fields.append("subject (auto)")
    
    # Update subject if provided
    if subject is not None:
        fixed_slot.subject = subject
        if "subject (auto)" not in updated_fields:
            updated_fields.append("subject")
    
    # Update is_locked if provided
    if is_locked is not None:
        fixed_slot.is_locked = bool(is_locked)
        updated_fields.append("is_locked")
    
    # Save changes
    if updated_fields:
        try:
            fixed_slot.save()
        except Exception as e:
            return Response(
                {"detail": f"Error updating fixed slot: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        return Response(
            {"detail": "No fields to update. Provide teacher_code, subject, or is_locked."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Map day constant to display name
    day_display_map = {
        DaySlot.MONDAY: "Monday",
        DaySlot.TUESDAY: "Tuesday",
        DaySlot.WEDNESDAY: "Wednesday",
        DaySlot.THURSDAY: "Thursday",
        DaySlot.FRIDAY: "Friday",
        DaySlot.SATURDAY: "Saturday",
        DaySlot.SUNDAY: "Sunday",
    }
    
    return Response(
        {
            "message": f"Fixed slot updated successfully. Updated: {', '.join(updated_fields)}",
            "id": str(fixed_slot.id),
            "slot_code": fixed_slot.day_slot.slot_code,
            "day": day_display_map.get(fixed_slot.day_slot.day, fixed_slot.day_slot.day),
            "start_time": str(fixed_slot.day_slot.start_time),
            "end_time": str(fixed_slot.day_slot.end_time),
            "batch_code": fixed_slot.batch.code,
            "batch_name": fixed_slot.batch.name,
            "teacher_code": fixed_slot.teacher.teacher_code if fixed_slot.teacher else None,
            "teacher_name": f"{fixed_slot.teacher.first_name} {fixed_slot.teacher.last_name}".strip() if fixed_slot.teacher else None,
            "subject": fixed_slot.subject or "Free / Exam",
            "is_locked": fixed_slot.is_locked,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_fixed_slot(request, fixed_slot_id: str):
    """
    Delete/remove a fixed slot assignment.
    
    DELETE /api/timetable/admin/timetables/fixed-slots/<fixed_slot_id>/delete/
    
    Returns:
    {
        "message": "Fixed slot removed successfully.",
        "slot_code": "d1_s1",
        "batch_code": "BATCH-001"
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can delete fixed slots."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get fixed slot
    try:
        fixed_slot = FixedSlot.objects.select_related(
            "timetable", "timetable__center", "day_slot", "batch"
        ).get(id=fixed_slot_id)
    except FixedSlot.DoesNotExist:
        return Response(
            {"detail": "Fixed slot not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    timetable = fixed_slot.timetable
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage fixed slots in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Store info for response before deleting
    slot_code = fixed_slot.day_slot.slot_code
    batch_code = fixed_slot.batch.code
    batch_name = fixed_slot.batch.name
    
    # Delete the fixed slot
    fixed_slot.delete()
    
    return Response(
        {
            "message": "Fixed slot removed successfully.",
            "slot_code": slot_code,
            "batch_code": batch_code,
            "batch_name": batch_name,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def check_timetable_feasibility(request, timetable_id: str):
    """
    Check if timetable generation is feasible before running the genetic algorithm.
    
    POST /api/timetable/timetables/<timetable_id>/check-feasibility/
    
    Body (optional):
    {
        "start_slot": "d1_s1"  # Default: first slot
    }
    
    Returns:
    {
        "feasible": true/false,
        "violations": {
            "RULE_1": ["Slot d1_s1: Available Teachers=2, Total Batches=5"],
            "RULE_2": ["d1-d1_s1: Teacher TCH-001 not available"],
            "RULE_3": ["Batch BATCH-001: RemainingRequiredClasses=10, Total slots=8"],
            "RULE_4": [...],
            "RULE_5": [...],
            "RULE_6": [...]
        },
        "summary": {
            "total_batches": 5,
            "total_teachers": 10,
            "total_slots": 20,
            "total_violations": 3
        },
        "rules_explanation": {
            "RULE_1": "Each slot must have enough available teachers for all batches",
            "RULE_2": "Fixed slot teachers must be available in that slot",
            "RULE_3": "Batch must have enough slots to meet minimum class requirements",
            "RULE_4": "Batch must not exceed maximum class limit",
            "RULE_5": "Batch max classes must be >= min classes remaining",
            "RULE_6": "Teacher must have enough available slots to meet their minimum load"
        }
    }
    """
    try:
        timetable = Timetable.objects.get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can check feasibility."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get start_slot from request
    data = request.data
    start_slot = data.get("start_slot", None)
    
    try:
        # Build payload from models
        payload = build_full_payload(timetable_id)
        
        available_slots = payload["available_slots"]
        teachers_list = payload["teachers"]
        batches_dict = payload["batches"]
        fixed_slots = payload["fixed_slots"]
        
        # Convert to algorithm format
        teachers_dict = convert_teachers_to_algorithm_format(teachers_list)
        batches_dict_algo = convert_batches_to_algorithm_format(batches_dict, teachers_dict)
        
        # Determine start_slot if not provided
        if not start_slot:
            first_day = list(available_slots.keys())[0] if available_slots else None
            if first_day and available_slots[first_day]:
                start_slot = list(available_slots[first_day].keys())[0]
            else:
                return Response(
                    {"detail": "No slots available in timetable."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
        # Check feasibility
        feasible, violations = check_timetable_feasibility_from_start(
            available_slots=available_slots,
            teachers=teachers_dict,
            batches=batches_dict_algo,
            new_fixed_slots=fixed_slots,
            start_slot=start_slot
        )
        
        # Calculate total slots
        total_slots = sum(len(slots) for slots in available_slots.values())
        
        # Count total violations
        total_violations = sum(len(v) for v in violations.values())
        
        return Response(
            {
                "feasible": feasible,
                "violations": violations,
                "summary": {
                    "total_batches": len(batches_dict_algo),
                    "total_teachers": len(teachers_dict),
                    "total_slots": total_slots,
                    "total_violations": total_violations,
                    "start_slot": start_slot,
                },
                "rules_explanation": {
                    "RULE_1": "Each slot must have enough available teachers for all batches",
                    "RULE_2": "Fixed slot teachers must be available in that slot",
                    "RULE_3": "Batch must have enough slots to meet minimum class requirements",
                    "RULE_4": "Batch must not exceed maximum class limit",
                    "RULE_5": "Batch max classes must be >= min classes remaining",
                    "RULE_6": "Teacher must have enough available slots to meet their minimum load"
                }
            },
            status=status.HTTP_200_OK if feasible else status.HTTP_400_BAD_REQUEST,
        )
        
    except Exception as e:
        return Response(
            {"detail": f"Error checking feasibility: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def run_timetable_optimization(request, timetable_id: str):
    """
    Run timetable optimization using genetic algorithm (async with Celery).
    
    POST /api/timetable/timetables/<timetable_id>/optimize/
    
    Body:
    {
        "start_slot": "m1",  # Optional: Default is first slot
        "clear_existing": true,  # Optional: Clear existing entries (default: true)
        
        # User configurable parameters (from frontend)
        "max_retries": 1000,  # Maximum optimization attempts
        "max_try_for_slot_assign": 100,  # Max attempts per slot assignment
        "weight_power_fector": 3,  # Power factor for weight calculations
        "max_one_subject_repetation_per_day": 2,  # Max times subject can repeat per day
        "max_one_subject_repetation_per_day_penalty_fector": 0,  # Penalty factor for repetition
        "weight_penalty_consu_sub_repetation": [0.01, 0, 0, 0, 0]  # Penalties for consecutive days
    }
    
    HARDCODED (internal - not from frontend):
    - Genetic Algorithm: enabled
    - Async (Celery): enabled  
    - Generations: 100
    - Population Size: 50
    - Mutation Rate: 0.03
    
    Returns:
    {
        "task_id": "abc-123-def",
        "status": "PENDING",
        "algorithm_used": "genetic_algorithm",
        "message": "Genetic algorithm task started. Population: 50, Generations: 100"
    }
    
    Then poll for progress:
    GET /api/timetable/tasks/<task_id>/status/
    """
    try:
        timetable = Timetable.objects.get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can run optimization."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get common parameters
    data = request.data
    start_slot = data.get("start_slot", None)
    clear_existing = data.get("clear_existing", True)
    
    # HARDCODED: Always use genetic algorithm with async
    use_genetic_algorithm = True
    run_async = True
    
    # FROM FRONTEND: User configurable parameters
    max_retries = data.get("max_retries", 1000)
    max_try_for_slot_assign = data.get("max_try_for_slot_assign", 100)
    weight_power_fector = data.get("weight_power_fector", 3)
    max_one_subject_repetation_per_day = data.get("max_one_subject_repetation_per_day", 2)
    max_one_subject_repetation_per_day_penalty_fector = data.get("max_one_subject_repetation_per_day_penalty_fector", 0)
    weight_penalty_consu_sub_repetation = data.get("weight_penalty_consu_sub_repetation", [0.01, 0, 0, 0, 0])
    
    # HARDCODED: Genetic algorithm parameters (not from frontend)
    generations = 100
    population_size = 50
    mutation_rate = 0.03
    elite_size = 10
    tournament_size = 5
    max_retries_per_individual = max_try_for_slot_assign  # Use frontend value
    
    # HARDCODED: Fitness function parameters (internal algorithm settings)
    hard_constraint_penalty = 30000
    min_weekly_classes_penalty = 40
    min_weekly_classes_penalty_exponent = 2
    max_weekly_classes_penalty = 200
    max_weekly_classes_penalty_exponent = 4
    consu_sub_rep_penalty = 40
    consu_sub_rep_penalty_fector = 3
    sub_variation_per_day_reward_fector = 1.2
    sub_spread_over_week_reward_fector = 2.0
    
    try:
        # Build payload from models
        payload = build_full_payload(timetable_id)
        
        available_slots = payload["available_slots"]
        teachers_list = payload["teachers"]
        batches_dict = payload["batches"]
        fixed_slots = payload["fixed_slots"]
        
        # Convert to algorithm format
        teachers_dict = convert_teachers_to_algorithm_format(teachers_list)
        batches_dict_algo = convert_batches_to_algorithm_format(batches_dict, teachers_dict)
        
        # Determine start_slot if not provided
        if not start_slot:
            first_day = list(available_slots.keys())[0] if available_slots else None
            if first_day and available_slots[first_day]:
                start_slot = list(available_slots[first_day].keys())[0]
            else:
                return Response(
                    {"detail": "No slots available in timetable."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        
        # Step 1: Check feasibility
        feasible, violations = check_timetable_feasibility_from_start(
            available_slots=available_slots,
            teachers=teachers_dict,
            batches=batches_dict_algo,
            new_fixed_slots=fixed_slots,
            start_slot=start_slot
        )
        
        if not feasible:
            return Response(
                {
                    "success": False,
                    "feasible": False,
                    "violations": violations,
                    "timetable_generated": False,
                    "entries_created": 0,
                    "message": "Timetable generation is not feasible. Please check violations and adjust constraints."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Step 2: Generate timetable using selected algorithm
        if use_genetic_algorithm:
            # Serialize data for Celery task or sync execution
            teachers_data = {}
            for code, teacher in teachers_dict.items():
                teachers_data[code] = {
                    'code': teacher.code,
                    'name': teacher.name,
                    'employ_id': getattr(teacher, 'employ_id', ''),
                    'subject': getattr(teacher, 'subject', ''),
                    'avilable_slots': list(teacher.avilable_slots)
                }
            
            batches_data = {}
            for key, batch in batches_dict_algo.items():
                sub_teachers_serialized = {}
                for sub_key, sub_data in batch.sub_teachers.items():
                    sub_teachers_serialized[sub_key] = {
                        'subject': sub_data['subject'],
                        'teacher_code': sub_data['teacher'].code,
                        'min_class_per_week': sub_data['min_class_per_week'],
                        'max_class_per_week': sub_data['max_class_per_week'],
                        'max_class_per_day': sub_data['max_class_per_day']
                    }
                batches_data[key] = {
                    'batch_code': batch.batch_code,
                    'sub_teachers': sub_teachers_serialized
                }
            
            if run_async:
                # Run with Celery asynchronously
                from .tasks import run_genetic_algorithm_task
                from accounts.utils import get_current_db
                
                task = run_genetic_algorithm_task.delay(
                    timetable_id=str(timetable_id),
                    batches_data=batches_data,
                    teachers_data=teachers_data,
                    available_slots=available_slots,
                    fixed_slots=fixed_slots,
                    generations=generations,
                    population_size=population_size,
                    mutation_rate=mutation_rate,
                    elite_size=elite_size,
                    tournament_size=tournament_size,
                    max_retries_per_individual=max_retries_per_individual,
                    clear_existing=clear_existing,
                    hard_constraint_penalty=hard_constraint_penalty,
                    min_weekly_classes_penalty=min_weekly_classes_penalty,
                    min_weekly_classes_penalty_exponent=min_weekly_classes_penalty_exponent,
                    max_weekly_classes_penalty=max_weekly_classes_penalty,
                    max_weekly_classes_penalty_exponent=max_weekly_classes_penalty_exponent,
                    consu_sub_rep_penalty=consu_sub_rep_penalty,
                    consu_sub_rep_penalty_fector=consu_sub_rep_penalty_fector,
                    sub_variation_per_day_reward_fector=sub_variation_per_day_reward_fector,
                    sub_spread_over_week_reward_fector=sub_spread_over_week_reward_fector,
                    tenant_db=get_current_db()
                )
                
                return Response(
                    {
                        "task_id": task.id,
                        "status": "PENDING",
                        "algorithm_used": "genetic_algorithm",
                        "message": f"Genetic algorithm task started. Population: {population_size}, Generations: {generations}"
                    },
                    status=status.HTTP_202_ACCEPTED,
                )
            else:
                # Run genetic algorithm synchronously
                try:
                    generated_timetable, best_fitness = run_genetic_algorithm(
                        batches=batches_dict_algo,
                        teachers=teachers_dict,
                        avilable_slots=available_slots,
                        fixed_slots=fixed_slots,
                        generations=generations,
                        population_size=population_size,
                        mutation_rate=mutation_rate,
                        elite_size=elite_size,
                        tournament_size=tournament_size,
                        max_retries_per_individual=max_retries_per_individual
                    )
                    
                    # Check final constraints
                    final_violations = check_constraints(
                        generated_timetable, batches_dict_algo, teachers_dict, available_slots, fixed_slots
                    )
                    
                    algorithm_used = "genetic_algorithm"
                    additional_info = {
                        "best_fitness": best_fitness,
                        "generations": generations,
                        "population_size": population_size,
                        "final_violations": final_violations
                    }
                except Exception as e:
                    return Response(
                        {
                            "success": False,
                            "feasible": True,
                            "violations": {},
                            "timetable_generated": False,
                            "entries_created": 0,
                            "algorithm_used": "genetic_algorithm",
                            "message": f"Failed to generate timetable with genetic algorithm: {str(e)}"
                        },
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )
        else:
            # Use random generation algorithm
            try:
                generated_timetable = generate_random_timetable(
                    batches=batches_dict_algo,
                    teachers=teachers_dict,
                    avilable_slots=available_slots,
                    fixed_slots=fixed_slots,
                    MAX_RETRIES=max_retries,
                    max_try_for_slot_assign=max_try_for_slot_assign,
                    weight_power_fector=weight_power_fector,
                    max_one_subject_repetation_per_day=max_one_subject_repetation_per_day,
                    max_one_subject_repetation_per_day_penalty_fector=max_one_subject_repetation_per_day_penalty_fector,
                    weight_penalty_consu_sub_repetation=weight_penalty_consu_sub_repetation
                )
                algorithm_used = "random_generation"
                additional_info = {}
            except Exception as e:
                return Response(
                    {
                        "success": False,
                        "feasible": True,
                        "violations": {},
                        "timetable_generated": False,
                        "entries_created": 0,
                        "algorithm_used": "random_generation",
                        "message": f"Failed to generate timetable: {str(e)}"
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        
        # Step 3: Save results to database
        entries_created = save_generated_timetable(
            timetable, generated_timetable, teachers_dict, batches_dict_algo, clear_existing
        )
        
        return Response(
            {
                "success": True,
                "feasible": True,
                "violations": {},
                "timetable_generated": True,
                "entries_created": entries_created,
                "algorithm_used": algorithm_used,
                "message": f"Timetable generated successfully using {algorithm_used}. Created {entries_created} entries.",
                **additional_info
            },
            status=status.HTTP_200_OK,
        )
        
    except Exception as e:
        import traceback
        return Response(
            {
                "success": False,
                "feasible": None,
                "violations": {},
                "timetable_generated": False,
                "entries_created": 0,
                "message": f"Error during optimization: {str(e)}",
                "traceback": traceback.format_exc() if settings.DEBUG else None
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_genetic_algorithm_task_status(request, task_id: str):
    """
    Get the status of a genetic algorithm task.
    
    GET /api/timetable/tasks/<task_id>/status/
    
    Returns:
    {
        "task_id": "...",
        "status": "PROGRESS|SUCCESS|FAILURE|PENDING",
        "progress": {
            "current": 50,
            "total": 100,
            "status": "Generation 50/100",
            "best_fitness": 1250.5
        },
        "result": {...}  # Only if status is SUCCESS
    }
    """
    from celery.result import AsyncResult
    
    task = AsyncResult(task_id)
    
    response = {
        "task_id": task_id,
        "status": task.status,
    }
    
    if task.status == 'PROGRESS':
        response["progress"] = task.info
    elif task.status == 'SUCCESS':
        response["result"] = task.result
    elif task.status == 'FAILURE':
        response["error"] = str(task.result)
    
    return Response(response, status=status.HTTP_200_OK)


def save_generated_timetable(timetable, generated_timetable, teachers_dict, batches_dict_algo, clear_existing):
    """Helper function to save generated timetable to database."""
    # Day mapping for weekly timetables
    slot_code_to_dayslot = {}
    for day_slot in timetable.day_slots.all():
        if day_slot.day_index:
            day_key = f"d{day_slot.day_index}"
        else:
            day_key = DAY_MAP_SHORT.get(day_slot.day, "")
        
        code = day_slot.slot_code or f"{day_key}{day_slot.slot_number}"
        slot_code_to_dayslot[code] = day_slot
    
    # Create mapping: teacher_code -> User object
    teacher_code_to_user = {}
    for teacher_code in teachers_dict.keys():
        user = AccountUser.objects.filter(
            teacher_code=teacher_code
        ).first() or AccountUser.objects.filter(
            username=teacher_code
        ).first()
        if user:
            teacher_code_to_user[teacher_code] = user
    
    # Create mapping: batch_code -> Batch object
    batch_code_to_batch = {}
    for batch_code in batches_dict_algo.keys():
        batch = Batch.objects.filter(code=batch_code).first()
        if batch:
            batch_code_to_batch[batch_code] = batch
    
    entries_created = 0
    
    with transaction.atomic():
        if clear_existing:
            TimetableEntry.objects.filter(day_slot__timetable=timetable).delete()
        
        for day_key, slots in generated_timetable.items():
            for slot_code, batch_assignments in slots.items():
                day_slot = slot_code_to_dayslot.get(slot_code)
                if not day_slot:
                    continue
                
                for batch_code, (subject, teacher_code) in batch_assignments.items():
                    batch = batch_code_to_batch.get(batch_code)
                    if not batch:
                        continue
                    
                    teacher = teacher_code_to_user.get(teacher_code)
                    
                    entry, created = TimetableEntry.objects.get_or_create(
                        day_slot=day_slot,
                        batch=batch,
                        defaults={
                            "subject": subject,
                            "teacher": teacher,
                        }
                    )
                    if not created:
                        entry.subject = subject
                        entry.teacher = teacher
                        entry.save()
                    
                    entries_created += 1
    
    return entries_created


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def regenerate_timetable_from_slot(request, timetable_id: str):
    """
    Regenerate timetable from a specific slot using genetic algorithm (async with Celery).
    
    This keeps all assignments before the stop_slot fixed and regenerates the rest
    using the genetic algorithm for better optimization.
    
    POST /api/timetable/timetables/<timetable_id>/regenerate-from-slot/
    
    Body:
    {
        "stop_slot": "w3",  # Required: slot code to stop at (e.g., 'm1', 'tu3', 'w3', 'd1_3')
        
        # User configurable parameters (optional)
        "max_retries": 1000,
        "max_try_for_slot_assign": 100,
        "weight_power_fector": 3,
        "max_one_subject_repetation_per_day": 2,
        "max_one_subject_repetation_per_day_penalty_fector": 0,
        "weight_penalty_consu_sub_repetation": [0.01, 0, 0, 0, 0]
    }
    
    Returns:
    {
        "task_id": "abc-123-def",
        "status": "PENDING",
        "algorithm_used": "genetic_algorithm",
        "stop_slot": "w3",
        "fixed_slots_count": 15,
        "message": "Genetic algorithm task started..."
    }
    
    Then poll for progress:
    GET /api/timetable/tasks/<task_id>/status/
    """
    try:
        timetable = Timetable.objects.get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin or Super Admin can regenerate timetable."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    data = request.data
    stop_slot = data.get("stop_slot")
    
    if not stop_slot:
        return Response(
            {"detail": "stop_slot is required. Example: 'm1', 'tu3', 'w3', 'd1_3'"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # FROM FRONTEND: User configurable parameters
    max_retries = data.get("max_retries", 1000)
    max_try_for_slot_assign = data.get("max_try_for_slot_assign", 100)
    weight_power_fector = data.get("weight_power_fector", 3)
    max_one_subject_repetation_per_day = data.get("max_one_subject_repetation_per_day", 2)
    max_one_subject_repetation_per_day_penalty_fector = data.get("max_one_subject_repetation_per_day_penalty_fector", 0)
    weight_penalty_consu_sub_repetation = data.get("weight_penalty_consu_sub_repetation", [0.01, 0, 0, 0, 0])
    
    # HARDCODED: Genetic algorithm parameters
    generations = 100
    population_size = 50
    mutation_rate = 0.03
    elite_size = 10
    tournament_size = 5
    max_retries_per_individual = max_try_for_slot_assign
    
    # HARDCODED: Fitness function parameters
    hard_constraint_penalty = 30000
    min_weekly_classes_penalty = 40
    min_weekly_classes_penalty_exponent = 2
    max_weekly_classes_penalty = 200
    max_weekly_classes_penalty_exponent = 4
    consu_sub_rep_penalty = 40
    consu_sub_rep_penalty_fector = 3
    sub_variation_per_day_reward_fector = 1.2
    sub_spread_over_week_reward_fector = 2.0
    
    try:
        # Build payload from models
        payload = build_full_payload(timetable_id)
        
        available_slots = payload["available_slots"]
        teachers_list = payload["teachers"]
        batches_dict = payload["batches"]
        original_fixed_slots = payload["fixed_slots"]
        
        # Validate stop_slot exists in available_slots
        slot_found = False
        for day, slots in available_slots.items():
            if stop_slot in slots:
                slot_found = True
                break
        
        if not slot_found:
            return Response(
                {"detail": f"stop_slot '{stop_slot}' not found in available slots."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Convert to algorithm format
        teachers_dict = convert_teachers_to_algorithm_format(teachers_list)
        batches_dict_algo = convert_batches_to_algorithm_format(batches_dict, teachers_dict)
        
        # Get current timetable from database
        current_timetable = {}
        entries = TimetableEntry.objects.filter(
            day_slot__timetable=timetable
        ).select_related('day_slot', 'batch', 'teacher')
        
        for entry in entries:
            day_slot = entry.day_slot
            # Determine day key
            if day_slot.day_index:
                day_key = f"d{day_slot.day_index}"
            else:
                day_key = DAY_MAP_SHORT.get(day_slot.day, "")
            
            slot_code = day_slot.slot_code or f"{day_key}{day_slot.slot_number}"
            batch_code = entry.batch.code
            subject = entry.subject or ""
            teacher_code = entry.teacher.teacher_code if entry.teacher else ""
            
            if day_key not in current_timetable:
                current_timetable[day_key] = {}
            if slot_code not in current_timetable[day_key]:
                current_timetable[day_key][slot_code] = {}
            
            current_timetable[day_key][slot_code][batch_code] = (subject, teacher_code)
        
        # Generate new fixed slots from current timetable up to stop_slot
        new_fixed_slots = generate_new_fixed_slots(
            timetable=current_timetable,
            original_fixed_slots=original_fixed_slots,
            available_slots=available_slots,
            stop_slot=stop_slot
        )
        
        # Count fixed slots
        fixed_slots_count = sum(
            len(batches) 
            for slots in new_fixed_slots.values() 
            for batches in slots.values()
        )
        
        # Determine start_slot (first slot after stop_slot)
        start_slot = None
        found_stop = False
        for day in available_slots:
            for slot in available_slots[day]:
                if found_stop:
                    start_slot = slot
                    break
                if slot == stop_slot:
                    found_stop = True
            if start_slot:
                break
        
        if not start_slot:
            # stop_slot is the last slot, nothing to regenerate
            return Response(
                {
                    "success": True,
                    "feasible": True,
                    "violations": {},
                    "timetable_generated": False,
                    "entries_created": 0,
                    "stop_slot": stop_slot,
                    "fixed_slots_count": fixed_slots_count,
                    "message": "stop_slot is the last slot. No regeneration needed."
                },
                status=status.HTTP_200_OK,
            )
        
        # Check feasibility from start_slot
        feasible, violations = check_timetable_feasibility_from_start(
            available_slots=available_slots,
            teachers=teachers_dict,
            batches=batches_dict_algo,
            new_fixed_slots=new_fixed_slots,
            start_slot=start_slot
        )
        
        if not feasible:
            return Response(
                {
                    "success": False,
                    "feasible": False,
                    "violations": violations,
                    "timetable_generated": False,
                    "entries_created": 0,
                    "stop_slot": stop_slot,
                    "fixed_slots_count": fixed_slots_count,
                    "message": "Timetable regeneration is not feasible from this slot. Check violations."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        
        # Serialize data for Celery task
        teachers_data = {}
        for code, teacher in teachers_dict.items():
            teachers_data[code] = {
                'code': teacher.code,
                'name': teacher.name,
                'employ_id': getattr(teacher, 'employ_id', ''),
                'subject': getattr(teacher, 'subject', ''),
                'avilable_slots': list(teacher.avilable_slots)
            }
        
        batches_data = {}
        for key, batch in batches_dict_algo.items():
            sub_teachers_serialized = {}
            for sub_key, sub_data in batch.sub_teachers.items():
                sub_teachers_serialized[sub_key] = {
                    'subject': sub_data['subject'],
                    'teacher_code': sub_data['teacher'].code,
                    'min_class_per_week': sub_data['min_class_per_week'],
                    'max_class_per_week': sub_data['max_class_per_week'],
                    'max_class_per_day': sub_data['max_class_per_day']
                }
            batches_data[key] = {
                'batch_code': batch.batch_code,
                'sub_teachers': sub_teachers_serialized
            }
        
        # Run with Celery asynchronously
        from .tasks import run_genetic_algorithm_task
        
        task = run_genetic_algorithm_task.delay(
            timetable_id=str(timetable_id),
            batches_data=batches_data,
            teachers_data=teachers_data,
            available_slots=available_slots,
            fixed_slots=new_fixed_slots,  # Use the new fixed slots (up to stop_slot)
            generations=generations,
            population_size=population_size,
            mutation_rate=mutation_rate,
            elite_size=elite_size,
            tournament_size=tournament_size,
            max_retries_per_individual=max_retries_per_individual,
            clear_existing=True,  # Always clear and regenerate
            hard_constraint_penalty=hard_constraint_penalty,
            min_weekly_classes_penalty=min_weekly_classes_penalty,
            min_weekly_classes_penalty_exponent=min_weekly_classes_penalty_exponent,
            max_weekly_classes_penalty=max_weekly_classes_penalty,
            max_weekly_classes_penalty_exponent=max_weekly_classes_penalty_exponent,
            consu_sub_rep_penalty=consu_sub_rep_penalty,
            consu_sub_rep_penalty_fector=consu_sub_rep_penalty_fector,
            sub_variation_per_day_reward_fector=sub_variation_per_day_reward_fector,
            sub_spread_over_week_reward_fector=sub_spread_over_week_reward_fector
        )
        
        return Response(
            {
                "task_id": task.id,
                "status": "PENDING",
                "algorithm_used": "genetic_algorithm",
                "stop_slot": stop_slot,
                "fixed_slots_count": fixed_slots_count,
                "message": f"Genetic algorithm task started. Regenerating from slot '{stop_slot}'. Population: {population_size}, Generations: {generations}"
            },
            status=status.HTTP_202_ACCEPTED,
        )
        
    except Exception as e:
        import traceback
        return Response(
            {
                "success": False,
                "feasible": None,
                "violations": {},
                "timetable_generated": False,
                "entries_created": 0,
                "stop_slot": stop_slot,
                "fixed_slots_count": 0,
                "message": f"Error during regeneration: {str(e)}",
                "traceback": traceback.format_exc() if settings.DEBUG else None
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_all_batches_timetable(request, timetable_id: str):
    """
    Get timetable for all batches in a timetable.
    
    Returns timetable organized by batch, showing all slots where each batch has classes.
    
    GET /api/timetable/timetables/<timetable_id>/batches/
    
    Returns:
    {
        "timetable_id": "uuid",
        "timetable": "Center Name - 2025-01-01 to 2025-03-31",
        "batches": [
            {
                "batch_id": "uuid",
                "batch_code": "BATCH-001",
                "batch_name": "Super 30 - Batch A",
                "program": "Super 30",
                "slots": {
                    "mon": [
                        {
                            "slot_id": "uuid",
                            "slot_code": "m1",
                            "slot_number": 1,
                            "start_time": "08:00",
                            "end_time": "09:30",
                            "subject": "Physics",
                            "room_number": "101",
                            "teacher": {
                                "teacher_code": "TCH-XXXX-001",
                                "teacher_name": "Teacher Name"
                            }
                        }
                    ],
                    "tue": [...],
                    ...
                },
                "total_classes": 10
            }
        ],
        "total_batches": 5
    }
    """
    try:
        timetable = Timetable.objects.select_related('center').get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": f"Timetable with id '{timetable_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": f"You can only view timetables in your center '{user.center.name}'."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get all batches assigned to this timetable (via BatchFacultyLoad or TimetableEntry)
    batch_ids = set()
    
    # From BatchFacultyLoad
    batch_ids.update(
        BatchFacultyLoad.objects.filter(timetable=timetable).values_list('batch_id', flat=True)
    )
    
    # From TimetableEntry
    batch_ids.update(
        TimetableEntry.objects.filter(day_slot__timetable=timetable).values_list('batch_id', flat=True)
    )
    
    batches = Batch.objects.filter(id__in=batch_ids).select_related('program').order_by('code')
    
    # Get all timetable entries
    entries = TimetableEntry.objects.filter(
        day_slot__timetable=timetable
    ).select_related('day_slot', 'batch', 'teacher').order_by('day_slot__day', 'day_slot__slot_number')
    
    # Get all fixed slots (for teacher info override)
    fixed_slots = FixedSlot.objects.filter(
        timetable=timetable
    ).select_related('day_slot', 'batch', 'teacher')
    
    # Create a map: (day_slot_id, batch_id) -> fixed slot info
    fixed_slot_map = {}
    for fs in fixed_slots:
        key = (fs.day_slot_id, fs.batch_id)
        fixed_slot_map[key] = {
            "teacher_code": fs.teacher.teacher_code if fs.teacher else None,
            "teacher_name": fs.teacher.get_full_name() if fs.teacher else None,
        }
    
    # Day mapping helper: supports both weekly (mon..sun) and date-based (d1..dN)
    def _day_key_for_slot(slot: DaySlot) -> str:
        if slot.day_index:
            return f"d{slot.day_index}"
        if slot.day == DaySlot.MONDAY:
            return "mon"
        if slot.day == DaySlot.TUESDAY:
            return "tue"
        if slot.day == DaySlot.WEDNESDAY:
            return "wed"
        if slot.day == DaySlot.THURSDAY:
            return "thu"
        if slot.day == DaySlot.FRIDAY:
            return "fri"
        if slot.day == DaySlot.SATURDAY:
            return "sat"
        if slot.day == DaySlot.SUNDAY:
            return "sun"
        return "unknown"
    
    # Organize by batch
    batches_data = []
    for batch in batches:
        batch_entries = [e for e in entries if e.batch_id == batch.id]
        
        slots_by_day = {}
        
        for entry in batch_entries:
            day_key = _day_key_for_slot(entry.day_slot)
            if day_key == "unknown":
                continue
            slot_data = {
                "slot_id": str(entry.day_slot.id),
                "slot_code": entry.day_slot.slot_code,
                "slot_number": entry.day_slot.slot_number,
                "start_time": entry.day_slot.start_time.strftime("%H:%M"),
                "end_time": entry.day_slot.end_time.strftime("%H:%M"),
                "subject": entry.subject,
                "room_number": entry.room_number or "",
                "day_index": entry.day_slot.day_index,
                "actual_date": str(entry.day_slot.actual_date) if entry.day_slot.actual_date else None,
            }
            
            # Add teacher info - first from entry, then from fixed slot as override
            key = (entry.day_slot_id, entry.batch_id)
            if key in fixed_slot_map:
                # Fixed slot overrides entry teacher
                slot_data["teacher"] = fixed_slot_map[key]
            elif entry.teacher:
                # Use teacher from TimetableEntry (from optimization)
                slot_data["teacher"] = {
                    "teacher_code": entry.teacher.teacher_code or entry.teacher.username,
                    "teacher_name": entry.teacher.get_full_name() or entry.teacher.username,
                }
            else:
                slot_data["teacher"] = None
            
            slots_by_day.setdefault(day_key, []).append(slot_data)
        
        batches_data.append({
            "batch_id": str(batch.id),
            "batch_code": batch.code,
            "batch_name": batch.name,
            "program": batch.program.name if batch.program else "",
            "slots": slots_by_day,
            "total_classes": len(batch_entries),
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "batches": batches_data,
            "total_batches": len(batches_data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_batch_timetable(request, timetable_id: str, batch_id: str):
    """
    Get timetable for a specific batch in a timetable.
    
    GET /api/timetable/timetables/<timetable_id>/batches/<batch_id>/
    
    Returns:
    {
        "timetable_id": "uuid",
        "batch_id": "uuid",
        "batch_code": "BATCH-001",
        "batch_name": "Super 30 - Batch A",
        "slots": {
            "mon": [...],
            "tue": [...],
            ...
        },
        "total_classes": 10
    }
    """
    try:
        timetable = Timetable.objects.select_related('center').get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": f"Timetable with id '{timetable_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    try:
        batch = Batch.objects.select_related('program').get(id=batch_id)
    except Batch.DoesNotExist:
        return Response(
            {"detail": f"Batch with id '{batch_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": f"You can only view timetables in your center '{user.center.name}'."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get timetable entries for this batch
    entries = TimetableEntry.objects.filter(
        day_slot__timetable=timetable,
        batch=batch
    ).select_related('day_slot').order_by('day_slot__day', 'day_slot__slot_number')
    
    # Get fixed slots for this batch
    fixed_slots = FixedSlot.objects.filter(
        timetable=timetable,
        batch=batch
    ).select_related('day_slot', 'teacher')
    
    # Create fixed slot map
    fixed_slot_map = {}
    for fs in fixed_slots:
        fixed_slot_map[fs.day_slot_id] = {
            "teacher_code": fs.teacher.teacher_code if fs.teacher else None,
            "teacher_name": fs.teacher.get_full_name() if fs.teacher else None,
            "subject": fs.subject or "",
        }
    
    # Day mapping helper: supports both weekly (mon..sun) and date-based (d1..dN)
    def _day_key_for_slot(slot: DaySlot) -> str:
        if slot.day_index:
            return f"d{slot.day_index}"
        if slot.day == DaySlot.MONDAY:
            return "mon"
        if slot.day == DaySlot.TUESDAY:
            return "tue"
        if slot.day == DaySlot.WEDNESDAY:
            return "wed"
        if slot.day == DaySlot.THURSDAY:
            return "thu"
        if slot.day == DaySlot.FRIDAY:
            return "fri"
        if slot.day == DaySlot.SATURDAY:
            return "sat"
        if slot.day == DaySlot.SUNDAY:
            return "sun"
        return "unknown"
    
    slots_by_day = {}
    
    for entry in entries:
        day_key = _day_key_for_slot(entry.day_slot)
        if day_key == "unknown":
            continue
        slot_data = {
            "slot_id": str(entry.day_slot.id),
            "slot_code": entry.day_slot.slot_code,
            "slot_number": entry.day_slot.slot_number,
            "start_time": entry.day_slot.start_time.strftime("%H:%M"),
            "end_time": entry.day_slot.end_time.strftime("%H:%M"),
            "subject": entry.subject,
            "room_number": entry.room_number or "",
            "day_index": entry.day_slot.day_index,
            "actual_date": str(entry.day_slot.actual_date) if entry.day_slot.actual_date else None,
        }
        
        # Add teacher info from fixed slot if available
        if entry.day_slot_id in fixed_slot_map:
            slot_data["teacher"] = {
                "teacher_code": fixed_slot_map[entry.day_slot_id]["teacher_code"],
                "teacher_name": fixed_slot_map[entry.day_slot_id]["teacher_name"],
            }
            # Override subject if fixed slot has it
            if fixed_slot_map[entry.day_slot_id]["subject"]:
                slot_data["subject"] = fixed_slot_map[entry.day_slot_id]["subject"]
        else:
            slot_data["teacher"] = None
        
        slots_by_day.setdefault(day_key, []).append(slot_data)
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "batch_id": str(batch.id),
            "batch_code": batch.code,
            "batch_name": batch.name,
            "program": batch.program.name if batch.program else "",
            "slots": slots_by_day,
            "total_classes": len(entries),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_teacher_timetable(request, timetable_id: str, teacher_id: str = None):
    """
    Get timetable for a specific teacher or all teachers in a timetable.
    
    GET /api/timetable/timetables/<timetable_id>/teachers/ - Get all teachers timetables
    GET /api/timetable/timetables/<timetable_id>/teachers/<teacher_id>/ - Get specific teacher timetable
    
    Query params:
    - teacher_code: Filter by teacher code (alternative to teacher_id)
    
    Returns:
    {
        "timetable_id": "uuid",
        "teachers": [
            {
                "teacher_id": "uuid",
                "teacher_code": "TCH-XXXX-001",
                "teacher_name": "Teacher Name",
                "slots": {
                    "mon": [
                        {
                            "slot_id": "uuid",
                            "slot_code": "m1",
                            "slot_number": 1,
                            "start_time": "08:00",
                            "end_time": "09:30",
                            "batch_code": "BATCH-001",
                            "batch_name": "Super 30 - Batch A",
                            "subject": "Physics",
                            "room_number": "101"
                        }
                    ],
                    ...
                },
                "total_classes": 10,
                "batches": ["BATCH-001", "BATCH-002"]
            }
        ],
        "total_teachers": 5
    }
    """
    try:
        timetable = Timetable.objects.select_related('center').get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": f"Timetable with id '{timetable_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions
    user = request.user
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": f"You can only view timetables in your center '{user.center.name}'."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Get teacher(s)
    teacher_code = request.query_params.get('teacher_code')
    
    if teacher_id:
        try:
            teachers = [AccountUser.objects.get(id=teacher_id, role=AccountUser.ROLE_TEACHER)]
        except AccountUser.DoesNotExist:
            return Response(
                {"detail": f"Teacher with id '{teacher_id}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
    elif teacher_code:
        try:
            teachers = [AccountUser.objects.get(
                teacher_code__iexact=teacher_code,
                role=AccountUser.ROLE_TEACHER,
                center=timetable.center
            )]
        except AccountUser.DoesNotExist:
            return Response(
                {"detail": f"Teacher with code '{teacher_code}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
    else:
        # Get all teachers assigned to this timetable (exclude FREE teachers)
        teacher_ids = set()
        
        # From BatchFacultyLoad
        teacher_ids.update(
            BatchFacultyLoad.objects.filter(timetable=timetable).values_list('teacher_id', flat=True)
        )
        
        # From FixedSlot
        teacher_ids.update(
            FixedSlot.objects.filter(
                timetable=timetable,
                teacher__isnull=False
            ).values_list('teacher_id', flat=True)
        )
        
        # From TimetableEntry (optimization results)
        teacher_ids.update(
            TimetableEntry.objects.filter(
                day_slot__timetable=timetable,
                teacher__isnull=False
            ).values_list('teacher_id', flat=True)
        )
        
        teachers = AccountUser.objects.filter(
            id__in=teacher_ids,
            role=AccountUser.ROLE_TEACHER
        ).exclude(
            teacher_code__istartswith="FREE"  # Exclude FREE1, FREE2, etc.
        ).order_by('teacher_code', 'username')
    
    # Get all fixed slots for these teachers
    fixed_slots = FixedSlot.objects.filter(
        timetable=timetable,
        teacher__in=teachers
    ).select_related('day_slot', 'batch', 'teacher')
    
    # Get all timetable entries with teachers
    entries = TimetableEntry.objects.filter(
        day_slot__timetable=timetable
    ).select_related('day_slot', 'batch', 'teacher')
    
    # Day mapping helper: supports both weekly (mon..sun) and date-based (d1..dN)
    def _day_key_for_slot(slot: DaySlot) -> str:
        if slot.day_index:
            return f"d{slot.day_index}"
        if slot.day == DaySlot.MONDAY:
            return "mon"
        if slot.day == DaySlot.TUESDAY:
            return "tue"
        if slot.day == DaySlot.WEDNESDAY:
            return "wed"
        if slot.day == DaySlot.THURSDAY:
            return "thu"
        if slot.day == DaySlot.FRIDAY:
            return "fri"
        if slot.day == DaySlot.SATURDAY:
            return "sat"
        if slot.day == DaySlot.SUNDAY:
            return "sun"
        return "unknown"
    
    teachers_data = []
    for teacher in teachers:
        slots_by_day = {}
        batches_set = set()
        total_classes = 0
        
        # Get slots from TimetableEntry (from optimization)
        teacher_entries = [e for e in entries if e.teacher_id == teacher.id]
        for entry in teacher_entries:
            day_key = _day_key_for_slot(entry.day_slot)
            if day_key == "unknown":
                continue
            
            slot_data = {
                "slot_id": str(entry.day_slot.id),
                "slot_code": entry.day_slot.slot_code,
                "slot_number": entry.day_slot.slot_number,
                "start_time": entry.day_slot.start_time.strftime("%H:%M"),
                "end_time": entry.day_slot.end_time.strftime("%H:%M"),
                "batch_code": entry.batch.code,
                "batch_name": entry.batch.name,
                "subject": entry.subject,
                "room_number": entry.room_number or "",
                "day_index": entry.day_slot.day_index,
                "actual_date": str(entry.day_slot.actual_date) if entry.day_slot.actual_date else None,
                "is_fixed": False,
            }
            
            slots_by_day.setdefault(day_key, []).append(slot_data)
            batches_set.add(entry.batch.code)
            total_classes += 1
        
        # Get slots from FixedSlot (override/add fixed assignments)
        teacher_fixed_slots = [fs for fs in fixed_slots if fs.teacher_id == teacher.id]
        for fs in teacher_fixed_slots:
            day_key = _day_key_for_slot(fs.day_slot)
            if day_key == "unknown":
                continue
            
            # Check if this slot already exists from entries
            existing_slot = None
            if day_key in slots_by_day:
                for s in slots_by_day[day_key]:
                    if s["slot_id"] == str(fs.day_slot.id) and s["batch_code"] == fs.batch.code:
                        existing_slot = s
                        break
            
            if existing_slot:
                # Update existing slot to mark as fixed
                existing_slot["is_fixed"] = True
                existing_slot["subject"] = fs.subject or existing_slot["subject"]
            else:
                # Add new fixed slot
                slot_data = {
                    "slot_id": str(fs.day_slot.id),
                    "slot_code": fs.day_slot.slot_code,
                    "slot_number": fs.day_slot.slot_number,
                    "start_time": fs.day_slot.start_time.strftime("%H:%M"),
                    "end_time": fs.day_slot.end_time.strftime("%H:%M"),
                    "batch_code": fs.batch.code,
                    "batch_name": fs.batch.name,
                    "subject": fs.subject or "",
                    "room_number": "",
                    "day_index": fs.day_slot.day_index,
                    "actual_date": str(fs.day_slot.actual_date) if fs.day_slot.actual_date else None,
                    "is_fixed": True,
                }
                slots_by_day.setdefault(day_key, []).append(slot_data)
                batches_set.add(fs.batch.code)
                total_classes += 1
        
        # Also check BatchFacultyLoad to see which batches this teacher is assigned to
        faculty_loads = BatchFacultyLoad.objects.filter(
            timetable=timetable,
            teacher=teacher
        ).select_related('batch')
        
        for load in faculty_loads:
            batches_set.add(load.batch.code)
        
        teachers_data.append({
            "teacher_id": str(teacher.id),
            "teacher_code": teacher.teacher_code or teacher.username,
            "teacher_name": teacher.get_full_name(),
            "slots": slots_by_day,
            "total_classes": total_classes,
            "batches": sorted(list(batches_set)),
        })
    
    return Response(
        {
            "timetable_id": str(timetable.id),
            "timetable": str(timetable),
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "teachers": teachers_data,
            "total_teachers": len(teachers_data),
        },
        status=status.HTTP_200_OK,
    )



@api_view(["POST"])
@permission_classes([IsAuthenticated])
def activate_timetable(request, timetable_id: str):
    """
    Activate a timetable for its assigned batches.
    
    POST /api/timetable/admin/timetables/<timetable_id>/activate/
    
    Optional payload:
    {
        "deactivate_others": false  // Default: false. If true, deactivates other timetables sharing same batches
    }
    
    Returns:
    {
        "message": "Timetable activated successfully.",
        "timetable_id": "uuid",
        "timetable_name": "...",
        "is_active": true,
        "batches_affected": ["BATCH-001", "BATCH-002"],
        "deactivated_timetables": [
            {"id": "uuid", "name": "..."}
        ]
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can activate timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    deactivate_others = request.data.get("deactivate_others", False)
    
    # Get all batches assigned to this timetable
    batch_ids = set()
    
    # From TimetableBatch
    batch_ids.update(
        TimetableBatch.objects.filter(timetable=timetable).values_list('batch_id', flat=True)
    )
    
    # From BatchFacultyLoad
    batch_ids.update(
        BatchFacultyLoad.objects.filter(timetable=timetable).values_list('batch_id', flat=True)
    )
    
    # Get batch codes for response
    batches = Batch.objects.filter(id__in=batch_ids)
    batch_codes = [b.code for b in batches]
    
    deactivated_timetables = []
    
    with transaction.atomic():
        if deactivate_others and batch_ids:
            # Find all other timetables that have these batches assigned
            other_timetable_ids = set()
            
            # From TimetableBatch
            other_timetable_ids.update(
                TimetableBatch.objects.filter(
                    batch_id__in=batch_ids
                ).exclude(
                    timetable=timetable
                ).values_list('timetable_id', flat=True)
            )
            
            # From BatchFacultyLoad
            other_timetable_ids.update(
                BatchFacultyLoad.objects.filter(
                    batch_id__in=batch_ids
                ).exclude(
                    timetable=timetable
                ).values_list('timetable_id', flat=True)
            )
            
            # Deactivate other timetables
            if other_timetable_ids:
                other_timetables = Timetable.objects.filter(
                    id__in=other_timetable_ids,
                    is_active=True
                )
                
                for tt in other_timetables:
                    deactivated_timetables.append({
                        "id": str(tt.id),
                        "name": tt.name or str(tt),
                    })
                
                other_timetables.update(is_active=False)
        
        # Activate this timetable
        timetable.is_active = True
        timetable.save()
    
    # Send emails to all teachers if requested
    send_emails = request.data.get("send_emails", True)
    email_results = None
    
    if send_emails:
        from .email_service import send_timetable_emails_to_all_teachers
        email_results = send_timetable_emails_to_all_teachers(timetable)
    
    response_data = {
        "message": "Timetable activated successfully.",
        "timetable_id": str(timetable.id),
        "timetable_name": timetable.name or str(timetable),
        "center": timetable.center.name,
        "is_active": True,
        "batches_affected": batch_codes,
        "deactivated_timetables": deactivated_timetables,
        "deactivated_count": len(deactivated_timetables),
    }
    
    if email_results:
        response_data["email_notifications"] = {
            "total_teachers": email_results['total_teachers'],
            "emails_sent": email_results['emails_sent'],
            "emails_failed": email_results['emails_failed'],
            "no_email": email_results['no_email'],
        }
    
    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def deactivate_timetable(request, timetable_id: str):
    """
    Deactivate a timetable.
    
    POST /api/timetable/admin/timetables/<timetable_id>/deactivate/
    
    Returns:
    {
        "message": "Timetable deactivated successfully.",
        "timetable_id": "uuid",
        "timetable_name": "...",
        "is_active": false
    }
    """
    user = request.user
    
    # Check if user is Admin or Super Admin
    if user.role.lower() not in (AccountUser.ROLE_ADMIN.lower(), AccountUser.ROLE_SUPER_ADMIN.lower()):
        return Response(
            {"detail": "Only Admin and Super Admin can deactivate timetables."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetable
    try:
        timetable = Timetable.objects.select_related("center").get(id=timetable_id)
    except Timetable.DoesNotExist:
        return Response(
            {"detail": "Timetable not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions: Admin can only manage their center's timetables
    if user.role.lower() == AccountUser.ROLE_ADMIN.lower():
        if not user.center:
            return Response(
                {"detail": "Admin user is not linked to any center."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timetable.center != user.center:
            return Response(
                {"detail": "You can only manage timetables in your center."},
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Deactivate the timetable
    timetable.is_active = False
    timetable.save()
    
    return Response(
        {
            "message": "Timetable deactivated successfully.",
            "timetable_id": str(timetable.id),
            "timetable_name": timetable.name or str(timetable),
            "center": timetable.center.name,
            "is_active": False,
        },
        status=status.HTTP_200_OK,
    )
