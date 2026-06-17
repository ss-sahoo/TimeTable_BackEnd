"""
Google OAuth2 authentication views.

Accepts a Google ID token from the frontend, verifies it using
google-auth, finds or creates the corresponding user, and returns
JWT tokens in the same format as the regular login endpoint.
"""

import logging

from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from django.db import transaction
from .models import User, Institute

logger = logging.getLogger(__name__)


def _get_google_client_id():
    """Return the configured Google Client ID."""
    # Try from config.py
    try:
        from config import GOOGLE_CLIENT_ID
        if GOOGLE_CLIENT_ID:
            return GOOGLE_CLIENT_ID
    except ImportError:
        pass

    # Try from django settings
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if client_id:
        return client_id

    # Try from environment variable directly
    import os
    env_id = os.getenv('GOOGLE_CLIENT_ID', '')
    if env_id:
        return env_id

    # Final fallback to the known client ID
    return '976886272254-0qajikvplhfa5hhl8vv24tgn4kbtf1a5.apps.googleusercontent.com'


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def google_login(request):
    """
    Authenticate a user via Google OAuth2.

    Expects JSON body:
        { "credential": "<Google ID token>", "force_login": <bool>, ...device_info }
    """
    credential = request.data.get('credential')
    if not credential:
        return Response(
            {'detail': 'Google credential token is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    client_id = _get_google_client_id()
    if not client_id:
        logger.error("GOOGLE_CLIENT_ID is not configured on the server.")
        return Response(
            {'detail': 'Google login is not configured on this server.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # --- Verify the token with Google ---
    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            client_id,
        )
    except ValueError as exc:
        logger.warning("Google token verification failed: %s", exc)
        return Response(
            {'detail': 'Invalid Google token. Please try again.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Extract user info from the verified token
    google_email = idinfo.get('email', '').lower().strip()
    google_first = idinfo.get('given_name', '')
    google_last = idinfo.get('family_name', '')
    google_name = idinfo.get('name', '')

    if not google_email:
        return Response(
            {'detail': 'Could not retrieve email from Google account.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # --- Find existing user ---
    user = User.objects.filter(email__iexact=google_email).first()

    if user is None:
        # Brand-new Google user: do NOT auto-create account yet.
        # Return a signal to the frontend to show the onboarding page.
        # The account will be created during onboarding (create/join institute).
        logger.info("New Google user detected (no account): %s — returning new_user signal", google_email)
        return Response(
            {
                'new_user': True,
                'onboarding_required': True,
                'google_profile': {
                    'email': google_email,
                    'first_name': google_first or (google_name.split()[0] if google_name else ''),
                    'last_name': google_last or (' '.join(google_name.split()[1:]) if google_name else ''),
                    'full_name': google_name,
                },
                'message': 'New user — please complete onboarding.',
            },
            status=status.HTTP_200_OK,
        )
    else:
        if not user.is_active:
            return Response(
                {'detail': 'Your account is disabled. Please contact support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

    # --- Device Session Management ---
    from .device_session_manager import DeviceSessionManager
    device_info = {
        'user_agent': request.data.get('user_agent', request.META.get('HTTP_USER_AGENT', '')),
        'screen_resolution': request.data.get('screen_resolution', ''),
        'timezone': request.data.get('timezone', ''),
        'device_type': request.data.get('device_type', ''),
        'browser': request.data.get('browser', ''),
        'os': request.data.get('os', ''),
        'ip_address': request.META.get('REMOTE_ADDR', ''),
    }

    session = None
    try:
        device_manager = DeviceSessionManager()
        has_conflict, conflict_info = device_manager.check_session_conflict(user, device_info)
        force_login = request.data.get('force_login', False)

        if has_conflict and not force_login:
            return Response(
                {
                    "has_conflict": True,
                    "conflict_info": conflict_info,
                    "message": "You are already logged in on another device.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        
        session = device_manager.create_session(user, device_info, force_logout_others=force_login)
        
        # Log activity
        from .utils import log_activity
        log_activity(
            institute=user.institute,
            log_type='login',
            title='Google Login',
            description=f'User {user.get_full_name()} logged in via Google from {device_info["browser"]} on {device_info["os"]}.',
            user=user,
            status='info',
            request=request
        ) if user.institute else None

    except Exception as e:
        logger.error(f"Device session error in Google login: {str(e)}", exc_info=True)
        # Continue without session if it fails

    # --- Generate JWT tokens ---
    refresh = RefreshToken.for_user(user)
    if session:
        refresh['device_fingerprint'] = session.device_fingerprint
        access_token = refresh.access_token
        access_token['device_fingerprint'] = session.device_fingerprint

    tokens = {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }

    # Build center info
    center_id = None
    center_name = None
    if user.center_id:
        center_id = str(user.center_id)
        center_name = user.center.name if user.center else None
    else:
        admin_center = user.admin_centers.first()
        if admin_center:
            center_id = str(admin_center.id)
            center_name = admin_center.name

    # Determine if onboarding is required for existing users (new users already returned above)
    # An existing user with no institute still needs to complete onboarding
    onboarding_required = user.institute is None

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
                }
                for m in user.memberships.filter(is_active=True).select_related('institute', 'center')
            ],
        },
        'onboarding_required': onboarding_required,
        'message': 'Google login successful',
    }

    if session:
        response_data['device_session'] = {
            'device_fingerprint': session.device_fingerprint,
            'device_type': session.device_type,
            'browser': session.browser,
            'os': session.os,
        }

    return Response(response_data)

@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@csrf_exempt
def google_onboarding_create_institute(request):
    """
    Onboarding step for brand-new Google users to create an institute.
    Creates BOTH the user and the institute.
    """
    credential = request.data.get('credential')
    institute_data = request.data.get('institute_data')

    if not credential or not institute_data:
        return Response(
            {'detail': 'Google credential and institute data are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # 1. Verify Google token
        client_id = _get_google_client_id()
        try:
            idinfo = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
        except Exception as exc:
            return Response({'detail': f'Invalid Google token: {str(exc)}'}, status=status.HTTP_401_UNAUTHORIZED)

        google_email = idinfo.get('email', '').lower().strip()
        google_first = idinfo.get('given_name', '')
        google_last = idinfo.get('family_name', '')
        google_name = idinfo.get('name', '')

        # 2. Check if user already exists (safety)
        user = User.objects.filter(email__iexact=google_email).first()
        if user and user.institute:
            return Response({'detail': 'User already belongs to an institute.'}, status=status.HTTP_400_BAD_REQUEST)

        # 3. Create Institute and User in a transaction
        with transaction.atomic():
            # Check domain uniqueness if provided
            domain = institute_data.get('domain')
            if not domain: # Convert empty string to None for unique constraint
                domain = None
                
            if domain and Institute.objects.filter(domain=domain).exists():
                 return Response({'detail': 'An institute with this domain already exists.'}, status=status.HTTP_400_BAD_REQUEST)
            
            name = institute_data.get('name', '').strip()
            if not name:
                 return Response({'detail': 'Institute name is required.'}, status=status.HTTP_400_BAD_REQUEST)
            
            if Institute.objects.filter(name__iexact=name).exists():
                 return Response({'detail': f'An institute named "{name}" already exists. Please choose a different name.'}, status=status.HTTP_400_BAD_REQUEST)
                 
            institute = Institute.objects.create(
                name=name,
                domain=domain,
                description=institute_data.get('description', ''),
                address=institute_data.get('address', ''),
                contact_email=institute_data.get('contact_email', google_email),
                contact_phone=institute_data.get('contact_phone', ''),
                website=institute_data.get('website', ''),
                is_verified=True
            )

            # Create or Update User
            if not user:
                username = google_email.split('@')[0]
                # Ensure unique username
                base_username = username
                counter = 1
                while User.objects.filter(username=username).exists():
                    username = f"{base_username}{counter}"
                    counter += 1
                    
                user = User.objects.create_user(
                    email=google_email,
                    username=username,
                    first_name=google_first or (google_name.split()[0] if google_name else ''),
                    last_name=google_last or (' '.join(google_name.split()[1:]) if google_name else ''),
                    role='super_admin',
                    institute=institute,
                    is_staff=True
                )
            else:
                # Update existing user who had no institute
                user.institute = institute
                user.role = 'super_admin'
                user.is_staff = True
                user.save()

            institute.created_by = user
            institute.save()

        # 4. Generate tokens and return success
        refresh = RefreshToken.for_user(user)
        
        # Optional: Device session
        from .device_session_manager import DeviceSessionManager
        device_info = {
            'user_agent': request.data.get('user_agent', request.META.get('HTTP_USER_AGENT', '')),
            'browser': request.data.get('browser', ''),
            'os': request.data.get('os', ''),
            'ip_address': request.META.get('REMOTE_ADDR', ''),
        }
        
        try:
            device_manager = DeviceSessionManager()
            session = device_manager.create_session(user, device_info, force_logout_others=True)
            refresh['device_fingerprint'] = session.device_fingerprint
        except Exception as e:
            logger.warning(f"Failed to create device session during onboarding: {str(e)}")

        response_data = {
            'tokens': {
                'refresh': str(refresh),
                'access': str(refresh.access_token),
            },
            'user': {
                'id': str(user.id),
                'username': user.username,
                'email': user.email,
                'full_name': user.get_full_name(),
                'role': user.role,
                'institute_id': user.institute_id,
                'institute_name': user.institute.name
            },
            'message': 'Institute and user created successfully.'
        }
        return Response(response_data, status=status.HTTP_201_CREATED)
    except Exception as e:
        logger.error(f"Onboarding error: {str(e)}", exc_info=True)
        # Check if it's a known database error to provide a better message
        error_msg = str(e)
        if "unique constraint" in error_msg.lower():
            if "name" in error_msg.lower():
                error_msg = "An institute with this name already exists."
            elif "domain" in error_msg.lower():
                error_msg = "This domain is already registered."
                
        return Response({'detail': error_msg}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
