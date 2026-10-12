# ui_crm tasks_trash results

PASS 10 · FAIL 0 · BLOCKED 1

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-TSK-01 | Tasks page: counters and list equal DB | PASS |  |  |
| UIC-TSK-02 | Date filters: overdue / next 7 days / no due date; status Done/All | PASS |  |  |
| UIC-TSK-03 | New task: title required; create with type/priority/due/assignee -> DB and visible to assignee | PASS |  |  |
| UIC-TSK-04 | Complete, reopen and delete (confirm) a task | PASS |  |  |
| UIC-TSK-05 | Task linked to a contact shows a link to it | PASS |  |  |
| UIC-TSK-06 | AGENT: no "Everyone" scope, only own tasks; cannot modify others' task via API | PASS |  |  |
| UIC-TSK-07 | Empty state and API failure | PASS |  |  |
| UIC-RD-01 | Recently deleted lists deleted contacts (30 minus purged-window one), paginated, days remaining | PASS |  |  |
| UIC-RD-02 | Search, select-all restore and single restore update DB and the Contacts list | PASS |  |  |
| UIC-RD-03 | Manual purge of a deleted record | BLOCKED | not-implemented | No purge/permanent-delete control on Recently deleted (items expire after 90 days) |
| UIC-RD-04 | AGENT without manage_prospects: page not reachable, API refused | PASS |  |  |
