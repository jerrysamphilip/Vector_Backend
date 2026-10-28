"""Unit tests (no database) for MySQL TLS connect_args (chunk D8) and the production warning."""
import ssl

import pytest
from pymysql.connections import Connection

from app.core import database
from app.core.config import Settings, database_tls_warning
from app.core.database import (
    MYSQL_INIT_COMMAND,
    MYSQL_SESSION_INIT_COMMAND,
    mysql_connect_args,
    mysql_ssl_connect_args,
)


@pytest.fixture()
def ca_file(tmp_path):
    path = tmp_path / "global-bundle.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
    return str(path)


@pytest.mark.parametrize("mode", [None, "", "disabled", "DISABLED", " disabled "])
def test_dbb_tls_u01_disabled_adds_nothing(mode):
    assert mysql_ssl_connect_args(mode, "/ignored.pem") == {}


def test_dbb_tls_u02_required_encrypts_without_verification():
    assert mysql_ssl_connect_args("required") == {"ssl": {"check_hostname": False, "verify_mode": "none"}}


def test_dbb_tls_u03_verify_ca_checks_chain_not_host(ca_file):
    assert mysql_ssl_connect_args("verify_ca", ca_file) == {
        "ssl": {"ca": ca_file, "check_hostname": False, "verify_mode": "required"}}


@pytest.mark.parametrize("mode", ["verify_identity", "VERIFY-IDENTITY"])
def test_dbb_tls_u04_verify_identity_checks_host(mode, ca_file):
    assert mysql_ssl_connect_args(mode, ca_file) == {
        "ssl": {"ca": ca_file, "check_hostname": True, "verify_mode": "required"}}


@pytest.mark.parametrize("mode", ["verify_ca", "verify_identity"])
def test_dbb_tls_u05_verify_modes_need_a_ca_file(mode, tmp_path):
    with pytest.raises(ValueError, match="MYSQL_SSL_CA"):
        mysql_ssl_connect_args(mode, "")
    with pytest.raises(ValueError, match="readable file"):
        mysql_ssl_connect_args(mode, str(tmp_path / "missing.pem"))


@pytest.mark.parametrize("mode", ["preferred", "on", "true", "tls"])
def test_dbb_tls_u06_unknown_mode_is_refused(mode):
    with pytest.raises(ValueError, match="MYSQL_SSL_MODE"):
        mysql_ssl_connect_args(mode)


def _context_for(args):
    """The SSLContext PyMySQL builds from these connect_args (no connection is opened)."""
    conn = Connection.__new__(Connection)
    return conn._create_ssl_ctx(args["ssl"])


def test_dbb_tls_u07_pymysql_contexts_match_the_modes(monkeypatch, ca_file):
    # The fake bundle is not a real certificate: build an empty client context instead of loading it
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    expected = {"required": (ssl.CERT_NONE, False), "verify_ca": (ssl.CERT_REQUIRED, False),
                "verify_identity": (ssl.CERT_REQUIRED, True)}
    for mode, (verify_mode, check_hostname) in expected.items():
        ctx = _context_for(mysql_ssl_connect_args(mode, ca_file))
        assert (ctx.verify_mode, ctx.check_hostname) == (verify_mode, check_hostname), mode


def test_dbb_tls_u08_connect_args_keep_session_settings_and_add_tls(monkeypatch, ca_file):
    monkeypatch.setattr(database.settings, "MYSQL_SSL_MODE", "disabled")
    args = mysql_connect_args(connect_timeout=10)
    assert "ssl" not in args
    assert args["connect_timeout"] == 10
    assert args["init_command"] == MYSQL_SESSION_INIT_COMMAND
    assert MYSQL_SESSION_INIT_COMMAND.startswith(MYSQL_INIT_COMMAND)
    assert "innodb_ft" not in MYSQL_SESSION_INIT_COMMAND  # 0034 dropped the FULLTEXT index (D7 reverted)
    monkeypatch.setattr(database.settings, "MYSQL_SSL_MODE", "verify_identity")
    monkeypatch.setattr(database.settings, "MYSQL_SSL_CA", ca_file)
    assert mysql_connect_args()["ssl"]["check_hostname"] is True


def test_dbb_tls_u09_engine_default_is_plain_tcp():
    # Off until DEC-01: the default settings add no ssl argument
    assert Settings.model_fields["MYSQL_SSL_MODE"].default == "disabled"
    assert Settings.model_fields["MYSQL_SSL_CA"].default == ""


@pytest.mark.parametrize("env,mode,warns", [
    ("production", "disabled", True), ("prod", "", True), ("production", "verify_identity", False),
    ("development", "disabled", False), ("staging", "disabled", False),
])
def test_dbb_tls_u10_production_warning_not_failure(env, mode, warns):
    s = Settings(ENVIRONMENT=env, MYSQL_SSL_MODE=mode)
    message = database_tls_warning(s)
    assert (message is not None) is warns
    if warns:
        assert "MYSQL_SSL_MODE" in message and "RDS" in message
