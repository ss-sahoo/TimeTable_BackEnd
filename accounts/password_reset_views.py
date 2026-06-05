from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from exam_flow_backend.throttling import (
    PasswordResetAnonRateThrottle,
    PasswordResetUserRateThrottle,
)
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
import logging

User = get_user_model()
logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetAnonRateThrottle, PasswordResetUserRateThrottle])
def forgot_password(request):
    """
    Request a password reset email.

    Request body:
        {"email": "user@example.com"}

    Responses:
        200: {"message": "..."} — always returned for valid input, even if the
             email is not registered (does not reveal account existence).
        400: {"error": "Email is required"}
        500: {"error": "Failed to send password reset email..."} — mail backend
             failure.

    Rate limited per `password_reset` scope.
    """
    email = request.data.get('email')
    
    if not email:
        return Response({
            'error': 'Email is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        user = User.objects.get(email=email, is_active=True)
    except User.DoesNotExist:
        # Don't reveal if email exists or not for security
        return Response({
            'message': 'If an account with this email exists, you will receive a password reset link.'
        }, status=status.HTTP_200_OK)
    
    # Generate reset token
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    
    # Create reset link
    reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"
    
    try:
        # Prepare email context
        context = {
            'user': user,
            'reset_link': reset_link,
            'site_name': user.institute.name if user.institute else "Exam Flow System",
            'frontend_url': settings.FRONTEND_URL,
        }
        
        # Send email
        subject = "Password Reset Request - Exam Flow System"
        text_content = render_to_string('emails/password_reset.txt', context)
        html_content = render_to_string('emails/password_reset.html', context)
        
        msg = EmailMultiAlternatives(subject, text_content, settings.DEFAULT_FROM_EMAIL, [user.email])
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        
        logger.info(f"Password reset email sent to {user.email}")
        
        return Response({
            'message': 'If an account with this email exists, you will receive a password reset link.'
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Failed to send password reset email to {user.email}: {str(e)}")
        return Response({
            'error': 'Failed to send password reset email. Please try again later.'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetAnonRateThrottle, PasswordResetUserRateThrottle])
def reset_password(request):
    """
    Confirm password reset with the token from the email.

    Request body:
        {
            "uid": "<base64-encoded user id from the link>",
            "token": "<token from the link>",
            "new_password": "...",
            "confirm_password": "..."
        }

    Responses:
        200: {"message": "Password has been reset successfully..."}
        400: {"error": "..."} — missing fields, mismatched passwords,
             invalid/expired link, or failing Django password validators
             (in which case `error` is a list of validator messages).
    """
    uid = request.data.get('uid')
    token = request.data.get('token')
    new_password = request.data.get('new_password')
    confirm_password = request.data.get('confirm_password')
    
    if not all([uid, token, new_password, confirm_password]):
        return Response({
            'error': 'All fields are required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    if new_password != confirm_password:
        return Response({
            'error': 'Passwords do not match'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Decode user ID
        user_id = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=user_id, is_active=True)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return Response({
            'error': 'Invalid reset link'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Verify token
    if not default_token_generator.check_token(user, token):
        return Response({
            'error': 'Invalid or expired reset link'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate password
    try:
        validate_password(new_password, user)
    except ValidationError as e:
        return Response({
            'error': list(e.messages)
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Set new password
    user.set_password(new_password)
    user.save()
    
    logger.info(f"Password reset successful for user {user.email}")
    
    return Response({
        'message': 'Password has been reset successfully. You can now login with your new password.'
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetAnonRateThrottle, PasswordResetUserRateThrottle])
def validate_reset_token(request):
    """
    Check whether a password reset link is still valid (used by the frontend
    to gate the reset form before the user submits a new password).

    Request body:
        {"uid": "...", "token": "..."}

    Responses:
        200: {"valid": true, "user_email": "..."}
        400: {"valid": false, "error": "Invalid or expired reset link"}
    """
    uid = request.data.get('uid')
    token = request.data.get('token')
    
    if not uid or not token:
        return Response({
            'error': 'UID and token are required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Decode user ID
        user_id = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=user_id, is_active=True)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return Response({
            'valid': False,
            'error': 'Invalid reset link'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Verify token
    if not default_token_generator.check_token(user, token):
        return Response({
            'valid': False,
            'error': 'Invalid or expired reset link'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    return Response({
        'valid': True,
        'user_email': user.email
    }, status=status.HTTP_200_OK)