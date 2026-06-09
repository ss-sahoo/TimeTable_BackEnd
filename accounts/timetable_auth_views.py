"""
Timetable-specific authentication APIs using JWT tokens.
Role-aware login endpoints for timetable system.
"""

from typing import Optional, Tuple
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User
from .device_session_manager import DeviceSessionManager
from exam_flow_backend.throttling import LoginAnonRateThrottle, LoginUserRateThrottle


def _get_user_by_identifier(identifier: str) -> Optional[User]:
    """
    Allow login using username (which can be a code like ADM001, TCH001, STU001),
    email, OR teacher_code (for teachers).
    
    Uses prioritized lookup to avoid MultipleObjectsReturned when the
    identifier matches different fields on different users.
    
    Code-based login:
    - Admin: username like ADM001, ADM002, etc.
    - Teacher: username or teacher_code like TCH001, TCH002, etc.
    - Student: username like STU001, STU002, etc.
    """
    return (
        User.objects.filter(username__iexact=identifier).first()
        or User.objects.filter(email__iexact=identifier).first()
        or User.objects.filter(teacher_code__iexact=identifier).first()
    )


def _build_tokens_for_user(user: User) -> dict:
    """
    Create JWT refresh + access tokens for the authenticated user.
    """
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }


class BaseRoleLoginView(APIView):
    """
    Base class: implement role-specific login by setting `allowed_roles`.
    """

    permission_classes = [AllowAny]
    throttle_classes = [LoginAnonRateThrottle, LoginUserRateThrottle]
    allowed_roles: Tuple[str, ...] = ()

    def post(self, request, *args, **kwargs):
        identifier = request.data.get("username")  # username OR email
        password = request.data.get("password")
        force_switch = request.data.get("force_switch", False)  # Allow forcing device switch
        
        # Get device information from request
        device_info = {
            'user_agent': request.data.get('user_agent', request.META.get('HTTP_USER_AGENT', '')),
            'screen_resolution': request.data.get('screen_resolution', ''),
            'timezone': request.data.get('timezone', ''),
            'device_type': request.data.get('device_type', ''),
            'browser': request.data.get('browser', ''),
            'os': request.data.get('os', ''),
            'ip_address': request.META.get('REMOTE_ADDR', ''),
        }

        if not identifier or not password:
            return Response(
                {"detail": "Both 'username' and 'password' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get user from accounts.User model
        user = _get_user_by_identifier(identifier)
        if not user:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Check password directly
        if not user.check_password(password):
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        
        # Check if user is active
        if not user.is_active:
            return Response(
                {"detail": "User account is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check role compatibility (support both formats)
        if self.allowed_roles:
            # Map roles for compatibility
            role_mapping = {
                'super_admin': ['super_admin', 'SUPER_ADMIN', 'manager'],
                'SUPER_ADMIN': ['super_admin', 'SUPER_ADMIN', 'manager'],
                'ADMIN': ['ADMIN', 'admin', 'institute_admin', 'super_admin', 'SUPER_ADMIN'],
                'admin': ['ADMIN', 'admin', 'institute_admin', 'super_admin', 'SUPER_ADMIN'],
                'institute_admin': ['ADMIN', 'admin', 'institute_admin', 'super_admin', 'SUPER_ADMIN'],
                'TEACHER': ['TEACHER', 'teacher'],
                'teacher': ['TEACHER', 'teacher'],
                'STUDENT': ['STUDENT', 'student'],
                'student': ['STUDENT', 'student'],
                'STAFF': ['STAFF', 'staff'],
                'staff': ['STAFF', 'staff'],
                'manager': ['manager', 'super_admin', 'SUPER_ADMIN'],
            }
            
            user_allowed = False
            for allowed_role in self.allowed_roles:
                compatible_roles = role_mapping.get(allowed_role, [allowed_role])
                if user.role in compatible_roles:
                    user_allowed = True
                    break
            
            if not user_allowed:
                return Response(
                    {
                        "detail": "User does not have permission to use this login endpoint.",
                        "role": user.role,
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Check for device conflicts (unless force_switch is True)
        try:
            device_manager = DeviceSessionManager()
            
            if not force_switch:
                has_conflict, conflict_info = device_manager.check_session_conflict(user, device_info)
                
                if has_conflict:
                    # Return conflict information without creating tokens
                    return Response(
                        {
                            "has_conflict": True,
                            "conflict_info": conflict_info,
                            "message": "You are already logged in on another device.",
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

            # No conflict or force_switch is True, create session and tokens
            # If force_switch, invalidate all other sessions
            session = device_manager.create_session(user, device_info, force_logout_others=force_switch)
        except Exception as e:
            # Log the error but don't block login
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Device session error: {str(e)}", exc_info=True)
            # Continue with login without device session
            session = None
        
        tokens = _build_tokens_for_user(user)

        response_data = {
            "tokens": tokens,
            "user": {
                "id": str(user.id),
                "username": user.username,
                "email": user.email,
                "full_name": user.get_full_name(),
                "role": user.role,
                "center_id": user.center_id,
                "institute_id": user.institute_id,
                "institute_name": user.institute.name if user.institute else None,
                "db_name": user.institute.db_name if user.institute else 'default',
                "associated_institutes": [
                    {
                        "id": m.institute.id,
                        "name": m.institute.name,
                        "db_name": m.institute.db_name,
                        "role": m.role,
                        "teacher_code": m.teacher_code,
                        "center_id": str(m.center_id) if m.center_id else None,
                        "center_name": m.center.name if m.center else None,
                    } for m in user.memberships.filter(is_active=True).select_related('institute', 'center')
                ]
            },
        }
        
        # Add device session info if available
        if session:
            response_data["device_session"] = {
                "device_fingerprint": session.device_fingerprint,
                "device_type": session.device_type,
                "browser": session.browser,
                "os": session.os,
            }

        return Response(response_data, status=status.HTTP_200_OK)


class SuperAdminLoginView(BaseRoleLoginView):
    """
    Only users with role = SUPER_ADMIN or super_admin can login here.
    """
    allowed_roles = ('super_admin',)


class AdminLoginView(BaseRoleLoginView):
    """
    Admin login (center admins). We also allow SUPER_ADMIN here if desired.
    """
    allowed_roles = ('admin', 'institute_admin', 'super_admin')


class TeacherLoginView(BaseRoleLoginView):
    """
    Teacher login.
    """
    allowed_roles = ('teacher',)


class StudentLoginView(BaseRoleLoginView):
    """
    Student login.
    """
    allowed_roles = ('student',)


class StaffLoginView(BaseRoleLoginView):
    """
    Non-teaching staff login.
    """
    allowed_roles = ('staff',)


class ManagerLoginView(BaseRoleLoginView):
    """
    Manager login - Company level management.
    Has access to all features across all institutes.
    """
    allowed_roles = ('manager', 'super_admin')


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([LoginUserRateThrottle])
def change_password(request):
    """
    Allow Admin and Student to change their password.
    
    Payload:
    {
        "old_password": "current_password",
        "new_password": "new_secure_password"
    }
    
    Returns:
    {
        "message": "Password changed successfully."
    }
    """
    user = request.user
    
    # Only Admin and Student can change their password via this API
    if user.role not in ('admin', 'ADMIN', 'institute_admin', 'student', 'STUDENT'):
        return Response(
            {"detail": "Only Admin and Student can change password via this API."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    old_password = request.data.get("old_password")
    new_password = request.data.get("new_password")
    
    if not old_password or not new_password:
        return Response(
            {"detail": "old_password and new_password are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Verify old password
    if not user.check_password(old_password):
        return Response(
            {"detail": "Invalid old password."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate new password against Django's password validators (length,
    # commonness, user-attribute similarity, numeric-only). Returns a 400
    # with the same {"detail": ...} shape the frontend already handles.
    from django.contrib.auth.password_validation import validate_password as _validate_password
    from django.core.exceptions import ValidationError as _DjValidationError
    try:
        _validate_password(new_password, user)
    except _DjValidationError as exc:
        return Response(
            {"detail": " ".join(exc.messages)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Set new password
    user.set_password(new_password)
    user.save()
    
    return Response(
        {"message": "Password changed successfully."},
        status=status.HTTP_200_OK,
    )

