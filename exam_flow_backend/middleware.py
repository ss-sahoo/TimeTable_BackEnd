"""
Custom middleware for handling PDF responses and CSRF
"""
import re
import uuid

from django.utils.deprecation import MiddlewareMixin


class DisableCSRFForAPI(MiddlewareMixin):
    """
    Middleware to disable CSRF for API endpoints
    """

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Skip CSRF for API endpoints
        if request.path.startswith('/api/'):
            setattr(request, '_dont_enforce_csrf_checks', True)
        return None


class PDFResponseMiddleware:
    """
    Middleware to add proper headers for PDF files to allow embedding
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Add headers for PDF files
        if request.path.startswith('/media/') and request.path.endswith('.pdf'):
            response['Content-Type'] = 'application/pdf'
            response['X-Content-Type-Options'] = 'nosniff'
            # Allow embedding in iframes from same origin
            response['X-Frame-Options'] = 'SAMEORIGIN'
            # For cross-origin, use CSP
            response['Content-Security-Policy'] = "frame-ancestors 'self' http://localhost:5173 http://127.0.0.1:5173"

        return response


# Accept only safe X-Request-Id values from clients (UUID or short alnum).
# Anything else gets replaced with a server-generated UUID4 to prevent log
# injection via crafted headers.
_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9._\-]{8,64}$')


class RequestIDMiddleware:
    """
    Assigns each request a stable id and echoes it back as the
    `X-Request-Id` response header. Clients may supply their own id via the
    same header; if missing or malformed, the server generates a UUID4.

    Views and the DRF exception handler can read `request.id` to include
    the correlation id in logs and error payloads.
    """
    HEADER = 'X-Request-Id'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get('HTTP_X_REQUEST_ID', '')
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request.id = request_id

        response = self.get_response(request)
        response[self.HEADER] = request_id
        return response


class APICacheControlMiddleware:
    """
    Force `Cache-Control: no-store` on private API responses so authenticated
    JSON cannot be cached by intermediaries or the browser disk cache. The
    OpenAPI schema and Swagger UI keep their normal cache headers so docs
    remain cacheable.
    """
    EXEMPT_PREFIXES = ('/api/schema', '/api/docs', '/api/redoc')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        if path.startswith('/api/') and not path.startswith(self.EXEMPT_PREFIXES):
            # Don't override if a view already set an explicit cache policy.
            if 'Cache-Control' not in response:
                response['Cache-Control'] = 'no-store'
        return response
