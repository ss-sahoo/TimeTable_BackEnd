"""
GET APIs for Centers, Batches, Users, and Timetables
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q
from .models import Center, Batch, Program, User
from timetable.models import Timetable


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_people(request, center_id=None):
    """
    GET /api/timetable/centers/<center_id>/users/
    
    List all users in a center.
    Also used by the Exam app's People Management page.
    
    If center_id is provided as URL param, filter by that center.
    Otherwise, use query param center_id or the user's own center.
    
    Query params:
    - role: Filter by role
    - search: Search by name, email, username
    - is_active: Filter by active status
    - center_id: (query param) Filter by center
    - institute_id: (query param) Filter by institute
    """
    user = request.user
    
    # Determine center_id from URL param, query param, or user's center
    effective_center_id = center_id or request.query_params.get('center_id')
    institute_id = request.query_params.get('institute_id')
    
    # Build base queryset
    if user.role in ['super_admin', 'SUPER_ADMIN']:
        if effective_center_id:
            queryset = User.objects.filter(
                Q(center_id=effective_center_id) | Q(center__isnull=True, institute=user.institute)
            )
        elif institute_id:
            queryset = User.objects.filter(institute_id=institute_id)
        elif user.institute:
            queryset = User.objects.filter(institute=user.institute)
        else:
            queryset = User.objects.all()
    elif user.role in ['institute_admin', 'admin', 'exam_admin', 'ADMIN']:
        if effective_center_id:
            queryset = User.objects.filter(
                Q(center_id=effective_center_id) | Q(center__isnull=True, institute=user.institute)
            )
        elif user.institute:
            queryset = User.objects.filter(
                Q(institute=user.institute) | Q(center__institute=user.institute)
            )
        else:
            queryset = User.objects.none()
    else:
        if user.institute:
            queryset = User.objects.filter(institute=user.institute)
        else:
            queryset = User.objects.none()
    
    # Apply filters
    role_filter = request.query_params.get('role')
    if role_filter:
        role_variants = [role_filter, role_filter.lower(), role_filter.upper()]
        queryset = queryset.filter(role__in=role_variants)
    
    search = request.query_params.get('search')
    if search:
        queryset = queryset.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(email__icontains=search) |
            Q(username__icontains=search)
        )
    
    is_active = request.query_params.get('is_active')
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active.lower() == 'true')
    
    # Exclude super admins from the list by default
    queryset = queryset.exclude(role__in=['super_admin', 'SUPER_ADMIN'])
    
    # Order by name
    queryset = queryset.order_by('first_name', 'last_name')
    
    # Serialize
    users_data = []
    for u in queryset:
        users_data.append({
            'id': str(u.id),
            'username': u.username,
            'email': u.email,
            'first_name': u.first_name,
            'last_name': u.last_name,
            'full_name': u.get_full_name(),
            'role': u.role,
            'is_active': u.is_active,
            'phone': getattr(u, 'phone', '') or getattr(u, 'phone_number', ''),
            'teacher_code': getattr(u, 'teacher_code', ''),
            'teacher_subjects': getattr(u, 'teacher_subjects', '') or '',
            'teacher_employee_id': getattr(u, 'teacher_employee_id', '') or '',
            'institute_id': u.institute_id,
            'institute_name': u.institute.name if u.institute else None,
            'center_id': str(u.center_id) if u.center_id else None,
            'center_name': u.center.name if u.center else None,
            'created_at': u.created_at.isoformat() if hasattr(u, 'created_at') and u.created_at else None,
        })
    
    # Role counts
    role_counts = {
        'teacher': queryset.filter(role__in=['teacher', 'TEACHER']).count(),
        'student': queryset.filter(role__in=['student', 'STUDENT']).count(),
        'staff': queryset.filter(role__in=['staff', 'STAFF']).count(),
        'admin': queryset.filter(role__in=['admin', 'ADMIN', 'institute_admin']).count(),
    }
    
    return Response(
        {
            'users': users_data,
            'results': users_data,
            'count': len(users_data),
            'role_counts': role_counts,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_centers(request):
    """
    GET /api/timetable/centers/
    
    List all centers. 
    - Super Admin: sees all centers
    - Admin: sees only their center
    - Others: sees all centers (or can be restricted)
    
    Query params:
    - institute_id: Filter by institute
    - city: Filter by city
    - search: Search by name
    """
    user = request.user
    
    # Get the correct DB for this user's institute
    from accounts.utils import get_current_db
    current_db = get_current_db() or 'default'

    # Base queryset - use explicit DB and avoid cross-DB select_related
    centers = Center.objects.using(current_db).all()
    
    # Filter by role - Admins see only their center, Super Admins see all
    if user.role in ['admin', 'ADMIN', 'institute_admin'] and user.center:
        centers = centers.filter(id=user.center.id)
    
    # Query filters
    institute_id = request.query_params.get('institute_id')
    
    # If user has an institute, they should only see centers for that institute
    # unless they are a global super admin (no institute assigned)
    effective_institute_id = getattr(user, 'institute_id', None) or institute_id
    
    if effective_institute_id:
        centers = centers.filter(institute_id=effective_institute_id)
    
    city = request.query_params.get('city')
    if city:
        centers = centers.filter(city__icontains=city)
    
    search = request.query_params.get('search')
    if search:
        centers = centers.filter(name__icontains=search)
    
    # Serialize
    # Pre-fetch institutes from default DB to avoid cross-DB FK lookups
    institute_ids = list(centers.values_list('institute_id', flat=True).distinct())
    from accounts.models import Institute
    institutes_map = {i.id: i for i in Institute.objects.using('default').filter(id__in=institute_ids)}

    centers_data = []
    for center in centers:
        # Count admins for this center (exclude super admins)
        admin_count = User.objects.using('default').filter(
            center_id=center.id,
            role__in=['admin', 'ADMIN', 'institute_admin']
        ).exclude(
            role__in=['super_admin', 'SUPER_ADMIN']
        ).count()

        inst = institutes_map.get(center.institute_id)
        centers_data.append({
            "id": str(center.id),
            "name": center.name,
            "city": center.city,
            "address": center.address,
            "institute": {
                "id": inst.id if inst else center.institute_id,
                "name": inst.name if inst else "",
            },
            "admin_count": admin_count,
            "created_at": center.created_at.isoformat() if hasattr(center, 'created_at') else None,
        })
    
    return Response(
        {
            "count": len(centers_data),
            "results": centers_data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_center(request, center_id: str):
    """
    GET /api/timetable/centers/<center_id>/
    
    Get details of a specific center.
    """
    try:
        center = Center.objects.select_related('institute').get(id=center_id)
    except Center.DoesNotExist:
        return Response(
            {"detail": "Center not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions - Admins can only access their center, Super Admins can access all
    user = request.user
    if user.role in ['admin', 'ADMIN', 'institute_admin'] and user.center and user.center.id != center.id:
        return Response(
            {"detail": "You can only access your own center."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    return Response(
        {
            "id": str(center.id),
            "name": center.name,
            "city": center.city,
            "address": center.address,
            "institute": {
                "id": center.institute.id,
                "name": center.institute.name,
                "head_office_location": center.institute.head_office_location,
            },
            "created_at": center.created_at.isoformat() if hasattr(center, 'created_at') else None,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_center_programs(request, center_id: str):
    """
    GET /api/timetable/centers/<center_id>/programs/
    
    List all programs in a center.
    
    Query params:
    - search: Search by program name
    - is_active: Filter by active status (true/false)
    """
    try:
        center = Center.objects.get(id=center_id)
    except Center.DoesNotExist:
        return Response(
            {"detail": "Center not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions - Admins can only access their center, Super Admins can access all
    user = request.user
    if user.role in ['admin', 'ADMIN', 'institute_admin'] and user.center and user.center.id != center.id:
        return Response(
            {"detail": "You can only access your own center."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get programs in this center
    programs = Program.objects.filter(center=center).select_related('center')
    
    # Query filters
    search = request.query_params.get('search')
    if search:
        programs = programs.filter(name__icontains=search)
    
    is_active = request.query_params.get('is_active')
    if is_active is not None:
        is_active_bool = is_active.lower() == 'true'
        programs = programs.filter(is_active=is_active_bool)
    
    # Serialize
    programs_data = []
    for program in programs:
        programs_data.append({
            "id": str(program.id),
            "name": program.name,
            "description": program.description,
            "category": program.category,
            "is_active": program.is_active,
            "batches_count": program.batches.count(),
            "created_at": program.created_at.isoformat() if hasattr(program, 'created_at') else None,
        })
    
    return Response(
        {
            "center_id": str(center.id),
            "center_name": center.name,
            "count": len(programs_data),
            "results": programs_data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_center_batches(request, center_id: str):
    """
    GET /api/timetable/centers/<center_id>/batches/
    
    List all batches in a center.
    
    Query params:
    - program_id: Filter by program
    - search: Search by batch code or name
    """
    try:
        center = Center.objects.get(id=center_id)
    except Center.DoesNotExist:
        return Response(
            {"detail": "Center not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions - Admins can only access their center, Super Admins can access all
    user = request.user
    if user.role in ['admin', 'ADMIN', 'institute_admin'] and user.center and user.center.id != center.id:
        return Response(
            {"detail": "You can only access your own center."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Verify user belongs to same institute as the center
    if user.institute and center.institute and user.institute.id != center.institute.id:
        return Response(
            {"detail": "You do not have access to this center."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        # Get batches in this center - filter by center AND by center's institute programs
        batches = Batch.objects.filter(center=center).select_related('program')
        # Also ensure batches belong to programs of the same institute
        if center.institute:
            batches = batches.filter(
                Q(program__institute=center.institute) | Q(program__isnull=True)
            )
        
        # Query filters
        program_id = request.query_params.get('program_id')
        if program_id:
            batches = batches.filter(program_id=program_id)
        
        search = request.query_params.get('search')
        if search:
            batches = batches.filter(
                Q(code__icontains=search) | Q(name__icontains=search)
            )
        
        # Serialize
        batches_data = []
        for batch in batches:
            try:
                student_count = batch.enrollments.filter(status='ACTIVE').count() if hasattr(batch, 'enrollments') else 0
            except Exception:
                student_count = 0
            
            try:
                teacher_count = batch.teachers.count() if hasattr(batch, 'teachers') else 0
            except Exception:
                teacher_count = 0
            
            batch_data = {
                "id": str(batch.id),
                "code": batch.code,
                "name": batch.name,
                "start_date": str(batch.start_date) if batch.start_date else None,
                "end_date": str(batch.end_date) if batch.end_date else None,
                "program": None,
                "student_count": student_count,
                "teacher_count": teacher_count,
                "created_at": batch.created_at.isoformat() if hasattr(batch, 'created_at') else None,
            }
            
            if batch.program:
                batch_data["program"] = {
                    "id": str(batch.program.id),
                    "name": batch.program.name,
                }
            
            batches_data.append(batch_data)
        
        return Response(
            {
                "center_id": str(center.id),
                "center_name": center.name,
                "count": len(batches_data),
                "results": batches_data,
            },
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error listing center batches: {e}")
        return Response(
            {"detail": f"Error fetching batches: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_center_timetables(request, center_id: str):
    """
    GET /api/timetable/centers/<center_id>/timetables/
    
    List all timetables in a center.
    
    Query params:
    - is_active: Filter by active status (true/false)
    - from_date: Filter by from_date (YYYY-MM-DD)
    - to_date: Filter by to_date (YYYY-MM-DD)
    """
    try:
        center = Center.objects.get(id=center_id)
    except Center.DoesNotExist:
        return Response(
            {"detail": "Center not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check permissions - Admins can only access their center, Super Admins can access all
    user = request.user
    if user.role in ['admin', 'ADMIN', 'institute_admin'] and user.center and user.center.id != center.id:
        return Response(
            {"detail": "You can only access your own center."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get timetables in this center
    timetables = Timetable.objects.filter(center=center)
    
    # Query filters
    is_active = request.query_params.get('is_active')
    if is_active is not None:
        is_active_bool = is_active.lower() == 'true'
        timetables = timetables.filter(is_active=is_active_bool)
    
    from_date = request.query_params.get('from_date')
    if from_date:
        timetables = timetables.filter(from_date__gte=from_date)
    
    to_date = request.query_params.get('to_date')
    if to_date:
        timetables = timetables.filter(to_date__lte=to_date)
    
    # Order by date
    timetables = timetables.order_by('-from_date', '-to_date')
    
    # Serialize
    timetables_data = []
    for timetable in timetables:
        timetables_data.append({
            "id": str(timetable.id),
            "name": timetable.name,
            "description": timetable.description,
            "from_date": str(timetable.from_date),
            "to_date": str(timetable.to_date),
            "is_active": timetable.is_active,
            "created_at": timetable.created_at.isoformat() if hasattr(timetable, 'created_at') else None,
        })
    
    return Response(
        {
            "center_id": str(center.id),
            "center_name": center.name,
            "count": len(timetables_data),
            "results": timetables_data,
        },
        status=status.HTTP_200_OK,
    )
