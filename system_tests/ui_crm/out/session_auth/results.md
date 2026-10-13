# ui_crm session_auth results

PASS 21 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIF-SESS-01 | Signed out: /app/dashboard redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-02 | Signed out: /app/contacts redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-03 | Signed out: /app/contacts/06f1db77-bcb5-41f2-a327-ac1250e3b323 redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-04 | Signed out: /app/deals redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-05 | Signed out: /app/templates redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-06 | Signed out: /app/settings redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-07 | Signed out: /app/webhooks redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-08 | Signed out: /admin redirects to /login and shows no workspace data | PASS |  |  |
| UIF-SESS-11 | Signing in after the redirect returns to the page that was requested (/app/deals) | PASS |  |  |
| UIF-SESS-12 | API without credentials -> 401 (no cookie, garbage bearer, tampered token) | PASS |  |  |
| UIF-SESS-13 | Access cookie expires mid-use: navigating to Contacts refreshes silently (one POST /auth/refresh 200), page stays and shows data | PASS |  |  |
| UIF-SESS-14 | Access cookie expires while a form is open: submit is retried after the refresh with the new X-CSRF-Token; contact saved, no login page | PASS |  |  |
| UIF-SESS-15 | Several parallel 401s (full reload of a contact record without the access cookie) share ONE refresh; the record renders | PASS |  |  |
| UIF-SESS-16 | CSRF: a cookie-authenticated POST without / with a wrong X-CSRF-Token -> 403, nothing created; GET needs no token | PASS |  |  |
| UIF-SESS-17 | Signed-in user opening /login is sent to /app/dashboard | PASS |  |  |
| UIF-SESS-18 | Session fully expired (access cookie gone, refresh token revoked): next action ends on /login with the stored user cleared | PASS |  |  |
| UIF-SESS-19 | After a session expiry, signing in again returns to the page the user was on (not the dashboard) | PASS |  |  |
| UIF-SESS-20 | Sign out (profile menu): /login, stored user cleared, auth cookies removed, the refresh token is revoked | PASS |  |  |
| UIF-SESS-21 | Back button after sign-out does not show the contact record (stays on / returns to /login) | PASS |  |  |
| UIF-SESS-22 | The access token captured before sign-out no longer works afterwards | PASS |  |  |
| UIF-SESS-23 | Sign out in one tab: the other tab of the same browser ends on /login at its next action and shows no data | PASS |  |  |
