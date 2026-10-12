# UI outreach/admin - security results

PASS 9 · FAIL 0 · BLOCKED 0

| ID | Test case | Status | Category | Detail |
|---|---|---|---|---|
| UIO-SEC-01 | Account security renders for Super Admin: 2FA Off, "Set up two-factor", workspace policy card (switch disabled until own 2FA on) | PASS |  |  |
| UIO-SEC-02 | Enable flow: QR + secret shown; Cancel returns; wrong code shows error; correct TOTP shows 10 recovery codes | PASS |  |  |
| UIO-SEC-03 | Recovery codes: Download produces a file, Copy gives feedback, "I've saved them" hides them and shows the 2FA On state with remaining count | PASS |  |  |
| UIO-SEC-04 | Policy switch now enabled; turning it on requires 2FA for everyone (DB tenants.require_mfa=1); admin is forced to the security screen | PASS |  |  |
| UIO-SEC-05 | Turning the policy off releases admins again (require_mfa=0) | PASS |  |  |
| UIO-SEC-06 | New recovery codes: needs a valid current code; success replaces all codes (old code no longer works) | PASS |  |  |
| UIO-SEC-07 | Disable: requires password + code; wrong password -> error; correct -> 2FA Off in DB, secret and codes wiped | PASS |  |  |
| UIO-SEC-08 | Disable with a recovery code instead of an authenticator code works | PASS |  |  |
| UIO-SEC-09 | Security screen API failure shows an error note; Agent can open own security page | PASS |  |  |
