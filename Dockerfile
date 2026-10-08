FROM python:3.12.7-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a prefix the runtime user can read
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12.7-slim

WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /install /usr/local

# Unprivileged runtime user (UID/GID 10001, matches runAsUser in manifests/)
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app

# Copy application code (owned by root, read-only for the app user)
COPY . .

# Writable upload storage (ATTACHMENTS_DIR defaults to /app/uploads/attachments)
RUN mkdir -p /app/uploads/attachments && chown -R app:app /app/uploads

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001

# Expose the port the app runs on
EXPOSE 8001

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
