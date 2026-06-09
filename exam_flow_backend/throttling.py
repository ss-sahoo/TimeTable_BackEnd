from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


# Per-route throttle classes. Each binds to a `DEFAULT_THROTTLE_RATES` scope
# defined in settings.REST_FRAMEWORK. They are intentionally narrow (one
# subclass per scope) so they can be attached to function-based views via
# `@throttle_classes([...])` without depending on the view exposing a
# `throttle_scope` attribute (which `@api_view`'s wrapper does not always
# forward cleanly across DRF versions).


class LoginAnonRateThrottle(AnonRateThrottle):
    scope = 'login'


class LoginUserRateThrottle(UserRateThrottle):
    scope = 'login'


class PasswordResetAnonRateThrottle(AnonRateThrottle):
    scope = 'password_reset'


class PasswordResetUserRateThrottle(UserRateThrottle):
    scope = 'password_reset'


class PublicAccessAnonRateThrottle(AnonRateThrottle):
    scope = 'public_access'


class BulkExtractUserRateThrottle(UserRateThrottle):
    scope = 'bulk_extract'
