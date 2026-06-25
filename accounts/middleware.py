"""
Tenant Middleware for Multi-Subdomain SaaS Architecture.

How it works:
  - If the request comes from `kiit.exams.dashoapp.com`, the middleware
    extracts `kiit` and attaches the matching Institute to request.institute.
  - If the request comes from the root domain `exams.dashoapp.com` or
    `localhost`, no institute is set (this is the Platform Owner portal).
  - All downstream views can then use `request.institute` to scope data.

Usage in views:
    institute = getattr(request, 'institute', None)
    if institute:
        exams = Exam.objects.filter(institute=institute)
"""

import logging

logger = logging.getLogger(__name__)

# The primary domain — requests from this domain are NOT tenant-specific
ROOT_DOMAIN = 'exams.dashoapp.com'
TIMETABLE_DOMAIN = 'timetable.dashoapp.com'
# Development hostnames to skip tenant detection
DEV_HOSTS = {'localhost', '127.0.0.1', 'testserver'}


class TenantMiddleware:
    """
    Middleware that extracts the subdomain from the request host and
    attaches the matching Institute to the request object.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.institute = None
        request.subdomain = None

        try:
            host = request.get_host().split(':')[0].lower()  # strip port if any

            # Skip for dev / root domain / timetable domain
            if host in DEV_HOSTS or host == ROOT_DOMAIN or host == TIMETABLE_DOMAIN:
                return self.get_response(request)

            # Check for subdomain pattern: something.exams.dashoapp.com
            if host.endswith('.' + ROOT_DOMAIN):
                subdomain = host.replace('.' + ROOT_DOMAIN, '').strip()
                if subdomain:
                    request.subdomain = subdomain
                    # Lazy import to avoid circular imports at startup
                    from accounts.models import Institute
                    try:
                        institute = Institute.objects.get(subdomain=subdomain, is_active=True)
                        request.institute = institute
                        logger.debug(f"[Tenant] Resolved '{subdomain}' → Institute: {institute.name}")
                    except Institute.DoesNotExist:
                        logger.warning(f"[Tenant] No active institute found for subdomain: '{subdomain}'")

        except Exception as e:
            logger.error(f"[Tenant] Middleware error: {e}")

        return self.get_response(request)
