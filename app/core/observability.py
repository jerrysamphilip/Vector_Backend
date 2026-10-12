# app/core/observability.py
"""
Observability: request IDs in logs, Prometheus metrics and optional Sentry.

- Request IDs: every request gets an id (the caller's X-Request-ID when it is sane, else a new
  one). It is kept in a contextvar, added to every log record (``%(request_id)s``) and returned
  in the X-Request-ID response header, so a user-reported error can be found in the logs.
- Logs: plain text by default; LOG_FORMAT=json writes one JSON object per line.
- Metrics: ``GET /metrics`` (Prometheus text format). On unless METRICS_ENABLED=false; when
  METRICS_TOKEN is set the scraper must send ``Authorization: Bearer <token>``.
  HTTP metrics are labelled by the route template (``/campaigns/{campaign_id}``), never the raw
  path, so label cardinality stays bounded.
- Business metrics: call the helpers below from services / jobs:
    record_email_sent(result)        emails_sent_total{result}
    record_ses_event(event_type)     ses_webhook_events_total{type}
    record_imap_sync(result)         imap_sync_runs_total{result}
    record_job_heartbeat(job)        job_last_success_timestamp{job}  (unix seconds)
  Metrics live in the process that records them. A separate worker process should call
  ``start_metrics_server()`` (METRICS_PORT, default 9100) so its job heartbeats can be scraped.
- Sentry: only when SENTRY_DSN is set. No PII, and request bodies, cookies, query strings and
  auth headers are stripped from every event before it leaves the process.
"""

import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from typing import Optional

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """The current request's id ("-" outside a request)."""
    return _request_id.get()


# ── Logging ─────────────────────────────────────────────────────

TEXT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` so formatters can always use %(request_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line (LOG_FORMAT=json)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Attach the request-id filter and formatter to the root handlers (call after basicConfig)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig()
    json_logs = (os.getenv("LOG_FORMAT") or "").strip().lower() == "json"
    formatter = JsonFormatter() if json_logs else logging.Formatter(TEXT_LOG_FORMAT)
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
        handler.setFormatter(formatter)


# ── Metrics ─────────────────────────────────────────────────────

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0))

EMAILS_SENT = Counter("emails_sent_total", "Outbound emails by send result", ["result"])
SES_WEBHOOK_EVENTS = Counter("ses_webhook_events_total", "SES notifications received", ["type"])
IMAP_SYNC_RUNS = Counter("imap_sync_runs_total", "IMAP sync runs by result", ["result"])
JOB_LAST_SUCCESS = Gauge(
    "job_last_success_timestamp", "Unix time a background job last finished successfully", ["job"])

_SES_TYPES = {"Bounce", "Complaint", "Delivery", "Open", "Click", "Reject", "DeliveryDelay",
              "Rendering Failure", "Subscription", "Send", "SubscriptionConfirmation"}


def _label(value, default: str = "unknown", limit: int = 40) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else default


def record_email_sent(result: str) -> None:
    """Count one send attempt, e.g. result='sent' | 'failed' | 'bounced' | 'blocked'."""
    EMAILS_SENT.labels(result=_label(result).lower()).inc()


def record_ses_event(event_type: Optional[str]) -> None:
    """Count one SES webhook notification by type (unknown types are grouped as 'other')."""
    label = event_type if event_type in _SES_TYPES else ("other" if event_type else "unknown")
    SES_WEBHOOK_EVENTS.labels(type=label).inc()


def record_imap_sync(result: str) -> None:
    """Count one IMAP sync run, e.g. result='ok' | 'error'."""
    IMAP_SYNC_RUNS.labels(result=_label(result).lower()).inc()


def record_job_heartbeat(job: str) -> None:
    """Mark a background job as having just completed successfully (alert on staleness)."""
    JOB_LAST_SUCCESS.labels(job=_label(job)).set(time.time())


def start_metrics_server(port: Optional[int] = None) -> bool:
    """Serve /metrics from a non-HTTP-API process (e.g. the worker). Returns True if started."""
    if (os.getenv("METRICS_ENABLED", "true").strip().lower() in ("0", "false", "no", "off")):
        return False
    from prometheus_client import start_http_server
    port = int(port or os.getenv("METRICS_PORT", "9100"))
    start_http_server(port)
    logger.info("Metrics server listening on :%s", port)
    return True


# ── Middleware ──────────────────────────────────────────────────

class ObservabilityMiddleware:
    """Pure ASGI middleware: request id + HTTP metrics (route template labels)."""

    SKIP_METRICS = ("/metrics",)

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        incoming = None
        for name, value in scope.get("headers") or ():
            if name == b"x-request-id":
                incoming = value.decode("latin-1").strip()
                break
        request_id = incoming if incoming and _REQUEST_ID_OK.match(incoming) else uuid.uuid4().hex
        token = _request_id.set(request_id)
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if not path.endswith(self.SKIP_METRICS):
                route = scope.get("route")
                template = getattr(route, "path_format", None) or getattr(route, "path", None) or "unmatched"
                method = scope.get("method", "GET")
                HTTP_REQUESTS.labels(method=method, route=template, status=str(status["code"])).inc()
                HTTP_LATENCY.labels(method=method, route=template).observe(time.perf_counter() - start)
            _request_id.reset(token)


def _metrics_flags():
    from app.core.config import settings
    return bool(getattr(settings, "METRICS_ENABLED", True)), (getattr(settings, "METRICS_TOKEN", "") or "").strip()


def install_observability(app) -> None:
    """Register the request-id/metrics middleware and the /metrics endpoint on the app."""
    import hmac

    app.add_middleware(ObservabilityMiddleware)
    enabled, token = _metrics_flags()
    if not enabled:
        return
    from app.core.config import settings
    if settings.is_production and not token:
        logger.warning("/metrics is enabled without METRICS_TOKEN; keep it off the public ingress")

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request):
        if token:
            presented = request.headers.get("authorization", "")
            if not hmac.compare_digest(presented.encode(), f"Bearer {token}".encode()):
                return PlainTextResponse("Unauthorized", status_code=401)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Sentry ──────────────────────────────────────────────────────

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-webhook-token",
                      "proxy-authorization", "x-amz-security-token"}


def _scrub_event(event, hint=None):
    """Drop request bodies, cookies, query strings and credentials from a Sentry event."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        if request.get("query_string"):
            request["query_string"] = "[Filtered]"
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SENSITIVE_HEADERS:
                    headers[key] = "[Filtered]"
    event.pop("user", None)
    request_id = _request_id.get()
    if request_id != "-":
        tags = event.get("tags")
        if isinstance(tags, dict):
            tags.setdefault("request_id", request_id)
        elif tags is None:
            event["tags"] = {"request_id": request_id}
    return event


def init_sentry() -> bool:
    """Initialise Sentry when SENTRY_DSN is set. Returns True when enabled."""
    from app.core.config import settings
    dsn = (getattr(settings, "SENTRY_DSN", "") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error reporting is off")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.ENVIRONMENT,
        release=settings.APP_VERSION,
        traces_sample_rate=float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0) or 0.0),
        send_default_pii=False,
        max_request_body_size="never",
        integrations=[StarletteIntegration(), FastApiIntegration()],
        before_send=_scrub_event,
        before_send_transaction=_scrub_event,
    )
    logger.info("Sentry error reporting enabled (environment=%s)", settings.ENVIRONMENT)
    return True
