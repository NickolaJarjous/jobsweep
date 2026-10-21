"""Local review board: `python jobsweep.py --serve`.

A single-page view of everything swept, with the status and notes you set
written straight back to the SQLite store. Standard library only — no Node,
no framework, no build step.
"""

from __future__ import annotations

import html
import json
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobsweep</title>
<style>
  :root {{
    --bg:#f7f7f5; --card:#fff; --ink:#1a1a18; --muted:#6b6b66;
    --line:#e3e3df; --accent:#2f6f4f; --warn:#8a5a1a; --bad:#8a2a2a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#17181a; --card:#1f2124; --ink:#e9e9e6; --muted:#9a9a94;
      --line:#2e3034; --accent:#6fbf8f; --warn:#d9a44a; --bad:#e06a6a;
    }}
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}}
  header{{padding:20px 16px 8px;max-width:1100px;margin:0 auto}}
  h1{{margin:0 0 2px;font-size:19px;letter-spacing:-.01em}}
  .sub{{color:var(--muted);font-size:13px}}
  .bar{{display:flex;gap:6px;flex-wrap:wrap;max-width:1100px;margin:14px auto 0;
        padding:0 16px}}
  .chip{{border:1px solid var(--line);background:var(--card);color:var(--ink);
         border-radius:999px;padding:5px 11px;font-size:12.5px;cursor:pointer;
         text-decoration:none;display:inline-block}}
  .chip.on{{background:var(--ink);color:var(--bg);border-color:var(--ink)}}
  main{{max-width:1100px;margin:0 auto;padding:14px 16px 56px}}
  .job{{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:13px 15px;margin-bottom:9px}}
  .top{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
  .t{{font-weight:600;font-size:15px}}
  .t a{{color:inherit;text-decoration:none}} .t a:hover{{text-decoration:underline}}
  .meta{{color:var(--muted);font-size:12.5px;margin-top:3px}}
  .why{{font-size:12.5px;color:var(--muted);margin-top:6px;font-style:italic}}
  .fit{{margin-left:auto;display:flex;gap:7px;align-items:center;flex-shrink:0}}
  .pill{{font-size:11.5px;padding:2px 8px;border-radius:999px;
         border:1px solid var(--line);white-space:nowrap}}
  .apply{{color:var(--accent);border-color:currentColor}}
  .consider{{color:var(--warn);border-color:currentColor}}
  .skip{{color:var(--bad);border-color:currentColor}}
  .ctl{{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}}
  select,input[type=text]{{background:var(--bg);color:var(--ink);
    border:1px solid var(--line);border-radius:7px;padding:6px 9px;font:inherit;
    font-size:13px}}
  input[type=text]{{flex:1;min-width:180px}}
  .saved{{color:var(--accent);font-size:12px;align-self:center;opacity:0}}
  .empty{{color:var(--muted);padding:30px 0;text-align:center}}
  .deets{{font-size:12.5px;color:var(--muted);margin-top:7px}}
  .deets ul{{margin:3px 0 0;padding-left:18px}}
  summary{{cursor:pointer}}
</style></head><body>
<header>
  <h1>jobsweep</h1>
  <div class="sub">{total} postings · {swept}</div>
</header>
<div class="bar">{chips}</div>
<main>{body}</main>
<script>
async function save(uid, el) {{
  const row = el.closest('.job');
  const status = row.querySelector('select').value;
  const notes  = row.querySelector('input[type=text]').value;
  const flag   = row.querySelector('.saved');
  const r = await fetch('/api/status', {{
    method:'POST', headers:{{'content-type':'application/json'}},
    body: JSON.stringify({{uid, status, notes}})
  }});
  flag.textContent = r.ok ? 'saved' : 'failed';
  flag.style.opacity = 1;
  setTimeout(()=>{{flag.style.opacity=0}}, 1400);
}}
</script>
</body></html>"""


def _job_html(r: sqlite3.Row) -> str:
    esc = html.escape
    try:
        reasons = ", ".join(json.loads(r["reasons"] or "[]"))
    except Exception:
        reasons = ""
    fit = ""
    if r["fit_score"] is not None:
        verdict = (r["fit_verdict"] or "consider").lower()
        fit = (f'<span class="pill {esc(verdict)}">{r["fit_score"]} · '
               f'{esc(verdict)}</span>')
    opts = "".join(
        f'<option value="{s}"{" selected" if r["status"] == s else ""}>{s}</option>'
        for s in store.STATUSES)
    title = esc(r["title"] or "—")
    # Only http(s) becomes a link: a javascript: URL from a hostile posting
    # would otherwise run in the page's origin when clicked.
    safe_url = r["url"] if (r["url"] or "").startswith(("http://", "https://")) else ""
    link = (f'<a href="{esc(safe_url)}" target="_blank" rel="noopener">{title}</a>'
            if safe_url else title)

    deets = ""
    if r["fit_json"]:
        try:
            d = json.loads(r["fit_json"])
            parts = []
            if d.get("one_liner"):
                parts.append(f'<div>{esc(str(d["one_liner"]))}</div>')
            for key in ("strengths", "gaps", "risks"):
                vals = [v for v in (d.get(key) or []) if v]
                if vals:
                    lis = "".join(f"<li>{esc(str(v))}</li>" for v in vals)
                    parts.append(f"<b>{key}</b><ul>{lis}</ul>")
            if parts:
                deets = ('<details class="deets"><summary>assessment</summary>'
                         + "".join(parts) + "</details>")
        except Exception:
            pass

    return f"""<div class="job">
  <div class="top"><span class="t">{link}</span>
    <span class="fit">{fit}<span class="pill">kw {r["score"] if r["score"] is not None else "—"}</span></span></div>
  <div class="meta">{esc(r["company"] or "")} · {esc(r["location"] or "—")}
    · <code>{esc(r["ats"] or "")}</code>{" · " + esc(r["salary"]) if r["salary"] else ""}
    {" · posted " + esc((r["posted_at"] or "")[:10]) if r["posted_at"] else ""}</div>
  {f'<div class="why">{esc(reasons)}</div>' if reasons else ""}
  {deets}
  <div class="ctl">
    <select onchange="save('{r["uid"]}', this)">{opts}</select>
    <input type="text" placeholder="notes" value="{esc(r["notes"] or "")}"
           onchange="save('{r["uid"]}', this)">
    <span class="saved"></span>
  </div>
</div>"""


def render(db: str | Path, status: str | None) -> str:
    with store.connect(db) as conn:
        rows = store.all_jobs(conn, status)
        counts = store.counts(conn)
        total = sum(counts.values())
        active = total - counts.get("skipped", 0)
    chips = [f'<a class="chip{"" if status else " on"}" href="/">all {active}</a>']
    for s in store.STATUSES:
        n = counts.get(s, 0)
        if n:
            chips.append(f'<a class="chip{" on" if status == s else ""}"'
                         f' href="/?status={s}">{s} {n}</a>')
    body = ("".join(_job_html(r) for r in rows) if rows
            else '<div class="empty">Nothing here yet — run a sweep.</div>')
    return PAGE.format(total=active, swept=f"{len(rows)} shown",
                       chips="".join(chips), body=body)


def serve(db: str | Path, port: int = 8765, open_browser: bool = True) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def do_GET(self):
            # Only the board itself. Browsers also ask for /favicon.ico on
            # every load, and answering that with a full render doubled the
            # database reads per page view.
            if self.path.split("?", 1)[0] != "/":
                self.send_error(404)
                return
            status = None
            if "?status=" in self.path:
                status = self.path.split("?status=", 1)[1].split("&")[0]
                if status not in store.STATUSES:
                    status = None
            page = render(db, status).encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if self.path != "/api/status":
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("content-length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                with store.connect(db) as conn:
                    hit = store.set_status(conn, data["uid"], data["status"],
                                           data.get("notes"))
            except Exception as e:
                self.send_error(400, str(e))
                return
            if not hit:
                # Reporting "saved" for a row that does not exist would hide a
                # mismatched database entirely.
                self.send_error(404, "no such job in this database")
                return
            self.send_response(204)
            self.end_headers()

    url = f"http://127.0.0.1:{port}/"
    print(f"Review board: {url}  (ctrl-c to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
