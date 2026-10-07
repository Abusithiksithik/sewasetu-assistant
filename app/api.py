# -*- coding: utf-8 -*-
"""
Internal API used by the citizen and officer MCP servers (the `assistant` service).

Design rules:
  * The portal stays the system of record. Every write goes through the same
    functions the web form uses: validate_application(), create_application(),
    lock_mobile(), save_age_proof(). No channel can admit what another refuses.
  * This API is not public. Caddy does not route /internal/ and every call must
    carry X-Internal-Key. The MCP server has already verified the person's token;
    it passes who the person is in X-Acting-Subject ("citizen:<mobile>" or
    "officer:<username>") and the application acting for them in X-Client-Name.
  * Answers are written for a person: plain-language problems, not codes.
"""

import os
import re
import json
import base64
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, abort, render_template_string

import app as portal

api = Blueprint("api", __name__)

INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")
EVAL_MOBILE_PREFIX = "500000"      # eval personas only ever use 500000xxxx numbers
HARD_FLAGS = ("under_60_at_submission", "ifsc_invalid")
STATUS_WORDS = {
    "PENDING": "waiting for the block officer's decision",
    "APPROVED": "approved by the block officer; pension will be credited by DBT",
    "DEEMED_APPROVED": "approved automatically under the Right to Public Services Act "
                       "because it was not decided within %d days" % portal.SLA_DAYS,
    "REJECTED": "rejected by the block officer",
    "WITHDRAWN": "withdrawn by the applicant",
}


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

