"""/eval-dashboard: the invented citizens, their conversations, what was checked, what passed."""

import glob
import html
import json
import os

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

EVAL_DIR = os.environ.get("EVAL_DIR", os.path.join(os.path.dirname(__file__), "evals"))
RESULTS_DIR = os.environ.get("EVAL_RESULTS_DIR", os.path.join(EVAL_DIR, "results"))


def load_personas():
    with open(os.path.join(EVAL_DIR, "personas.json"), encoding="utf-8") as f:
        return json.load(f)


def load_runs():
    runs = []
    for p in sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                runs.append(json.load(f))
        except Exception:
            continue
    return runs


e = lambda s: html.escape(str(s if s is not None else ""))

CSS = """
:root{--ink:#1E2B3A;--paper:#F6F8F7;--pass:#2F7A4F;--fail:#B4442F;--part:#9A6B12;--rule:#D5DBD8;--soft:#5B6773}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
 font:16px/1.55 "Noto Sans","Noto Sans Bengali",system-ui,sans-serif}
main{max-width:780px;margin:0 auto;padding:40px 20px 80px}
h1{font:600 2rem/1.2 "Noto Serif",Georgia,serif;margin:0 0 .4rem}
h2{font:600 1.35rem/1.3 "Noto Serif",Georgia,serif;margin:3rem 0 1rem;padding-top:1rem;border-top:2px solid var(--ink)}
p.lede{color:var(--soft);max-width:62ch;margin:0 0 1.5rem}
.runs{display:flex;flex-wrap:wrap;gap:6px 18px;margin:1rem 0 0;padding:0;list-style:none;font-size:.95rem}
.runs b{font-variant-numeric:tabular-nums}
.person{padding:1.6rem 0;border-bottom:1px solid var(--rule)}
.person header{display:flex;justify-content:space-between;align-items:baseline;gap:1rem}
.person h3{font:600 1.2rem/1.3 "Noto Serif","Noto Serif Bengali",Georgia,serif;margin:0}
.who{color:var(--soft);font-size:.92rem;margin:.15rem 0 .8rem}
blockquote{margin:.6rem 0 1rem;padding:0 0 0 1rem;border-left:3px solid var(--ink);
 font:italic 1.12rem/1.5 "Noto Serif","Noto Serif Bengali",Georgia,serif}
.why{font-size:.95rem;margin:.4rem 0}
.mark{font-weight:700;white-space:nowrap}
.ok{color:var(--pass)} .no{color:var(--fail)} .na{color:var(--part)}
ul.checks{list-style:none;padding:0;margin:.6rem 0}
ul.checks li{padding:.2rem 0 .2rem 1.6rem;text-indent:-1.6rem;font-size:.95rem}
details{margin-top:.6rem}
summary{cursor:pointer;color:var(--ink);text-decoration:underline;text-underline-offset:3px}
summary:focus-visible,a:focus-visible{outline:3px solid var(--part);outline-offset:2px}
.turn{margin:.7rem 0;padding:.5rem .8rem;border-radius:4px;font-size:.93rem;white-space:pre-wrap}
.turn.citizen{background:#E7ECEA}
.turn.assistant{background:#fff;border:1px solid var(--rule)}
.turn.tool{background:transparent;color:var(--soft);font:13px/1.45 ui-monospace,Menlo,monospace;padding:.2rem .8rem}
.turn .r{display:block;font-size:.8rem;color:var(--soft);margin-bottom:.2rem}
table{border-collapse:collapse;width:100%;font-size:.93rem}
td,th{text-align:left;padding:.45rem .5rem;border-bottom:1px solid var(--rule);vertical-align:top}
.empty{padding:1rem 1.2rem;border:1px dashed var(--soft);border-radius:4px}
code{font-size:.9em}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
"""


def mark(v):
    if v is True:
        return '<span class="mark ok">&#10003; passed</span>'
    if v is False:
        return '<span class="mark no">&#10007; failed</span>'
    return '<span class="mark na">not run</span>'


