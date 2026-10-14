# admin_controls results

PASS 17 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| AC-001 | A list can be renamed and given a description (trimmed, stored) | PASS |  |  |
| AC-002 | An uploaded prospect list can be renamed too | PASS |  |  |
| AC-003 | Rename is tenant-isolated (404) and bounded (blank 400, too long 422) | PASS |  |  |
| AC-004 | Without manage_prospects a list cannot be renamed (403) | PASS |  |  |
| AC-010 | Resending straight away is refused (429, once a minute per user) | PASS |  |  |
| AC-011 | After a minute: new temporary password and sign-in link; the old invitation and links are revoked | PASS |  |  |
| AC-012 | A second resend within the minute is refused again (429) | PASS |  |  |
| AC-013 | Resend guards: joined user 409, other workspace 404, agent 403, admin on a Super Admin invite 403 | PASS |  |  |
| AC-020 | Reports-to cannot be the user or someone in their own team (400) | PASS |  |  |
| AC-021 | manager_id is bounded (422) | PASS |  |  |
| AC-030 | Add domain: normalised, stored for this workspace, DNS guidance for SPF / DKIM / DMARC | PASS |  |  |
| AC-031 | Unique per workspace (409); another workspace may add the same name (201) | PASS |  |  |
| AC-032 | Hostname validation rejects schemes, addresses, IPs, wildcards, spaces and blanks | PASS |  |  |
| AC-033 | Only admins add domains (agent 403); any role reads the DNS records | PASS |  |  |
| AC-034 | DNS records of another workspace's domain are 404 | PASS |  |  |
| AC-035 | Check status (diagnostics scan) runs on an added domain and the statuses are refreshed | PASS |  |  |
| AC-040 | New endpoints are documented (OpenAPI) | PASS |  |  |
