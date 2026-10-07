# app/utils/conversation_lock.py
"""
Cross-thread lock guarding Conversation find-or-create.

The email scheduler (asyncio task on the main thread) and the IMAP sync
service (run via loop.run_in_executor on a worker thread — see main.py)
both do a check-then-insert on Conversation keyed by (prospect_id, inbox_id),
with no unique DB constraint backing it. Without serialization, a send and
an inbound sync landing in the same window can each find no existing
Conversation and create a duplicate, splitting a thread's messages across
two rows (one of which then looks blank in the inbox).

This lock only protects against races within a single process. It relies on
the current single-replica deployment (see manifests/deployment.yaml); if
this service is ever scaled to multiple replicas, the race returns and a
DB-level unique constraint on (tenant_id, prospect_id, inbox_id) becomes
necessary.
"""

import threading

conversation_creation_lock = threading.Lock()
