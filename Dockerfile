# Base images are pinned by digest (P-2). Refresh with `python3 scripts/pin_images.py` before a
# release; `python3 scripts/pin_images.py --check` fails on any unpinned FROM / image line.
FROM python:3.12.7-slim@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a prefix the runtime user can read
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12.7-slim@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /install /usr/local

# Unprivileged runtime user (UID/GID 10001, matches runAsUser in manifests/)
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

# Copy application code (owned by root, read-only for the app user)
COPY . .

# Writable upload storage (ATTACHMENTS_DIR defaults to /app/uploads/attachments). In Kubernetes the
# root filesystem is read-only and /app/uploads and /tmp are emptyDir volumes (manifests/).
# Bytecode is compiled at build time because nothing can be written at run time.
RUN mkdir -p /app/uploads/attachments && chown -R app:app /app/uploads \
    && python -m compileall -q /app/app /app/alembic

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001

# Expose the port the app runs on
EXPOSE 8001

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
