"""
Sending inboxes (mailboxes) of the email accounts page (F-20, BR-DF-05): add, list, edit, resume,
delete, the Microsoft 365 connection, the IMAP history sync and the IMAP / SMTP connection tests.

``InboxService`` serves app/routers/inbox_router.py: one method per route, each one unit of work
(app/repositories/inbox_mailboxes.py); errors are domain errors (app/core/errors.py). Mail servers must
be public (SSRF, F007): ``check_mail_host`` refuses loopback / private / link-local / metadata hosts
with a 400 (private hosts are allowed on a local setup), and connections go through
``providers.mail_connector()``, which pins the checked addresses when deployed (no DNS rebinding).
No mail-server round trip runs inside a database transaction (T-1).
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
import socket
import uuid
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.adapters import providers
from app.adapters.mail import MailConnector
from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, DomainError, NotFoundError, ValidationFailed
from app.core.tls import tls_error_message
from app.domain.inbox_mailbox import (InvalidPort, changes_receiving, clean_updates, domain_of_address, effective_imap,
                                      warmup_toggle)
from app.domain.inbox_sync import header_summary, imap_date
from app.models.user import User
from app.repositories.inbox_mailboxes import MailboxRepository, MailboxUnitOfWork, SendingInbox, Session
from app.schemas.inbox_schema import (SendingInboxCreate, SendingInboxCreatedResponse, SendingInboxResponse,
                                      SendingInboxUpdate)
from app.services import ms365_oauth, sending_alerts
from app.services.deliverability_service import deliverability_service
from app.services.imap_sync_service import IMAPSyncService
from app.utils import host_safety

logger = logging.getLogger(__name__)

INBOX_NOT_FOUND = "Inbox not found"
TEST_TIMEOUT_SECONDS = 20
RECENT_TEST_MESSAGES = 10
ZOHO_IMAP_HELP = ("For Zoho: enable IMAP in Zoho Mail Settings → Mail Accounts → IMAP Access, "
                  "and use an App Password (Settings → Security → App Passwords).")


class MailSyncFailed(DomainError):
    """The IMAP history sync could not reach or sign in to the mail server (502)."""
    status_code = 502
    default_code = "MAIL_SYNC_FAILED"


def check_mail_host(host: Optional[str]) -> None:
    """Mail servers must be public (SSRF, F007): refuses loopback, private, link-local and cloud
    metadata addresses, checked on every address the name resolves to, with a 400. On a local setup
    (settings.is_local_environment: mailpit, test servers) private and loopback hosts are allowed and
    names are not resolved; metadata / link-local IP literals are refused there too."""
    if not host:
        return
    local = settings.is_local_environment
    try:
        host_safety.check_host(host, allow_private=local, resolver=None if local else host_safety.resolve)
    except host_safety.UnsafeHostError as exc:
        raise BadRequestError(f"Mail server not allowed: {exc}", code="MAIL_HOST_NOT_ALLOWED") from exc


def _is_oauth(inbox: SendingInbox) -> bool:
    return (inbox.auth_type or "PASSWORD") == ms365_oauth.AUTH_TYPE


R = TypeVar("R", bound=SendingInboxResponse)


def _with_imap(response: R, inbox: SendingInbox) -> R:
    """``imap_configured`` / ``imap_derived_from_smtp`` (S-26): replies can be read, with saved IMAP settings
    or ones derived from SMTP."""
    imap = effective_imap(inbox)
    response.imap_configured = imap.configured(_is_oauth(inbox))
    response.imap_derived_from_smtp = imap.imap_host_derived
    return response


def _new_inbox(tenant_id: str, data: SendingInboxCreate) -> SendingInbox:
    warmup = data.warmup_enabled is not False
    return SendingInbox(
        inbox_id=str(uuid.uuid4()), tenant_id=tenant_id, email_address=data.email_address,
        provider=data.provider or "SMTP", daily_limit=data.daily_limit, status="ACTIVE",
        smtp_host=data.smtp_host, smtp_port=data.smtp_port or 587, smtp_username=data.smtp_username,
        smtp_password=data.smtp_password, smtp_use_ssl=data.smtp_use_ssl or False,
        imap_host=data.imap_host, imap_port=data.imap_port or 993, imap_username=data.imap_username,
        imap_password=data.imap_password,
        warmup_enabled=data.warmup_enabled if data.warmup_enabled is not None else True,
        warmup_start_date=utcnow() if warmup else None,
        max_emails_per_day=data.max_emails_per_day,
        delay_between_emails=60 if data.delay_between_emails is None else data.delay_between_emails,
        warmup_auto_adjust=data.warmup_auto_adjust if data.warmup_auto_adjust is not None else True,
        warmup_randomize=data.warmup_randomize if data.warmup_randomize is not None else True,
        warmup_reply_rate_target=data.warmup_reply_rate_target or 35,
        warmup_max_target=data.warmup_max_target, warmup_identifier=data.warmup_identifier,
        warmup_status="ACTIVE" if warmup else "DISABLED",
    )


def _smtp_error(exc: smtplib.SMTPAuthenticationError) -> str:
    detail = exc.smtp_error.decode(errors='ignore') if isinstance(exc.smtp_error, bytes) else exc.smtp_error
    return f"SMTP sign-in rejected: {exc.smtp_code} {detail}"


def _quit(server: Any) -> None:
    try:
        server.quit()
    except (smtplib.SMTPException, OSError) as exc:
        logger.debug("SMTP test: quit failed: %s", exc)


class InboxService:
    def __init__(self, uow: Any, *, connector: Optional[MailConnector] = None,
                 imap_sync: Optional[Callable[[], Any]] = None,
                 domain_verifier: Optional[Callable[[Any, str, str], Optional[dict]]] = None) -> None:
        self.uow = uow
        self._connector = connector
        self._imap_sync = imap_sync
        self._domain_verifier = domain_verifier

    @classmethod
    def for_session(cls, db: Session) -> "InboxService":
        return cls(MailboxUnitOfWork.for_session(db), imap_sync=lambda: IMAPSyncService(db))

    @property
    def connector(self) -> MailConnector:
        return self._connector if self._connector is not None else providers.mail_connector()

    @staticmethod
    def _get(repo: MailboxRepository, user: User, inbox_id: str) -> SendingInbox:
        inbox = repo.get(user.tenant_id, inbox_id)
        if inbox is None:
            raise NotFoundError(INBOX_NOT_FOUND)
        return inbox

    @staticmethod
    def _with_status(session: Any, tenant_id: str, inboxes: List[SendingInbox]) -> List[SendingInboxResponse]:
        """Pause reason and today's capacity (F-20) on each mailbox."""
        status = sending_alerts.mailbox_status(session, tenant_id, inboxes)
        return [_with_imap(SendingInboxResponse.model_validate(i), i).model_copy(update=status.get(i.inbox_id, {}))
                for i in inboxes]

    # ── Microsoft 365 (BR-DF-05) ──────────────────────────────────────────
    def start_microsoft(self, user: User, inbox_id: str, issue_nonce: Callable[[], str]) -> Dict[str, str]:
        """Where to send the browser to connect a Microsoft 365 mailbox. ``issue_nonce`` binds the sign-in
        to the browser (app/services/oauth_binding.py); it is called once the mailbox is found."""
        if not ms365_oauth.is_configured():
            raise BadRequestError(
                "Microsoft 365 sign-in is not set up on the server. Set MS365_CLIENT_ID, "
                "MS365_CLIENT_SECRET and MS365_REDIRECT_URI from your Azure app registration.")
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            state = ms365_oauth.make_state(inbox.inbox_id, user.tenant_id, user.user_id, issue_nonce())
            return {"authorize_url": ms365_oauth.authorize_url(inbox, state)}

    def finish_microsoft(self, tenant_id: str, inbox_id: str, code: str) -> Dict[str, str]:
        """Store the tokens of a Microsoft sign-in and check the mailbox accepts them over IMAP. Returns
        the query parameters of the redirect back to the app (ms365=connected / error)."""
        with self.uow as uow:
            inbox = uow.inboxes.get(tenant_id, inbox_id)
            if inbox is None:
                return {"ms365": "error", "message": INBOX_NOT_FOUND}
            try:
                ms365_oauth.complete_connection(uow.session, inbox, code)
            except ms365_oauth.OAuthError as exc:
                return {"ms365": "error", "message": str(exc), "inbox": inbox.inbox_id}
            error = self._imap_token_check(uow.session, inbox)
            if error is None:
                return {"ms365": "connected", "inbox": inbox.inbox_id}
            inbox.oauth_error = error[:2000]
            uow.commit()
            return {"ms365": "error", "message": inbox.oauth_error, "inbox": inbox.inbox_id}

    def _imap_token_check(self, session: Any, inbox: SendingInbox) -> Optional[str]:
        try:
            mail = self.connector.open_imap(inbox.imap_host, inbox.imap_port, timeout=TEST_TIMEOUT_SECONDS)
            ms365_oauth.imap_login(mail, inbox.imap_username or inbox.email_address,
                                   ms365_oauth.imap_secret(session, inbox))
            mail.logout()
            return None
        except Exception as exc:  # any IMAP failure is reported back to the user on the inbox
            logger.warning("IMAP check after Microsoft sign-in failed for inbox %s: %s", inbox.inbox_id, exc)
            return (tls_error_message(exc) or
                    f"Signed in, but IMAP rejected the token: {exc}. Check IMAP is enabled for the mailbox.")

    # ── CRUD ──────────────────────────────────────────────────────────────
    def create(self, user: User, data: SendingInboxCreate) -> SendingInboxCreatedResponse:
        """Add a mailbox, then start the verification of its domain (a failure there is only logged)."""
        check_mail_host(data.smtp_host)
        check_mail_host(data.imap_host)
        with self.uow as uow:
            if uow.inboxes.by_email(user.tenant_id, data.email_address):
                raise BadRequestError("Inbox with this email already exists")
            inbox = uow.inboxes.add(_new_inbox(user.tenant_id, data))
            uow.commit()
            uow.inboxes.refresh(inbox)
            verification = self._verify_domain(uow.session, inbox)
            response = _with_imap(SendingInboxCreatedResponse.model_validate(inbox), inbox)
        response.domain_verification = verification
        return response

    def _verify_domain(self, session: Any, inbox: SendingInbox) -> Optional[dict]:
        domain = domain_of_address(inbox.email_address)
        if domain is None:
            return None
        try:
            if self._domain_verifier is not None:
                return self._domain_verifier(session, inbox.tenant_id, domain)
            return deliverability_service.verify_domain_and_get_tokens(domain, session, tenant_id=inbox.tenant_id)
        except Exception as e:  # the mailbox is created anyway
            logger.exception("Domain verification failed for %s: %s", domain, e)
            return None

    def list(self, user: User) -> List[SendingInboxResponse]:
        with self.uow as uow:
            return self._with_status(uow.session, user.tenant_id, uow.inboxes.list(user.tenant_id))

    def get(self, user: User, inbox_id: str) -> SendingInboxResponse:
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            return self._with_status(uow.session, user.tenant_id, [inbox])[0]

    def resume(self, user: User, inbox_id: str) -> SendingInboxResponse:
        """Resume sending from a paused mailbox (F-20); logged in its activity. No-op when active."""
        with self.uow as uow:
            try:
                inbox = sending_alerts.resume_inbox(uow.session, user, inbox_id)
            except sending_alerts.InboxNotFound as exc:
                raise NotFoundError(INBOX_NOT_FOUND) from exc
            return self._with_status(uow.session, user.tenant_id, [inbox])[0]

    def update(self, user: User, inbox_id: str, data: SendingInboxUpdate) -> SendingInboxResponse:
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            updates = data.model_dump(exclude_unset=True)
            fields: Dict[str, Any] = {}
            if "warmup_enabled" in updates:
                fields.update(warmup_toggle(bool(updates["warmup_enabled"]), inbox.warmup_start_date, utcnow()))
            try:
                updates = clean_updates(updates)
            except InvalidPort as exc:
                raise ValidationFailed(str(exc)) from exc
            for host_field in ("smtp_host", "imap_host"):
                if host_field in updates:
                    check_mail_host(updates[host_field])
            fields.update(updates)
            if changes_receiving(updates):
                fields["imap_last_error"] = None
            for key, value in fields.items():
                setattr(inbox, key, value)
            uow.commit()
            uow.inboxes.refresh(inbox)
            return _with_imap(SendingInboxResponse.model_validate(inbox), inbox)

    def delete(self, user: User, inbox_id: str) -> None:
        with self.uow as uow:
            uow.inboxes.delete(self._get(uow.inboxes, user, inbox_id))
            uow.commit()

    # ── IMAP history sync and connection tests ────────────────────────────
    def sync_history(self, user: User, inbox_id: str, days: int) -> Dict[str, Any]:
        """Sync the last ``days`` days of the mailbox now (MailSyncFailed when the server fails)."""
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            imap = effective_imap(inbox)  # S-26: derived from SMTP when not saved
            if not imap.configured(_is_oauth(inbox)):
                raise BadRequestError(
                    f"IMAP is not configured for {inbox.email_address}. "
                    "Please set imap_host, imap_username, and imap_password in the inbox settings.")
            check_mail_host(imap.effective_imap_host)  # saved before the check existed: re-check before connecting
            email_address, imap_host = inbox.email_address, imap.effective_imap_host
        if self._imap_sync is None or not self._imap_sync().sync_inbox(inbox, days_back=days):
            raise MailSyncFailed(
                f"IMAP sync failed for {email_address}. "
                f"Check that imap_host={imap_host} is reachable and credentials are correct.")
        with self.uow:
            last_sync_at = inbox.last_sync_at
        return {
            "status": "success",
            "message": f"IMAP sync for last {days} days completed.",
            "inbox": email_address,
            "imap_host": imap_host,
            "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
        }

    def test_imap(self, user: User, inbox_id: str) -> Dict[str, Any]:
        """Sign in over IMAP and list the latest messages of the last 7 days (diagnoses missing replies)."""
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            imap = effective_imap(inbox)  # S-26: gaps filled from the SMTP settings
            if not imap.effective_imap_host:
                return {"status": "not_configured", "inbox": inbox.email_address,
                        "error": "imap_host is not set. Set it to imap.zoho.in (Zoho India) or imap.zoho.com"}
            if not imap.effective_imap_password and not _is_oauth(inbox):
                return {"status": "not_configured", "inbox": inbox.email_address,
                        "error": "imap_password is not set. For Zoho, use an App Password (not your login password)."}
            check_mail_host(imap.effective_imap_host)
            result: Dict[str, Any] = {
                "inbox": inbox.email_address, "imap_host": imap.effective_imap_host,
                "imap_port": imap.effective_imap_port, "imap_username": imap.effective_imap_username,
                "imap_derived_from_smtp": imap.imap_host_derived,
                "last_sync_at": inbox.last_sync_at.isoformat() if inbox.last_sync_at else None,
                "status": None, "error": None, "recent_inbox_emails": [],
            }
            host, port = imap.effective_imap_host, imap.effective_imap_port
            try:
                secret = ms365_oauth.imap_secret(uow.session, inbox) if _is_oauth(inbox) \
                    else imap.effective_imap_password
            except Exception as e:  # e.g. the Microsoft token cannot be refreshed
                return self._imap_failure(result, e)
            uow.commit()
        return self._imap_probe(result, host, port, secret)

    def _imap_probe(self, result: Dict[str, Any], host: str, port: Optional[int], secret: Any) -> Dict[str, Any]:
        try:
            mail = self.connector.open_imap(host, port, timeout=TEST_TIMEOUT_SECONDS)
            ms365_oauth.imap_login(mail, result["imap_username"], secret)
            result["status"] = "connected"
            status, _ = mail.select("INBOX", readonly=True)
            if status != "OK":
                result["error"] = "Could not select INBOX folder"
            else:
                self._recent_messages(mail, result)
            mail.logout()
        except Exception as e:
            return self._imap_failure(result, e)
        return result

    @staticmethod
    def _imap_failure(result: Dict[str, Any], e: Exception) -> Dict[str, Any]:
        if isinstance(e, imaplib.IMAP4.error):
            result["status"] = "auth_failed"
            result["error"] = f"IMAP login failed: {e}. {ZOHO_IMAP_HELP}"
        else:  # connection test: report any failure to the caller
            logger.info("IMAP connection test failed for %s: %s", result.get("imap_host"), e)
            result["status"] = "connection_failed"
            result["error"] = tls_error_message(e) or str(e)
        return result

    @staticmethod
    def _recent_messages(mail: Any, result: Dict[str, Any]) -> None:
        """How many messages arrived in the last 7 days, and the headers of the latest ten."""
        status, data = mail.search(None, f'(SINCE "{imap_date((utcnow() - timedelta(days=7)).date())}")')
        message_ids = data[0].split() if status == "OK" else []
        result["inbox_message_count_last_7d"] = len(message_ids)
        for msg_id in message_ids[-RECENT_TEST_MESSAGES:]:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822.HEADER)")
                result["recent_inbox_emails"].append(header_summary(msg_data[0][1]))
            except Exception as e:  # one unreadable message must not hide the others
                logger.info("IMAP test: could not read message %s: %s", msg_id, e)
                result["recent_inbox_emails"].append({"error": str(e)})

    def test_smtp(self, user: User, inbox_id: str) -> Dict[str, Any]:
        """Sign in to the mailbox's SMTP server (no email is sent) to check the sending credentials."""
        with self.uow as uow:
            inbox = self._get(uow.inboxes, user, inbox_id)
            if not inbox.smtp_host:
                return {"status": "not_configured", "error": "SMTP host is not set."}
            if not inbox.smtp_password and not _is_oauth(inbox):
                return {"status": "not_configured", "error": "SMTP password is not set."}
            check_mail_host(inbox.smtp_host)
            host, port = inbox.smtp_host, inbox.smtp_port or 587
            use_ssl = bool(inbox.smtp_use_ssl or port == 465)
            username = inbox.smtp_username or inbox.email_address
            try:
                secret = ms365_oauth.smtp_secret(uow.session, inbox)
            except Exception as e:
                logger.info("SMTP connection test failed for %s:%s: %s", host, port, e)
                return {"status": "connection_failed", "error": tls_error_message(e) or str(e)}
            uow.commit()
        return self._smtp_probe(host, port, use_ssl, username, secret)

    def _smtp_probe(self, host: str, port: int, use_ssl: bool, username: str, secret: Any) -> Dict[str, Any]:
        try:
            server = self.connector.open_smtp(host, port, use_ssl=use_ssl, timeout=TEST_TIMEOUT_SECONDS)
            try:
                ms365_oauth.smtp_login(server, username, secret)
            finally:
                _quit(server)
        except smtplib.SMTPAuthenticationError as e:
            return {"status": "auth_failed", "error": _smtp_error(e)}
        except (socket.timeout, TimeoutError):
            return {"status": "connection_failed", "error": f"Timed out connecting to {host}:{port}."}
        except Exception as e:  # connection test: report any failure to the caller
            logger.info("SMTP connection test failed for %s:%s: %s", host, port, e)
            return {"status": "connection_failed", "error": tls_error_message(e) or str(e)}
        return {"status": "connected", "smtp_host": host, "smtp_port": port, "smtp_username": username}
