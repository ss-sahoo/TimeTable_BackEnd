from django.utils.deprecation import MiddlewareMixin
from accounts.utils import set_current_db, clear_current_db
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

# The root domain — requests from here are the Platform Owner portal (no tenant)
ROOT_DOMAIN = getattr(settings, 'ROOT_DOMAIN', 'exams.dashoapp.com')
TIMETABLE_DOMAIN = getattr(settings, 'TIMETABLE_DOMAIN', 'timetable.dashoapp.com')
DEV_HOSTS = {'localhost', '127.0.0.1', 'testserver'}


class TenantMiddleware(MiddlewareMixin):
    """
    Middleware that:
    1. Extracts the subdomain from the request host and attaches the matching
       Institute to request.institute / request.subdomain.
       e.g. kiit.exams.dashoapp.com -> Institute(subdomain='kiit')
    2. Sets the correct tenant database connection for the request thread
       (existing DB-routing behaviour is fully preserved).
    """

    def process_request(self, request):
        # Phase 1: Subdomain -> Institute resolution
        request.institute = None
        request.subdomain = None

        try:
            host = request.get_host().split(':')[0].lower()

            if host not in DEV_HOSTS and host != ROOT_DOMAIN and host != TIMETABLE_DOMAIN:
                if host.endswith('.' + ROOT_DOMAIN):
                    subdomain = host.replace('.' + ROOT_DOMAIN, '').strip().lower()
                    if subdomain:
                        request.subdomain = subdomain
                        from .models import Institute
                        try:
                            institute = Institute.objects.using('default').get(
                                subdomain=subdomain, is_active=True
                            )
                            request.institute = institute
                            logger.debug(
                                f"[Tenant] Subdomain '{subdomain}' -> Institute: {institute.name}"
                            )
                        except Institute.DoesNotExist:
                            logger.warning(
                                f"[Tenant] No active institute for subdomain: '{subdomain}'"
                            )
        except Exception as e:
            logger.error(f"[Tenant] Subdomain detection error: {e}")

        # Phase 2: DB routing (existing logic — unchanged)
        requested_db = request.headers.get('X-Institute-DB')
        tenant_db = None

        # Identify the user (session or JWT)
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated):
            try:
                jwt_auth = JWTAuthentication()
                header = jwt_auth.get_header(request)
                if header:
                    raw_token = jwt_auth.get_raw_token(header)
                    validated_token = jwt_auth.get_validated_token(raw_token)
                    user = jwt_auth.get_user(validated_token)
            except Exception:
                user = None

        # Determine the correct tenant DB
        if user and user.is_authenticated:
            if requested_db:
                from .models import Institute
                try:
                    target_institute = Institute.objects.using('default').get(db_name=requested_db)
                    if user.role == 'super_admin':
                        tenant_db = requested_db
                    elif user.institute_id == target_institute.id:
                        tenant_db = requested_db
                    elif user.memberships.filter(institute=target_institute, is_active=True).exists():
                        tenant_db = requested_db
                    else:
                        logger.warning(
                            f"User {user.email} attempted to access unauthorized database: {requested_db}"
                        )
                        tenant_db = user.institute.db_name if user.institute else None
                except Institute.DoesNotExist:
                    logger.error(f"Requested database {requested_db} does not exist.")
                    tenant_db = user.institute.db_name if user.institute else None
            else:
                # If the subdomain points to a specific institute, prefer that DB
                if request.institute and request.institute.db_name:
                    tenant_db = request.institute.db_name
                elif user.institute and user.institute.db_name:
                    tenant_db = user.institute.db_name
        else:
            tenant_db = requested_db

        # Final DB selection
        if not tenant_db or tenant_db == 'default':
            set_current_db('default')
            return None

        if tenant_db not in settings.DATABASES:
            from .models import Institute
            try:
                institute = Institute.objects.using('default').get(db_name=tenant_db)
                from .database_utils import register_institute_database
                register_institute_database(institute)
            except Institute.DoesNotExist:
                logger.error(f"Tenant database {tenant_db} requested but no such institute found.")
                set_current_db('default')
                return None

        set_current_db(tenant_db)
        return None

    def process_response(self, request, response):
        """Clear the thread-local storage on response."""
        clear_current_db()
        return response
