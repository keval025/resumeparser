"""Smoke test of the live site: the full flow for account A, then proof that account B can't see A's data.

Get each account's access token: sign in on the live site, open the browser console (F12) on /app and run
    copy((await Auth.session()).access_token)
which copies the token to the clipboard. Use a second Google account (or a magic-link e-mail) for B.

PowerShell:
    $env:TOKEN_A = "<paste token of your main account>"
    $env:TOKEN_B = "<paste token of the second account>"
    python scripts/check_live.py https://your-app.onrender.com

Tokens expire after about an hour. The flow uses 1 parse, 2 matches, 1 tailored resume and 1 comparison
of account A's free quota (less if they are cached). --skip-flow only checks access control on existing data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
SAMPLE_PDF = ROOT / "data" / "uploads" / "sample_resume.pdf"

passed = failed = 0


def check(ok: bool, what: str, detail: object = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {what}")
    else:
        failed += 1
        print(f"  FAIL  {what}  {str(detail)[:300]}")
    return ok


def download(base: str, link: dict) -> httpx.Response:
    """Follow a signed download link (absolute on Supabase, relative in local mode)."""
    return httpx.get(urljoin(base + "/", link["url"]), timeout=60)


def wait(client: httpx.Client, path: str, key: str = "status", timeout: int = 240) -> dict:
    deadline = time.time() + timeout
    while True:
        body = client.get(path).json()
        if body.get(key) != "pending" or time.time() > deadline:
            return body
        time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--skip-flow", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    token_a, token_b = os.getenv("TOKEN_A"), os.getenv("TOKEN_B")
    if not token_a or not token_b:
        print("Set TOKEN_A and TOKEN_B first (see the top of this file).")
        return 2
    anon = httpx.Client(base_url=base, timeout=120)
    a = httpx.Client(base_url=base, timeout=180, headers={"Authorization": f"Bearer {token_a}"})
    b = httpx.Client(base_url=base, timeout=180, headers={"Authorization": f"Bearer {token_b}"})

    print("Public pages and health")
    health = anon.get("/health").json()
    check(health.get("status") == "ok" and health.get("auth") == "supabase" and health.get("storage") == "SupabaseStorage", "/health ok, Supabase auth + storage", health)
    for path in ("/", "/login", "/privacy", "/terms"):
        check(anon.get(path).status_code == 200, f"GET {path} is public")
    check(anon.get("/resumes").status_code == 401, "API without a token -> 401")
    check(anon.get("/resumes", headers={"Authorization": "Bearer nonsense"}).status_code == 401, "API with a bad token -> 401")
    config = anon.get("/config.js").text
    check("service_role" not in config and "sb_secret" not in config, "/config.js has no service-role key")

    me_a, me_b = a.get("/me").json(), b.get("/me").json()
    check(me_a.get("user", {}).get("id") and me_b.get("user", {}).get("id") and me_a["user"]["id"] != me_b["user"]["id"],
          f"two different accounts: A={me_a.get('user', {}).get('email')} B={me_b.get('user', {}).get('email')}", (me_a, me_b))

    if not args.skip_flow:
        print("\nFull flow as account A: upload -> match -> tailor -> compare -> export")
        with SAMPLE_PDF.open("rb") as f:
            up = a.post("/resumes", files={"file": ("sample_resume.pdf", f, "application/pdf")})
        check(up.status_code == 202, "upload resume", up.text)
        resume = wait(a, f"/resumes/{up.json()['id']}")
        check(resume.get("status") == "done", "resume parsed by Gemini", resume.get("error"))
        jd = (FIXTURES / "jd_python_developer.txt").read_text(encoding="utf-8")
        m = a.post("/match", data={"jd_text": jd, "resume_id": resume["id"]})
        match = wait(a, f"/match/{m.json()['match_id']}")
        check(match.get("status") == "done" and isinstance(match.get("score"), int), f"match scored ({match.get('score')})", match.get("error"))
        t = a.post("/tailor", json={"match_id": match["match_id"]})
        tailor = wait(a, f"/tailor/{t.json()['tailor_id']}")
        check(tailor.get("status") == "done", f"tailored ({tailor.get('old_score')} -> {tailor.get('new_score')})", tailor.get("error"))
        docx = a.post(f"/tailor/{tailor['tailor_id']}/export", json={"format": "docx"})
        if docx.status_code == 409:
            check(True, "export blocked while placeholders remain (expected for unconfirmed [X] values)")
        else:
            link = docx.json()
            file = download(base, link)
            check(file.status_code == 200 and file.content[:2] == b"PK", "DOCX export downloads through a signed link", docx.text)
        jobs = [{"text": jd, "label": "Python Developer"}, {"text": (FIXTURES / "jd_product_analyst.txt").read_text(encoding="utf-8"), "label": "Product Analyst"}]
        c = a.post("/compare", data={"resume_id": resume["id"], "jobs": json.dumps(jobs)})
        comparison = wait(a, f"/compare/{c.json()['compare_id']}")
        check(comparison.get("status") == "done" and len(comparison["insights"]["ranking"]) == 2, "comparison ranked 2 jobs")
        pdf = a.get(f"/compare/{comparison['compare_id']}/export?format=pdf")
        check(pdf.status_code == 200 and download(base, pdf.json()).content.startswith(b"%PDF"), "comparison PDF report (Chromium on the server)", pdf.text)

    print("\nAccount B cannot see or touch account A's data")
    ids = {
        "resume": [r["id"] for r in a.get("/resumes").json()],
        "match": [m["match_id"] for m in a.get("/matches").json()],
        "tailor": [t["tailor_id"] for t in a.get("/tailors").json()],
        "compare": [c["compare_id"] for c in a.get("/compares").json()],
    }
    check(any(ids.values()), f"account A has data ({ {k: len(v) for k, v in ids.items()} })")
    b_lists = {path: {json.dumps(x) for x in b.get(path).json()} for path in ("/resumes", "/matches", "/tailors", "/compares")}
    leaked = [i for values in ids.values() for i in values if any(i in row for rows in b_lists.values() for row in rows)]
    check(not leaked, "none of A's ids appear in B's lists", leaked)
    probes = (
        [f"/resumes/{i}" for i in ids["resume"]] + [f"/resumes/{i}/file" for i in ids["resume"]]
        + [f"/match/{i}" for i in ids["match"]] + [f"/tailor/{i}" for i in ids["tailor"]]
        + [f"/compare/{i}" for i in ids["compare"]] + [f"/compare/{i}/export?format=csv" for i in ids["compare"]]
    )
    statuses = {p: b.get(p).status_code for p in probes}
    check(all(s == 404 for s in statuses.values()), f"B gets 404 on all {len(probes)} of A's records", {p: s for p, s in statuses.items() if s != 404})
    if ids["match"]:
        r = b.post("/tailor", json={"match_id": ids["match"][0]})
        check(r.status_code == 404, "B cannot tailor A's match", r.status_code)
    if ids["resume"]:
        r = b.post("/match", data={"jd_text": "x" * 80, "resume_id": ids["resume"][0]})
        check(r.status_code == 404, "B cannot match against A's resume", r.status_code)

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
