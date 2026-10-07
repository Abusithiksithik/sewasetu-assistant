"""Scripted smoke test of every persona's tool path, without a model.

Runs the exact tool calls a perfect assistant would make, through the real MCP servers with
real sign-ins, and checks the database. If this fails, the model evals cannot pass; if this
passes and a model eval fails, the problem is in how the model uses the tools.

    docker compose exec assistant python -m evals.smoke_tools
"""

import asyncio
import base64
import os

from evals.run_evals import PUBLIC, HERE, sign_in, reset, fixture, state, call_tool, officer_get

F = lambda n: base64.b64encode(open(os.path.join(HERE, "files", n), "rb").read()).decode()
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  <- %s" % (detail,)))


async def citizen(mobile):
    reset(mobile)
    return sign_in("citizen", PUBLIC + "/mcp/citizen", mobile=mobile)["access_token"]


async def main():
    # Ranu: happy path, messy input formats
    t = await citizen("5000000001")
    st = await call_tool("citizen", t, "my_pension_status")
    check("status works with no application", st["active_application"] is None and st["scheme"]["window_open"], st)
    d = await call_tool("citizen", t, "save_application_details",
                        {"applicant_name": "Ranu Bora", "date_of_birth": "14 August 1959", "gender": "f",
                         "marital_status": "widow", "late_husband_name": "Dinesh Bora", "village": "Lakhipur"})
    check("dob in words normalised", d["details"]["dob"] == "14/08/1959", d["details"])
    check("block inferred from village", d["details"]["block"] == "Sonari", d)
    d = await call_tool("citizen", t, "save_application_details", {"bank_account": "3124 5678 901", "ifsc": "sbin 0003077"})
    check("still needs age proof", not d["ready_to_submit"] and any("age proof" in p for p in d["problems"]), d["problems"])
    d = await call_tool("citizen", t, "attach_age_proof", {"filename": "aadhaar_ranu.png", "content_base64": F("aadhaar_ranu.png")})
    check("ready after proof", d.get("ready_to_submit"), d)
    r = await call_tool("citizen", t, "submit_application", {"confirmed": False})
    check("submit refuses without confirmation", r["submitted"] is False)
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    a = state("5000000001")["applications"]
    check("Ranu submitted with exact fields", r["submitted"] and len(a) == 1 and a[0]["bank_account"] == "31245678901"
          and a[0]["ifsc"] == "SBIN0003077" and a[0]["dob"] == "1959-08-14" and a[0]["has_doc"], a)
    r2 = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    check("resubmit is not a second application", len(state("5000000001")["applications"]) == 1, r2)

    # Hemanta: under 60, can apply on 20 October
    t = await citizen("5000000002")
    e = await call_tool("citizen", t, "check_eligibility", {"date_of_birth": "20-10-1966"})
    check("Hemanta not eligible, turns 60 on 20 October 2026", not e["eligible_today"] and e["turns_60_on"] == "20 October 2026"
          and e["can_apply_before_window_closes"], e)
    await call_tool("citizen", t, "save_application_details", {"applicant_name": "Hemanta Kalita", "date_of_birth": "20/10/1966",
                    "gender": "male", "marital_status": "married", "village": "Namti Chariali", "bank_account": "40987654321",
                    "ifsc": "PUNB0556610"})
    await call_tool("citizen", t, "attach_age_proof", {"filename": "x.png", "content_base64": F("aadhaar_ranu.png")})
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    check("under-60 submission refused by the portal rule", not r["submitted"] and not state("5000000002")["applications"], r)

    # Kamala: UTBI explained, then fixed
    t = await citizen("5000000003")
    d = await call_tool("citizen", t, "save_application_details", {"applicant_name": "Kamala Deka", "date_of_birth": "05/02/1954",
                        "gender": "Female", "marital_status": "Widowed", "village": "Udaypur", "bank_account": "556677889900",
                        "ifsc": "UTBI0PUR2819"})
    check("UTBI explained in plain words", any("Punjab National Bank" in p for p in d["problems"]), d["problems"])
    await call_tool("citizen", t, "save_application_details", {"ifsc": "PUNB0556610"})
    await call_tool("citizen", t, "attach_age_proof", {"filename": "k.png", "content_base64": F("aadhaar_kamala.png")})
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    check("Kamala submitted with new IFSC", r["submitted"] and state("5000000003")["applications"][0]["ifsc"] == "PUNB0556610", r)

    # Abdul: blocked by existing, withdraw, reapply
    reset("5000000004")
    old = fixture("5000000004", {"applicant_name": "Abdul Hussain", "dob": "1962-07-11", "village": "Dighaljan", "block": "Rajapara",
                                 "bank_account": "12906812552", "ifsc": "SBIN0003077", "status": "PENDING", "days_ago": 4})
    t = sign_in("citizen", PUBLIC + "/mcp/citizen", mobile="5000000004")["access_token"]
    await call_tool("citizen", t, "save_application_details", {"applicant_name": "Abdul Hussain", "date_of_birth": "11/07/1962",
                    "gender": "Male", "marital_status": "Married", "village": "Dighaljan", "bank_account": "129068125527",
                    "ifsc": "SBIN0003077"})
    await call_tool("citizen", t, "attach_age_proof", {"filename": "a.png", "content_base64": F("aadhaar_abdul.png")})
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    check("blocked while old one is pending", not r["submitted"] and r.get("existing_application") == old, r)
    w = await call_tool("citizen", t, "withdraw_application", {"application_no": old, "confirmed": True})
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    s = {x["application_no"]: x for x in state("5000000004")["applications"]}
    check("old withdrawn, new pending with corrected account", w["withdrawn"] and s[old]["status"] == "WITHDRAWN"
          and s[r["application_no"]]["bank_account"] == "129068125527", s)

    # Khagen: Assamese numerals and name
    t = await citizen("5000000005")
    d = await call_tool("citizen", t, "save_application_details", {"applicant_name": "খগেন ডেকা", "date_of_birth": "02/03/1957",
                        "gender": "Male", "marital_status": "Married", "village": "Chengelimara",
                        "bank_account": "50211234567", "ifsc": "IDIB000S118"})
    await call_tool("citizen", t, "attach_age_proof", {"filename": "আধাৰ.png", "content_base64": F("aadhaar_khagen.png")})
    r = await call_tool("citizen", t, "submit_application", {"confirmed": True})
    a = state("5000000005")["applications"]
    check("Assamese name stored intact; Assamese file name accepted", r["submitted"] and a[0]["applicant_name"] == "খগেন ডেকা"
          and a[0]["block"] == "Khelua", r)

    # Lakhi: details kept, nothing submitted
    t = await citizen("5000000008")
    await call_tool("citizen", t, "save_application_details", {"applicant_name": "Lakhi Chetia", "date_of_birth": "9/12/1962"})
    s = state("5000000008")
    check("draft kept, nothing submitted", not s["applications"] and s["draft"]["data"]["applicant_name"] == "Lakhi Chetia", s)

    # upload link
    link = await call_tool("citizen", t, "get_upload_link")
    check("upload link issued", "/u/" in link["url"], link)

    # Officer: Rina's three
    nos = {}
    for k, m, dob, ifsc in (("good", "5000000091", "1950-03-12", "PUNB0556610"), ("young", "5000000092", "1966-12-30", "SBIN0003077"),
                            ("utbi", "5000000093", "1948-08-04", "UTBI0PUR2819")):
        reset(m)
        nos[k] = fixture(m, {"applicant_name": "Officer test " + k, "dob": dob, "village": "Hatipara", "block": "Namti",
                             "bank_account": "91959801384", "ifsc": ifsc, "status": "PENDING", "days_ago": 12})
    o = sign_in("officer", PUBLIC + "/mcp/officer")["access_token"]
    q = await call_tool("officer", o, "list_pending", {"block": "namti", "limit": 5})
    check("queue filters by block, oldest first", q["block"] == "Namti" and all(i["block"] == "Namti" for i in q["items"]), q)
    ap = await call_tool("officer", o, "open_application", {"application_no": nos["young"]})
    check("under-60 flagged", "under_60_at_submission" in ap["flags"] and ap["age_at_submission"] == 59, ap["flags"])
    r = await call_tool("officer", o, "decide_application", {"application_no": nos["young"], "decision": "approve"})
    check("approval of under-60 blocked", r["done"] is False and "under_60_at_submission" in r["blocked_by"], r)
    r = await call_tool("officer", o, "decide_application", {"application_no": nos["utbi"], "decision": "reject", "reason": "no"})
    check("reject without a real reason refused", r["done"] is False, r)
    r1 = await call_tool("officer", o, "decide_application", {"application_no": nos["good"], "decision": "approve"})
    r2 = await call_tool("officer", o, "decide_application", {"application_no": nos["young"], "decision": "reject",
                         "reason": "You were 59 when you applied. Please apply again after your 60th birthday."})
    r3 = await call_tool("officer", o, "decide_application", {"application_no": nos["utbi"], "decision": "reject",
                         "reason": "Old United Bank IFSC no longer works. Apply again with the new PNB IFSC."})
    check("decisions recorded", r1["status"] == "APPROVED" and r2["status"] == "REJECTED" and r3["status"] == "REJECTED", (r1, r2, r3))
    again = await call_tool("officer", o, "decide_application", {"application_no": nos["good"], "decision": "reject", "reason": "changed mind"})
    check("a decided application cannot be re-decided", again["done"] is False, again)
    rep = await call_tool("officer", o, "integrity_report")
    check("integrity report has the under-60 figures", rep["beneficiaries_under_60_when_they_applied"]["approved_by_officer"] >= 2585, rep)

    # rejected citizen sees the reason
    t = sign_in("citizen", PUBLIC + "/mcp/citizen", mobile="5000000092")["access_token"]
    st = await call_tool("citizen", t, "my_pension_status")
    check("citizen sees officer's reason", "60th birthday" in st["applications"][0].get("officer_note", ""), st["applications"])

    for m in ("5000000001", "5000000002", "5000000003", "5000000004", "5000000005", "5000000008",
              "5000000091", "5000000092", "5000000093"):
        reset(m)
    print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))


if __name__ == "__main__":
    asyncio.run(main())
