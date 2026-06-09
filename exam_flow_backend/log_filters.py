"""
Logging filter that injects the current request id into every log record so
the configured formatter can reference `%(request_id)s` safely even outside
the request/response cycle (Celery tasks, management commands, etc.).
"""
import logging


class RequestIDFilter(logging.Filter):
    """Ensure every record has a `request_id` attribute."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, 'request_id'):
            record.request_id = '-'
        return True
