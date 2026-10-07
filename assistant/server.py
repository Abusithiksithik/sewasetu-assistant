"""Sewa Setu assistant: two MCP servers over streamable HTTP, and the eval dashboard.

  /mcp/citizen   tools for an applicant (token: role=citizen, aud=<base>/mcp/citizen)
  /mcp/officer   tools for a block officer (token: role=officer, aud=<base>/mcp/officer)
  /.well-known/oauth-protected-resource/mcp/{citizen,officer}   RFC 9728 metadata
  /eval-dashboard  personas, conversations, checks, results

They are separate FastMCP servers with separate audiences, so a citizen's assistant
never even lists the officer's tools. Neither touches the database: they call the
portal's internal API, which reuses the portal's own validation and write paths.
"""

import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from pydantic import Field
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from auth import PortalTokenVerifier, ISSUER, PORTAL_INTERNAL, INTERNAL_KEY
import dashboard

BASE = ISSUER  # the public address; MCP servers live on the same origin as the portal
CITIZEN_URL = BASE + "/mcp/citizen"
OFFICER_URL = BASE + "/mcp/officer"


async def portal(method: str, path: str, role: str, **kw):
    tok = get_access_token()
    if tok is None:
        return {"problem": "Not signed in."}
    headers = {"X-Internal-Key": INTERNAL_KEY, "X-Acting-Subject": getattr(tok, "subject", ""),
               "X-Client-Name": "assistant client %s" % tok.client_id[:40]}
    if not headers["X-Acting-Subject"].startswith(role + ":"):
        return {"problem": "This sign-in is not allowed to do that."}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, PORTAL_INTERNAL + path, headers=headers, **kw)
    try:
        return r.json()
    except ValueError:
        return {"problem": "The portal did not answer (HTTP %d). Try again in a minute." % r.status_code}


# ---------------------------------------------------------------------------
# Citizen MCP
# ---------------------------------------------------------------------------

CITIZEN_GUIDE = """You are helping an older person in Purvanchal with the state Old Age Pension
(Sewa Setu portal). The person is signed in with their mobile number; you can only see and act on
that mobile's applications.

How to help:
- Speak the person's language (Assamese, Hindi, English, a mix). Short sentences. One or two
  questions at a time. Never use words like JSON, draft, field, API.
- Start with my_pension_status. If they already have an application, talk about that first; a
  mobile number can hold only one active application.
- Ask their date of birth early and call check_eligibility. If they are under 60, tell them the
  date they can apply and stop; do not save or submit anything.
- Save details with save_application_details as soon as you hear them (call it many times; any
  order). Needed: full name as on the age proof, date of birth, gender, marital status, village,
  block, bank account number, IFSC. Read back numbers digit by digit.
- Never guess or invent a date of birth, account number or IFSC. If unsure, ask. If the IFSC is
  an old United Bank (UTBI...) code, explain it no longer works and ask for the new PNB code from
  the passbook, the bank SMS, or the branch.
- Age proof: if you can read the file, use attach_age_proof; otherwise give get_upload_link to the
  person or a family member.
- Before submitting: call review_application, read every detail back, read the declaration, and
  ask "Shall I submit?". Call submit_application(confirmed=true) only after a clear yes. If they
  want to wait or ask someone first, do not submit; the details stay saved for 2 hours of sign-in.
- Withdraw only a PENDING application, only after explaining that it is final and getting a clear yes.
- Tell them the application number and the date by which the officer must decide."""

DECLARATION = ("I hereby declare that the information furnished above is true to the best of my knowledge "
               "and belief. I understand that furnishing false information is punishable under applicable "
               "law and will result in cancellation of pension.")

citizen = FastMCP(
    "Sewa Setu - Old Age Pension (for applicants)",
    instructions=CITIZEN_GUIDE,
    host="0.0.0.0", stateless_http=True, json_response=True,
    streamable_http_path="/mcp/citizen",
    token_verifier=PortalTokenVerifier("citizen", CITIZEN_URL),
    auth=AuthSettings(issuer_url=BASE, resource_server_url=CITIZEN_URL, required_scopes=["citizen"]),
)


@citizen.tool()
async def my_pension_status() -> dict:
    """Start here. Shows the signed-in mobile number, every pension application on it with its status
    in plain words (and the officer's reason if rejected), any details saved so far, and the scheme's
    last date."""
    return await portal("GET", "/internal/api/citizen/overview", "citizen")


@citizen.tool()
async def check_eligibility(
    date_of_birth: Annotated[str, Field(description="As the person says it, e.g. 10/04/1957, 1957-04-10, 10 April 1957")],
) -> dict:
    """Check the age rule (60 or above today). If under 60, returns the date they turn 60 and whether
    that is before the scheme window closes. Does not save anything."""
    return await portal("GET", "/internal/api/eligibility", "citizen", params={"dob": date_of_birth})


