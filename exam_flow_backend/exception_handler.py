"""
Custom DRF exception handler.

Normalizes every error response from the API to a single shape so the frontend
has one schema to parse:

    {
        "detail":   "human-readable summary",   # always present
        "code":     "snake_case_error_code",    # always present
        "errors":   {field: [msg, ...], ...},   # only on validation errors
        "request_id": "<uuid>",                 # for log correlation, if set
        "error":    "<mirror of detail>"        # legacy alias for back-compat
    }

The legacy `error` key is kept so existing frontend code that reads `body.error`
keeps working during the migration window. New code should read `detail`.
"""
from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def _code_for(exc: Exception, status_code: int) -> str:
    """Pick a stable machine-readable code for the given exception/status."""
    code_attr = getattr(exc, 'default_code', None) or getattr(exc, 'code', None)
    if isinstance(code_attr, str) and code_attr:
        return code_attr
    return {
        400: 'invalid',
        401: 'not_authenticated',
        403: 'permission_denied',
        404: 'not_found',
        405: 'method_not_allowed',
        406: 'not_acceptable',
        409: 'conflict',
        413: 'request_too_large',
        415: 'unsupported_media_type',
        429: 'throttled',
        500: 'server_error',
        503: 'service_unavailable',
    }.get(status_code, 'error')


def _extract_detail_and_errors(payload):
    """
    DRF returns assorted shapes. Reduce to:
      detail: one-line summary string
      errors: dict of field -> list[str], or None
    """
    if isinstance(payload, dict):
        # Field-level validation errors look like {"field": ["msg", ...], ...}
        # or sometimes {"detail": "..."} or {"non_field_errors": [...]}.
        if 'detail' in payload and len(payload) == 1:
            return str(payload['detail']), None

        flat_errors = {}
        for key, value in payload.items():
            if isinstance(value, list):
                flat_errors[key] = [str(v) for v in value]
            elif isinstance(value, dict):
                flat_errors[key] = [str(value)]
            else:
                flat_errors[key] = [str(value)]

        # Compose a one-line detail from the first error we find.
        first_field, first_msgs = next(iter(flat_errors.items()))
        if first_field == 'non_field_errors':
            detail = first_msgs[0] if first_msgs else 'Validation error.'
        else:
            detail = f'{first_field}: {first_msgs[0]}' if first_msgs else 'Validation error.'
        return detail, flat_errors

    if isinstance(payload, list):
        msgs = [str(v) for v in payload]
        return (msgs[0] if msgs else 'Validation error.', {'non_field_errors': msgs})

    return str(payload), None


def custom_exception_handler(exc, context):
    """
    Wraps DRF's default handler. For exceptions DRF does not know about
    (e.g. uncaught Python errors) we still return a normalized 500 JSON body
    instead of leaking a Django HTML traceback page.
    """
    # Let DRF handle the exceptions it knows about (APIException subclasses,
    # Http404, PermissionDenied). It will return None for unknown exceptions.
    response = drf_exception_handler(exc, context)

    request = context.get('request') if context else None
    request_id = getattr(request, 'id', None) if request else None

    if response is None:
        # Unhandled exception: log with traceback, return a generic 500.
        logger.exception(
            'Unhandled exception in API view',
            extra={'request_id': request_id},
        )
        body = {
            'detail': 'An unexpected error occurred. Please try again later.',
            'code': 'server_error',
        }
        if request_id:
            body['request_id'] = request_id
        body['error'] = body['detail']
        return Response(body, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    detail, field_errors = _extract_detail_and_errors(response.data)
    code = _code_for(exc, response.status_code)

    normalized = {
        'detail': detail,
        'code': code,
    }
    if field_errors:
        normalized['errors'] = field_errors
    if request_id:
        normalized['request_id'] = request_id

    # Backwards-compatible alias for older frontend code that reads `error`.
    normalized['error'] = detail

    # Preserve special hints DRF attaches (e.g. throttle Retry-After header).
    response.data = normalized
    return response
