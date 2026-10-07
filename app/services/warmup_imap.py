"""
Real IMAP engagement for warmup emails.

After a configurable delay, connects to the recipient's IMAP server to:
  1. Search INBOX for the warmup email.
  2. If not found in INBOX, check provider spam/junk folders.
  3. Rescue from spam (atomic MOVE via RFC 6851, or COPY+DELETE fallback).
  4. Mark the message as \\Seen.

All blocking I/O runs through asyncio.to_thread() so the event loop is
never blocked.
"""

import asyncio
import imaplib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Ordered list of spam folder names to probe (most common first).
_SPAM_FOLDERS = [
    "[Gmail]/Spam",   # Gmail / Google Workspace
    "Junk Email",     # Outlook / Hotmail / Office 365
    "Junk",           # generic
    "Spam",           # generic
    "Bulk Mail",      # Yahoo
    "INBOX.Spam",     # Dovecot / cPanel
    "INBOX.Junk",
]


def _connect(host: str, port: int, username: str, password: str) -> imaplib.IMAP4:
    """Blocking IMAP connect + login. Port 993 → SSL; anything else → plain."""
    if not port or port == 993:
        imap = imaplib.IMAP4_SSL(host, port or 993)
    else:
        imap = imaplib.IMAP4(host, port)
    imap.login(username, password)
    return imap


def _search_folder(
    imap: imaplib.IMAP4,
    folder: str,
    subject: str,
    sender_email: str,
) -> Optional[bytes]:
    """
    Select folder and search for the latest message matching subject + sender.
    Returns UID bytes if found, None otherwise.
    """
    try:
        status, _ = imap.select(f'"{folder}"', readonly=False)
        if status != "OK":
            return None
    except Exception:
        return None

    criteria = f'(FROM "{sender_email}" SUBJECT "{subject}")'
    try:
        status, data = imap.uid("SEARCH", None, criteria)
    except Exception:
        return None

    if status != "OK" or not data or not data[0]:
        return None

    uids = data[0].split()
    return uids[-1] if uids else None


def _rescue_from_spam(imap: imaplib.IMAP4, uid: bytes) -> bool:
    """
    Move the message from current folder (spam) to INBOX.
    Prefers RFC 6851 UID MOVE (atomic); falls back to COPY + STORE Deleted + EXPUNGE.
    Returns True on success.
    """
    # RFC 6851 MOVE (atomic, no server-side copy artifact)
    try:
        status, _ = imap.uid("MOVE", uid, "INBOX")
        if status == "OK":
            return True
    except Exception:
        pass

    # Fallback: COPY → INBOX then delete from spam
    try:
        status, _ = imap.uid("COPY", uid, "INBOX")
        if status != "OK":
            return False
        imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        imap.expunge()
        return True
    except Exception as exc:
        logger.debug("[WarmupIMAP] rescue fallback failed: %s", exc)
        return False


def _engage_blocking(
    host: str,
    port: int,
    username: str,
    password: str,
    subject: str,
    sender_email: str,
) -> dict:
    """
    Full blocking IMAP engagement. Call via asyncio.to_thread().

    Returns:
      {
        "found": bool,
        "rescued_from_spam": bool,
        "error": str | None,
      }
    """
    result = {"found": False, "rescued_from_spam": False, "error": None}
    imap = None

    try:
        imap = _connect(host, port, username, password)

        # 1. Look in INBOX first
        uid = _search_folder(imap, "INBOX", subject, sender_email)
        if uid:
            result["found"] = True
            imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            return result

        # 2. Probe known spam/junk folders
        for folder in _SPAM_FOLDERS:
            uid = _search_folder(imap, folder, subject, sender_email)
            if uid:
                result["found"] = True
                rescued = _rescue_from_spam(imap, uid)
                result["rescued_from_spam"] = rescued

                if rescued:
                    # Find the rescued copy in INBOX and mark it read
                    uid2 = _search_folder(imap, "INBOX", subject, sender_email)
                    if uid2:
                        imap.uid("STORE", uid2, "+FLAGS", r"(\Seen)")
                else:
                    # At least mark read in spam
                    imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
                return result

        # Email not found yet — it may still be in transit; that's normal
        return result

    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("[WarmupIMAP] Engagement failed (%s@%s): %s", username, host, exc)
        return result

    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


async def engage_after_delay(
    host: str,
    port: int,
    username: str,
    password: str,
    subject: str,
    sender_email: str,
    delay_seconds: int,
) -> dict:
    """
    Wait delay_seconds, then perform real IMAP engagement.
    Safe to fire with asyncio.create_task().
    """
    await asyncio.sleep(delay_seconds)
    return await asyncio.to_thread(
        _engage_blocking, host, port, username, password, subject, sender_email
    )