def render():
    personas = load_personas()
    runs = load_runs()
    latest = runs[0] if runs else None
    by_id = {r["persona_id"]: r for r in (latest or {}).get("results", [])}

    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>Sewa Setu assistant: evaluations</title>',
           '<link rel="preconnect" href="https://fonts.googleapis.com">',
           '<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;600;700&family=Noto+Sans+Bengali:wght@400;600'
           '&family=Noto+Serif:ital,wght@0,600;1,400&family=Noto+Serif+Bengali:wght@600&display=swap" rel="stylesheet">',
           '<style>', CSS, '</style></head><body><main>',
           '<h1>Who we tested the pension assistant with</h1>',
           '<p class="lede">Each person below is invented, but built from the portal\'s real data and the '
           'ways people actually get stuck. A citizen-simulator model plays them; the assistant is a small '
           'model connected to our citizen or officer MCP server through the official MCP client, signed in '
           'through the portal\'s own OTP and consent pages. After each conversation we read the portal\'s '
           'database and check what must be true.</p>']

    if latest:
        n = len(latest["results"])
        p = sum(1 for r in latest["results"] if r.get("passed"))
        out.append('<ul class="runs">')
        for r in runs[:6]:
            rn = len(r["results"]); rp = sum(1 for x in r["results"] if x.get("passed"))
            out.append('<li>%s: <b>%d of %d</b> people fully served (assistant %s)</li>'
                       % (e(r.get("started_at", "")[:16].replace("T", " ")), rp, rn, e(r.get("assistant_model", ""))))
        out.append('</ul>')
        out.append('<p class="lede" style="margin-top:.8rem">Latest run: <b>%d of %d</b> passed every check. '
                   'MCP client: %s.</p>' % (p, n, e(latest.get("client", ""))))
    else:
        out.append('<div class="empty">No evaluation has been run on this deployment yet. Run '
                   '<code>docker compose exec assistant python -m evals.run_evals</code>; results appear '
                   'here.</div>')

    for group, title in (("citizen", "Citizens"), ("officer", "Block officers")):
        out.append('<h2>%s</h2>' % title)
        for ps in [x for x in personas if x["server"] == group]:
            r = by_id.get(ps["id"])
            out.append('<article class="person" id="%s"><header><h3>%s</h3>%s</header>'
                       % (e(ps["id"]), e(ps["name"]), mark(r.get("passed") if r else None)))
            out.append('<p class="who">%s</p>' % e(ps["who"]))
            out.append('<blockquote>%s</blockquote>' % e(ps["opening_line"]))
            out.append('<p class="why"><b>Why this person:</b> %s</p>' % e(ps["why"]))
            out.append('<p class="why"><b>What must be true afterwards:</b> %s</p>' % e(ps["expect_text"]))
            if r:
                out.append('<ul class="checks">')
                for c in r.get("checks", []):
                    out.append('<li>%s %s <span style="color:var(--soft)">%s</span></li>'
                               % ('<span class="ok">&#10003;</span>' if c["passed"] else '<span class="no">&#10007;</span>',
                                  e(c["name"]), e(c.get("detail", ""))))
                out.append('</ul>')
                if r.get("error"):
                    out.append('<p class="no">Run error: %s</p>' % e(r["error"]))
                out.append('<details><summary>Read the conversation (%d turns)</summary>' % len(r.get("transcript", [])))
                for t in r.get("transcript", []):
                    role = t.get("role")
                    label = {"citizen": ps["name"].split(" (")[0], "assistant": "Assistant", "tool": "Tool call"}.get(role, role)
                    out.append('<div class="turn %s"><span class="r">%s</span>%s</div>' % (e(role), e(label), e(t.get("text", ""))))
                out.append('</details>')
            out.append('</article>')

    if latest and latest.get("security"):
        out.append('<h2>Sign-in and access checks</h2><p class="lede">Deterministic checks, no model involved: '
                   'run against the live endpoints with real tokens from the portal.</p><table>'
                   '<tr><th>Check</th><th>Result</th><th>Detail</th></tr>')
        for c in latest["security"]:
            out.append('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (e(c["name"]), mark(c["passed"]), e(c.get("detail", ""))))
        out.append('</table>')

    if runs:
        out.append('<h2>What the evals changed</h2><p class="lede">See <code>evals/CHANGELOG.md</code> in the repository '
                   'for each failure we saw and the change it led to.</p>')
    out.append('</main></body></html>')
    return "".join(out)


async def page(_req):
    return HTMLResponse(render())


async def data(_req):
    return JSONResponse({"personas": load_personas(), "runs": load_runs()[:5]})


routes = [Route("/eval-dashboard", page), Route("/eval-dashboard/data.json", data)]
