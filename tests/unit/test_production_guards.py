"""Production configuration guards (I-11 fail closed, BR-DF-09; docs/CI_GATES.md).

Each case starts a fresh interpreter with a minimal, explicit environment (no .env, no
app-properties.ini profile, no OpenAI key) and imports the app, so the start-up checks in
app/core/config.py, app/core/secrets_guard.py and app/main.py run exactly as in a pod.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOOD_PROD = {
    "ENVIRONMENT": "production",
    "JWT_SECRET_KEY": "p" * 48,
    "CREDENTIALS_ENCRYPTION_KEY": "unit-prod-credentials-key-not-the-dev-one",
    "METRICS_TOKEN": "m" * 40,
    "FRONTEND_URL": "https://outreach.example.com",
    "AWS_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:000000000000:unit",
    "RUN_BACKGROUND_JOBS": "false",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "9",  # closed port: importing the app must not need a database
    "AWS_EC2_METADATA_DISABLED": "true",
}


def _run(code: str, env: dict, drop: tuple = ()) -> subprocess.CompletedProcess:
    full = {k: v for k, v in env.items() if k not in drop}
    full.update({"OPENAI_API_KEY": "", "APP_PROFILE": "unit-none", "PATH": os.environ.get("PATH", ""),
                 "PYTHONDONTWRITEBYTECODE": "1"})
    return subprocess.run([sys.executable, "-c", code], env=full, cwd=ROOT, capture_output=True, text=True,
                          timeout=120)


def _refused(proc: subprocess.CompletedProcess, needle: str) -> None:
    assert proc.returncode != 0, f"start-up was not refused:\n{proc.stdout}\n{proc.stderr}"
    assert needle in proc.stderr, proc.stderr[-2000:]


def test_good_production_config_starts():
    proc = _run("import app.main as m; print('docs', m.app.docs_url, m.app.openapi_url)", GOOD_PROD)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "docs None None" in proc.stdout


def test_default_jwt_secret_is_refused():
    _refused(_run("import app.core.config", {**GOOD_PROD, "JWT_SECRET_KEY": "change-this-in-production-use-a-long-random-string"}),
             "JWT_SECRET_KEY")


def test_missing_jwt_secret_is_refused():
    _refused(_run("import app.core.config", GOOD_PROD, drop=("JWT_SECRET_KEY",)), "JWT_SECRET_KEY")


def test_mail_tls_verify_false_is_refused():
    _refused(_run("import app.core.config", {**GOOD_PROD, "MAIL_TLS_VERIFY": "false"}), "MAIL_TLS_VERIFY")


@pytest.mark.parametrize("missing", [None, ""])
def test_missing_environment_when_deployed_is_refused(missing):
    env = {**GOOD_PROD, "KUBERNETES_SERVICE_HOST": "10.0.0.1"}
    if missing is None:
        env.pop("ENVIRONMENT")
    else:
        env["ENVIRONMENT"] = missing
    _refused(_run("import app.core.config", env), "ENVIRONMENT is not set")


def test_explicit_environment_when_deployed_starts():
    env = {**GOOD_PROD, "KUBERNETES_SERVICE_HOST": "10.0.0.1"}
    proc = _run("from app.core.config import settings; print(settings.ENVIRONMENT)", env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().endswith("production")


def test_missing_environment_locally_still_defaults_to_development():
    env = {k: v for k, v in GOOD_PROD.items() if k != "ENVIRONMENT"}
    proc = _run("from app.core.config import settings; print(settings.ENVIRONMENT)", env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().endswith("development")


@pytest.mark.parametrize("origins", ["*", "https://outreach.example.com,*", "https://*.example.com"])
def test_wildcard_cors_origin_is_refused(origins):
    _refused(_run("import app.core.config", {**GOOD_PROD, "FRONTEND_URL": origins}), "wildcard origin")


def test_docs_are_disabled_in_production():
    code = ("from fastapi.testclient import TestClient; import app.main as m; c = TestClient(m.app);"
            "print([c.get(p).status_code for p in ('/docs', '/redoc', '/openapi.json')])")
    proc = _run(code, GOOD_PROD)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "[404, 404, 404]" in proc.stdout


def test_docs_can_be_reenabled_explicitly():
    proc = _run("import app.main as m; print('docs', m.app.docs_url)", {**GOOD_PROD, "ENABLE_DOCS": "true"})
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "docs /docs" in proc.stdout


def test_self_signup_defaults_off_in_production():
    proc = _run("from app.core.config import settings; print('signup', settings.self_signup_enabled)", GOOD_PROD)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "signup False" in proc.stdout


def test_metrics_token_is_required_in_production():
    _refused(_run("import app.core.config", GOOD_PROD, drop=("METRICS_TOKEN",)), "METRICS_TOKEN")


def test_metrics_can_be_disabled_instead_of_tokened():
    proc = _run("import app.core.config", {**GOOD_PROD, "METRICS_ENABLED": "false"}, drop=("METRICS_TOKEN",))
    assert proc.returncode == 0, proc.stderr[-3000:]


def test_metrics_endpoint_requires_the_token_in_production():
    code = ("from fastapi.testclient import TestClient; import app.main as m; c = TestClient(m.app);"
            "print(c.get('/metrics').status_code, "
            "c.get('/metrics', headers={'Authorization': 'Bearer " + "m" * 40 + "'}).status_code)")
    proc = _run(code, GOOD_PROD)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().endswith("401 200")


def test_self_signup_defaults_off_outside_production_too():
    env = {**GOOD_PROD, "ENVIRONMENT": "development"}
    proc = _run("from app.core.config import settings; print('signup', settings.self_signup_enabled)", env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "signup False" in proc.stdout
    proc = _run("from app.core.config import settings; print('signup', settings.self_signup_enabled)",
                {**env, "ALLOW_SELF_SIGNUP": "true"})
    assert "signup True" in proc.stdout


def test_health_and_root_do_not_expose_version_or_docs_in_production():
    code = ("from fastapi.testclient import TestClient; import app.main as m; c = TestClient(m.app);"
            "h = c.get('/health').json(); r = c.get('/').json();"
            "print('version' in h, 'version' in r, 'docs' in r, h['status'])")
    proc = _run(code, GOOD_PROD)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.strip().endswith("False False False healthy")
