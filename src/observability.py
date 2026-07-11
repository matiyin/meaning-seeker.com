"""
GlitchTip / Sentry-compatible error reporting and logging integration.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def init_observability(*, service: str) -> None:
    """Initialize GlitchTip/Sentry if SENTRY_DSN or GLITCHTIP_DSN is set."""
    global _initialized
    if _initialized:
        return

    dsn = os.getenv("SENTRY_DSN", "").strip() or os.getenv("GLITCHTIP_DSN", "").strip()
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.warning("sentry-sdk not installed; error reporting disabled")
        return

    environment = os.getenv("SENTRY_ENVIRONMENT", os.getenv("ENVIRONMENT", "production"))
    release = os.getenv("SENTRY_RELEASE", "").strip() or None
    traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0"))

    integrations = [
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ]
    if service == "web":
        integrations.extend([StarletteIntegration(), FastApiIntegration()])

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=integrations,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        attach_stacktrace=True,
    )
    sentry_sdk.set_tag("service", service)
    _initialized = True
    logger.info("GlitchTip/Sentry initialized for service=%s env=%s", service, environment)


def capture_exception(exc: BaseException, **context: object) -> None:
    """Report an exception to GlitchTip when configured."""
    try:
        import sentry_sdk
    except ImportError:
        return

    if not sentry_sdk.Hub.current.client:
        return

    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_exception(exc)


def capture_message(message: str, level: str = "error", **context: object) -> None:
    """Report a message to GlitchTip when configured."""
    try:
        import sentry_sdk
    except ImportError:
        return

    if not sentry_sdk.Hub.current.client:
        return

    with sentry_sdk.push_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level=level)