@citizen.tool()
async def save_application_details(
    applicant_name: Annotated[str | None, Field(description="Full name exactly as on the age proof; any script")] = None,
    date_of_birth: Annotated[str | None, Field(description="Day, month, year; e.g. 10/04/1957")] = None,
    gender: Annotated[str | None, Field(description="Male, Female or Other")] = None,
    marital_status: Annotated[str | None, Field(description="Married, Unmarried or Widowed")] = None,
    late_husband_name: Annotated[str | None, Field(description="Only for widows; optional")] = None,
    village: Annotated[str | None, Field(description="Village or town")] = None,
    block: Annotated[str | None, Field(description="One of: Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti, Khelua")] = None,
    bank_account: Annotated[str | None, Field(description="Account number, digits only, 9-18 digits")] = None,
    ifsc: Annotated[str | None, Field(description="11-character bank branch code from the passbook")] = None,
) -> dict:
    """Save any of the application details you have learned (only pass what you know; call again to add
    or correct). Returns everything saved so far, the problems still to fix, and ready_to_submit."""
    body = {"applicant_name": applicant_name, "dob": date_of_birth, "gender": gender,
            "marital_status": marital_status, "husband_name": late_husband_name, "village": village,
            "block": block, "bank_account": bank_account, "ifsc": ifsc}
    return await portal("PATCH", "/internal/api/citizen/draft", "citizen",
                        json={k: v for k, v in body.items() if v not in (None, "")})


@citizen.tool()
async def attach_age_proof(
    filename: Annotated[str, Field(description="File name with extension: .jpg, .jpeg, .png or .pdf")],
    content_base64: Annotated[str, Field(description="The file's bytes, base64-encoded (max 5 MB)")],
) -> dict:
    """Attach the age-proof document (Aadhaar, birth certificate, voter ID, school certificate) when you
    can read the file yourself. Otherwise use get_upload_link."""
    return await portal("POST", "/internal/api/citizen/draft/document", "citizen",
                        json={"filename": filename, "content_base64": content_base64})


@citizen.tool()
async def get_upload_link() -> dict:
    """A one-time web link (valid 2 hours) where the person or a family member can upload a photo of the
    age proof from a phone. Give them the link; then call review_application to see it attached."""
    return await portal("POST", "/internal/api/citizen/upload-link", "citizen")


@citizen.tool()
async def review_application() -> dict:
    """Everything that would be submitted, the problems left, and the declaration to read to the person
    before asking for their yes."""
    o = await portal("GET", "/internal/api/citizen/overview", "citizen")
    if "problem" in o:
        return o
    return {"draft": o.get("draft") or "Nothing saved yet.", "active_application": o.get("active_application"),
            "declaration_to_read_aloud": DECLARATION}


@citizen.tool()
async def submit_application(
    confirmed: Annotated[bool, Field(description="True only after the person heard the details and the declaration and clearly said yes")],
) -> dict:
    """Submit the saved application. Safe to retry: it never creates two applications."""
    if not confirmed:
        return {"submitted": False, "problem": "Read the details and declaration to the person and get a clear yes first."}
    return await portal("POST", "/internal/api/citizen/submit", "citizen")


@citizen.tool()
async def withdraw_application(
    application_no: Annotated[str, Field(description="e.g. SSP26123456")],
    confirmed: Annotated[bool, Field(description="True only after the person understood withdrawal is final and said yes")],
) -> dict:
    """Withdraw a PENDING application (for example to correct a mistake and apply again). Final."""
    if not confirmed:
        return {"withdrawn": False, "problem": "Explain that withdrawal is final and get a clear yes first."}
    return await portal("POST", "/internal/api/citizen/withdraw", "citizen", json={"application_no": application_no})


# ---------------------------------------------------------------------------
# Officer MCP
# ---------------------------------------------------------------------------

OFFICER_GUIDE = """You are assisting a block officer of the Department of Social Welfare with Old Age
Pension applications. Applications not decided within 15 days stand approved under the RTPS Act,
so work oldest first. Every decision is recorded in the audit trail under the officer's name and
the applicant gets an SMS.

- Open an application (open_application) before deciding it, and look at its flags.
- Hard flags block approval: under_60_at_submission (scheme rule) and ifsc_invalid (DBT will fail).
  Reject with a clear reason the applicant can act on, or ask the officer before using an override.
- possible_duplicate_of: check the other application before approving.
- Never decide in bulk without the officer having asked for it; report what you did, application by
  application."""

