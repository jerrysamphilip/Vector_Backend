# app/worker.py
"""
Background worker: `python -m app.worker`.

Runs the background loops from app/jobs.py outside the API process (deployments set
RUN_BACKGROUND_JOBS=false on the API). Each loop iteration takes a MySQL named lock, so
extra worker replicas, or an API that still runs the loops, never double-run an iteration.

Health on WORKER_HEALTH_PORT (default 8002):
  /health        the process is up (liveness)
  /health/ready  the database answers and no loop heartbeat is older than 3x its interval
"""
import asyncio
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app.worker")

from sqlalchemy import text  # noqa: E402

from app import jobs  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import engine  # noqa: E402


def _readiness() -> tuple:
    body = {"status": "ready", "database": "ok", "heartbeats": {
        name: round(time.time() - beat, 1) for name, beat in jobs.HEARTBEATS.items()}}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        logger.warning("Readiness check failed: database unreachable (%s)", type(e).__name__)
        body.update(status="unavailable", database="unreachable")
        return 503, body
    stale = jobs.stale_jobs()
    if stale:
        body.update(status="unavailable", stale_loops=stale)
        return 503, body
    return 200, body


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            status, body = 200, {"status": "alive", "app": settings.APP_NAME, "role": "worker"}
        elif path == "/health/ready":
            status, body = _readiness()
        elif path == "/metrics":
            # This process's job heartbeats and engine counters (port 8002 is cluster-internal)
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        else:
            status, body = 404, {"detail": "Not Found"}
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # probes every few seconds; keep them out of the logs
        logger.debug("health %s", fmt % args)


def start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="worker-health", daemon=True).start()
    logger.info("Worker health server listening on :%s (/health, /health/ready)", port)
    return server


async def main() -> None:
    # Refuse to start (when deployed) without the key that decrypts mailbox passwords
    from app.core.encrypted_type import check_credentials_key
    check_credentials_key()

    server = start_health_server(settings.WORKER_HEALTH_PORT)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = jobs.start_jobs()
    logger.info("Worker started %d background loops", len(tasks))
    await stop.wait()
    logger.info("Worker shutting down")
    await jobs.stop_jobs(tasks)
    server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