_schema_ready = False


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS drafts (mobile VARCHAR(15) PRIMARY KEY, data JSONB,
                       doc_path VARCHAR(200), doc_name VARCHAR(100), updated_at TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS upload_links (token_hash VARCHAR(64) PRIMARY KEY,
                       mobile VARCHAR(15), created_at TIMESTAMP, expires_at TIMESTAMP, used_at TIMESTAMP)""")
        conn.commit()
    except Exception:
        conn.rollback()      # another worker created them at the same moment
    cur.close(); conn.close()
    _schema_ready = True


@api.before_request
def _guard():
    if request.path.startswith("/internal/"):
        if not INTERNAL_API_KEY or not secrets.compare_digest(
                request.headers.get("X-Internal-Key", ""), INTERNAL_API_KEY):
            abort(404)
    ensure_schema()


def _subject(role):
    sub = request.headers.get("X-Acting-Subject", "")
    if not sub.startswith(role + ":") or len(sub) <= len(role) + 1:
        abort(403)
    return sub.split(":", 1)[1]


def _client_name():
    return (request.headers.get("X-Client-Name") or "assistant")[:60]


def _body():
    return request.get_json(silent=True) or {}


def _db():
    conn = portal.get_db()
    return conn, conn.cursor()


def _iso(v):
    return v.isoformat(sep=" ", timespec="minutes") if isinstance(v, datetime) else (
        v.isoformat() if v is not None and hasattr(v, "isoformat") else v)


# ---------------------------------------------------------------------------
# normalising what people say into what the form expects
# ---------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def normalise_dob(raw):
    """Accept the ways people write dates; return DD/MM/YYYY or None. Day-first, as in India."""
    s = (raw or "").strip().lower().replace(",", " ")
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})", s)          # 1957-04-10
    if m:
        y, mo, d = m.groups()
    else:
        m = re.fullmatch(r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})", s)       # 10/04/1957
        if m:
            d, mo, y = m.groups()
        else:
            m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})", s)   # 10 April 1957
            m2 = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", s)  # April 10 1957
            if m and m.group(2)[:3] in MONTHS:
                d, mo, y = m.group(1), MONTHS[m.group(2)[:3]], m.group(3)
            elif m2 and m2.group(1)[:3] in MONTHS:
                d, mo, y = m2.group(2), MONTHS[m2.group(1)[:3]], m2.group(3)
            else:
                return None
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%d/%m/%Y")
    except ValueError:
        return None


def normalise_block(raw):
    s = re.sub(r"\s+block$", "", (raw or "").strip().lower()).strip()
    if not s:
        return None
    for b in portal.BLOCKS:
        if s == b.lower():
            return b
    starts = [b for b in portal.BLOCKS if b.lower().startswith(s) or s.startswith(b.lower().split()[0])]
    return starts[0] if len(starts) == 1 else None


def normalise_choice(raw, table):
    s = (raw or "").strip().lower()
    for value, words in table.items():
        if s in words:
            return value
    return None


GENDERS = {"Male": {"male", "m", "man", "purush", "pursh", "gents"},
           "Female": {"female", "f", "woman", "mahila", "lady", "ladies"},
           "Other": {"other", "transgender", "third gender"}}
MARITAL = {"Married": {"married", "vivahit"},
           "Unmarried": {"unmarried", "single", "never married", "not married"},
           "Widowed": {"widowed", "widow", "widower", "vidhwa", "husband died", "wife died"}}


def village_blocks(cur, village):
    cur.execute("SELECT block, count(*) FROM applications WHERE lower(village) = lower(%s) "
                "GROUP BY block ORDER BY count(*) DESC", (village.strip(),))
    return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# citizen
# ---------------------------------------------------------------------------

def _draft(cur, mobile):
    cur.execute("SELECT data, doc_path, doc_name, updated_at FROM drafts WHERE mobile = %s", (mobile,))
    r = cur.fetchone()
    if not r:
        return {"data": {}, "doc_path": "", "doc_name": "", "updated_at": None}
    return {"data": r[0] or {}, "doc_path": r[1] or "", "doc_name": r[2] or "", "updated_at": r[3]}


def _save_draft(cur, mobile, d):
    cur.execute("""INSERT INTO drafts (mobile, data, doc_path, doc_name, updated_at) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (mobile) DO UPDATE SET data = EXCLUDED.data, doc_path = EXCLUDED.doc_path,
                   doc_name = EXCLUDED.doc_name, updated_at = EXCLUDED.updated_at""",
                (mobile, json.dumps(d["data"]), d["doc_path"], d["doc_name"], datetime.now()))


def _draft_view(d):
    errors, cleaned = portal.validate_application(d["data"], d["doc_path"])
    shown = {k: d["data"].get(k, "") for k in
             ("applicant_name", "dob", "gender", "marital_status", "husband_name",
              "village", "block", "bank_account", "ifsc")}
    return {"details": shown, "age_proof": d["doc_name"] or None,
            "problems": errors, "ready_to_submit": not errors,
            "age_today": portal.age_on(cleaned["dob"], portal.now_ist().date()) if cleaned["dob"] else None}


def _applications(cur, mobile):
    cur.execute("SELECT id, application_no, applicant_name, status, submitted_at, decided_at "
                "FROM applications WHERE mobile = %s ORDER BY submitted_at DESC", (mobile,))
    out = []
    for i, no, name, st, sub, dec in cur.fetchall():
        cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = %s "
                    "AND coalesce(note, '') <> '' ORDER BY at DESC LIMIT 1", (i, st))
        note = cur.fetchone()
        item = {"application_no": no, "applicant_name": name, "status": st,
                "meaning": STATUS_WORDS.get(st, st), "submitted_at": _iso(sub), "decided_at": _iso(dec)}
        if st == "PENDING" and sub:
            due = sub + timedelta(days=portal.SLA_DAYS)
            item["decision_due_by"] = due.strftime("%d %B %Y")
        if note and st in ("REJECTED", "APPROVED", "WITHDRAWN"):
            item["officer_note"] = note[0]
        out.append(item)
    return out


@api.get("/internal/api/citizen/overview")
def citizen_overview():
    mobile = _subject("citizen")
    conn, cur = _db()
    apps = _applications(cur, mobile)
    d = _draft(cur, mobile)
    cur.close(); conn.close()
    hours = portal.deadline_remaining()
    return jsonify({
        "signed_in_mobile": mobile,
        "applications": apps,
        "active_application": next((a for a in apps if a["status"] in portal.ACTIVE_STATUSES), None),
        "draft": _draft_view(d) if (d["data"] or d["doc_path"]) else None,
        "scheme": {"window_open": hours is not None,
                   "last_date": portal.SCHEME_DEADLINE.strftime("%d %B %Y, %I:%M %p"),
                   "minimum_age": portal.MIN_AGE},
        "status_portal": "Status can also be seen on the portal with this mobile number and the "
                         "date of birth as password (DDMMYYYY).",
    })


@api.get("/internal/api/eligibility")
def eligibility():
    dob_s = normalise_dob(request.args.get("dob"))
    if not dob_s:
        return jsonify({"understood": False,
                        "message": "Could not read that date of birth. Ask for day, month and year."})
    dob = portal.parse_dob(dob_s)
    today = portal.now_ist().date()
    age = portal.age_on(dob, today)
    out = {"understood": True, "date_of_birth": dob_s, "age_today": age,
           "minimum_age": portal.MIN_AGE, "eligible_today": age >= portal.MIN_AGE,
           "window_closes": portal.SCHEME_DEADLINE.strftime("%d %B %Y")}
    if age < portal.MIN_AGE:
        try:
            sixty = dob.replace(year=dob.year + portal.MIN_AGE)
        except ValueError:                                  # born 29 Feb
            sixty = dob.replace(year=dob.year + portal.MIN_AGE, day=28)
        out["turns_60_on"] = sixty.strftime("%d %B %Y")
        out["can_apply_before_window_closes"] = sixty <= portal.SCHEME_DEADLINE.date()
        out["message"] = ("Not yet eligible. Can apply from %s%s." % (
            out["turns_60_on"],
            "" if out["can_apply_before_window_closes"] else
            ", which is after this scheme window closes on " + out["window_closes"]))
    else:
        out["message"] = "Meets the age rule (60 or above)."
    return jsonify(out)


@api.patch("/internal/api/citizen/draft")
def citizen_draft():
    mobile = _subject("citizen")
    body = _body()
    conn, cur = _db()
    d = _draft(cur, mobile)
    notes = []
    for key in ("applicant_name", "village", "husband_name"):
        if body.get(key):
            d["data"][key] = str(body[key]).strip()
    if body.get("dob"):
        v = normalise_dob(str(body["dob"]))
        if v:
            d["data"]["dob"] = v
        else:
            notes.append("Could not read date of birth '%s'. Ask for day, month and year." % body["dob"])
    if body.get("gender"):
        v = normalise_choice(str(body["gender"]), GENDERS)
        d["data"]["gender"] = v or str(body["gender"]).strip().title()
    if body.get("marital_status"):
        v = normalise_choice(str(body["marital_status"]), MARITAL)
        d["data"]["marital_status"] = v or str(body["marital_status"]).strip().title()
    if body.get("block"):
        v = normalise_block(str(body["block"]))
        if v:
            d["data"]["block"] = v
        else:
            notes.append("'%s' is not one of the blocks: %s." % (body["block"], ", ".join(portal.BLOCKS)))
    if body.get("bank_account"):
        d["data"]["bank_account"] = re.sub(r"[\s-]", "", str(body["bank_account"]))
    if body.get("ifsc"):
        d["data"]["ifsc"] = re.sub(r"[\s-]", "", str(body["ifsc"])).upper()
    if d["data"].get("village") and not d["data"].get("block"):
        blocks = village_blocks(cur, d["data"]["village"])
        if len(blocks) == 1:
            d["data"]["block"] = blocks[0]
            notes.append("Block set to %s because %s is in that block. Confirm with the person."
                         % (blocks[0], d["data"]["village"]))
        elif blocks:
            notes.append("%s appears in more than one block (%s). Ask which one."
                         % (d["data"]["village"], ", ".join(blocks)))
    if d["data"].get("marital_status") != "Widowed" and d["data"].get("husband_name"):
        d["data"]["husband_name"] = ""          # only asked of widows
    _save_draft(cur, mobile, d)
    conn.commit()
    cur.close(); conn.close()
    view = _draft_view(d)
    view["notes"] = notes
    return jsonify(view)


@api.post("/internal/api/citizen/draft/document")
def citizen_document():
    mobile = _subject("citizen")
    body = _body()
    try:
        content = base64.b64decode(body.get("content_base64") or "", validate=False)
    except Exception:
        return jsonify({"saved": False, "problem": "The file content could not be read (not base64)."}), 400
    path, err = portal.save_age_proof(mobile, body.get("filename") or "", content)
    if err:
        return jsonify({"saved": False, "problem": err}), 400
    conn, cur = _db()
    d = _draft(cur, mobile)
    d["doc_path"], d["doc_name"] = path, os.path.basename(body.get("filename") or "document")[:100]
    _save_draft(cur, mobile, d)
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"saved": True, "age_proof": d["doc_name"], **_draft_view(d)})


@api.post("/internal/api/citizen/upload-link")
def citizen_upload_link():
    mobile = _subject("citizen")
    token = secrets.token_urlsafe(18)
    conn, cur = _db()
    cur.execute("INSERT INTO upload_links (token_hash, mobile, created_at, expires_at) VALUES (%s,%s,%s,%s)",
                (hashlib.sha256(token.encode()).hexdigest(), mobile, datetime.now(),
                 datetime.now() + timedelta(hours=2)))
    conn.commit()
    cur.close(); conn.close()
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return jsonify({"url": "%s/u/%s" % (base, token), "valid_for": "2 hours, one upload"})


@api.post("/internal/api/citizen/submit")
def citizen_submit():
    mobile = _subject("citizen")
    conn, cur = _db()
    d = _draft(cur, mobile)
    errors, cleaned = portal.validate_application(d["data"], d["doc_path"])
    if errors:
        cur.close(); conn.close()
        return jsonify({"submitted": False, "problems": errors})
    portal.lock_mobile(cur, mobile)
    existing = portal.active_application(cur, mobile)
    if existing:
        cur.execute("SELECT applicant_name, dob, submitted_at FROM applications WHERE id = %s", (existing[0],))
        name, dob, sub = cur.fetchone()
        conn.rollback(); cur.close(); conn.close()
        if name == cleaned["applicant_name"] and dob == cleaned["dob"] and sub and \
                datetime.now() - sub < timedelta(minutes=30):
            return jsonify({"submitted": True, "already_submitted": True, "application_no": existing[1],
                            "status": existing[2],
                            "message": "This application was already submitted; nothing new was created."})
        return jsonify({"submitted": False, "existing_application": existing[1], "existing_status": existing[2],
                        "problems": ["This mobile number already has application %s (%s). Only one active "
                                     "application is allowed. A PENDING one can be withdrawn first if the "
                                     "person wants to apply afresh." % (existing[1], existing[2])]})
    # same person already on the rolls under another mobile? Allowed (namesakes exist), but flagged.
    cur.execute("SELECT application_no FROM applications WHERE lower(applicant_name) = lower(%s) AND dob = %s "
                "AND mobile <> %s AND status IN %s LIMIT 1",
                (cleaned["applicant_name"], cleaned["dob"], mobile, portal.ACTIVE_STATUSES))
    dup = cur.fetchone()
    note = "via %s" % _client_name()
    if dup:
        note += "; possible duplicate of %s" % dup[0]
    new_id, app_no = portal.create_application(cur, mobile, cleaned, note)
    cur.execute("DELETE FROM drafts WHERE mobile = %s", (mobile,))
    conn.commit()
    cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal with mobile "
                            "no. and password (DOB as DDMMYYYY)." % app_no)
    due = (datetime.now() + timedelta(days=portal.SLA_DAYS)).strftime("%d %B %Y")
    return jsonify({"submitted": True, "application_no": app_no, "status": "PENDING",
                    "decision_due_by": due,
                    "message": "Submitted. An SMS with the application number has been sent. The block "
                               "officer must decide by %s." % due})


@api.post("/internal/api/citizen/withdraw")
def citizen_withdraw():
    mobile = _subject("citizen")
    app_no = (_body().get("application_no") or "").strip().upper()
    conn, cur = _db()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
                "WHERE application_no = %s AND mobile = %s AND status = 'PENDING' RETURNING id",
                (datetime.now(), app_no, mobile))
    row = cur.fetchone()
    if not row:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({"withdrawn": False,
                        "problem": "Only a PENDING application on this mobile number can be withdrawn."})
    portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s" % mobile, "via %s" % _client_name())
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"withdrawn": True, "application_no": app_no,
                    "message": "Withdrawn. A fresh application can now be submitted."})


# public page behind the one-time upload link (for a family member's phone)
UPLOAD_PAGE = """{% extends "base.html" %}{% block content %}
<h2>Upload age proof</h2>
{% if done %}<p>Thank you. The document is attached to the application being prepared for
mobile ending {{ tail }}. You can close this page and go back to the assistant.</p>
{% elif error %}<div class="flash">{{ error }}</div>{% endif %}
{% if not done and not dead %}
<p>Aadhaar card, birth certificate, voter ID or school certificate showing date of birth.
A clear photo (JPG/PNG) or PDF, up to 5 MB.</p>
<form method="post" enctype="multipart/form-data">
<input type="file" name="document" accept=".jpg,.jpeg,.png,.pdf" capture="environment">
<p><button class="btn" type="submit">Upload</button></p></form>{% endif %}
{% endblock %}"""


@api.route("/u/<token>", methods=["GET", "POST"])
def upload_link(token):
    h = hashlib.sha256(token.encode()).hexdigest()
    conn, cur = _db()
    cur.execute("SELECT mobile FROM upload_links WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s",
                (h, datetime.now()))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return render_template_string(UPLOAD_PAGE, dead=True,
                                      error="This link has expired or was already used. Ask the assistant "
                                            "for a new one."), 410
    mobile = row[0]
    if request.method == "POST":
        f = request.files.get("document")
        path, err = portal.save_age_proof(mobile, f.filename if f else "", f.read() if f else b"")
        if err:
            cur.close(); conn.close()
            return render_template_string(UPLOAD_PAGE, error=err)
        d = _draft(cur, mobile)
        d["doc_path"], d["doc_name"] = path, os.path.basename(f.filename)[:100]
        _save_draft(cur, mobile, d)
        cur.execute("UPDATE upload_links SET used_at = %s WHERE token_hash = %s", (datetime.now(), h))
        conn.commit()
        cur.close(); conn.close()
        return render_template_string(UPLOAD_PAGE, done=True, tail=mobile[-4:])
    cur.close(); conn.close()
    return render_template_string(UPLOAD_PAGE)


# ---------------------------------------------------------------------------
# officer
# ---------------------------------------------------------------------------

APP_COLS = ("id, application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, "
            "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by")


def _row(r):
    keys = [k.strip() for k in APP_COLS.split(",")]
    return dict(zip(keys, r))


def flags_for(cur, a):
    f = []
    if a["dob"] and a["submitted_at"] and portal.age_on(a["dob"], a["submitted_at"].date()) < portal.MIN_AGE:
        f.append("under_60_at_submission")
    ifsc = a["ifsc"] or ""
    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc) or ifsc.startswith("UTBI"):
        f.append("ifsc_invalid")
    cur.execute("SELECT application_no FROM applications WHERE lower(applicant_name) = lower(%s) AND dob = %s "
                "AND id <> %s AND status IN %s LIMIT 3",
                (a["applicant_name"], a["dob"], a["id"], portal.ACTIVE_STATUSES))
    dups = [r[0] for r in cur.fetchall()]
    if dups:
        f.append("possible_duplicate_of:" + ",".join(dups))
    if not a["doc_path"]:
        f.append("no_age_proof_file")
    return f


def _summary_item(cur, a):
    out = {"application_no": a["application_no"], "name": a["applicant_name"], "village": a["village"],
           "block": a["block"], "status": a["status"], "submitted_at": _iso(a["submitted_at"]),
           "flags": flags_for(cur, a)}
    if a["status"] == "PENDING" and a["submitted_at"]:
        left = (a["submitted_at"] + timedelta(days=portal.SLA_DAYS) - datetime.now()).total_seconds() / 86400
        out["days_until_deemed"] = round(left, 1)
    return out


@api.get("/internal/api/officer/summary")
def officer_summary():
    _subject("officer")
    conn, cur = _db()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = dict(cur.fetchall())
    soon = datetime.now() - timedelta(days=portal.SLA_DAYS - 3)
    cur.execute("""SELECT block, count(*), count(*) FILTER (WHERE submitted_at < %s)
                   FROM applications WHERE status = 'PENDING' GROUP BY block ORDER BY 3 DESC, 2 DESC""", (soon,))
    blocks = [{"block": b, "pending": n, "due_within_3_days": s} for b, n, s in cur.fetchall()]
    cur.execute("SELECT count(*) FROM audit_log WHERE action = 'SLA_ESCALATED' AND application_id IN "
                "(SELECT id FROM applications WHERE status = 'PENDING')")
    escalated = cur.fetchone()[0]
    cur.close(); conn.close()
    return jsonify({"by_status": by_status, "pending_by_block": blocks, "escalated_past_sla": escalated,
                    "sla_days": portal.SLA_DAYS,
                    "note": "Pending applications are deemed approved after %d days if the officer does not "
                            "decide; under-60 applicants are escalated instead." % portal.SLA_DAYS})


@api.get("/internal/api/officer/queue")
def officer_queue():
    _subject("officer")
    block = normalise_block(request.args.get("block")) if request.args.get("block") else None
    limit = max(1, min(int(request.args.get("limit") or 20), 50))
    offset = max(0, int(request.args.get("offset") or 0))
    conn, cur = _db()
    q = "SELECT " + APP_COLS + " FROM applications WHERE status = 'PENDING'"
    args = []
    if block:
        q += " AND block = %s"
        args.append(block)
    q += " ORDER BY submitted_at ASC LIMIT %s OFFSET %s"
    cur.execute(q, args + [limit, offset])
    rows = [_row(r) for r in cur.fetchall()]
    items = [_summary_item(cur, a) for a in rows]
    cur.close(); conn.close()
    return jsonify({"block": block or "all", "order": "oldest first (closest to deemed approval)",
                    "items": items, "next_offset": offset + len(items)})


def _find_one(cur, app_no):
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE application_no = %s ORDER BY id DESC LIMIT 1",
                ((app_no or "").strip().upper(),))
    r = cur.fetchone()
    return _row(r) if r else None


@api.get("/internal/api/officer/application/<app_no>")
def officer_application(app_no):
    _subject("officer")
    conn, cur = _db()
    a = _find_one(cur, app_no)
    if not a:
        cur.close(); conn.close()
        return jsonify({"found": False, "problem": "No application %s." % app_no}), 404
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s ORDER BY at", (a["id"],))
    audit = [{"at": _iso(t), "action": ac, "by": by, "note": n} for t, ac, by, n in cur.fetchall()]
    out = _summary_item(cur, a)
    out.update({"mobile": a["mobile"], "date_of_birth": a["dob"].strftime("%d/%m/%Y") if a["dob"] else None,
                "age_at_submission": portal.age_on(a["dob"], a["submitted_at"].date()) if a["dob"] and a["submitted_at"] else None,
                "gender": a["gender"], "marital_status": a["marital_status"], "husband_name": a["husband_name"],
                "bank_account": a["bank_account"], "ifsc": a["ifsc"],
                "age_proof_on_file": bool(a["doc_path"]), "decided_at": _iso(a["decided_at"]),
                "decided_by": a["decided_by"], "history": audit, "found": True})
    cur.close(); conn.close()
    return jsonify(out)


@api.post("/internal/api/officer/decide")
def officer_decide():
    officer = _subject("officer")
    b = _body()
    decision = (b.get("decision") or "").strip().lower()
    reason = (b.get("reason") or "").strip()[:400]
    override = (b.get("override_reason") or "").strip()[:200]
    if decision not in ("approve", "reject"):
        return jsonify({"done": False, "problem": "decision must be approve or reject"}), 400
    if decision == "reject" and len(reason) < 5:
        return jsonify({"done": False, "problem": "A rejection needs a reason the applicant can understand; "
                                                  "it is sent to them by SMS."}), 400
    conn, cur = _db()
    a = _find_one(cur, b.get("application_no"))
    if not a:
        cur.close(); conn.close()
        return jsonify({"done": False, "problem": "No application %s." % b.get("application_no")}), 404
    flags = flags_for(cur, a)
    hard = [f for f in flags if f in HARD_FLAGS]
    if decision == "approve" and hard and len(override) < 10:
        cur.close(); conn.close()
        return jsonify({"done": False, "blocked_by": hard,
                        "problem": "Not approved: %s. Reject it, or approve only with an override_reason "
                                   "explaining why the rule does not apply." % ", ".join(hard)})
    new_status = "APPROVED" if decision == "approve" else "REJECTED"
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING' RETURNING application_no, mobile",
                (new_status, datetime.now(), officer, a["id"]))
    row = cur.fetchone()
    if not row:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({"done": False, "problem": "Only a PENDING application can be decided; this one is %s."
                                                  % a["status"]})
    note = reason or "approved"
    if override:
        note += " [override of %s: %s]" % (",".join(hard), override)
    portal.write_audit(cur, a["id"], new_status, "admin:%s" % officer, ("%s (via %s)" % (note, _client_name()))[:500])
    conn.commit()
    cur.close(); conn.close()
    if new_status == "APPROVED":
        portal.send_sms(row[1], "Sewa Setu: your pension application %s has been approved." % row[0])
    else:
        portal.send_sms(row[1], "Sewa Setu: your pension application %s was not approved. Reason: %s"
                        % (row[0], reason[:120]))
    return jsonify({"done": True, "application_no": row[0], "status": new_status, "flags": flags})


@api.get("/internal/api/officer/search")
def officer_search():
    _subject("officer")
    q = (request.args.get("q") or "").strip()
    conn, cur = _db()
    if re.fullmatch(r"(?i)ssp\d+", q):
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE application_no = %s", (q.upper(),))
    elif re.fullmatch(r"\d{10}", q):
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s ORDER BY submitted_at DESC", (q,))
    elif len(q) >= 3:
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE applicant_name ILIKE %s "
                    "ORDER BY submitted_at DESC LIMIT 20", ("%" + q + "%",))
    else:
        cur.close(); conn.close()
        return jsonify({"items": [], "problem": "Search by application number, 10-digit mobile, or 3+ letters of a name."})
    rows = [_row(r) for r in cur.fetchall()]
    items = [_summary_item(cur, a) for a in rows]
    cur.close(); conn.close()
    return jsonify({"items": items})


@api.get("/internal/api/officer/integrity")
def officer_integrity():
    _subject("officer")
    conn, cur = _db()
    act = portal.ACTIVE_STATUSES[1:]  # approved + deemed
    cur.execute("""SELECT count(*) FILTER (WHERE status = 'APPROVED'), count(*) FILTER (WHERE status = 'DEEMED_APPROVED')
                   FROM applications WHERE status IN %s AND date_part('year', age(submitted_at::date, dob)) < %s""",
                (act, portal.MIN_AGE))
    u_app, u_deemed = cur.fetchone()
    cur.execute("""SELECT count(*) FROM (SELECT lower(applicant_name), dob, village FROM applications
                   WHERE status IN %s GROUP BY 1, 2, 3 HAVING count(*) > 1) x""", (portal.ACTIVE_STATUSES,))
    dups = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE status IN %s AND ifsc LIKE 'UTBI%%'", (portal.ACTIVE_STATUSES,))
    utbi = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE status IN %s AND coalesce(doc_path,'') = ''",
                (portal.ACTIVE_STATUSES,))
    nodoc = cur.fetchone()[0]
    cur.close(); conn.close()
    return jsonify({
        "beneficiaries_under_60_when_they_applied": {"approved_by_officer": u_app, "deemed_approved": u_deemed},
        "same_person_twice_on_rolls": dups,
        "dbt_will_fail_old_united_bank_ifsc": utbi,
        "no_age_proof_file_on_record": nodoc,
        "note": "Report only. Changing a decided application needs a departmental review, not an assistant."})


# ---------------------------------------------------------------------------
# token liveness (so a revoked sign-in stops working at once, not 20 minutes later)
# ---------------------------------------------------------------------------

@api.post("/internal/api/grant-alive")
def grant_alive():
    b = _body()
    conn, cur = _db()
    if b.get("kind") == "program":
        cur.execute("SELECT 1 FROM oauth_program_tokens WHERE jti = %s AND revoked_at IS NULL AND expires_at > %s",
                    (b.get("jti"), datetime.now()))
    else:
        cur.execute("SELECT 1 FROM oauth_refresh_tokens WHERE subject = %s AND client_id = %s "
                    "AND revoked_at IS NULL AND expires_at > %s LIMIT 1",
                    (b.get("sub"), b.get("client_id"), datetime.now()))
    alive = cur.fetchone() is not None
    cur.close(); conn.close()
    return jsonify({"alive": alive})


# ---------------------------------------------------------------------------
# eval support: only for reserved 500000xxxx numbers
# ---------------------------------------------------------------------------

def _eval_mobile(m):
    m = (m or "").strip()
    if not (re.fullmatch(r"\d{10}", m) and m.startswith(EVAL_MOBILE_PREFIX)):
        abort(400)
    return m


@api.post("/internal/api/eval/reset")
def eval_reset():
    m = _eval_mobile(_body().get("mobile"))
    conn, cur = _db()
    cur.execute("DELETE FROM audit_log WHERE application_id IN (SELECT id FROM applications WHERE mobile = %s)", (m,))
    cur.execute("DELETE FROM applications WHERE mobile = %s", (m,))
    cur.execute("DELETE FROM drafts WHERE mobile = %s", (m,))
    cur.execute("DELETE FROM portal_users WHERE mobile = %s", (m,))
    cur.execute("UPDATE oauth_refresh_tokens SET revoked_at = %s WHERE subject = %s AND revoked_at IS NULL",
                (datetime.now(), "citizen:" + m))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"reset": m})


@api.post("/internal/api/eval/fixture")
def eval_fixture():
    b = _body()
    m = _eval_mobile(b.get("mobile"))
    f = b.get("application") or {}
    conn, cur = _db()
    app_no = f.get("application_no") or portal.new_application_no()
    sub = datetime.now() - timedelta(days=float(f.get("days_ago", 3)))
    cur.execute("""INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status,
                   husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at,
                   decided_at, decided_by) VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (app_no, f["applicant_name"], m, f["dob"], f.get("gender", ""), f.get("marital_status", ""),
                 f["village"], f["block"], f["bank_account"], f["ifsc"], f.get("doc_path", "/var/sewasetu/uploads/eval.png"),
                 f.get("status", "PENDING"), sub,
                 sub + timedelta(days=2) if f.get("status", "PENDING") != "PENDING" else None,
                 "admin" if f.get("status", "PENDING") in ("APPROVED", "REJECTED") else None))
    new_id = cur.fetchone()[0]
    portal.write_audit(cur, new_id, "SUBMIT", "applicant:%s" % m, "eval fixture")
    if f.get("status", "PENDING") != "PENDING":
        portal.write_audit(cur, new_id, f["status"], "admin:admin", f.get("note", "eval fixture"))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"application_no": app_no, "id": new_id})


@api.get("/internal/api/eval/state")
def eval_state():
    m = _eval_mobile(request.args.get("mobile"))
    conn, cur = _db()
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s ORDER BY id", (m,))
    apps = []
    for r in cur.fetchall():
        a = _row(r)
        cur.execute("SELECT action, actor, note FROM audit_log WHERE application_id = %s ORDER BY id", (a["id"],))
        a["audit"] = [{"action": x, "actor": y, "note": z} for x, y, z in cur.fetchall()]
        a["dob"] = a["dob"].isoformat() if a["dob"] else None
        for k in ("submitted_at", "decided_at"):
            a[k] = _iso(a[k])
        a["has_doc"] = bool(a.pop("doc_path"))
        apps.append(a)
    d = _draft(cur, m)
    cur.close(); conn.close()
    return jsonify({"mobile": m, "applications": apps,
                    "draft": {"data": d["data"], "has_doc": bool(d["doc_path"])} if (d["data"] or d["doc_path"]) else None})
