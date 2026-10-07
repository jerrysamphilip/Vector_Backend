# diagnose_duplicate_conversations.py
"""
Read-only diagnostic for the "blank body in Sent Items" bug.

Root cause under investigation: the email scheduler and the IMAP sync
service each independently do a check-then-insert on Conversation keyed by
(tenant_id, prospect_id, inbox_id), racing across threads with no unique
DB constraint backing it (see app/utils/conversation_lock.py for the fix
to the race itself). This script finds any duplicate Conversation groups
already created by that race, and separately checks whether any SENT
EmailMessage rows actually have a null/empty body (a different, unrelated
failure mode this would also catch).

Usage:
    python diagnose_duplicate_conversations.py [campaign_id]

Run this from wherever the app's DATABASE_URL actually resolves (e.g.
inside the backend container) — it reuses app.core.database.SessionLocal,
so it points at whatever DB your running app is configured for.

This script makes NO writes — every query is a SELECT.
"""

import sys
from app.core.database import SessionLocal
from app.models.conversation import Conversation
from app.models.email_message import EmailMessage
from sqlalchemy import func


def main():
    campaign_id = sys.argv[1] if len(sys.argv) > 1 else None

    db = SessionLocal()
    try:
        # 1. Duplicate Conversation groups: same (tenant_id, prospect_id, inbox_id)
        #    appearing more than once. This is the direct fingerprint of the race.
        dup_groups = (
            db.query(
                Conversation.tenant_id,
                Conversation.prospect_id,
                Conversation.inbox_id,
                func.count(Conversation.id).label("conv_count"),
            )
            .group_by(Conversation.tenant_id, Conversation.prospect_id, Conversation.inbox_id)
            .having(func.count(Conversation.id) > 1)
            .all()
        )

        print(f"Found {len(dup_groups)} prospect+inbox pairs with duplicate Conversation rows.\n")

        affected_campaign_hits = 0

        for tenant_id, prospect_id, inbox_id, conv_count in dup_groups:
            convs = (
                db.query(Conversation)
                .filter(
                    Conversation.tenant_id == tenant_id,
                    Conversation.prospect_id == prospect_id,
                    Conversation.inbox_id == inbox_id,
                )
                .order_by(Conversation.created_at)
                .all()
            )

            print(f"--- prospect={prospect_id} inbox={inbox_id} ({conv_count} conversations) ---")
            for conv in convs:
                messages = (
                    db.query(EmailMessage)
                    .filter(EmailMessage.conversation_id == conv.id)
                    .all()
                )
                sent_with_body = sum(
                    1 for m in messages if m.status == "SENT" and (m.body_text or "").strip()
                )
                sent_blank = sum(
                    1 for m in messages if m.status == "SENT" and not (m.body_text or "").strip()
                )
                campaign_ids = {m.campaign_id for m in messages if m.campaign_id}
                if campaign_id and campaign_id in campaign_ids:
                    affected_campaign_hits += 1

                print(
                    f"    conv={conv.id} created_at={conv.created_at} "
                    f"messages={len(messages)} sent_with_body={sent_with_body} "
                    f"sent_blank={sent_blank} campaigns={campaign_ids or '-'}"
                )
            print()

        # 2. Direct check: any SENT message with a genuinely null/empty body,
        #    independent of the duplicate-conversation theory.
        blank_sent_query = db.query(EmailMessage).filter(
            EmailMessage.status == "SENT",
        )
        if campaign_id:
            blank_sent_query = blank_sent_query.filter(EmailMessage.campaign_id == campaign_id)

        blank_sent = [
            m for m in blank_sent_query.all() if not (m.body_text or "").strip()
        ]

        print(f"SENT messages with null/empty body_text{f' (campaign {campaign_id})' if campaign_id else ''}: {len(blank_sent)}")
        for m in blank_sent[:20]:
            print(f"    message_id={m.message_id} prospect_id={m.prospect_id} conversation_id={m.conversation_id} sent_at={m.sent_at}")
        if len(blank_sent) > 20:
            print(f"    ... and {len(blank_sent) - 20} more")

        if campaign_id:
            print(f"\nDuplicate-conversation groups touching campaign {campaign_id}: {affected_campaign_hits}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
