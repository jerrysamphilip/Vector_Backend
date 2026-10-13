# ui_crm new_features results

PASS 9 · FAIL 3 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-NF-01 | Companies card: lists the primary company; Add a second company with a label (search + pick) -> card + DB | PASS |  |  |
| UIC-NF-02 | Companies card: star makes the second company primary (badge moves, DB is_primary + contact account) | PASS |  |  |
| UIC-NF-03 | Companies card: removing the primary asks for the next primary (pre-selected), Cancel keeps it, Remove -> Alpha primary | PASS |  |  |
| UIC-NF-04 | Company page shows association chips: label + "Associated" on the non-primary company; card link opens it | PASS |  |  |
| UIC-NF-05 | @mention: type "@B", pick Bianca with the keyboard, save -> highlighted in timeline; note_mentions + MENTION notification (not for author) | PASS |  |  |
| UIC-NF-06 | Mentioned colleague's bell shows the MENTION (unread), click opens the contact record and marks it read | FAIL | unclassified | mention visible to Bianca on the record |
| UIC-NF-07 | Mentioning an AGENT who does not own the contact: the notification link should open a record they can see | FAIL | unclassified | mentioned agent cannot open the record: GET /contacts/{id} -> 404, page shows "Contact not found" |
| UIC-NF-08 | Sales reports > Leads & SQLs: "SQL to win" card numbers match GET /sales-reports/sql-to-win (and the seeded data) | PASS |  |  |
| UIC-NF-09 | Forecast tab: "Forecast accuracy" card; admin "Take snapshot now" stores a MANUAL snapshot (DB) and the card shows it | PASS |  |  |
| UIC-NF-10 | Contact record "+ Create deal": form opens with the primary company pre-selected; Create -> deal linked to contact + company | FAIL | unclassified | deal row: got "a2a7c080-13c6-4bce-94d9-7f0e9f3a8bbd\|ee6f441a-ea58-49cd-93a8-1fa21f83ec19\|25000.00\|cdebb305-c8cc-445c-897a-41c5fa287109" expected "a2a7c080-13c6-4bce-94d9-7f0e9f3a8bbd\|ee6f441a-ea58-49cd-93a8-1fa21f83ec19\|25000\|cdebb305-c8cc-445c-897a-41c5fa287109" |
| UIC-NF-11 | AccountDetail shows the Deals section (count, total, deal link); a company without deals shows the empty state | PASS |  |  |
| UIC-NF-12 | Recently deleted: select -> "Delete forever" -> confirm modal (Cancel keeps it) -> contact removed from DB + PURGE_CONTACT audit row | PASS |  |  |
