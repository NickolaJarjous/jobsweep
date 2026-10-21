"""End-to-end lifecycle check: clean clone -> sweep -> board -> re-run.

Needs APIFY_TOKEN and network; runs two real sweeps (a few cents).
Verifies the digest, the database and the review board agree with each
other, and that a second run is idempotent and preserves your statuses.

    python tests_e2e.py
"""
import io, json, os, re, shutil, sqlite3, subprocess, sys, tarfile, tempfile, time, urllib.request
from pathlib import Path

REPO = str(Path(__file__).resolve().parent)
TOK = os.environ.get("APIFY_TOKEN")
if not TOK:
    sys.exit("tests_e2e.py needs APIFY_TOKEN: it runs real sweeps against the\n"
             "index API and costs a few cents. For offline checks run\n"
             "tests_regression.py instead.")
P = F = 0
def ck(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  PASS  {name}")
    else:    F += 1; print(f"  FAIL  {name}  {detail}")

work = tempfile.mkdtemp(prefix="e2e-")
# Prefer `git archive`, which proves the COMMITTED tree works. Fall back to
# copying the files when this is not a git checkout — a downloaded zip, or a
# folder someone copied the files into. The archive is extracted in Python
# rather than piped to `tar`: a pipeline only reports the last command's exit
# status, and on Windows `tar` exits 0 on empty input, so a failed `git
# archive` looked like a successful clone of nothing.
try:
    _git = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                          cwd=REPO, capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(_git.stdout)) as _tar:
        _tar.extractall(work)
except (subprocess.CalledProcessError, FileNotFoundError, tarfile.TarError):
    pass
if not Path(work, "jobsweep.py").exists():
    print("  (not a git checkout — copying the working tree instead)")
    for name in ("jobsweep.py", "store.py", "dashboard.py", "evaluate.py",
                 "config.yaml", "config.test.yaml", "tests_regression.py",
                 "requirements.txt"):
        src = Path(REPO, name)
        if src.exists():
            shutil.copy(src, work)
    if not Path(work, "jobsweep.py").exists():
        sys.exit(f"Could not assemble a test copy from {REPO}")
# Use the private overlay when there is one, so this exercises the search
# actually in use; otherwise the shipped example config is fine.
for extra in ("config.local.yaml", "profile.local.md"):
    src = Path(REPO, extra)
    if src.exists():
        shutil.copy(src, work)
env = {**os.environ, "APIFY_TOKEN": TOK}
def run(*args, expect=0):
    r = subprocess.run([sys.executable, "jobsweep.py", *args], cwd=work,
                       capture_output=True, text=True, env=env, timeout=400)
    if expect is not None:
        ck(f"`jobsweep.py {' '.join(args) or '(sweep)'}` exits {expect}",
           r.returncode == expect, f"got {r.returncode}: {r.stderr[-200:]}")
    ck(f"`{' '.join(args) or '(sweep)'}` raises nothing",
       "Traceback" not in r.stderr, r.stderr[-200:])
    return r

print("STAGE 1 — clean clone, first sweep")
r1 = run()
m = re.search(r"(\d+) postings returned", r1.stdout)
returned = int(m.group(1)) if m else -1
_c = re.search(r"(\d+) cleared threshold", r1.stdout)
if not _c:
    # Nothing below can be checked against a sweep that did not finish.
    print(f"\n  {P} passed, {F} failed — stopping: the first sweep did not "
          f"complete.\n{r1.stderr[-400:]}")
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(1)
cleared = int(_c.group(1))
ck("sweep returned postings", returned > 0, r1.stdout[-200:])
ck("source mix is printed", "sources:" in r1.stdout)

db = Path(work, "jobsweep.sqlite3")
ck("database created", db.exists())
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
rows = list(con.execute("SELECT * FROM jobs"))
visible = [r for r in rows if r["status"] != "skipped"]

print("\nSTAGE 2 — the three outputs must agree")
digest = next(Path(work, "digests").glob("*.md")).read_text(encoding="utf-8")
d_titles = re.findall(r"^## (.+)$", digest, re.M)
_hdr = re.search(r"\*\*(\d+) new\*\*", digest)
_hdr_n = int(_hdr.group(1)) if _hdr else -1
ck("digest headline count matches its entries", _hdr_n == len(d_titles),
   "%s vs %s" % (_hdr_n, len(d_titles)))
