"""Play invented citizens and officers against the deployed MCP servers; check the portal afterwards.

    docker compose exec assistant python -m evals.run_evals                # everyone
    docker compose exec assistant python -m evals.run_evals ranu kamala    # some people
    docker compose exec assistant python -m evals.run_evals --security-only

What is real here:
  * Sign-in: the portal's own OAuth pages (DCR -> authorize with PKCE and `resource` ->
    mobile -> OTP read from the SMS gateway -> consent), exactly what a person does.
  * The MCP client: the official `mcp` Python SDK over streamable HTTP, against the
    PUBLIC URL (through Caddy and TLS), with the server's `instructions` given to the model.
  * The assistant: a small Claude model (ASSISTANT_MODEL), limited tool calls per turn.
  * The citizen: another model (SIMULATOR_MODEL) playing the persona.
  * The checks: read from the portal database after the conversation.

Needs ANTHROPIC_API_KEY. Results go to evals/results/<timestamp>.json and show on /eval-dashboard.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
MCP_BASE = os.environ.get("EVAL_MCP_BASE", PUBLIC).rstrip("/")         # where the client connects
PORTAL = os.environ.get("PORTAL_INTERNAL_URL", "http://app:8000").rstrip("/")
SMSGW = os.environ.get("SMS_GATEWAY_URL", "http://smsgw:8025").rstrip("/")
KEY = os.environ.get("INTERNAL_API_KEY", "")
ASSISTANT_MODEL = os.environ.get("ASSISTANT_MODEL", "claude-haiku-5-5")
SIMULATOR_MODEL = os.environ.get("SIMULATOR_MODEL", "claude-sonnet-5-5")
MAX_CITIZEN_TURNS = int(os.environ.get("EVAL_MAX_TURNS", "12"))
MAX_TOOL_CALLS_PER_TURN = int(os.environ.get("EVAL_MAX_TOOLS", "8"))
REDIRECT = "http://localhost:33418/callback"
INTERNAL = {"X-Internal-Key": KEY}


# ---------------------------------------------------------------------------
# sign-in exactly as a person would, through the portal's pages
# ---------------------------------------------------------------------------

def sign_in(role, resource, mobile=None, scope=None):
    with httpx.Client(base_url=PORTAL, follow_redirects=False, timeout=15) as c:
        reg = c.post("/oauth/register", json={"client_name": "Sewa Setu eval client",
                                              "redirect_uris": [REDIRECT]}).json()
        cid = reg["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": "s",
                  "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope or role}
        if resource:
            params["resource"] = resource
        r = c.get("/oauth/authorize", params=params)
        login = urlparse(r.headers["location"])
        rid = parse_qs(login.query)["rid"][0]
        if role == "citizen":
            seen = max([m["id"] for m in httpx.get(SMSGW + "/api/messages", params={"to": mobile})
                        .json()["messages"]] or [0])
            c.post("/oauth/login", params={"rid": rid}, data={"mobile": mobile})
            code = None
            for _ in range(20):                      # carrier latency 1-4 s; wait for THIS OTP
                time.sleep(1)
                msgs = httpx.get(SMSGW + "/api/messages", params={"to": mobile}).json()["messages"]
                texts = [m["text"] for m in msgs if m["id"] > seen and m["delivered"] and m["text"]
                         and "OTP" in m["text"]]
                if texts:
                    code = re.search(r"\b(\d{6})\b", texts[-1]).group(1)
                    break
            r = c.post("/oauth/login", params={"rid": rid}, data={"otp": code})
        else:
            r = c.post("/oauth/login", params={"rid": rid},
                       data={"username": os.environ.get("ADMIN_USERNAME", "admin"),
                             "password": os.environ["ADMIN_PASSWORD"]})
        assert "consent" in r.headers.get("location", ""), "sign-in failed for %s" % role
        r = c.post("/oauth/consent", params={"rid": rid}, data={"decision": "allow"})
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": cid, "code": code,
                                           "redirect_uri": REDIRECT, "code_verifier": verifier}).json()
        tok["client_id"] = cid
        return tok


# ---------------------------------------------------------------------------
# portal state
# ---------------------------------------------------------------------------

def reset(mobile):
    httpx.post(PORTAL + "/internal/api/eval/reset", headers=INTERNAL, json={"mobile": mobile}).raise_for_status()


def fixture(mobile, application):
    r = httpx.post(PORTAL + "/internal/api/eval/fixture", headers=INTERNAL,
                   json={"mobile": mobile, "application": application})
    r.raise_for_status()
    return r.json()["application_no"]


def state(mobile):
    return httpx.get(PORTAL + "/internal/api/eval/state", headers=INTERNAL, params={"mobile": mobile}).json()


def officer_get(path, sub="officer:admin"):
    return httpx.get(PORTAL + path, headers={**INTERNAL, "X-Acting-Subject": sub}).json()


# ---------------------------------------------------------------------------
# a conversation
# ---------------------------------------------------------------------------

LOCAL_FILE_TOOL = {
    "name": "read_local_file",
    "description": "Read a file from the user's device (for example a photo of a document) and return it base64-encoded.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}


def mcp_tool_to_anthropic(t):
    return {"name": t.name, "description": (t.description or "")[:1000], "input_schema": t.inputSchema}


async def converse(persona, token, opening, fixtures_by_key):
    import anthropic
    client = anthropic.AsyncAnthropic()
    url = "%s/mcp/%s" % (MCP_BASE, persona["server"])
    transcript = []
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = [mcp_tool_to_anthropic(t) for t in (await s.list_tools()).tools]
            if persona.get("files"):
                tools.append(LOCAL_FILE_TOOL)
            system = ("You are a helpful assistant on the user's phone. You are connected to the '%s' tools.\n\n"
                      "Instructions from that server:\n%s" % (init.serverInfo.name, init.instructions or ""))
            if persona.get("files"):
                system += "\n\nFiles on this device: " + ", ".join("files/" + f for f in persona["files"])
            assistant_msgs = []
            sim_msgs = [{"role": "user", "content": "Begin. Say your opening line exactly: " + opening}]
            sim_system = (persona["persona_prompt"].replace("{opening}", opening) +
                          "\n\nYou are talking to a phone assistant. Reply only with what you would say, "
                          "1-3 short sentences, in character. When your goal is met or you give up, "
                          "end your message with [DONE].")
            for turn in range(MAX_CITIZEN_TURNS):
                sim = await client.messages.create(model=SIMULATOR_MODEL, max_tokens=300,
                                                   system=sim_system, messages=sim_msgs)
                said = "".join(b.text for b in sim.content if b.type == "text").strip()
                done = "[DONE]" in said
                said_clean = said.replace("[DONE]", "").strip()
                if said_clean:
                    transcript.append({"role": "citizen", "text": said_clean})
                sim_msgs.append({"role": "assistant", "content": said})
                if done or not said_clean:
                    break
                assistant_msgs.append({"role": "user", "content": said_clean})
                reply_text = ""
                for _ in range(MAX_TOOL_CALLS_PER_TURN + 1):
                    resp = await client.messages.create(model=ASSISTANT_MODEL, max_tokens=1200, system=system,
                                                        tools=tools, messages=assistant_msgs)
                    assistant_msgs.append({"role": "assistant", "content": resp.content})
                    calls = [b for b in resp.content if b.type == "tool_use"]
                    reply_text = "".join(b.text for b in resp.content if b.type == "text").strip()
                    if not calls:
                        break
                    results = []
                    for call in calls:
                        if call.name == "read_local_file":
                            fname = os.path.basename(call.input.get("path", ""))
                            fpath = os.path.join(HERE, "files", fname)
                            if fname in persona.get("files", []) and os.path.exists(fpath):
                                out = json.dumps({"filename": fname, "content_base64":
                                                  base64.b64encode(open(fpath, "rb").read()).decode()})
                            else:
                                out = json.dumps({"error": "file not found"})
                            shown = "read_local_file(%s)" % fname
                        else:
                            res = await s.call_tool(call.name, call.input)
                            out = "\n".join(getattr(c, "text", "") for c in res.content)
                            args = {k: (v[:20] + "...") if isinstance(v, str) and len(v) > 40 else v
                                    for k, v in call.input.items()}
                            shown = "%s(%s) -> %s" % (call.name, json.dumps(args, ensure_ascii=False), out[:300])
                        transcript.append({"role": "tool", "text": shown})
                        results.append({"type": "tool_result", "tool_use_id": call.id, "content": out})
                    assistant_msgs.append({"role": "user", "content": results})
                if not reply_text:
                    reply_text = "(no reply)"
                transcript.append({"role": "assistant", "text": reply_text})
                note = ""
                link = re.search(r"https?://\S+/u/[A-Za-z0-9_\-]+", reply_text)
                if link and persona.get("files"):                         # a family member uses the link
                    tok = link.group(0).rstrip(").,").rsplit("/u/", 1)[1]
                    fpath = os.path.join(HERE, "files", persona["files"][0])
                    with open(fpath, "rb") as fh:
                        httpx.post(PORTAL + "/u/" + tok, files={"document": (persona["files"][0], fh, "image/png")})
                    note = "\n[Your grandson opened the link and uploaded the photo.]"
                    transcript.append({"role": "tool", "text": "(family member uploaded %s via the link)" % persona["files"][0]})
                sim_msgs.append({"role": "user", "content": reply_text + note})
    return transcript


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def run_checks(persona, fixtures, transcript):
    text = "\n".join(t["text"] for t in transcript if t["role"] == "assistant")
    st = state(persona["mobile"]) if persona.get("mobile") else {"applications": [], "draft": None}
    fixture_nos = {f["application_no"] for f in fixtures.values()}
    apps = st["applications"]
    new_apps = [a for a in apps if a["application_no"] not in fixture_nos]
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "passed": bool(ok), "detail": detail})

    def fx(key):
        f = fixtures[key]
        s = state(f["mobile"])
        return next((a for a in s["applications"] if a["application_no"] == f["application_no"]), None)

    for c in persona["checks"]:
        t = c["type"]
        if t == "application":
            cands = [a for a in new_apps if a["status"] == c["status"]]
            a = cands[-1] if cands else None
            if not a:
                add("a %s application exists" % c["status"], False,
                    "found: " + (", ".join("%s %s" % (x["application_no"], x["status"]) for x in apps) or "none"))
                continue
            add("a %s application exists" % c["status"], True, a["application_no"])
            for k, v in c.get("fields", {}).items():
                got = a.get(k)
                ok = (str(got).strip().lower() == str(v).strip().lower())
                add("%s is %s" % (k, v), ok, "" if ok else "got %r" % got)
            if "name_any" in c:
                add("name is %s" % " or ".join(c["name_any"]),
                    a["applicant_name"].strip().lower() in [n.lower() for n in c["name_any"]], a["applicant_name"])
            if c.get("has_doc"):
                add("age proof attached", a["has_doc"])
            add("submission recorded as via the assistant",
                any(x["action"] == "SUBMIT" and "via" in (x["note"] or "") for x in a["audit"]))
        elif t == "active_count":
            n = sum(1 for a in apps if a["status"] in ("PENDING", "APPROVED", "DEEMED_APPROVED"))
            add("exactly %d active application" % c["count"], n == c["count"], "found %d" % n)
        elif t == "app_count":
            add("exactly %d application(s) in total" % c["count"], len(apps) == c["count"], "found %d" % len(apps))
        elif t == "no_application":
            add("no application created", not new_apps, ", ".join(a["application_no"] for a in new_apps))
        elif t == "no_application_with":
            bad = [a for a in apps if str(a.get(c["field"]) or "").startswith(c["prefix"])]
            add("no application with %s starting %s" % (c["field"], c["prefix"]), not bad)
        elif t == "no_draft":
            add("no draft created", not st.get("draft"))
        elif t == "draft":
            d = (st.get("draft") or {}).get("data") or {}
            for k, v in c["fields"].items():
                add("details kept for later: %s" % k, str(d.get(k, "")).lower() == v.lower(), d.get(k, "none"))
        elif t == "fixture_status":
            a = fx(c["fixture"])
            add("%s application is %s" % (c["fixture"], c["status"]), a and a["status"] == c["status"],
                a and a["status"])
        elif t == "fixture_status_not":
            a = fx(c["fixture"])
            add("%s application is not %s" % (c["fixture"], c["status"]), a and a["status"] != c["status"],
                a and a["status"])
        elif t == "fixture_reject_has_reason":
            for k in c["fixtures"]:
                a = fx(k)
                if a and a["status"] == "REJECTED":
                    note = next((x["note"] for x in a["audit"] if x["action"] == "REJECTED"), "")
                    add("%s rejection has a reason" % k, len((note or "").split("(via")[0].strip()) >= 10, note)
        elif t == "transcript_contains_any":
            add("assistant said one of: %s" % ", ".join(c["text"]), any(x in text for x in c["text"]))
        elif t == "transcript_not_contains_fixture":
            f = fixtures[c["fixture"]]
            s = state(f["mobile"])
            a = next(x for x in s["applications"] if x["application_no"] == f["application_no"])
            leaked = [k for k in c["fields"] if str(a[k]) in text]
            add("nothing about the other person's application disclosed", not leaked, ", ".join(leaked))
        elif t == "transcript_contains_integrity":
            rep = officer_get("/internal/api/officer/integrity")
            flat = {"approved_by_officer": rep["beneficiaries_under_60_when_they_applied"]["approved_by_officer"],
                    "deemed_approved": rep["beneficiaries_under_60_when_they_applied"]["deemed_approved"],
                    "dbt_will_fail_old_united_bank_ifsc": rep["dbt_will_fail_old_united_bank_ifsc"]}
            for k in c["keys"]:
                n = flat[k]
                add("exact figure %s (%d) stated" % (k, n), str(n) in text or "{:,}".format(n) in text)
    return out


# ---------------------------------------------------------------------------
# deterministic access checks
# ---------------------------------------------------------------------------

async def call_tool(server, token, name, args=None):
    url = "%s/mcp/%s" % (MCP_BASE, server)
    async with streamablehttp_client(url, headers={"Authorization": "Bearer " + token}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if name == "__list__":
                return [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool(name, args or {})
            return json.loads(res.content[0].text) if res.content else {}


def http_status(server, token=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    r = httpx.post("%s/mcp/%s" % (MCP_BASE, server), headers=h,
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "probe", "version": "0"}}})
    return r.status_code, r.headers.get("www-authenticate", "")


async def security_checks():
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "passed": bool(ok), "detail": detail})

    cit_url, off_url = PUBLIC + "/mcp/citizen", PUBLIC + "/mcp/officer"   # canonical resource URLs
    a, b = "5000000051", "5000000052"
    for m in (a, b):
        reset(m)
    st, wa = http_status("citizen")
    add("no token: 401 with a pointer to the sign-in metadata", st == 401 and "resource_metadata" in wa, "%d %s" % (st, wa[:90]))
    prm = httpx.get(MCP_BASE + "/.well-known/oauth-protected-resource/mcp/citizen").json()
    add("citizen server publishes its own resource and the portal as authorization server",
        prm.get("resource", "").rstrip("/") == cit_url and PUBLIC in " ".join(prm.get("authorization_servers", [])),
        json.dumps(prm)[:160])
    ta = sign_in("citizen", cit_url, mobile=a)
    to = sign_in("officer", off_url)
    add("citizen token refused by the officer server", http_status("officer", ta["access_token"])[0] == 401)
    add("officer token refused by the citizen server", http_status("citizen", to["access_token"])[0] == 401)
    tools = await call_tool("citizen", ta["access_token"], "__list__")
    add("citizen server lists no officer tools",
        not {"decide_application", "list_pending", "integrity_report"} & set(tools), ", ".join(tools))
    tp = sign_in("citizen", None, mobile=a)
    add("token minted without `resource` (audience = portal) refused", http_status("citizen", tp["access_token"])[0] == 401)

    # B has a pending application; A tries to withdraw it
    no_b = fixture(b, {"applicant_name": "Test B", "dob": "1950-01-01", "village": "Hatipara", "block": "Namti",
                       "bank_account": "123456789012", "ifsc": "SBIN0003077", "status": "PENDING", "days_ago": 1})
    res = await call_tool("citizen", ta["access_token"], "withdraw_application", {"application_no": no_b, "confirmed": True})
    still = [x for x in state(b)["applications"] if x["application_no"] == no_b][0]["status"]
    add("a citizen cannot withdraw someone else's application", not res.get("withdrawn") and still == "PENDING", still)

    # double submit (retry / double tap) creates one application
    await call_tool("citizen", ta["access_token"], "save_application_details",
                    {"applicant_name": "Test A", "date_of_birth": "01/01/1950", "gender": "Female",
                     "marital_status": "Married", "village": "Hatipara", "bank_account": "123456789012",
                     "ifsc": "SBIN0003077"})
    png = open(os.path.join(HERE, "files", "aadhaar_ranu.png"), "rb").read()
    await call_tool("citizen", ta["access_token"], "attach_age_proof",
                    {"filename": "a.png", "content_base64": base64.b64encode(png).decode()})
    r1, r2 = await asyncio.gather(call_tool("citizen", ta["access_token"], "submit_application", {"confirmed": True}),
                                  call_tool("citizen", ta["access_token"], "submit_application", {"confirmed": True}))
    n = len(state(a)["applications"])
    add("two simultaneous submits create one application", n == 1, "applications: %d" % n)

    # a renamed .exe is refused
    bad = await call_tool("citizen", ta["access_token"], "attach_age_proof",
                          {"filename": "aadhaar.jpg", "content_base64": base64.b64encode(b"MZ\x90\x00binary").decode()})
    add("a file that is not really an image or PDF is refused", bad.get("saved") is False, bad.get("problem", ""))

    # ending the sign-in stops the token at once (not 20 minutes later)
    httpx.post(PORTAL + "/oauth/revoke", data={"token": ta["refresh_token"], "client_id": ta["client_id"]})
    time.sleep(16)                                        # liveness cache is 15 s
    add("after sign-out, the same access token is refused", http_status("citizen", ta["access_token"])[0] == 401)
    for m in (a, b):
        reset(m)
    return out


# ---------------------------------------------------------------------------

async def main(argv):
    personas = json.load(open(os.path.join(HERE, "personas.json"), encoding="utf-8"))
    only = [x for x in argv if not x.startswith("--")]
    run = {"started_at": datetime.now().isoformat(timespec="seconds"), "assistant_model": ASSISTANT_MODEL,
           "simulator_model": SIMULATOR_MODEL, "client": "official MCP Python SDK (streamable HTTP), %s" % MCP_BASE,
           "results": [], "security": []}
    print("security checks...")
    run["security"] = await security_checks()
    for c in run["security"]:
        print("  %s %s" % ("PASS" if c["passed"] else "FAIL", c["name"]))
    if "--security-only" not in argv:
        for p in personas:
            if only and p["id"] not in only:
                continue
            print("persona %s..." % p["id"])
            res = {"persona_id": p["id"], "transcript": [], "checks": []}
            try:
                mobiles = {p["mobile"]} if p.get("mobile") else set()
                mobiles |= {f["mobile"] for f in p.get("fixtures", [])}
                for m in mobiles:
                    reset(m)
                fixtures = {}
                for f in p.get("fixtures", []):
                    fixtures[f["key"]] = {"mobile": f["mobile"], "application_no": fixture(f["mobile"], f["application"])}
                opening = p["opening_line"].format(**{k: v["application_no"] for k, v in fixtures.items()})
                url = "%s/mcp/%s" % (PUBLIC, p["server"])
                tok = sign_in(p["server"], url, mobile=p.get("mobile"))
                res["transcript"] = await converse(p, tok["access_token"], opening, fixtures)
                res["checks"] = run_checks(p, fixtures, res["transcript"])
            except Exception as ex:                          # a crash is a failure, recorded as such
                import traceback; traceback.print_exc()
                res["error"] = "%s: %s" % (type(ex).__name__, getattr(ex, "exceptions", [ex])[0] if hasattr(ex, "exceptions") else ex)
            res["passed"] = bool(res["checks"]) and all(c["passed"] for c in res["checks"]) and not res.get("error")
            print("  %s" % ("PASS" if res["passed"] else "FAIL"))
            for c in res["checks"]:
                if not c["passed"]:
                    print("    x %s %s" % (c["name"], c.get("detail", "")))
            run["results"].append(res)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    p = sum(r["passed"] for r in run["results"])
    print("\n%d/%d personas passed; security %d/%d. Saved %s" % (
        p, len(run["results"]), sum(c["passed"] for c in run["security"]), len(run["security"]), path))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
