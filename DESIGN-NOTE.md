# Design note: Sewa Setu in an agentic world

**What exists.** Two MCP servers over streamable HTTP on the portal's own origin:
`/mcp/citizen` (8 tools) and `/mcp/officer` (6 tools). They sign people in through the
portal's existing OAuth server and act only through a small internal API inside the portal,
so the portal stays the system of record. `/eval-dashboard` shows 11 invented people, their
conversations, and what was checked in the database.

## The three decisions that mattered most

**1. The portal's rules are the only rules.** The MCP servers hold no logic about who may
apply. Every write goes through the portal's own `validate_application()`,
`create_application()` and file-saving code, now shared by the web form, the assistant and
the upload link. The assistant's job is to turn how people talk ("14 August 59",
"widow", "dhemaji", "3124 5678 901") into what the form expects, and to turn the portal's
refusals into sentences a 70-year-old understands. A small model with few turns cannot be
trusted to enforce eligibility; a database transaction can. The same reasoning put a lock
around submission (agents retry) and made `submit_application` safe to call twice.

**2. Two servers, two audiences, and signing out means signing out.** The citizen and
officer servers are separate OAuth protected resources. A token is accepted only if its
audience is that exact server, its role matches, and the sign-in behind it is still alive.
The last check was not in the portal: ending a sign-in from "Connected applications" left
the access token working for 20 minutes, and program tokens could never be revoked. On
shared phones and kiosks, that is the difference between "I signed out" and someone else
withdrawing your application.

**3. Irreversible steps need a person's yes, and some need an officer's reason.** Submit
(a legal declaration) and withdraw (final) take an explicit `confirmed` flag that the tool
descriptions tie to reading back the details. Approving an application with a hard flag
(under 60 when applied, a dead United Bank IFSC) is refused unless the officer gives an
override reason, which goes into the audit trail. Deemed approval no longer approves
under-60s. 2,849 such people are already on the rolls; making rubber-stamping faster would
have made that worse.

## What I would do differently with more time

Officers are one shared `admin` account, so "block officer" is a label, not a permission.
I would add per-block officer accounts and scope the officer token to a block, so Namti's
officer cannot decide Khelua's applications, and the audit trail names a person. I left it
because it changes the portal's sign-in, and the brief asked to keep the portal as it is
where possible.