officer = FastMCP(
    "Sewa Setu - Old Age Pension (block officer)",
    instructions=OFFICER_GUIDE,
    host="0.0.0.0", stateless_http=True, json_response=True,
    streamable_http_path="/mcp/officer",
    token_verifier=PortalTokenVerifier("officer", OFFICER_URL),
    auth=AuthSettings(issuer_url=BASE, resource_server_url=OFFICER_URL, required_scopes=["officer"]),
)


@officer.tool()
async def queue_overview() -> dict:
    """Counts by status, pending per block, how many are within 3 days of deemed approval, and how many
    were escalated past the SLA."""
    return await portal("GET", "/internal/api/officer/summary", "officer")


@officer.tool()
async def list_pending(
    block: Annotated[str | None, Field(description="Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti or Khelua; empty for all")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
    offset: int = 0,
) -> dict:
    """Pending applications, oldest first (closest to deemed approval), with flags and days left."""
    params = {"limit": limit, "offset": offset}
    if block:
        params["block"] = block
    return await portal("GET", "/internal/api/officer/queue", "officer", params=params)


@officer.tool()
async def open_application(application_no: Annotated[str, Field(description="e.g. SSP26123456")]) -> dict:
    """Full details of one application: applicant, bank, age at submission, flags, and the audit history."""
    return await portal("GET", "/internal/api/officer/application/" + application_no.strip(), "officer")


@officer.tool()
async def decide_application(
    application_no: str,
    decision: Literal["approve", "reject"],
    reason: Annotated[str, Field(description="Required for reject: plain words, sent to the applicant by SMS")] = "",
    override_reason: Annotated[str | None, Field(description="Only if the officer explicitly decided to approve despite a hard flag")] = None,
) -> dict:
    """Approve or reject a PENDING application. Approval is refused when a hard flag is present unless
    an override_reason from the officer is given."""
    return await portal("POST", "/internal/api/officer/decide", "officer",
                        json={"application_no": application_no, "decision": decision, "reason": reason,
                              "override_reason": override_reason})


@officer.tool()
async def find_applications(query: Annotated[str, Field(description="Application number, 10-digit mobile, or part of a name")]) -> dict:
    """Search all applications (any status)."""
    return await portal("GET", "/internal/api/officer/search", "officer", params={"q": query})


@officer.tool()
async def integrity_report() -> dict:
    """Problems among people already on the pension rolls (under-60 approvals, the same person twice,
    old IFSC codes that will fail DBT). Report only; nothing is changed."""
    return await portal("GET", "/internal/api/officer/integrity", "officer")


# ---------------------------------------------------------------------------
# One ASGI app: dispatch by path to the right server; one lifespan for both.
# ---------------------------------------------------------------------------

citizen_app = citizen.streamable_http_app()
officer_app = officer.streamable_http_app()


@asynccontextmanager
async def lifespan(_app):
    async with citizen.session_manager.run(), officer.session_manager.run():
        yield


async def health(_req):
    return PlainTextResponse("ok")


site = Starlette(lifespan=lifespan, routes=[
    Route("/healthz", health),
    Route("/mcp", lambda r: RedirectResponse("/eval-dashboard")),
    *dashboard.routes,
])


def prm(resource, scope):
    """RFC 9728 metadata, written out by hand: the SDK's URL type appends a trailing slash
    to the issuer ("https://host/"), which strict clients then fail to match against the
    portal's issuer ("https://host")."""
    body = {"resource": resource, "authorization_servers": [BASE], "scopes_supported": [scope],
            "bearer_methods_supported": ["header"], "resource_name": "Sewa Setu (%s)" % scope,
            "resource_documentation": BASE + "/about"}

    async def endpoint(_req):
        return JSONResponse(body, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"})
    return endpoint


PRM = {"/.well-known/oauth-protected-resource/mcp/citizen": prm(CITIZEN_URL, "citizen"),
       "/.well-known/oauth-protected-resource/mcp/officer": prm(OFFICER_URL, "officer")}
prm_app = Starlette(routes=[Route(path, ep, methods=["GET", "OPTIONS"]) for path, ep in PRM.items()])


class Dispatcher:
    def __init__(self):
        self.lifespan_app = site

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            p = scope["path"]
            if p.rstrip("/") in PRM:
                return await prm_app(scope, receive, send)
            if p.startswith("/mcp/citizen") or p.startswith("/.well-known/oauth-protected-resource/mcp/citizen"):
                return await citizen_app(scope, receive, send)
            if p.startswith("/mcp/officer") or p.startswith("/.well-known/oauth-protected-resource/mcp/officer"):
                return await officer_app(scope, receive, send)
        return await site(scope, receive, send)


app = Dispatcher()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")), proxy_headers=True,
                forwarded_allow_ips="*")
