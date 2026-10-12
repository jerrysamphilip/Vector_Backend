# ui_crm accounts results

PASS 0 · FAIL 1 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIC-ACCOUNTS-CRASH | area crashed | FAIL | test-issue | Error: POST /accounts -> 422 {"success":false,"error":"Validation Error","detail":[{"type":"string_type","loc":["body","annual_revenue"],"msg":"Input should be a valid string","input":500000}],"code":"VALIDATION_ERROR"}     at must (file:///home/user/Vector_Backend/system_tests/ui_crm/crm_common.mjs:34:41)     at process.processTicksAndRejections (node:internal/process/task_queues:105:5) |
