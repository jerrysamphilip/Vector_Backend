# UI outreach/admin - inboxdomain results

PASS 10 · FAIL 2 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-INBOX-00 | Inbox with existing conversations but no IMAP-configured mailbox still shows the conversations (not only the IMAP setup guide) | FAIL | defect | conversations hidden behind IMAP setup guide although 2 conversations exist (API returns them) |
| UIO-INBOX-01 | Unified Inbox lists both conversations (unread highlighted) with subject/snippet; no problems | PASS |  |  |
| UIO-INBOX-02 | Search narrows the list; opening a thread shows the message and marks it read (DB is_unread=0) | FAIL | defect | search did not filter |
| UIO-INBOX-03 | Reply form: empty reply does nothing; Enter sends a reply -> OUTBOUND message stored for that conversation | PASS |  |  |
| UIO-INBOX-04 | Reply failure (API 500) restores the draft text and does not crash | PASS |  |  |
| UIO-INBOX-05 | Sync button without IMAP reports a clear message; Refresh works | PASS |  |  |
| UIO-INBOX-06 | Tenant isolation: conversations of this workspace are not visible to another workspace | PASS |  |  |
| UIO-DOM-01 | Domain Health lists the workspace sending domain with SPF/DKIM/DMARC chips, KPIs consistent with inbox count | PASS |  |  |
| UIO-DOM-02 | Trend filter select: switching All domains <-> single domain re-renders the chart without errors | PASS |  |  |
| UIO-DOM-03 | Details modal shows DNS/campaign/inbox info; Run Diagnostics gives feedback; Close works | PASS |  |  |
| UIO-DOM-04 | Delete domain: confirm, removes the domain card and DB record (no add-domain UI exists: domains derive from inboxes) | PASS |  |  |
| UIO-DOM-05 | AGENT role can open Domain Health without forbidden-API noise | PASS |  |  |
