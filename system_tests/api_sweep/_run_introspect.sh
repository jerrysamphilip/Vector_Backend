# Introspect the guards of the code the live API runs: the image of the running api container (override with
# TEST_API_IMAGE), with only the introspection script mounted. Mounting the working tree instead would describe
# uncommitted code that the live /openapi.json does not serve yet. No database is touched (RUN_BACKGROUND_JOBS=false).
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${TEST_API_IMAGE:-$(docker inspect "${TEST_API_CONTAINER:-vector-api-1}" --format '{{.Config.Image}}' 2>/dev/null || echo vector-api:latest)}"
docker run --rm --network vector_default -v "$HERE/_introspect.py:/tmp/_introspect.py:ro" -v /dev/null:/app/.env:ro -w /app -e PYTHONPATH=/app -e MYSQL_HOST=mysql -e MYSQL_USER=vector -e MYSQL_PASSWORD=vector -e MYSQL_DATABASE=outreach_ai -e JWT_SECRET_KEY=local-dev-only-change-me-0123456789abcdef -e CREDENTIALS_ENCRYPTION_KEY=local-dev-only-credentials-key -e ENVIRONMENT=development -e RUN_BACKGROUND_JOBS=false "$IMAGE" python /tmp/_introspect.py 2>&1
