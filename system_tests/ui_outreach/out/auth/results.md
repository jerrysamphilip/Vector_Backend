# UI outreach/admin - auth results

PASS 43 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-AUTH-01 | Login page renders email/password fields, Forgot password link, no problems | PASS |  |  |
| UIO-AUTH-02 | Empty submit shows "Email and password are required" and sends no request | PASS |  |  |
| UIO-AUTH-03 | Wrong password shows "Invalid email or password", stays on /login, no cookie session | PASS |  |  |
| UIO-AUTH-04 | Password show/hide toggle switches input type | PASS |  |  |
| UIO-AUTH-05 | Forgot password link navigates to /forgot-password | PASS |  |  |
| UIO-AUTH-06 | Suspended account: login shows "Account is suspended or inactive" | PASS |  |  |
| UIO-AUTH-07 | Locked after 10 failed attempts: UI shows "Too many failed sign-in attempts" (429), correct password also refused | PASS |  |  |
| UIO-AUTH-08 | Login API 500 degrades to a readable message (no crash / [object Object]) | PASS |  |  |
| UIO-AUTH-09 | Successful login lands on Home (/app/dashboard) with session cookies and stored user | PASS |  |  |
| UIO-AUTH-10 | Authenticated user opening /login is redirected to the app (PublicRoute) | PASS |  |  |
| UIO-AUTH-11 | Logout via profile menu returns to /login, clears stored user + cookies; Back does not reopen the app | PASS |  |  |
| UIO-AUTH-20 | ForgotPassword renders; empty email shows Email is required and sends nothing | PASS |  |  |
| UIO-AUTH-21 | Known email: generic success message and a reset-token row is stored (hash only) | PASS |  |  |
| UIO-AUTH-22 | Unknown email shows the same generic message (no enumeration) and stores nothing | PASS |  |  |
| UIO-AUTH-23 | Forgot password 500 -> readable error, no crash | PASS |  |  |
| UIO-AUTH-30 | ResetPassword without token: submitting shows "Invalid or missing reset token" | PASS |  |  |
| UIO-AUTH-31 | Short password rejected client-side (<8) | PASS |  |  |
| UIO-AUTH-32 | Mismatched confirmation rejected | PASS |  |  |
| UIO-AUTH-33 | Valid token: success message, redirect to /login, token marked used, new password works / old does not | PASS |  |  |
| UIO-AUTH-34 | Reusing the token shows "Invalid or expired reset token" | PASS |  |  |
| UIO-AUTH-35 | Expired token shows "Reset token has expired" | PASS |  |  |
| UIO-AUTH-40 | AcceptInvite without token: "Invalid or missing invitation token" | PASS |  |  |
| UIO-AUTH-41 | Validation: names required, password >= 8, confirmation matches | PASS |  |  |
| UIO-AUTH-42 | Invalid token -> server message shown (404 "Invalid or expired invitation") | PASS |  |  |
| UIO-AUTH-43 | Expired invitation -> "Invitation has expired" and invitation row EXPIRED | PASS |  |  |
| UIO-AUTH-44 | Valid invitation: account created (role/tenant from invite), redirect to /login, can then log in | PASS |  |  |
| UIO-AUTH-45 | Re-using the accepted invitation shows an error, no duplicate user | PASS |  |  |
| UIO-AUTH-50 | MagicLogin without token: "Missing magic login token" | PASS |  |  |
| UIO-AUTH-51 | MagicLogin with unknown token shows error (not blank) | PASS |  |  |
| UIO-AUTH-52 | Expired magic token shows "has expired" | PASS |  |  |
| UIO-AUTH-53 | Valid magic link on INVITED user: signs in, activates user, goes to /set-password; token single use | PASS |  |  |
| UIO-AUTH-54 | SetPassword validation (short, mismatch) and wrong temporary password error | PASS |  |  |
| UIO-AUTH-55 | SetPassword success: confirmation then Home; new password works | PASS |  |  |
| UIO-AUTH-56 | Used magic token cannot be replayed | PASS |  |  |
| UIO-AUTH-60 | MFA user: correct password shows the second step (no session yet) | PASS |  |  |
| UIO-AUTH-61 | Short code rejected client-side; wrong code shows server message | PASS |  |  |
| UIO-AUTH-62 | Correct TOTP completes sign-in to Home | PASS |  |  |
| UIO-AUTH-63 | Recovery code path signs in once; the same recovery code is then refused | PASS |  |  |
| UIO-AUTH-64 | Cancel on the second step returns to the email/password form | PASS |  |  |
| UIO-AUTH-65 | MFA user via magic link also gets the second step | PASS |  |  |
| UIO-AUTH-70 | Deleting only the access cookie: next navigation silently refreshes (stays signed in) | PASS |  |  |
| UIO-AUTH-71 | Deleting all session cookies redirects to /login and clears stored user | PASS |  |  |
| UIO-AUTH-72 | Unauthenticated deep link /app/campaigns redirects to /login, then back to the target after login | PASS |  |  |
