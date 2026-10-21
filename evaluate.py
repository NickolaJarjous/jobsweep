"""Optional LLM fit scoring.

The keyword scorer in jobsweep.py is cheap and runs on everything. This step
is slower and costs money, so it only runs on postings that already cleared
that bar, and every result is cached in the database — a posting is never
scored twice.

Needs ANTHROPIC_API_KEY. Without it, `--evaluate` reports that and exits
without touching anything.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("JOBSWEEP_MODEL", "claude-sonnet-4-5")

PROMPT = """You are screening a job posting for a specific candidate.

CANDIDATE PROFILE
{profile}

JOB POSTING
Title: {title}
Company: {company}
Location: {location} ({arrangement})
Salary: {salary}
Description:
{description}

Assess the fit honestly. A candidate is poorly served by optimistic scoring:
say plainly when a posting is a stretch or a mismatch.

Reply with ONLY a JSON object, no prose around it:
{{
  "fit_score": <0-100>,
  "recommendation": "apply" | "consider" | "skip",
  "strengths": ["<how the candidate matches, max 3>"],
  "gaps": ["<what they lack or what the posting demands they don't show, max 3>"],
  "risks": ["<anything that could rule them out, e.g. location, seniority>"],
  "one_liner": "<one sentence on whether to spend time on this>"
}}"""


def _call(prompt: str, api_key: str, timeout: int = 90) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 900,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        API, data=body, method="POST",
        headers={"content-type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return "".join(b.get("text", "") for b in payload.get("content", []))


def _parse(raw: str) -> dict | None:
    """Pull the JSON object out of the reply, tolerating stray prose."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or "fit_score" not in d:
        return None
    try:
        d["fit_score"] = max(0, min(100, int(d["fit_score"])))
    except (TypeError, ValueError):
        return None
    if d.get("recommendation") not in ("apply", "consider", "skip"):
        d["recommendation"] = "consider"
    return d


def score_row(row, profile: str, api_key: str) -> dict | None:
    prompt = PROMPT.format(
        profile=profile.strip()[:4000],
        title=row["title"], company=row["company"],
        location=row["location"] or "—",
        arrangement=row["arrangement"] or "unspecified",
        salary=row["salary"] or "not published",
        description=(row["description"] or "")[:6000],
    )
    try:
        return _parse(_call(prompt, api_key))
    except urllib.error.HTTPError as e:
        print(f"  ! {row['company']}: HTTP {e.code} {e.read()[:160]!r}",
              file=sys.stderr)
    except Exception as e:
        print(f"  ! {row['company']}: {e}", file=sys.stderr)
    return None


def api_key_or_none() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None
