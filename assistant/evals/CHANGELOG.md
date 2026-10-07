# What testing found, and what changed

Entries from building and scripted testing (`evals/smoke_tools.py`, `--security-only`):

- **Second sign-in for the same mobile failed.** The harness read the latest *delivered*
  OTP, which was the previous one while the new one was "in transit" (1-4 s). Fixed by
  waiting for a message newer than the request. Real people hit the same thing on slow
  networks; the portal's message "Invalid or expired OTP" does not say "wait for the new SMS".
- **Protected-resource metadata advertised `https://host/` as the authorization server**
  while the portal's issuer is `https://host`. Strict clients refuse the mismatch. Now
  written out exactly.
- **Assamese file names were rejected.** `secure_filename("আধাৰ.png")` strips everything,
  including the dot, so the extension check failed. Now the extension is taken first.
- **Deemed approval would block every pending application** if it required a document,
  because no row in the snapshot has one. Only the age rule is enforced there.
- **A token minted without `resource`** (audience = the portal) was accepted in the first
  version. Now refused; `ACCEPT_PORTAL_AUDIENCE=1` exists for clients that cannot send it.

## Model eval runs

Add an entry after each run of `python -m evals.run_evals`: who failed, what the transcript
showed, what you changed (tool description, server instructions, API message), and the
pass rate before and after.
