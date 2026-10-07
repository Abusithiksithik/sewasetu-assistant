# What the portal and its data showed (for the Director)

Snapshot `seed.sql`, 30 September 2026, 100,000 applications.

## The rolls
| Finding | Count | Why it matters |
|---|---|---|
| On the rolls (approved or deemed) though **under 60 on the day they applied** | 2,849 (2,585 by an officer, 264 deemed) | Scheme rule broken; most were 59. |
| Same name, date of birth and village, **two active pensions** on different mobiles | 25 people (50 rows) | Probable double payment. |
| IFSC **UTBI0PUR2819**: 12 characters, United Bank of India (merged into PNB, 2020) | 16,576 active | DBT to these accounts will fail; the portal's own validator rejects this code today. |
| No age-proof file on record | all 100,000 | Files were not migrated; an officer cannot re-check anyone. |
| Names in Assamese script | 300 | Rendered as "?" in the acknowledgment PDF (Helvetica). |

## The code, before it faces agents
- **Deemed approval ran at container start.** On first boot it would have approved 309
  pending applications at once, including 4 that were under 60. Deemed approval ignored
  eligibility entirely. Fixed: under-60s are escalated, not approved; audit rows are tied to
  the rows actually updated.
- **Default officer password worked** (`change-me-before-deploying`). Fixed: refused.
- **Two submissions could both pass the one-application check** (no lock). Agents retry.
  Fixed: per-mobile advisory lock, for the web form too.
- **Upload path traversal** (`../` in the filename). Fixed: one sanitised save function for all channels.
- **Signing out did not sign out**: revoking a sign-in leaves the access token valid for
  20-30 minutes; admin-issued program tokens could not be revoked at all. Fixed at the MCP
  servers (liveness check) and a revoke button for program tokens.
- **No protected-resource metadata**: MCP clients could not discover how to sign in. Each
  MCP server now publishes its own (RFC 9728) and accepts only tokens minted for it.
- **IFSC was optional**: an application without a branch code could not be paid. Now required.
- **Mixed clocks**: rules used IST, timestamps used the container clock (UTC). All containers now run in IST.
- Left as is, deliberately: OTPs are readable at `/__gateway/api/messages` (it is the simulated
  phone; testers need it); status-portal password is the date of birth (departmental circular).
