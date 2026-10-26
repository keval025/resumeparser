# Resume Parser

Turns resume PDFs (text-based **or scanned**) into one validated JSON record: name, contact
info, links, summary, education, experience, projects, skills, certifications and achievements.
Comes with a web UI, a JSON API and a batch CLI. Uses **Google Gemini** for structured extraction.

**Job Match:** upload a resume, paste a job description, and get a reproducible **match score out of 100**
with a per-component breakdown, matched / missing skills, and specific, grounded suggestions to improve
the resume for that job (see [Job Match](#job-match-resume--job-description)).

**Tailor My Resume:** after a match, apply the AI suggestions, review and edit every change side by side,
and download a job-specific resume as **PDF and DOCX** in three ATS-friendly templates, with the
before/after score (see [Tailor My Resume](#tailor-my-resume)).

**Compare Jobs:** score one resume against 2–10 jobs, rank them and see where to apply first (see [Compare Jobs](#compare-jobs)).

**Multi-user web app:** Google / magic-link sign-in (Supabase Auth), per-user data in Supabase Postgres, private
file storage, free-plan limits, a public landing page, and Docker deployment to Render. The step-by-step setup is
in **[DEPLOY.md](DEPLOY.md)**; the design is under [Multi-user web app](#multi-user-web-app).
Without Supabase settings the app still runs exactly as before: single-user, no login, SQLite.

```
PDF ─► ingest ─► triage ─► layout extract / OCR ─► clean ─► sections ─► regex rules
     ─► Gemini (JSON schema, temperature 0) ─► grounding check ─► normalize ─► confidence ─► JSON
```

---

## Quick start (Windows, step by step)

Open **PowerShell** and go to the project folder:
```powershell
cd C:\Users\SIS\Desktop\resume_parser
```

### Step 1 - Virtual environment + requirements
```powershell
py -3 -m venv .venv                       # create it (once)
.\.venv\Scripts\Activate.ps1              # activate it; your prompt now starts with (.venv)
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium     # once: the headless browser used for PDF export (~150 MB)
```
If PowerShell says *"running scripts is disabled on this system"*, run this once, then activate again:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
> You do not have to activate the venv to *run* the app: `run.bat` uses `.venv` automatically.
> Activate it when you want to run `pytest`, `python cli.py`, etc.

### Step 2 - Add your Gemini API key
1. Get a free key at <https://aistudio.google.com/apikey>.
2. Open the `.env` file in the project folder (`notepad .env`). If it doesn't exist: `copy .env.example .env`.
3. Put the key right after the `=` sign, with no quotes or spaces, and **save** the file:
   ```
   GEMINI_API_KEY=AIzaSy...your-key...
   GEMINI_MODEL=gemini-3.5-flash
   ```
4. Restart the server if it is running. `.env` is only read at startup.

### Step 3 - (Optional) OCR for scanned PDFs: Tesseract + Poppler
Text-based PDFs (exported from Word or Google Docs) work without these. Scanned or photographed resumes need them.

**Check whether they are installed:**
```powershell
tesseract --version          # should print "tesseract 5.x"
pdftoppm -v                  # should print "pdftoppm version ..."
```
"Not recognized" means a tool is either not installed or not on PATH. You can also open the
app and look at the top of the page: there is a yellow notice when OCR is missing. Or check
`http://127.0.0.1:8000/health` → `"ocr_available"`.

**Install Tesseract:**
1. Download the Windows installer from <https://github.com/UB-Mannheim/tesseract/wiki> (`tesseract-ocr-w64-setup-5.x.x.exe`).
2. Install with the default options → `C:\Program Files\Tesseract-OCR\`.
3. Add it to `.env` (simplest, no PATH editing):
   `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`

**Install Poppler:**
1. Download the latest `Release-xx.zip` from <https://github.com/oschwartz10612/poppler-windows/releases>.
2. Extract it to `C:\poppler`, so that `C:\poppler\Library\bin\pdftoppm.exe` exists.
3. Add it to `.env`: `POPPLER_PATH=C:\poppler\Library\bin`

Restart the server afterwards.

### Step 4 - Start the server (one command)
```powershell
.\run.bat
```
You'll see:
```
✅ Resume Parser running
🌐 App:      http://127.0.0.1:8000
📘 API docs: http://127.0.0.1:8000/docs
```
If port 8000 is busy it automatically uses **8001**, and the banner shows the URL it picked.
Stop the server with **Ctrl + C**. On Git Bash, macOS or Linux use `./run.sh`.

`run.bat` runs `serve.py`. That is the same as
`uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`, plus the port fallback.
`--reload` restarts the server automatically when you edit Python code.

### Step 5 - Open the app and test it
1. Open **<http://127.0.0.1:8000>** in your browser.
2. Drag `data\uploads\sample_resume.pdf` onto the upload box, or click the box and choose the file. That file is a demo resume included in the project.
3. The file shows **pending**. The page checks every 2 seconds, and it switches to **done** after about 5–20 s.
4. The result opens automatically:
   - a header card with the name, email, phone, location and clickable links;
   - Experience, Education, Skills (as chips), Projects, Certifications and Achievements;
   - confidence badges, and a yellow **Needs review** warning when a section looks unreliable;
   - **View raw JSON** and **⬇ Download JSON** buttons.
5. Every parsed resume appears in the **Parsed resumes** table. Click a row to open it again.
   Uploading the same file twice returns the saved result instantly.

To make another test PDF: `python -m tests.sample_resume data\uploads\another.pdf`.

### Step 6 - Try Job Match
1. Click the **🎯 Job Match** tab (or open <http://127.0.0.1:8000/#match>).
2. Left: pick a parsed resume from the list, **or** drop a resume PDF (it is parsed first).
3. Right: paste a job description. Two samples are in `tests\fixtures\` (`jd_python_developer.txt`,
   `jd_ai_product_manager.txt`). Open one in Notepad and copy everything.
4. Click **Analyze Match**. The button shows each step (reading job description → rating experience →
   scoring → writing suggestions); it takes about 20–60 s.
5. You get the score gauge, breakdown bars, skill chips, top-priority cards, bullet rewrites with copy
   buttons, a tailored summary, and skills to learn. **⬇ Download JSON** saves the full report;
   **🖨 Save as PDF** opens the print dialog with a print-friendly layout (choose "Save as PDF").
6. Analyzing the same resume + JD again returns the saved result instantly. Old analyses are in
   **Match history**.

### Step 7 - Tailor your resume and download it
1. On a match result, click **✂ Tailor my resume for this job**. The **✂ Tailor** tab opens and shows
   progress (tailoring resume → re-scoring → checking length, about 20–40 s).
2. At the top: **Score 62 → 81 (+19)**. Open "Per-component comparison" for the before/after of each part.
3. Review each change. Left = your original, right = the tailored version: removed words are red,
   added words green, placeholders yellow. Hover **why?** for the reason and the JD requirement it targets.
   Use **✓ Accept / ✗ Reject / ✎ Edit / 👁 hide** per bullet, or **Accept all / Reject all**. Edit the summary
   in its box. Drag sections by **⠿** to reorder them, or hide a whole section or project.
4. The **Live preview** on the right updates as you edit. Changes save automatically, and the score is
   recomputed on every save.
5. Placeholders such as `[X%]` or `[if true: Docker]` must be filled in (edit the bullet), rejected or hidden.
   **⬇ PDF / ⬇ DOCX stay disabled until none are left.**
6. Pick a template (Classic / Modern / Compact) and page size (A4 / Letter), then download. If the PDF is
   longer than your target (1 page under 3 years of experience, otherwise 2), click **↔ Auto-fit**.
7. Everything you tailored is listed under **My tailored resumes**, with download buttons.

### Step 8 - Compare several jobs
1. Click the **⚖ Compare Jobs** tab (or open <http://127.0.0.1:8000/#compare>).
2. Pick a parsed resume (or drop a resume PDF).
3. Fill in the job cards. Each card takes **Paste text**, a **Job URL** or an **Upload file** (.txt / .pdf / .docx),
   plus an optional label (e.g. "Swiggy – APM"), deadline, location and salary. **+ Add another job** adds up to 10.
   For a URL, **Fetch & preview** shows the text it read. LinkedIn and other login-only pages show
   *"Couldn't read this link, please paste the JD text"*.
   Samples: the three files in `tests\fixtures\` (AI Product Manager, Product Analyst, Python Developer).
4. Click **Compare**. A live list shows each job going pending → parsing → scoring → done (or failed, with the reason).
5. Results: a sortable ranking table (click any header), a "Where to apply first and why" summary, a per-component bar
   chart, the skill heatmap, and the **Learn these first** and **Your strengths** cards. Each row has **View full match**,
   **✂ Tailor resume** (opens the normal Tailor flow) and **Remove**. **⬇ CSV** and **🖨 PDF report** export it.
6. Old comparisons are listed under **My comparisons**. Running the same resume + jobs again is instant (cached).

---

## Common errors and fixes

| Problem | Fix |
|---|---|
| **Port in use**: `[WinError 10048] only one usage of each socket address` or "Ports 8000 and 8001 are both in use" | Another copy of the server is probably still running in another window. Press Ctrl+C there. To find what holds the port: `netstat -ano \| findstr :8000`. The last number is the PID; stop it with `taskkill /PID <pid> /F`. |
| **Missing key**: red banner "Gemini API key missing", or a resume fails with "GEMINI_API_KEY is not set" | Put `GEMINI_API_KEY=...` in `.env` (Step 2), **save the file**, then restart the server. The app still starts without a key; only parsing needs it. |
| "Gemini rejected the API key" | The key is wrong or was copied with spaces or quotes. Create a new one at aistudio.google.com/apikey. |
| "Gemini rate limit / quota exceeded" | The free tier has per-minute limits. Wait a minute and upload again. A job match makes 3–4 Gemini calls, so it uses the quota faster than a resume upload. |
| "Gemini server error (503); try again later" | The model is overloaded ("high demand"). Retry in a few seconds, or switch `GEMINI_MODEL` to another current Flash model. |
| "Chromium for Playwright is not installed" / PDF download fails with 503 | Run `python -m playwright install chromium` once (inside the venv), then restart the server. DOCX export works without it. |
| "Gemini's daily free-tier quota for '...' is used up" | Free keys get about 20 requests per model per day. Try tomorrow, or set `GEMINI_MODEL` to another model (e.g. `gemini-3.5-flash-lite`) and restart. A match uses 3–4 requests and a tailoring 2. |
| "Gemini model '...' was not found" | That model is retired or not offered to your key. List the models your key can use: `python -c "from google import genai; import os; from dotenv import load_dotenv; load_dotenv(); print([m.name for m in genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()])"` and set `GEMINI_MODEL` in `.env`. |
| **Tesseract not found**: yellow "OCR (Tesseract) not found" notice, or `ocr_unavailable` in the parser warnings | Install Tesseract (Step 3) and set `TESSERACT_CMD` in `.env`, then restart. Scanned pages also need Poppler (`POPPLER_PATH`). |
| **Module not found**: `ModuleNotFoundError: No module named 'fastapi'` (or `google`, `pymupdf`, ...) | The requirements aren't installed in the Python you're using. Run `.\.venv\Scripts\Activate.ps1` and then `pip install -r requirements.txt`, or just use `.\run.bat`, which always uses `.venv`. |
| `ModuleNotFoundError: No module named 'app'` | Run commands from the project folder (`cd C:\Users\SIS\Desktop\resume_parser`). |
| "Virtual environment not found" from `run.bat` | Do Step 1 first. |
| A section is missing from the result | Run `python inspect_pdf.py yourfile.pdf` to see how the PDF was read (see "Debugging" below). |

---

## Project structure

```
resume_parser/
├── app/
│   ├── main.py              # FastAPI app: API routes (all user-scoped), CORS, startup
│   ├── auth.py              # Supabase JWT verification -> CurrentUser, profile on first login, rate limits
│   ├── usage.py             # free-plan monthly limits (429 with a friendly message)
│   ├── storage.py           # private files: Supabase Storage (signed URLs) or a local folder
│   ├── account.py           # /me: usage, settings, download my data, delete my account
│   ├── web.py               # public pages, /config.js, security headers, local signed downloads
│   ├── cleanup.py           # daily job (APScheduler): resume auto-delete, old exports
│   ├── observability.py     # optional Sentry, with personal data scrubbed
│   ├── migrate.py           # runs Alembic from code (startup, tests, scripts)
│   ├── config.py            # settings from .env
│   ├── schema.py            # Pydantic models (ResumeData etc.)
│   ├── match_schema.py      # Pydantic models for Job Match (JobDescription, Suggestions, MatchResult)
│   ├── tailor_schema.py     # Pydantic models for Tailor (TailorDraft from Gemini, editable TailoredResume)
│   ├── templates/resume/    # Jinja2 resume template + classic.css / modern.css / compact.css
│   ├── db.py                # SQLModel tables (resumes, matches, tailored_resumes, comparisons, comparison_jobs)
│   ├── logging_config.py    # JSON-lines structured logging
│   ├── data/normalization.json   # editable skill & degree aliases
│   └── pipeline/
│       ├── ingest.py        # validation, SHA-256 hash, save raw file
│       ├── triage.py        # detect text-based vs scanned pages
│       ├── extract.py       # PyMuPDF lines w/ bbox, font size, bold, links
│       ├── ocr.py           # Tesseract fallback
│       ├── clean.py         # column ordering, unicode, bullets, headers/footers
│       ├── sections.py      # section detection (font signals + synonym dict)
│       ├── rules.py         # regex: email, phone, URLs, dates, name candidate
│       ├── llm/
│       │   ├── base.py            # LLMProvider interface + LLMError
│       │   ├── gemini_provider.py # Google Gemini implementation
│       │   └── __init__.py        # get_provider() factory
│       ├── llm_extract.py   # prompts, per-section strategy, retry-once validation
│       ├── normalize.py     # ISO dates, durations, skill/degree normalization
│       ├── validate.py      # grounding, sanity rules, confidence
│       ├── run.py           # orchestrates the full pipeline
│       ├── jd_parse.py      # Job Match: JD text -> JobDescription (Gemini + alias cleanup)
│       ├── match.py         # Job Match: deterministic scoring engine (7 components)
│       ├── suggest.py       # Job Match: Gemini suggestions + honesty checks
│       ├── match_run.py     # Job Match: orchestration, caching, progress stages
│       ├── tailor.py        # Tailor: Gemini draft + truthfulness validator + re-scoring + auto-fit trimming
│       ├── render.py        # Tailor: HTML (Jinja2) -> PDF (Playwright), DOCX (python-docx), file names
│       ├── tailor_run.py    # Tailor: orchestration, user edits, auto-fit, export
│       ├── jd_sources.py    # Compare: JD from a URL (httpx + readability + BeautifulSoup) or a .txt/.pdf/.docx file
│       ├── compare.py       # Compare: async batch runner (semaphore 3, cache, retries) + grounded AI summary
│       ├── compare_insights.py  # Compare: ranking, effort to 80+, recommendation, gaps, strengths, heatmap
│       └── compare_report.py    # Compare: CSV + printable HTML/PDF report
├── static/                  # landing.html (/), login.html, index.html (/app dashboard), privacy / terms,
│                            # auth.js (Supabase client), site.css, favicon.svg, og-image.png
├── migrations/              # Alembic: 0001 baseline, 0002 multi-user (user_id, profiles, usage, JSONB), 0003 RLS
├── scripts/                 # migrate_sqlite_to_postgres.py, check_live.py (live smoke test), make_og_image.py
├── Dockerfile, render.yaml  # production image (Chromium + Tesseract) and the Render blueprint
├── .github/workflows/ci.yml # tests on SQLite + Postgres, secret scan, Docker build + /health check
├── DEPLOY.md                # step-by-step: Supabase, Google sign-in, GitHub, Render
├── serve.py                 # starts uvicorn on 8000 (falls back to 8001)
├── run.bat / run.sh         # one-command start
├── cli.py                   # batch mode: folder of PDFs -> folder of JSON
├── eval.py                  # per-field accuracy vs tests/golden/*.json
├── inspect_pdf.py           # debug tool: shows each pre-LLM stage for one PDF
├── tests/                   # pytest (LLM mocked) + sample resume builder + golden JSON
│   └── fixtures/            # sample job descriptions (AI Product Manager, Product Analyst, Python Developer)
├── data/uploads/            # PDFs for the CLI (web uploads are stored in data/uploads/api/)
├── data/output/             # CLI writes JSON here
├── .env.example
└── requirements.txt
```

---

## API

Interactive docs: <http://127.0.0.1:8000/docs>. In PowerShell use `curl.exe`, not the `curl` alias.
In multi-user mode every data endpoint needs `-H "Authorization: Bearer <access token>"` (get one on `/app`
with F12 → Console: `copy((await Auth.session()).access_token)`); without Supabase settings no token is needed.

```powershell
curl.exe -F "file=@data/uploads/sample_resume.pdf" http://127.0.0.1:8000/resumes
# {"id":"6184a052c3ab4dd0b00206da1276dbb1","status":"pending","cached":false}

curl.exe http://127.0.0.1:8000/resumes/6184a052c3ab4dd0b00206da1276dbb1
# {"id":"...","file_name":"sample_resume.pdf","status":"done","data":{...},"error":null}

curl.exe http://127.0.0.1:8000/resumes      # list: id, file_name, status, name, email, needs_review
curl.exe http://127.0.0.1:8000/health

# Job Match: a PDF (or -F "resume_id=<id>") + the JD text
curl.exe -F "file=@data/uploads/sample_resume.pdf" -F "jd_text=<tests/fixtures/jd_python_developer.txt" http://127.0.0.1:8000/match
# {"match_id":"d79a94c2...","status":"pending","cached":false}
curl.exe http://127.0.0.1:8000/match/d79a94c2...
curl.exe http://127.0.0.1:8000/matches

# Compare Jobs: resume_id (or -F "file=@resume.pdf") + a JSON list of jobs; files go in job_files
curl.exe -F "resume_id=<id>" -F "job_files=@tests/fixtures/jd_product_analyst.txt" `
  -F 'jobs=[{"url":"https://job-boards.greenhouse.io/...","label":"Job A"},{"file_index":0,"label":"Swiggy - Analyst","deadline":"2026-12-15"}]' `
  http://127.0.0.1:8000/compare
# {"compare_id":"5b1e...","status":"pending"}
curl.exe http://127.0.0.1:8000/compare/5b1e...
curl.exe "http://127.0.0.1:8000/compare/5b1e.../export?format=csv"
# {"url":"/files/exports/...?exp=...&sig=...","file_name":"job_comparison_....csv","expires_in":600}
```
(`-F "jd_text=<file"` makes curl send the file's content as the form field.)

| Method | Path | What it does |
|---|---|---|
| GET | `/`, `/login`, `/app`, `/privacy`, `/terms` | Landing page, sign-in, the dashboard, legal pages (public) |
| GET | `/me` | Profile, plan, usage this month, settings |
| PUT | `/me/settings` | `{retention_days: 30 \| 90 \| null}` (resume auto-delete) |
| GET | `/me/export` | Everything stored about the account, as one JSON file |
| DELETE | `/me` | `{confirm: "DELETE"}`: deletes all rows, all stored files and the login account |
| GET | `/resumes/{id}/file` | 10-minute signed link to the original PDF |
| POST | `/resumes` | Upload a PDF. Parsing runs in the background. Invalid files get HTTP 400. |
| GET | `/resumes/{id}` | `{status: pending\|done\|failed, data, error}` |
| GET | `/resumes` | All resumes, newest first |
| POST | `/match` | Multipart: `jd_text` + either `file` (PDF) or `resume_id`. Returns `{match_id, status, cached}`; runs in the background. |
| GET | `/match/{id}` | `{status, stage, score, label, breakdown{component: {score, max, details, data}}, matched_skills, missing_skills{required, preferred}, semantic_similarity, suggestions, jd_parsed, warnings}` |
| GET | `/matches` | Match history (score, label, job title, candidate), newest first |
| POST | `/tailor` | JSON `{match_id, template, page_size, regenerate?}` → `{tailor_id, status, existing}`. Re-uses the latest tailoring of that match unless `regenerate: true`. |
| GET | `/tailor/{id}` | `{status, stage, tailored, document, changes_log, warnings, placeholders, old_score, new_score, score_change, old_breakdown, new_breakdown, page_count, target_pages}` |
| PUT | `/tailor/{id}` | JSON `{document, template?, page_size?}`: saves accepts / rejects / edits / hides / order and re-scores (no Gemini call) |
| POST | `/tailor/{id}/preview` | JSON `{document?, template?, page_size?}` → HTML preview (placeholders highlighted) |
| POST | `/tailor/{id}/autofit` | Switch to Compact, then hide the least relevant bullets / projects until the PDF fits the target |
| POST | `/tailor/{id}/export` | JSON `{format: pdf\|docx, template?, page_size?}` → `{url, file_name, expires_in, page_count, target_pages}`: a 10-minute signed download link (`409` while placeholders remain, `503` if Chromium is missing) |
| GET | `/tailors` | "My tailored resumes" |
| POST | `/compare` | Multipart: `jobs` (JSON list of 2–10 `{text \| url \| file_index, label, deadline, location, salary}`), `job_files` (JD files), and `resume_id` or `file`. Returns `{compare_id, status}`; runs in the background. |
| GET | `/compare/{id}` | `{status, jobs[{status: pending\|parsing\|scoring\|done\|failed, stage, error, match_id, ...}], progress, insights{ranking, common_gaps, strengths, heatmap}, summary, summary_source}` |
| POST | `/compare/{id}/jobs` | Multipart: one job (`text` / `url` / `file` + `label`, `deadline`, `location`, `salary`). Only that job is scored. |
| DELETE | `/compare/{id}/jobs/{job_id}` | Remove a job (409 while it is still being scored); returns the updated comparison |
| GET | `/compare/{id}/export?format=csv\|pdf` | `{url, file_name, expires_in}`: signed link to the CSV or the printable PDF report (503 if Chromium is missing) |
| GET | `/compares` | "My comparisons" |
| POST | `/jd/fetch` | JSON `{url}` → `{url, text}`: the JD read from a job page (422 with "Couldn't read this link…") |
| GET | `/health` | Database, Gemini key and OCR status |

CORS is enabled for `http://localhost:*` and `http://127.0.0.1:*`.

## CLI (batch)

```powershell
python cli.py --input data/uploads --output data/output
```
This writes one `<file>.json` per PDF and prints a summary table (file, name, #experience,
#projects, confidence, needs_review). A broken PDF is reported in the table, and the rest of
the batch continues. Flags: `--no-cache` and `--no-db`.

## Evaluate accuracy

Save hand-checked JSON as `tests/golden/<same-name>.json`, then run:
```powershell
python cli.py --input data/uploads --output data/output
python eval.py --pred data/output --golden tests/golden
```

## Tests

```powershell
pytest            # 204 tests, Gemini is mocked: no key or internet needed (PDF tests need Chromium)
pytest tests/test_match_scoring.py tests/test_match_e2e.py   # just the Job Match tests
pytest tests/test_tailor.py tests/test_render.py tests/test_tailor_e2e.py   # just the Tailor tests
```

## Debugging a PDF

```powershell
python inspect_pdf.py data\uploads\sample_resume.pdf          # every pre-LLM stage, no key needed
python inspect_pdf.py yourfile.pdf --lines                    # + every line with position/font
```

---

## Example output

```json
{
  "basics": {
    "name": "Priya Sharma",
    "email": "priya.sharma@example.com",
    "phone": "+919876543210",
    "location": "Pune, India",
    "links": {
      "linkedin": "https://www.linkedin.com/in/priya-sharma-dev",
      "github": "https://github.com/priyasharma",
      "portfolio": null,
      "other": []
    },
    "summary": "Backend developer with 3 years of experience building scalable APIs and data pipelines."
  },
  "education": [
    {
      "institution": "Vellore Institute of Technology",
      "degree": "B.Tech",
      "degree_normalized": "Bachelor's - Engineering",
      "field": "Computer Science",
      "start": "2017-01",
      "end": "2021-01",
      "grade": "CGPA 8.7/10"
    }
  ],
  "experience": [
    {
      "company": "Infosys Ltd",
      "title": "Software Engineer",
      "location": "Bengaluru",
      "start": "2021-07",
      "end": "present",
      "duration_months": 64,
      "bullets": [
        "Built REST APIs in Python and Django serving 2M requests per day",
        "Reduced report generation time by 40% using Redis caching"
      ],
      "skills_used": ["Python", "Django", "Redis"]
    }
  ],
  "projects": [
    {
      "name": "ResumeRank",
      "description": "AI resume screener",
      "tech_stack": ["Python", "FastAPI", "PostgreSQL"],
      "link": null,
      "outcomes": ["Ranked 500+ resumes per hour using ML and spaCy"],
      "start": null,
      "end": null
    }
  ],
  "skills": {
    "technical": ["Python", "JavaScript", "SQL"],
    "tools": ["Docker", "Git", "AWS", "PostgreSQL"],
    "soft": ["Leadership", "Communication"],
    "languages": []
  },
  "certifications": [
    { "name": "AWS Certified Cloud Practitioner", "issuer": "Amazon Web Services", "date": "2022-03" }
  ],
  "achievements": ["Winner, Smart India Hackathon 2020"],
  "meta": {
    "source_hash": "8a485734a6e24a0ee70e1b98da15fc0671db9a6442e5bde6d0c0b7ca5091d37f",
    "file_name": "sample_resume.pdf",
    "parser_version": "1.0.0",
    "is_scanned": false,
    "pages": 1,
    "confidence": {
      "basics": 1.0, "education": 1.0, "experience": 0.83, "projects": 1.0,
      "skills": 1.0, "certifications": 1.0, "achievements": 1.0
    },
    "needs_review": false,
    "warnings": ["possible_hallucination: experience[2].company = 'Google'"],
    "processing_ms": 37,
    "total_experience_months": 70
  }
}
```

---

## How it works (design notes)

- **LLM.**
  - `app/pipeline/llm/gemini_provider.py` calls Gemini through the official `google-genai` SDK.
  - It uses `response_mime_type="application/json"` with `response_json_schema` set to the Pydantic model's JSON Schema, and `temperature=0`. (The older `response_schema` field rejects the `additionalProperties: false` that the strict models produce.)
  - The answer is validated with Pydantic. If validation fails, the code retries once and includes the error message.
  - Short resumes use one call. Longer ones (over `LLM_SINGLE_CALL_MAX_CHARS`) use one call per section, in parallel.
- **Swapping providers.** Write a class with `name` and `complete_json(system, prompt, response_model) -> str` in `app/pipeline/llm/`, then register it in `get_provider()`.
- **Regex beats the LLM** for email, phone and links. PDF hyperlink annotations are read too, so a LinkedIn link hidden behind an icon is still found.
- **Grounding.** Every company, title, institution, project name and skill must appear in the PDF text: rapidfuzz score ≥ `GROUNDING_THRESHOLD`, 85 by default. Anything that doesn't is dropped and logged as `possible_hallucination`.
- **Confidence.** A 0–1 score is computed per section. `needs_review` is true if any score is below `REVIEW_THRESHOLD` (0.7).
- **Dates.**
  - Stored as `YYYY-MM`, or `present` for an ongoing job.
  - A year on its own becomes `YYYY-01`.
  - Durations are inclusive: Jan–Mar counts as 3 months.
  - Total experience merges overlapping jobs.
- **Aliases.** Edit `app/data/normalization.json` to add skill or degree aliases (JS → JavaScript, B.Tech → Bachelor's - Engineering).
- **Moving to Postgres.**
  1. `pip install "psycopg[binary]"`
  2. Set `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/resumes`.

---

## Job Match (Resume ↔ Job Description)

```
resume (PDF or saved id) ─► resume pipeline (cached by file hash)
JD text ─► Gemini: JobDescription ─► alias cleanup (JS → JavaScript, "Django or FastAPI" kept as one either/or item)
        ─► Gemini: rate each job/project's relevance 0–1 (rounded to 0.05)
        ─► Python: 7 score components ─► total 0–100 + label
        ─► Gemini: suggestions ─► Python honesty checks ─► stored in SQLite (matches table)
```

### How the score is computed

The number is computed in Python (`app/pipeline/match.py`), never by asking the LLM for a score. Each
component's `details` says why it got its points, and `data` holds the evidence (which skill matched how,
which keywords were found, per-job relevance reasons).

| Component | Max | How |
|---|---|---|
| Required skills | 35 | Share of the JD's required skills found in the resume. A skill counts if it is in the resume's skills / job skills / project tech stack (**exact** or via the **alias** map), **mentioned** in bullets / projects / summary, or a **fuzzy** match ≥ `MATCH_FUZZY_THRESHOLD` (85, rapidfuzz). "A / B" items count if either is present. |
| Preferred skills & tools | 10 | Same method on preferred skills + tools. |
| Experience relevance | 20 | Gemini rates each job and project 0–1 against the responsibilities, with a one-line reason. Ratings are rounded to 0.05 and averaged with jobs weighted ×2, projects ×1. |
| Years of experience | 10 | Full marks at or above the JD minimum, linear below it (half the years = half marks). Being above the maximum is never penalised. No minimum = full marks. |
| Education fit | 10 | Degree level 6 pts (meets = 6, one level below = 3, else 0; ladder: school < diploma < bachelor's < master's/MBA < doctorate) + field of study 4 pts (fuzzy match; "or related field" earns 2). |
| Keyword / ATS coverage | 10 | Weighted share of the JD's domain keywords (×2) and its top 15 responsibility terms by TF-IDF (×1) found in the resume text (light stemming: "developer" ≈ "development"). |
| Resume quality | 5 | Quantified bullets 2 (half the bullets with a number = full), action-verb bullets 1, contact + profile link 1, sensible length 1 (300–1000 words, ≤ 2 pages). |

Total = sum of the components rounded half-up to an integer. Labels: **85+ Excellent match**, **70–84 Strong**,
**50–69 Moderate**, **< 50 Low**.

**Reproducibility.** Given the same parsed resume, parsed JD and relevance ratings, the score is always the
same (there are tests for this). The two Gemini inputs are made stable too: temperature 0, ratings rounded to
0.05, a JD that was parsed before is re-used, and a finished match is stored and returned for the same resume
file hash + JD text hash (whitespace-insensitive) + scoring version.

**Semantic similarity** (optional, `MATCH_SEMANTIC_SIMILARITY=true`): cosine similarity of Gemini embeddings
(`GEMINI_EMBEDDING_MODEL`, default `gemini-embedding-001`; `text-embedding-004` is retired) of the resume and
the JD. It is shown for information only and does **not** change the score. If the call fails, the match still
succeeds and the metric is `null`.

### Suggestions and honesty checks

Gemini receives the parsed resume, parsed JD and the score breakdown, and returns `Suggestions`
(validated with Pydantic, retried once): a verdict, 5–8 top priorities (issue → action → example rewrite →
estimated points), keywords to add, bullet rewrites, a tailored summary and skills to learn. The prompt forbids
inventing experience, metrics, companies, skills or activities; unknown numbers must be placeholders like
`[X%]`, and unconfirmed claims must be written as `[if true: …]`. Python then enforces what it can:
- a number in a rewrite that appears nowhere in the resume becomes `[X]` / `[X%]` (shown as "honesty checks" in the UI);
- a technology that a bullet rewrite attaches to a job or project that doesn't state it becomes `[if true: X]`;
- a "keyword to add" that is actually a missing JD skill with no evidence in the resume is moved to **skills to learn**;
- each estimated gain is capped at the points that component can still gain, and priorities are sorted by gain.

The number check only looks for the number anywhere in the resume, so a rewrite can still reuse a real number
in the wrong place. Always read the rewrites before using them.

### How to test it
- **Automated:** `pytest tests/test_match_scoring.py tests/test_match_e2e.py`. This covers skill matching (exact / alias /
  fuzzy / text / either-or), years, degree levels, education fit, keywords, quality, labels, determinism,
  suggestion grounding, and the API (upload, `resume_id`, cache, validation errors, failures), with Gemini mocked.
- **Manually:** follow Quick start → Step 6 with the two sample JDs. The backend sample resume should score
  clearly higher against `jd_python_developer.txt` than against `jd_ai_product_manager.txt`.

---

## Tailor My Resume

```
match (resume + JD + score + suggestions)
  ─► Gemini (temperature 0.2): TailorDraft  ─► Python truthfulness validator ─► TailoredResume
  ─► Gemini: relevance of the tailored jobs/projects ─► existing match scorer ─► new score
  ─► Chromium renders the PDF ─► page count vs target
  ─► user review (accept / reject / edit / hide / reorder; re-scored on every save)
  ─► export: Jinja2 HTML → PDF (Playwright)  |  python-docx → DOCX
```

### Truthfulness rules (enforced in code, `app/pipeline/tailor.py`)
The prompt states the rules, but none of them depend on Gemini following it:

| Rule | What the validator does |
|---|---|
| Never change contact info, education, certifications, achievements | Copied from the original resume; Gemini doesn't return them. |
| Never add or alter companies, titles, dates | Each job must point at an original one (`source_index`). Anything different (rapidfuzz < 85) is **reverted** and logged in `warnings`; invented jobs and projects are removed. |
| Every bullet must come from an original bullet of the same job / project | Unmatched bullets are **removed**; bullets Gemini left out are kept **hidden**, so you can switch them back on. |
| Never invent numbers | A number that appears nowhere in the original becomes `[X]` / `[X%]` and the bullet is marked `needs_user_input`. |
| Keywords only where the original supports them | A technology a bullet attaches to a job / project that never mentions it becomes `[if true: Docker]`. |
| Only skills already in the resume | New skills are **removed**, dropped ones re-added; Gemini may only reorder them. |
| Your edits can't change facts either | `PUT /tailor/{id}` only accepts wording, decisions, visibility and order; names, companies, titles and dates always come from the validated version. |
| Year-only dates stay year-only | "2017 – 2021" is not rendered as "Jan 2017 – Jan 2021". |

**Export is blocked (HTTP 409) while any placeholder remains** in the visible content. The re-score
ignores placeholder text (an unconfirmed `[if true: Docker]` earns no points), as well as hidden content.

### Re-scoring
The tailored resume goes through the same scorer as Job Match. Gemini re-rates the relevance of the rewritten
jobs and projects once; every other component is deterministic. After that, edits are re-scored instantly
without Gemini. Both scores are stored (`old_score`, `new_score`), and the UI shows the per-component comparison.

### Templates and export
- **Classic** (serif), **Modern** (clean sans, one accent color), **Compact** (dense, for 1 page).
- All of them: single column, standard headings (Summary, Experience, Projects, Skills, Education,
  Certifications, Achievements), no tables / images / icons, selectable text, clickable links.
- PDF: Jinja2 HTML rendered by headless Chromium (Playwright), A4 or Letter. DOCX: python-docx, same structure
  (Heading 1 headings, List Bullet bullets, real hyperlinks, right-aligned dates via a tab stop).
- File name: `Firstname_Lastname_<Company>_<Role>.pdf`, e.g. `Priya_Sharma_FinStackTechnologies_PythonDeveloperBackend.pdf`.
- Page target: 1 page under 3 years of experience, otherwise 2. **Auto-fit** switches to Compact, then hides the
  least relevant content (low-relevance projects first, then the weakest unchanged bullets; every job keeps at
  least one bullet) until the PDF fits.
- The tests re-parse every exported PDF with this project's own parser and check that all sections are detected.

### How to test it
- **Automated:**
  - `pytest tests/test_tailor.py`: the validator (invented skill / company / title / date / number / bullet), placeholders blocking export, re-scoring, auto-fit order.
  - `pytest tests/test_render.py`: all 3 templates × PDF (A4 + Letter) and DOCX, page counts, clickable links, the ATS re-parse.
  - `pytest tests/test_tailor_e2e.py`: parse → match → tailor → edit → re-score → export through the API, Gemini mocked.
- **Manually:** Quick start → Step 6 (Job Match) → Step 7.


---

## Compare Jobs

```
resume + 2-10 JDs (pasted text | URL | .txt/.pdf/.docx)
  ─► per job, concurrently (asyncio, at most 3 jobs talking to Gemini at once):
       URL? fetch (httpx) ─► JSON-LD JobPosting or readability-lxml main text ─► BeautifulSoup clean-up
       cache: same resume hash + JD hash ─► stored match, no Gemini calls
       otherwise the normal Job Match: parse JD ─► rate experience ─► score ─► suggestions
       Gemini 429 / 5xx ─► retry with exponential backoff (2 s, 4 s, 8 s + jitter)
       any failure ─► that job is "failed" with a reason; the others carry on
  ─► insights in Python ─► Gemini writes a 3-5 line summary from those numbers only
```

Every job in a comparison is an ordinary Job Match underneath (`comparison_jobs.match_id`), so **View full match**
and **Tailor resume for this job** use the existing screens. Tables: `comparisons` (id, resume_id, created_at, summary,
status...) and `comparison_jobs` (id, comparison_id, match_id, label, url, deadline, location, salary, status, error...).

### The rules (`app/pipeline/compare_insights.py`)
| Insight | How it is computed |
|---|---|
| Ranking | Total score, highest first. Ties: higher required-skills score, then the earlier deadline, then the order added. Label: Excellent (85+) / Strong (70+) / Moderate (50+) / Low. |
| Effort to reach 80+ | Take the job's suggestions, biggest `estimated_score_gain` first, until they cover the gap to 80. Effort = their summed gain. **Low**: gap ≤ 10 in ≤ 2 fixes. **High**: gap ≥ 21, ≥ 5 fixes, or the suggestions can't close the gap. **Medium**: everything else. Already 80+ = Low. |
| Recommendation | score ≥ 75 → **Apply now**; 60–74 and effort not High → **Tailor, then apply**; otherwise (< 60, or High effort) → **Stretch / skip**. |
| Learn these first | Skills missing (required or preferred) in ≥ 50 % of the scored jobs, most frequent first (ties: required in more jobs). |
| Your strengths | Skills the scorer matched on your resume in more than half of the jobs. |
| Skill heatmap | Top 20 skills by number of JDs that mention them (ties: required in more). Cell = required / preferred / not mentioned, colored by whether your resume has the skill. |
| Summary | Gemini gets only those numbers (`summary_facts`). If its answer contains a number that is not in them, or Gemini fails, a rule-based summary is used instead (`summary_source: "rules"`). Same numbers again → the stored summary is re-used. |

### URL fetching
- Only `http(s)` links. Local / private network addresses are refused.
- Pages behind a login (LinkedIn, Glassdoor, a password form, an authwall redirect), non-HTML responses, HTTP errors and
  pages with too little JD-like text all give **"Couldn't read this link, please paste the JD text"** for that job only.

### Settings (`.env`)
`COMPARE_CONCURRENCY=3`, `COMPARE_RETRY_ATTEMPTS=4`, `COMPARE_RETRY_BASE_DELAY=2`.

### How to test it
- **Automated** (Gemini mocked):
  - `pytest tests/test_compare_insights.py`: recommendation rules, effort calculation, common gaps, strengths, ranking order, heatmap.
  - `pytest tests/test_jd_sources.py`: readability / JSON-LD extraction, LinkedIn login walls, HTTP errors, private URLs, .txt/.pdf/.docx files.
  - `pytest tests/test_compare_e2e.py`: 3 sample JDs end to end; a bad URL plus a Gemini error don't break the batch; re-running
    makes **no** new Gemini calls; 429s are retried; at most 3 jobs use Gemini at once; add / remove jobs; CSV + PDF export.
- **Manually:** Quick start → Step 8.

---

## Multi-user web app

Setup and deployment, step by step: **[DEPLOY.md](DEPLOY.md)**.

| Concern | How it works |
|---|---|
| Sign-in | Supabase Auth: Google, or an e-mail magic link (PKCE flow). The browser sends `Authorization: Bearer <access token>` on every API call (`static/auth.js`). |
| Token check | `app/auth.py` verifies the JWT: HS256 with `SUPABASE_JWT_SECRET`, or ES256/RS256 with the project's public keys (JWKS); issuer, audience `authenticated`, role and expiry are checked. Missing / invalid → 401. |
| Data isolation | Every user-owned table has a `user_id`. Lists and caches filter by it; a record id from someone else answers 404 (same as a missing one). `tests/test_auth.py` checks every endpoint and fails if a new endpoint is added without an auth test. |
| Database | Supabase Postgres via `DATABASE_URL` (SQLite locally). Alembic migrations run on every start. JSON columns are JSONB; `user_id` and `created_at` are indexed; Row Level Security is on for every table, so the public anon key can't read tables through Supabase's REST API. |
| Files | Private buckets `resumes` (uploaded PDFs) and `exports` (generated PDF / DOCX / CSV), paths `<user_id>/<uuid>.<ext>`. Downloads are signed links valid for 10 minutes. The service-role key is used only by the server. |
| Free plan | Per calendar month: 5 parses, 10 matches, 3 tailored resumes, 2 comparisons (`FREE_*_PER_MONTH`). Checked before any Gemini call; cached results are free. `plan = pro` in `profiles` = unlimited. |
| Rate limit | Per user: 20/minute for requests that start AI work or render files, 120/minute for everything else (`RATE_LIMIT_*`). In memory, per server process. |
| Privacy | Settings: download all my data (JSON), auto-delete resumes after 30 / 90 days (daily job), delete account (rows + files + login). PDFs: magic bytes, ≤ 10 MB, ≤ 10 pages. Logs never contain resume text, names, e-mails or file names. |
| Headers | CSP, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, HSTS (https); CORS only for `APP_BASE_URL`, `CORS_ORIGINS` and localhost. |
| Errors | Sentry when `SENTRY_DSN` is set (no request bodies, headers or e-mails sent). |

### How to test it
- `pytest`: 319 tests. The multi-user ones: `test_auth.py` (401s, bad tokens, ES256, isolation between two users,
  signed links), `test_usage.py` (limits, monthly reset, pro plan, rate limits), `test_account.py` (account deletion
  removes rows + files + login, data export, auto-delete job, upload validation), `test_migrations.py`
  (up/down, legacy database, models = migrations, RLS on Postgres, the SQLite → Postgres copy), `test_storage.py`.
- Against Postgres: `$env:TEST_DATABASE_URL="postgresql://..."; pytest` (each test gets its own schema). CI does this.
- Live site: `python scripts/check_live.py https://<your-app>.onrender.com` (see DEPLOY.md → Check that everything works).
