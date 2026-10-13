# Endpoint coverage (from /metrics http_requests_total deltas)


- Endpoints in OpenAPI: **313**
- Sweep only: hit **311/313 = 99.4%**; hit with 2xx **286/313 = 91.4%**
- Since API container start (all suites + sweep): hit **311/313 = 99.4%**; 2xx **286/313 = 91.4%**

| Method | Path | Guard | Sweep hit | Sweep 2xx | Checks pass/fail | Since-start hit | Since-start 2xx | Reason if not 2xx in sweep |
|---|---|---|---|---|---|---|---|---|
| GET | `/` | PUBLIC | Y | Y | 1/1 | Y | Y |  |
| GET | `/accounts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| POST | `/accounts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 26/1 | Y | Y |  |
| POST | `/accounts/backfill` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 11/1 | Y | Y |  |
| GET | `/accounts/deleted` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 15/1 | Y | Y |  |
| DELETE | `/accounts/{account_id}` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| GET | `/accounts/{account_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 39/1 | Y | Y |  |
| PATCH | `/accounts/{account_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 30/1 | Y | Y |  |
| POST | `/accounts/{account_id}/restore` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 15/1 | Y | Y |  |
| GET | `/api/admin/stats` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/api/admin/tenants` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| POST | `/api/admin/tenants` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/api/admin/tenants/{tenant_id}` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| PUT | `/api/admin/tenants/{tenant_id}/status` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/api/admin/users` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| POST | `/api/auth/forgot-password` | PUBLIC | Y | Y | 6/1 | Y | Y |  |
| GET | `/api/auth/google` | PUBLIC | Y | N | 1/0 | Y | N | environment (OAuth provider not configured) |
| POST | `/api/auth/google/callback` | PUBLIC | Y | N | 8/0 | Y | N | environment (OAuth provider not configured) |
| POST | `/api/auth/login` | PUBLIC | Y | Y | 11/0 | Y | Y |  |
| POST | `/api/auth/logout` | PUBLIC | Y | Y | 1/0 | Y | Y |  |
| POST | `/api/auth/magic-login` | PUBLIC | Y | Y | 7/0 | Y | Y |  |
| GET | `/api/auth/me` | any-auth-user | Y | Y | 16/1 | Y | Y |  |
| PUT | `/api/auth/me` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| PUT | `/api/auth/me/password` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| POST | `/api/auth/mfa/disable` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| POST | `/api/auth/mfa/enable` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| POST | `/api/auth/mfa/recovery-codes` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| POST | `/api/auth/mfa/setup` | any-auth-user | Y | Y | 9/1 | Y | Y |  |
| PUT | `/api/auth/mfa/tenant-policy` | roles=SUPER_ADMIN | Y | Y | 9/1 | Y | Y |  |
| POST | `/api/auth/mfa/verify` | PUBLIC | Y | Y | 11/0 | Y | Y |  |
| POST | `/api/auth/refresh` | PUBLIC | Y | Y | 2/0 | Y | Y |  |
| POST | `/api/auth/register` | PUBLIC | Y | Y | 15/0 | Y | Y |  |
| POST | `/api/auth/reset-password` | PUBLIC | Y | Y | 10/1 | Y | Y |  |
| GET | `/api/auth/session` | any-auth-user | Y | Y | 15/1 | Y | Y |  |
| GET | `/api/company-profiles` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| POST | `/api/company-profiles` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 25/0 | Y | Y |  |
| GET | `/api/company-profiles/sender-identity` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| PUT | `/api/company-profiles/sender-identity` | roles=ADMIN,SUPER_ADMIN | Y | Y | 20/0 | Y | Y |  |
| DELETE | `/api/company-profiles/{profile_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/api/company-profiles/{profile_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| PUT | `/api/company-profiles/{profile_id}` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 27/0 | Y | Y |  |
| GET | `/api/users` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| POST | `/api/users/accept-invite` | PUBLIC | Y | Y | 13/1 | Y | Y |  |
| GET | `/api/users/invitations` | roles=ADMIN,SUPER_ADMIN | Y | Y | 12/1 | Y | Y |  |
| DELETE | `/api/users/invitations/{invitation_id}` | roles=ADMIN,SUPER_ADMIN perm=manage_team | Y | Y | 16/1 | Y | Y |  |
| POST | `/api/users/invite` | roles=ADMIN,SUPER_ADMIN perm=manage_team | Y | Y | 20/1 | Y | Y |  |
| DELETE | `/api/users/{user_id}` | roles=ADMIN,SUPER_ADMIN perm=manage_team | Y | Y | 16/1 | Y | Y |  |
| DELETE | `/api/users/{user_id}/permanent` | roles=SUPER_ADMIN perm=manage_team | Y | Y | 16/1 | Y | Y |  |
| DELETE | `/api/users/{user_id}/permissions` | roles=SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| GET | `/api/users/{user_id}/permissions` | roles=ADMIN,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PUT | `/api/users/{user_id}/permissions` | roles=SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| PUT | `/api/users/{user_id}/reactivate` | roles=ADMIN,SUPER_ADMIN perm=manage_team | Y | Y | 15/1 | Y | Y |  |
| PUT | `/api/users/{user_id}/role` | roles=SUPER_ADMIN perm=manage_team | Y | Y | 22/1 | Y | Y |  |
| GET | `/automation-rules/` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 20/0 | Y | Y |  |
| POST | `/automation-rules/` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 30/0 | Y | Y |  |
| DELETE | `/automation-rules/{rule_id}` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| PUT | `/automation-rules/{rule_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 25/0 | Y | Y |  |
| DELETE | `/campaign-drafts/` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 9/1 | Y | Y |  |
| GET | `/campaign-drafts/` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| POST | `/campaign-drafts/` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/0 | Y | Y |  |
| GET | `/campaign-drafts/current` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| DELETE | `/campaign-drafts/{draft_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/campaign-drafts/{draft_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| POST | `/campaign-wizard/update-template` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| GET | `/campaigns` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/0 | Y | Y |  |
| POST | `/campaigns` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 34/0 | Y | Y |  |
| GET | `/campaigns/bounced-prospects/revalidation-candidates` | roles=ADMIN,SUPER_ADMIN | Y | Y | 12/1 | Y | Y |  |
| GET | `/campaigns/lists` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PATCH | `/campaigns/prospects/{prospect_id}/revalidate` | roles=ADMIN,SUPER_ADMIN | Y | Y | 21/0 | Y | Y |  |
| POST | `/campaigns/schedules/previews` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| DELETE | `/campaigns/sequences/{sequence_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 16/0 | Y | Y |  |
| PUT | `/campaigns/sequences/{sequence_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 31/0 | Y | Y |  |
| POST | `/campaigns/wizard-creations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 27/1 | Y | Y |  |
| POST | `/campaigns/wizard-executions` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 27/1 | Y | Y |  |
| DELETE | `/campaigns/{campaign_id}` | roles=ADMIN,SUPER_ADMIN perm=manage_campaigns | Y | Y | 16/0 | Y | Y |  |
| GET | `/campaigns/{campaign_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| PUT | `/campaigns/{campaign_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 35/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/activations` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 23/2 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/analytics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/0 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/audit` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/content/stub` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/enrollments` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 31/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/enrollments/bulk` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 28/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/generations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 33/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/launch` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 16/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/messages/{message_id}/mark-replied` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| PATCH | `/campaigns/{campaign_id}/messages/{message_id}/reschedule` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/metrics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | N | N | 14/1 | N | N | not measurable: the metrics middleware does not count paths ending in /metrics (the sweep did get 2xx, see HAPPY case) |
| POST | `/campaigns/{campaign_id}/pause` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 19/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/personas/{persona_type}/generations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 31/1 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/prospects` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/prospects` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 25/1 | Y | Y |  |
| DELETE | `/campaigns/{campaign_id}/prospects/{prospect_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 16/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/prospects/{prospect_id}/mark-opened` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | N | 12/1 | Y | N | see FINDINGS |
| POST | `/campaigns/{campaign_id}/prospects/{prospect_id}/mark-replied` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | N | 13/1 | Y | N | see FINDINGS |
| POST | `/campaigns/{campaign_id}/resume` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 16/0 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/schedule` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/schedule/reschedule-date` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/send-now` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 15/1 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/sequences` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/sequences` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_campaigns | Y | Y | 35/0 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/sequences/{step}/emails` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/campaigns/{campaign_id}/templates` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/templates/approve-all` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/templates/{template_id}/approve` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| POST | `/campaigns/{campaign_id}/templates/{template_id}/regenerate` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 25/0 | Y | Y |  |
| GET | `/connections` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/2 | Y | Y |  |
| DELETE | `/connections/{connection_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/connections/{connection_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| POST | `/connections/{connection_id}/sync` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| POST | `/connections/{provider}/start` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | N | 12/0 | Y | N | environment (OAuth provider not configured) |
| GET | `/contacts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| POST | `/contacts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 32/4 | Y | Y |  |
| DELETE | `/contacts/activities/{activity_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/contacts/activities/{activity_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 26/1 | Y | Y |  |
| GET | `/contacts/board` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| POST | `/contacts/bulk` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 31/1 | Y | Y |  |
| GET | `/contacts/deleted` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 21/1 | Y | Y |  |
| GET | `/contacts/duplicates` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 18/1 | Y | Y |  |
| GET | `/contacts/export` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 17/0 | Y | Y |  |
| GET | `/contacts/facets` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/contacts/fields` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/contacts/fields` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 24/1 | Y | Y |  |
| DELETE | `/contacts/fields/{field_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| PATCH | `/contacts/fields/{field_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| POST | `/contacts/merge` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/contacts/meta` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/contacts/owners` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/contacts/restore` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/1 | Y | Y |  |
| DELETE | `/contacts/{prospect_id}` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| GET | `/contacts/{prospect_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 39/1 | Y | Y |  |
| PATCH | `/contacts/{prospect_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 31/1 | Y | Y |  |
| POST | `/contacts/{prospect_id}/activities` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 27/1 | Y | Y |  |
| GET | `/contacts/{prospect_id}/history` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/1 | Y | Y |  |
| GET | `/contacts/{prospect_id}/timeline` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 21/1 | Y | Y |  |
| GET | `/conversations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/0 | Y | Y |  |
| GET | `/conversations/{conversation_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| POST | `/conversations/{conversation_id}/reply` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/0 | Y | Y |  |
| GET | `/deliverability/alert-preferences` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| PUT | `/deliverability/alert-preferences` | roles=ADMIN,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/deliverability/alerts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/0 | Y | Y |  |
| GET | `/deliverability/dashboard-alerts` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| GET | `/deliverability/domains` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| DELETE | `/deliverability/domains/{domain_name}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| POST | `/deliverability/domains/{domain_name}/scans` | roles=ADMIN,SUPER_ADMIN | Y | Y | 12/1 | Y | Y |  |
| GET | `/deliverability/domains/{domain_name}/sent-log` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| GET | `/deliverability/integrations/events` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/0 | Y | Y |  |
| POST | `/deliverability/integrations/ingest` | roles=ADMIN,PLATFORM_ADMIN,SUPER_ADMIN | Y | N | 21/1 | Y | N | see FINDINGS |
| GET | `/deliverability/integrations/metrics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | N | N | 22/0 | N | N | not measurable: the metrics middleware does not count paths ending in /metrics (the sweep did get 2xx, see HAPPY case) |
| GET | `/deliverability/integrations/status` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/deliverability/snapshots` | roles=ADMIN,SUPER_ADMIN | Y | Y | 9/1 | Y | Y |  |
| GET | `/deliverability/statistics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/0 | Y | Y |  |
| GET | `/deliverability/statistics/account` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| POST | `/emails/compliance-checks` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 20/0 | Y | Y |  |
| GET | `/emails/cta-options/{purpose}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| POST | `/emails/generations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/0 | Y | Y |  |
| POST | `/emails/sequences/generations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 26/0 | Y | Y |  |
| POST | `/emails/validations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| GET | `/health` | PUBLIC | Y | Y | 1/1 | Y | Y |  |
| GET | `/health/ready` | PUBLIC | Y | Y | 1/1 | Y | Y |  |
| GET | `/imports` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 12/7 | Y | Y |  |
| POST | `/imports` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/1 | Y | Y |  |
| POST | `/imports/preview` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/1 | Y | Y |  |
| GET | `/imports/{import_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/imports/{import_id}/errors.csv` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| GET | `/inboxes` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| POST | `/inboxes` | roles=ADMIN,SUPER_ADMIN perm=manage_inboxes | Y | Y | 29/0 | Y | Y |  |
| GET | `/inboxes/oauth/microsoft/status` | roles=ADMIN,SUPER_ADMIN | Y | Y | 12/1 | Y | Y |  |
| GET | `/inboxes/ses/quota` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/inboxes/warmup/overview` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| POST | `/inboxes/warmup/run` | roles=ADMIN,SUPER_ADMIN perm=manage_inboxes | Y | Y | 11/1 | Y | Y |  |
| DELETE | `/inboxes/{inbox_id}` | roles=ADMIN,SUPER_ADMIN perm=manage_inboxes | Y | Y | 16/0 | Y | Y |  |
| GET | `/inboxes/{inbox_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| PUT | `/inboxes/{inbox_id}` | roles=ADMIN,SUPER_ADMIN perm=manage_inboxes | Y | Y | 32/0 | Y | Y |  |
| POST | `/inboxes/{inbox_id}/oauth/microsoft/start` | roles=ADMIN,SUPER_ADMIN perm=manage_inboxes | Y | N | 15/0 | Y | N | environment (OAuth provider not configured) |
| POST | `/inboxes/{inbox_id}/sync` | roles=ADMIN,SUPER_ADMIN | Y | N | 17/0 | Y | N | environment/defect (AI/IMAP dependency or product 500, see FINDINGS) |
| GET | `/inboxes/{inbox_id}/test-imap` | roles=ADMIN,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| POST | `/inboxes/{inbox_id}/test-smtp` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| GET | `/inboxes/{inbox_id}/warmup` | roles=ADMIN,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| GET | `/leads` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| POST | `/leads` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| GET | `/leads/board` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| POST | `/leads/from-message` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | N | 19/1 | Y | N | see FINDINGS |
| GET | `/leads/meta` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/leads/reply-suggestions` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/leads/sql-queue` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| DELETE | `/leads/{lead_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/leads/{lead_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 39/1 | Y | Y |  |
| PATCH | `/leads/{lead_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 32/1 | Y | Y |  |
| POST | `/leads/{lead_id}/convert` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| GET | `/lists` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| POST | `/lists` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| POST | `/lists/preview` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| DELETE | `/lists/{list_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| GET | `/lists/{list_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PATCH | `/lists/{list_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| POST | `/lists/{list_id}/members` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| POST | `/lists/{list_id}/members/remove` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| GET | `/notifications` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| PUT | `/notifications/preferences` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/notifications/read` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/opportunities` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| POST | `/opportunities` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| GET | `/opportunities/board` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 20/1 | Y | Y |  |
| DELETE | `/opportunities/{opportunity_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/opportunities/{opportunity_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 39/1 | Y | Y |  |
| PATCH | `/opportunities/{opportunity_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 33/1 | Y | Y |  |
| POST | `/opportunities/{opportunity_id}/activities` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| GET | `/opportunities/{opportunity_id}/timeline` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/persona-blueprints` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 17/0 | Y | Y |  |
| POST | `/persona-blueprints` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/persona-blueprints/{persona_type}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 13/0 | Y | Y |  |
| GET | `/personas` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/pipeline/revenue` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/products` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/1 | Y | Y |  |
| POST | `/products` | roles=ADMIN,SUPER_ADMIN | Y | Y | 20/1 | Y | Y |  |
| PATCH | `/products/{product_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 24/1 | Y | Y |  |
| GET | `/proposals` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| POST | `/proposals` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| DELETE | `/proposals/{proposal_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/proposals/{proposal_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 27/1 | Y | Y |  |
| GET | `/proposals/{proposal_id}/lines` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PUT | `/proposals/{proposal_id}/lines` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/proposals/{proposal_id}/pdf` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| GET | `/prospect-lists` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/0 | Y | Y |  |
| DELETE | `/prospect-lists/{list_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| GET | `/prospect-lists/{list_id}/prospects` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 21/0 | Y | Y |  |
| PATCH | `/prospect-lists/{list_id}/prospects/{prospect_id}/notes` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 22/1 | Y | Y |  |
| POST | `/prospect-lists/{list_id}/revalidations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 15/1 | Y | Y |  |
| POST | `/prospect-lists/{list_id}/segmentations/previews` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| POST | `/prospects/classifications/batch` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/0 | Y | Y |  |
| POST | `/prospects/classifications/test` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 21/0 | Y | Y |  |
| GET | `/prospects/stats` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/0 | Y | Y |  |
| DELETE | `/prospects/{prospect_id}` | roles=ADMIN,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 16/1 | Y | Y |  |
| PATCH | `/prospects/{prospect_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 29/1 | Y | Y |  |
| GET | `/reports/campaign-comparison` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=view_analytics | Y | Y | 24/0 | Y | Y |  |
| GET | `/reports/custom` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/reports/custom` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/reports/custom/meta` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| POST | `/reports/custom/run` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 25/1 | Y | Y |  |
| DELETE | `/reports/custom/{report_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| GET | `/reports/custom/{report_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PATCH | `/reports/custom/{report_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| GET | `/reports/dashboard-summary` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=view_analytics | Y | Y | 24/0 | Y | Y |  |
| GET | `/reports/export/campaign-comparison` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=export_data | Y | Y | 19/0 | Y | Y |  |
| GET | `/reports/export/deliverability` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=export_data | Y | Y | 15/0 | Y | Y |  |
| GET | `/reports/export/global-analytics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=export_data | Y | Y | 19/0 | Y | Y |  |
| GET | `/reports/export/global-analytics/docx` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=export_data | Y | Y | 19/0 | Y | Y |  |
| GET | `/reports/export/prospects/{campaign_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=export_data | Y | Y | 16/0 | Y | Y |  |
| GET | `/reports/global-analytics` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=view_analytics | Y | Y | 21/0 | Y | Y |  |
| GET | `/reports/provider-performance` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=view_analytics | Y | Y | 20/0 | Y | Y |  |
| GET | `/sales-reports/campaign-roi` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales-reports/daily-limit` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| GET | `/sales-reports/dashboard` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales-reports/forecast` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| GET | `/sales-reports/funnel` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales-reports/leaderboard` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| GET | `/sales-reports/leads` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales-reports/pipeline` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales-reports/targets` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| GET | `/sales-reports/team-performance` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 19/1 | Y | Y |  |
| GET | `/sales/settings` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PUT | `/sales/settings` | roles=ADMIN,SUPER_ADMIN | Y | Y | 13/1 | Y | Y |  |
| GET | `/sales/stages` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/1 | Y | Y |  |
| POST | `/sales/stages` | roles=ADMIN,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| PATCH | `/sales/stages/{stage_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 27/1 | Y | Y |  |
| GET | `/sales/targets` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| PUT | `/sales/targets` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 24/1 | Y | Y |  |
| POST | `/scheduler/process` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| POST | `/scheduler/process/sync` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| POST | `/scheduler/start` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/scheduler/status` | roles=ADMIN,AGENT,MANAGER,PLATFORM_ADMIN,SUPER_ADMIN | Y | Y | 9/1 | Y | Y |  |
| POST | `/scheduler/stop` | roles=PLATFORM_ADMIN | Y | N | 8/0 | Y | N | platform-admin-only (no operator user; only 401/403 checked) |
| GET | `/search` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 21/1 | Y | Y |  |
| GET | `/tasks` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 28/1 | Y | Y |  |
| POST | `/tasks` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 29/1 | Y | Y |  |
| DELETE | `/tasks/{task_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/tasks/{task_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 33/1 | Y | Y |  |
| GET | `/team/hierarchy` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PUT | `/team/hierarchy/{user_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 23/1 | Y | Y |  |
| GET | `/template-library` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| POST | `/template-library` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 20/1 | Y | Y |  |
| DELETE | `/template-library/{template_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/template-library/{template_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 24/1 | Y | Y |  |
| POST | `/template-library/{template_id}/render` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| POST | `/templates` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_templates | Y | Y | 32/0 | Y | Y |  |
| DELETE | `/templates/{template_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_templates | Y | N | 14/1 | Y | N | see FINDINGS |
| GET | `/templates/{template_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/1 | Y | Y |  |
| PUT | `/templates/{template_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_templates | Y | Y | 27/0 | Y | Y |  |
| POST | `/templates/{template_id}/approve` | roles=ADMIN,MANAGER,SUPER_ADMIN | Y | Y | 21/0 | Y | Y |  |
| GET | `/templates/{template_id}/attachments` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| POST | `/templates/{template_id}/attachments` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_templates | Y | Y | 24/0 | Y | Y |  |
| DELETE | `/templates/{template_id}/attachments/{attachment_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_templates | Y | N | 14/1 | Y | N | see FINDINGS |
| GET | `/templates/{template_id}/attachments/{attachment_id}/download` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/0 | Y | Y |  |
| POST | `/templates/{template_id}/regenerations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | N | 33/0 | Y | N | environment/defect (AI/IMAP dependency or product 500, see FINDINGS) |
| GET | `/templates/{template_id}/versions` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 15/0 | Y | Y |  |
| GET | `/tracking/click/{message_id}` | PUBLIC | Y | Y | 8/0 | Y | Y |  |
| GET | `/tracking/open/{message_id}.png` | PUBLIC | Y | Y | 4/0 | Y | Y |  |
| GET | `/tracking/unsubscribe/{message_id}` | PUBLIC | Y | Y | 4/0 | Y | Y |  |
| POST | `/tracking/unsubscribe/{message_id}` | PUBLIC | Y | Y | 3/0 | Y | Y |  |
| POST | `/uploads/validations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 19/1 | Y | Y |  |
| POST | `/uploads/{upload_id}/confirmations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 30/1 | Y | Y |  |
| POST | `/uploads/{upload_id}/revalidations` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN perm=manage_prospects | Y | Y | 19/3 | Y | Y |  |
| GET | `/views` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 18/1 | Y | Y |  |
| POST | `/views` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 20/1 | Y | Y |  |
| DELETE | `/views/{view_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/views/{view_id}` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 24/1 | Y | Y |  |
| POST | `/webhooks/ses/notifications` | PUBLIC | Y | Y | 1/1 | Y | Y |  |
| GET | `/workflow/meta` | roles=ADMIN,AGENT,MANAGER,SUPER_ADMIN | Y | Y | 16/2 | Y | Y |  |
| GET | `/workflow/rules` | roles=ADMIN,SUPER_ADMIN | Y | Y | 12/1 | Y | Y |  |
| POST | `/workflow/rules` | roles=ADMIN,SUPER_ADMIN | Y | Y | 22/1 | Y | Y |  |
| DELETE | `/workflow/rules/{rule_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 14/1 | Y | Y |  |
| PATCH | `/workflow/rules/{rule_id}` | roles=ADMIN,SUPER_ADMIN | Y | Y | 26/1 | Y | Y |  |
