# ui_crm new_features results

PASS 11 · FAIL 1 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-NF-01 | Companies card: lists the primary company; Add a second company with a label (search + pick) -> card + DB | PASS |  |  |
| UIC-NF-02 | Companies card: star makes the second company primary (badge moves, DB is_primary + contact account) | PASS |  |  |
| UIC-NF-03 | Companies card: removing the primary asks for the next primary (pre-selected), Cancel keeps it, Remove -> Alpha primary | PASS |  |  |
| UIC-NF-04 | Company page shows association chips: label + "Associated" on the non-primary company; card link opens it | PASS |  |  |
| UIC-NF-05 | @mention: type "@B", pick Bianca with the keyboard, save -> highlighted in timeline; note_mentions + MENTION notification (not for author) | PASS |  |  |
| UIC-NF-06 | Mentioned colleague's bell shows the MENTION (unread), click opens the contact record and marks it read | PASS |  |  |
| UIC-NF-07 | Mentioning an AGENT who does not own the contact: the notification link should open a record they can see | FAIL | defect-backend | [@mention lets you pick any active user (app/services/mentions.py mentionable_users/validate: 'Any active user of the workspace can be mentioned'), but an AGENT only sees own contacts: the MENTION notification links to /app/contacts/{id}, GET /contacts/{id} -> 404 and the page says 'Contact not found'. Either limit the picker/validation to users who can see the record or grant read access via the mention.] mentioned agent cannot open the record: GET /contacts/{id} -> 404, page shows "Contact not |
| UIC-NF-08 | Sales reports > Leads & SQLs: "SQL to win" card numbers match GET /sales-reports/sql-to-win (and the seeded data) | PASS |  |  |
| UIC-NF-09 | Forecast tab: "Forecast accuracy" card; admin "Take snapshot now" stores a MANUAL snapshot (DB) and the card shows it | PASS |  |  |
| UIC-NF-10 | Contact record "+ Create deal": form opens with the primary company pre-selected; Create -> deal linked to contact + company | PASS |  |  |
| UIC-NF-11 | AccountDetail shows the Deals section (count, total, deal link); a company without deals shows the empty state | PASS |  |  |
| UIC-NF-12 | Recently deleted: select -> "Delete forever" -> confirm modal (Cancel keeps it) -> contact removed from DB + PURGE_CONTACT audit row | PASS |  |  |
