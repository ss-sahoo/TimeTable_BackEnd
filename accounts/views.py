from rest_framework import status, generics, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import login, logout
from django.contrib.auth.hashers import make_password
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from .models import User, Institute, UserPermission, InstituteSettings, InstituteInvitation, ActivityLog
from .serializers import (
    UserRegistrationSerializer, UserSerializer, UserLoginSerializer,
    InstituteSerializer, InstituteCreateSerializer, UserPermissionSerializer, 
    InstituteSettingsSerializer, ChangePasswordSerializer, InstituteInvitationSerializer,
    ActivityLogSerializer, UserCreationSerializer
)
from .jwt_utils import get_tokens_for_user
from rest_framework.exceptions import PermissionDenied


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def user_registration_view(request):
    """User registration - no institute required"""
    serializer = UserRegistrationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        user = serializer.save()

        # Log activity
        from .utils import log_activity
        log_activity(
            institute=user.institute,
            log_type='user',
            title='New User Registered',
            description=f'User {user.get_full_name()} ({user.email}) registered as {user.role}.',
            user=user,
            status='success',
            request=request
        ) if user.institute else None

    # Send credentials email outside transaction so email failure doesn't rollback user creation
    try:
        plain_password = request.data.get('password')
        if plain_password and user.email and '@temp.com' not in user.email:
            from .utils import send_credentials_email
            send_credentials_email(user, plain_password)
    except Exception:
        pass  # Never fail registration due to email issues

    tokens = get_tokens_for_user(user)

    return Response({
        'user': UserSerializer(user).data,
        'access': tokens['access'],
        'refresh': tokens['refresh'],
        'message': 'User registered successfully'
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def user_login_view(request):
    """
    Generic login endpoint - supports username, email, or teacher_code.
    Same authentication style as timetable role-based logins.
    Returns JWT tokens in the same format.
    Includes device session management.
    """
    from django.db.models import Q
    from rest_framework_simplejwt.tokens import RefreshToken
    from .device_session_manager import DeviceSessionManager
    
    identifier = request.data.get('email') or request.data.get('username')
    password = request.data.get('password')
    
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
        return Response({
            'detail': 'Both username/email and password are required.'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Get user by username, email, or teacher_code (same as timetable)
    # Use prioritized lookup to avoid MultipleObjectsReturned when the
    # identifier matches different fields on different users.
    user = (
        User.objects.filter(username__iexact=identifier).first()
        or User.objects.filter(email__iexact=identifier).first()
        or User.objects.filter(teacher_code__iexact=identifier).first()
    )
    if user is None:
        return Response({
            'detail': 'Invalid credentials.'
        }, status=status.HTTP_401_UNAUTHORIZED)
    
    # Check password
    if not user.check_password(password):
        return Response({
            'detail': 'Invalid credentials.'
        }, status=status.HTTP_401_UNAUTHORIZED)
    
    # Check if user is active
    if not user.is_active:
        return Response({
            'detail': 'User account is disabled.'
        }, status=status.HTTP_403_FORBIDDEN)
    
        # Check for device conflicts
    try:
        device_manager = DeviceSessionManager()
        has_conflict, conflict_info = device_manager.check_session_conflict(user, device_info)
        
        # Check if force login is requested
        force_login = request.data.get('force_login', False)
        
        if has_conflict and not force_login:
            # Return conflict information without creating tokens
            return Response(
                {
                    "has_conflict": True,
                    "conflict_info": conflict_info,
                    "message": "You are already logged in on another device.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        
        # No conflict OR force login, create session
        # If force_login is True, it will invalidate other sessions
        session = device_manager.create_session(user, device_info, force_logout_others=force_login)
        
        # Log activity
        from .utils import log_activity
        log_activity(
            institute=user.institute,
            log_type='login',
            title='User Login',
            description=f'User {user.get_full_name()} logged in from {device_info["browser"]} on {device_info["os"]}.',
            user=user,
            status='info',
            request=request
        ) if user.institute else None

    except Exception as e:
        # Log the error but don't block login
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Device session error: {str(e)}", exc_info=True)
        # Continue with login without device session
        session = None
    
    # Generate JWT tokens (same format as timetable)
    refresh = RefreshToken.for_user(user)
    
    # Add device fingerprint to the token payload for validation
    if session:
        refresh['device_fingerprint'] = session.device_fingerprint
        # Regenerate access token with the updated payload
        access_token = refresh.access_token
        access_token['device_fingerprint'] = session.device_fingerprint
    
    tokens = {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }
    
    # Get center_id - either from direct assignment or from admin_centers
    center_id = None
    center_name = None
    if user.center_id:
        center_id = str(user.center_id)
        center_name = user.center.name if user.center else None
    else:
        # Check if user is admin of any center
        admin_center = user.admin_centers.first()
        if admin_center:
            center_id = str(admin_center.id)
            center_name = admin_center.name
    
    response_data = {
        'tokens': tokens,
        'user': {
            'id': str(user.id),
            'username': user.username,
            'email': user.email,
            'full_name': user.get_full_name(),
            'first_name': user.first_name,
            'last_name': user.last_name,
            'role': user.role,
            'institute_id': user.institute_id,
            'institute_name': user.institute.name if user.institute else None,
            'db_name': user.institute.db_name if user.institute else 'default',
            'center_id': center_id,
            'center_name': center_name,
            'associated_institutes': [
                {
                    'id': m.institute.id,
                    'name': m.institute.name,
                    'db_name': m.institute.db_name,
                    'role': m.role,
                    'teacher_code': m.teacher_code,
                    'center_id': str(m.center_id) if m.center_id else None,
                    'center_name': m.center.name if m.center else None,
                } for m in user.memberships.filter(is_active=True).select_related('institute', 'center')
            ]
        },
        'message': 'Login successful'
    }
    
    # Add device session info if available
    if session:
        response_data['device_session'] = {
            'device_fingerprint': session.device_fingerprint,
            'device_type': session.device_type,
            'browser': session.browser,
            'os': session.os,
        }
    
    return Response(response_data)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])  # Allow any user to logout
@csrf_exempt
def user_logout_view(request):
    """User logout - properly clear all session data, CSRF tokens, and device sessions"""
    from .device_session_manager import DeviceSessionManager
    from rest_framework_simplejwt.authentication import JWTAuthentication
    import logging
    
    logger = logging.getLogger(__name__)
    
    try:
        # Get device fingerprint before logging out
        device_fingerprint = None
        
        # Try to get from header first
        device_fingerprint = request.headers.get('X-Device-Fingerprint')
        
        # If not in header, try to get from JWT token
        if not device_fingerprint and request.user.is_authenticated:
            try:
                jwt_auth = JWTAuthentication()
                auth_header = request.META.get('HTTP_AUTHORIZATION', '')
                if auth_header.startswith('Bearer '):
                    token = auth_header.split(' ')[1]
                    validated_token = jwt_auth.get_validated_token(token)
                    device_fingerprint = validated_token.get('device_fingerprint')
            except Exception as e:
                logger.warning(f"Could not extract device fingerprint from token: {str(e)}")
        
        # Invalidate device session if we have the fingerprint
        if device_fingerprint:
            success = DeviceSessionManager.invalidate_session(device_fingerprint)
            if success:
                logger.info(f"Invalidated device session on logout: {device_fingerprint[:8]}...")
            else:
                logger.warning(f"Could not invalidate device session: {device_fingerprint[:8]}...")
        elif request.user.is_authenticated:
            # If we don't have fingerprint, invalidate ALL active sessions for this user
            # This is a fallback to ensure logout works even without fingerprint
            from .models import DeviceSession
            invalidated_count = DeviceSession.objects.filter(
                user=request.user,
                is_active=True
            ).update(is_active=False)
            logger.info(f"Invalidated {invalidated_count} device session(s) for user {request.user.email} on logout")
        
        # Logout the user if authenticated
        if request.user.is_authenticated:
            logout(request)
        
        # Clear all session data
        request.session.flush()
        
        # Clear CSRF token from session
        if 'csrf_token' in request.session:
            del request.session['csrf_token']
        
        # Create response
        response = Response({'message': 'Logout successful'})
        
        # Clear all cookies with different paths and domains
        response.delete_cookie('sessionid', path='/')
        response.delete_cookie('csrftoken', path='/')
        response.delete_cookie('csrftoken', path='/', domain=None)
        response.delete_cookie('csrftoken', path='/', domain='localhost')
        response.delete_cookie('csrftoken', path='/', domain='127.0.0.1')
        
        # Set cookies to expire immediately
        response.set_cookie('sessionid', '', max_age=0, path='/')
        response.set_cookie('csrftoken', '', max_age=0, path='/')
        
        return response
    except Exception as e:
        # Even if there's an error, try to clear everything
        logger.error(f"Error during logout: {str(e)}", exc_info=True)
        try:
            request.session.flush()
        except:
            pass
        
        response = Response({'message': 'Logout successful'})
        response.delete_cookie('sessionid', path='/')
        response.delete_cookie('csrftoken', path='/')
        response.set_cookie('sessionid', '', max_age=0, path='/')
        response.set_cookie('csrftoken', '', max_age=0, path='/')
        return response


class UserProfileView(generics.RetrieveUpdateAPIView):
    """Get or update authenticated user's profile"""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class ChangePasswordView(generics.GenericAPIView):
    """Change user password"""
    serializer_class = ChangePasswordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        
        # Generate new JWT tokens
        refresh = RefreshToken.for_user(user)
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'message': 'Password changed successfully'
        })


class InstituteListCreateView(generics.ListCreateAPIView):
    """List all active institutes and create new institutes"""
    queryset = Institute.objects.filter(is_active=True)
    permission_classes = [permissions.AllowAny]
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return InstituteCreateSerializer
        return InstituteSerializer
    
    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]


class InstituteDetailView(generics.RetrieveAPIView):
    """Get institute details"""
    queryset = Institute.objects.all()
    serializer_class = InstituteSerializer
    permission_classes = [permissions.AllowAny]


class UserListView(generics.ListCreateAPIView):
    """List users within the same institute or create a new user"""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return UserCreationSerializer
        return UserSerializer

    def get_queryset(self):
        user = self.request.user
        # Priority: User's assigned institute (strict) > Query parameter (filter)
        eff_institute_id = getattr(user, 'institute_id', None) or self.request.query_params.get('institute_id')
        
        center_id = self.request.query_params.get('center_id')
        
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            queryset = User.objects.all()
            if eff_institute_id:
                queryset = queryset.filter(institute_id=eff_institute_id)
            if center_id:
                queryset = queryset.filter(center_id=center_id)
            return queryset
        
        # Only Institute Admins (and Exam Admins) see all institute users
        if user.role in ['institute_admin', 'exam_admin']:
            return User.objects.filter(institute=user.institute)
            
        # For Center Admin ('admin'), Teacher, Student, Staff - return users in their center
        # BUT also include Super_Admins from the same institute (so they are visible)
        if user.center:
            return User.objects.filter(
                Q(center=user.center) | 
                Q(role__in=['super_admin', 'SUPER_ADMIN'], institute=user.institute)
            ).distinct()
            
        # Fallback
        return User.objects.filter(id=user.id)

    def perform_create(self, serializer):
        user = self.request.user
        
        # Check permissions
        if user.role not in ['super_admin', 'SUPER_ADMIN', 'institute_admin', 'exam_admin', 'admin', 'ADMIN']:
             raise PermissionDenied("You do not have permission to create users.")

        # For institute admins, force the institute_id
        if user.role in ['institute_admin', 'exam_admin'] and user.institute:
             serializer.save(institute_id=user.institute.id)
        else:
             serializer.save()


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Get, update, or delete a user"""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Priority: User's assigned institute (strict) > Query parameter (filter)
        eff_institute_id = getattr(user, 'institute_id', None) or self.request.query_params.get('institute_id')
        
        if user.role in ['super_admin']:
            if eff_institute_id:
                return User.objects.filter(institute_id=eff_institute_id)
            return User.objects.all()
        
        if user.is_institute_admin():
            return User.objects.filter(institute=user.institute)
        return User.objects.filter(id=user.id)

    def perform_update(self, serializer):
        actor = self.request.user
        instance = serializer.instance

        # Allow super admins to update anyone
        if actor.role in ['super_admin']:
            serializer.save()
            return

        # Allow institute admins to update users in their institute or users updating themselves
        if actor.is_institute_admin() and instance.institute == actor.institute:
            serializer.save()
            return

        if actor.id == instance.id:
            serializer.save()
            return

        raise PermissionDenied("You do not have permission to update this user.")

    def perform_destroy(self, instance):
        actor = self.request.user

        # Allow super admins to delete anyone
        if actor.role in ['super_admin']:
            instance.delete()
            return

        if not actor.is_institute_admin():
            raise PermissionDenied("You do not have permission to delete users.")

        if instance.id == actor.id:
            raise PermissionDenied("You cannot delete your own account from this screen.")

        if instance.institute_id != actor.institute_id:
            raise PermissionDenied("You can only delete users from your institute.")

        # Prevent deleting the last institute admin
        if instance.role == 'institute_admin':
            admin_count = User.objects.filter(institute=instance.institute, role='institute_admin').exclude(id=instance.id).count()
            if admin_count == 0:
                raise PermissionDenied("You cannot remove the only institute admin.")

        instance.delete()


class UserPermissionListView(generics.ListCreateAPIView):
    """List and create user permissions"""
    serializer_class = UserPermissionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_institute_admin():
            return UserPermission.objects.filter(user__institute=user.institute)
        return UserPermission.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(granted_by=self.request.user)


class InstituteSettingsView(generics.RetrieveUpdateAPIView):
    """Get and update institute settings"""
    serializer_class = InstituteSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        user = self.request.user
        if not user.is_institute_admin():
            return None
        
        settings, created = InstituteSettings.objects.get_or_create(institute=user.institute)
        return settings


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def user_dashboard_view(request):
    """Get user dashboard data"""
    user = request.user
    
    dashboard_data = {
        'user': UserSerializer(user).data,
        'institute': InstituteSerializer(user.institute).data,
        'permissions': {
            'can_manage_exams': user.can_manage_exams(),
            'can_create_exams': user.can_create_exams(),
            'is_institute_admin': user.is_institute_admin(),
        }
    }
    
    # Add role-specific data
    if user.role in ['student', 'STUDENT']:
        dashboard_data['upcoming_exams'] = []
        dashboard_data['exam_history'] = []
    elif user.can_manage_exams():
        dashboard_data['created_exams'] = []
        dashboard_data['exam_analytics'] = []
    
    return Response(dashboard_data)


class InstituteUpdateView(generics.UpdateAPIView):
    """Update institute details"""
    serializer_class = InstituteSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            return Institute.objects.all()
        return Institute.objects.filter(users=user, users__role__in=['institute_admin', 'super_admin'])


class InstituteUserListView(generics.ListAPIView):
    """List users within an institute"""
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        institute_id = self.kwargs.get('institute_id')
        
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            return User.objects.filter(institute_id=institute_id)
        elif user.role in ['institute_admin', 'exam_admin'] and user.institute_id == institute_id:
            return User.objects.filter(institute_id=institute_id)
        else:
            return User.objects.none()


class InstituteInvitationListView(generics.ListCreateAPIView):
    """List and create institute invitations"""
    serializer_class = InstituteInvitationSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            return InstituteInvitation.objects.all()
        elif user.role in ['institute_admin', 'exam_admin']:
            return InstituteInvitation.objects.filter(institute=user.institute)
        else:
            return InstituteInvitation.objects.none()
    
    def perform_create(self, serializer):
        user = self.request.user
        if user.role not in ['super_admin', 'institute_admin', 'exam_admin']:
            raise permissions.PermissionDenied("You don't have permission to send invitations.")
        serializer.save()


class InstituteInvitationDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Manage individual institute invitations"""
    serializer_class = InstituteInvitationSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            return InstituteInvitation.objects.all()
        elif user.role in ['institute_admin', 'exam_admin']:
            return InstituteInvitation.objects.filter(institute=user.institute)
        else:
            return InstituteInvitation.objects.none()


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def accept_invitation(request, invitation_id):
    """Accept an institute invitation"""
    try:
        invitation = InstituteInvitation.objects.get(id=invitation_id, email=request.user.email)
    except InstituteInvitation.DoesNotExist:
        return Response({'error': 'Invitation not found'}, status=status.HTTP_404_NOT_FOUND)
    
    if invitation.is_expired():
        invitation.status = 'expired'
        invitation.save()
        return Response({'error': 'Invitation has expired'}, status=status.HTTP_400_BAD_REQUEST)
    
    if invitation.status != 'pending':
        return Response({'error': 'Invitation is no longer valid'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Update user's institute and role
    user = request.user
    user.institute = invitation.institute
    user.role = invitation.role
    user.save()
    
    # Update invitation status
    invitation.status = 'accepted'
    invitation.save()
    
    return Response({'message': 'Invitation accepted successfully'})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def decline_invitation(request, invitation_id):
    """Decline an institute invitation"""
    try:
        invitation = InstituteInvitation.objects.get(id=invitation_id, email=request.user.email)
    except InstituteInvitation.DoesNotExist:
        return Response({'error': 'Invitation not found'}, status=status.HTTP_404_NOT_FOUND)
    
    if invitation.status != 'pending':
        return Response({'error': 'Invitation is no longer valid'}, status=status.HTTP_400_BAD_REQUEST)
    
    invitation.status = 'declined'
    invitation.save()
    
    return Response({'message': 'Invitation declined'})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def my_invitations(request):
    """Get invitations for the current user"""
    invitations = InstituteInvitation.objects.filter(
        email=request.user.email,
        status='pending'
    ).exclude(expires_at__lt=timezone.now())
    
    serializer = InstituteInvitationSerializer(invitations, many=True)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def leave_institute(request):
    """Leave current institute"""
    user = request.user
    
    if user.role in ['super_admin', 'SUPER_ADMIN']:
        return Response({'error': 'Super admins cannot leave institutes'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not user.institute:
        return Response({'error': 'You are not part of any institute'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Check if user is the only admin
    if user.role == 'institute_admin':
        admin_count = user.institute.get_admins().count()
        if admin_count <= 1:
            return Response({'error': 'You cannot leave as you are the only admin'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Remove user from institute
    user.institute = None
    user.role = 'student'  # Reset to default role
    user.save()
    
    return Response({'message': 'Successfully left the institute'})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def institute_search(request):
    """Search for institutes by name or domain"""
    query = request.GET.get('q', '')
    if not query:
        return Response({'error': 'Search query is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    institutes = Institute.objects.filter(
        models.Q(name__icontains=query) | models.Q(domain__icontains=query),
        is_active=True
    )[:10]  # Limit to 10 results
    
    serializer = InstituteSerializer(institutes, many=True)
    return Response(serializer.data)


class ActivityLogListView(generics.ListAPIView):
    """
    List activity logs for an institute.
    """
    serializer_class = ActivityLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        
        # Only admins can view logs
        if user.role not in ['super_admin', 'institute_admin', 'exam_admin']:
            raise PermissionDenied("You do not have permission to view activity logs.")
            
        queryset = ActivityLog.objects.filter(institute=user.institute)
        
        # Filter by log type
        log_type = self.request.query_params.get('log_type')
        if log_type and log_type != 'all':
            queryset = queryset.filter(log_type=log_type)
            
        # Filter by search term
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                models.Q(title__icontains=search) |
                models.Q(description__icontains=search) |
                models.Q(user__first_name__icontains=search) |
                models.Q(user__last_name__icontains=search) |
                models.Q(user__email__icontains=search)
            )
            
        return queryset


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def admin_reset_password(request, pk):
    """
    Reset a user's password (for admins).
    """
    actor = request.user
    
    # Check permissions
    if not actor.is_institute_admin() and actor.role not in ['super_admin', 'SUPER_ADMIN']:
        return Response({
            'error': 'You do not have permission to reset passwords.'
        }, status=status.HTTP_403_FORBIDDEN)
    
    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return Response({
            'error': 'User not found.'
        }, status=status.HTTP_404_NOT_FOUND)
    
    # Check if advisor (admin) is in the same institute
    if actor.role not in ['super_admin', 'SUPER_ADMIN'] and user.institute != actor.institute:
        return Response({
            'error': 'You can only reset passwords for users in your institute.'
        }, status=status.HTTP_403_FORBIDDEN)

    password = request.data.get('password')
    confirm_password = request.data.get('confirm_password')

    if not password or not confirm_password:
        return Response({
            'error': 'Both password and confirm_password are required.'
        }, status=status.HTTP_400_BAD_REQUEST)

    if password != confirm_password:
        return Response({
            'error': 'Passwords do not match.'
        }, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(password)
    user.save()

    # Log activity
    from .utils import log_activity
    log_activity(
        institute=user.institute,
        log_type='user',
        title='User Password Reset',
        description=f'Password for {user.get_full_name()} ({user.email}) was reset by {actor.get_full_name()}.',
        user=user,
        status='success',
        request=request
    ) if user.institute else None

    return Response({
        'message': 'Password reset successfully.'
    }, status=status.HTTP_200_OK)