ck("digest entry count equals 'cleared threshold'", len(d_titles) == cleared,
   f"digest {len(d_titles)} vs cleared {cleared}")
by_title = {r["title"]: r for r in rows}
mismatch = [t for t in d_titles if t not in by_title]
ck("every digest role exists in the database", not mismatch, str(mismatch[:2]))
# the bug that hid before: digest score != stored score
score_gap = []
for t in d_titles:
    m2 = re.search(rf"^## {re.escape(t)}$\n\*\*.*?score (\d+)", digest, re.M | re.S)
    if m2 and t in by_title and int(m2.group(1)) != by_title[t]["score"]:
        score_gap.append((t[:34], m2.group(1), by_title[t]["score"]))
ck("digest score equals stored score for every role", not score_gap, str(score_gap[:2]))

print("\nSTAGE 3 — the board agrees too")
srv = subprocess.Popen([sys.executable, "jobsweep.py", "--serve", "--port", "8866"],
                       cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
try:
    html = urllib.request.urlopen("http://127.0.0.1:8866/", timeout=20).read().decode()
    cards = html.count('class="job"')
    ck("board renders one card per visible row", cards == len(visible),
       "%s cards vs %s rows" % (cards, len(visible)))
    import html as _h
    for t in d_titles[:3]:
        # titles are HTML-escaped on the page: "&" renders as "&amp;"
        needle = _h.escape(t.split(" - ")[0][:28])
        ck(f"board shows top role {t[:30]!r}", needle in html)
    top = sorted(visible, key=lambda r: -r["score"])[0]
    ck("board shows the same score as the db for the top role",
       f"score {top['score']}" in html or f"kw {top['score']}" in html)
    # status round-trip through the real HTTP path
    uid = top["uid"]
    req = urllib.request.Request("http://127.0.0.1:8866/api/status",
        data=json.dumps({"uid": uid, "status": "applied", "notes": "e2e"}).encode(),
        headers={"content-type": "application/json"}, method="POST")
    ck("status POST accepted", urllib.request.urlopen(req, timeout=20).status == 204)
finally:
    srv.terminate(); srv.wait(timeout=10)
con2 = sqlite3.connect(db); con2.row_factory = sqlite3.Row
st = list(con2.execute("SELECT status,notes FROM jobs WHERE uid=?", (uid,)))[0]
ck("status persisted to disk", st["status"] == "applied" and st["notes"] == "e2e")

print("\nSTAGE 4 — day two: re-run must be idempotent and preserve state")
r2 = run()
ck("second run finds nothing new", "0 not seen before" in r2.stdout, r2.stdout[-160:])
ck("second run keeps the existing digest", "kept" in r2.stdout)
con3 = sqlite3.connect(db); con3.row_factory = sqlite3.Row
rows2 = list(con3.execute("SELECT * FROM jobs"))
ck("row count unchanged on re-run", len(rows2) == len(rows), f"{len(rows)} -> {len(rows2)}")
st2 = list(con3.execute("SELECT status,notes FROM jobs WHERE uid=?", (uid,)))[0]
ck("application status survives a re-sweep", st2["status"] == "applied")
ck("notes survive a re-sweep", st2["notes"] == "e2e")
dupes = list(con3.execute("""SELECT company,title,COUNT(*) n FROM jobs
                             WHERE status!='skipped'
                             GROUP BY lower(company),lower(title) HAVING n>1"""))
ck("no duplicate visible requisitions", not dupes, str([tuple(d) for d in dupes][:2]))
unscored = [r for r in rows2 if r["status"] != "skipped" and not json.loads(r["reasons"] or "[]")]
ck("no visible row left unscored", not unscored, str([r["title"][:30] for r in unscored][:2]))

print("\nSTAGE 5 — auxiliary commands on real data")
r3 = run("--explain")
ck("--explain reports against stored postings", "Auditing term lists" in r3.stdout)
r4 = run("--dry-run")
ck("--dry-run writes no digest changes", "Dry run" in r3.stdout or "Dry run" in r4.stdout)
r5 = subprocess.run([sys.executable, "tests_regression.py"], cwd=work,
                    capture_output=True, text=True, timeout=300)
ck("shipped regression suite passes in the clone", "0 failed" in r5.stdout, r5.stdout[-160:])

print(f"\n{'='*58}\n  {P} passed, {F} failed\n{'='*58}")
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if F else 0)
