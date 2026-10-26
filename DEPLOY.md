# Deploying Resume Parser to the internet (beginner guide, Windows)

You will end up with:

```
Browser ──► Render (Docker: FastAPI + Chromium + Tesseract) ──► Supabase Postgres  (your data)
   │                       │                                └─► Supabase Storage  (private PDFs)
   └── Google / magic-link sign-in via Supabase Auth             └─► Google Gemini API (AI)
GitHub (code) ──push to main──► GitHub Actions (tests + Docker build) ──pass──► Render deploys
```

Time needed: about 60–90 minutes the first time. Cost: free tiers work for testing (see
[Plans and costs](#plans-and-costs)).

> **Placeholders used below**
> `<project-ref>`: your Supabase project id, e.g. `abcdefghijklmnop` (it is in your Supabase URL).
> `<your-app>`: your Render service name, e.g. `resume-parser`. Your site will be `https://<your-app>.onrender.com`.

Keep a Notepad file open while you work and paste each value into it as you go. **Never** paste
these values into GitHub, chat or email. They only go into `.env` (on your PC) and into Render.

| Name | Where it comes from | Secret? |
|---|---|---|
| `SUPABASE_URL` | Step 1.3 | no |
| `SUPABASE_ANON_KEY` | Step 1.3 | no (public by design) |
| `SUPABASE_SERVICE_ROLE_KEY` | Step 1.3 | **YES: full admin access** |
| `SUPABASE_JWT_SECRET` | Step 1.4 | **YES** |
| `DATABASE_URL` | Step 1.5 | **YES: contains the DB password** |
| Google Client ID / Secret | Step 2 | **Secret is secret** |
| `GEMINI_API_KEY` | your `.env` | **YES** |

---

## Step 1 – Create the Supabase project

1. Go to <https://supabase.com> → **Start your project** → sign in with GitHub or Google.
2. **New project**:
   - Name: `resume-parser`
   - Database password: click **Generate a password**, then **copy it into your Notepad now** (you can't see it again).
   - Region: **Southeast Asia (Singapore)**, or the one closest to your users. Use the same region on Render (Step 6).
   - Click **Create new project** and wait ~2 minutes.
3. **API URL and keys**: left sidebar → ⚙ **Project Settings** → **API Keys**.
   - `SUPABASE_URL`: shown on **Project Settings → Data API** as *Project URL*: `https://<project-ref>.supabase.co`
   - Open the **Legacy API Keys** tab:
     - `anon` `public` → this is `SUPABASE_ANON_KEY`
     - `service_role` `secret` → click **Reveal** → this is `SUPABASE_SERVICE_ROLE_KEY`
   - (The newer `sb_publishable_…` / `sb_secret_…` keys also work, but the legacy ones are the most compatible.)
4. **JWT secret**: **Project Settings → JWT Keys**.
   - If you see a **Legacy JWT Secret** tab, reveal and copy it → `SUPABASE_JWT_SECRET`.
   - New projects sign logins with *JWT Signing Keys* instead. That works without any secret (the app
     downloads the public keys automatically), so you can leave `SUPABASE_JWT_SECRET` empty.
5. **Database URL**: click **Connect** (top of the dashboard) → **Connection String** → choose **Session pooler**.
   Copy the URI. It looks like:
   ```
   postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
   ```
   Replace `[YOUR-PASSWORD]` (including the brackets) with the database password from 1.2 → this is `DATABASE_URL`.
   > Use the **Session pooler** (port 5432), not "Direct connection" (Render can't reach it: IPv6 only)
   > and not "Transaction pooler" (6543).
   > If your password contains `@ : / ? #`, reset it to one with only letters and digits (Project Settings → Database).

## Step 2 – Enable Google sign-in

### 2a. Google Cloud Console (creates the "Sign in with Google" app)

1. Go to <https://console.cloud.google.com> → top bar project picker → **New Project** → name `Resume Parser` → **Create**, then select it.
2. Menu ☰ → **APIs & Services** → **OAuth consent screen** (on newer consoles: **Google Auth Platform**) → **Get started**:
   - App name: `Resume Parser` · User support email: your Gmail
   - Audience: **External**
   - Contact email: your Gmail → **Create**
3. **Branding** (same section): add
   - Application home page: `https://<your-app>.onrender.com`
   - Privacy policy link: `https://<your-app>.onrender.com/privacy`
   - Terms of service link: `https://<your-app>.onrender.com/terms`
   - Authorized domains: `onrender.com` and `supabase.co`
   (You can fill these in after Step 6, once you know the Render URL.)
4. **Clients** → **Create client**:
   - Application type: **Web application** · Name: `Supabase`
   - **Authorized JavaScript origins**:
     ```
     https://<your-app>.onrender.com
     http://localhost:8000
     ```
   - **Authorized redirect URIs**: exactly this one (it is Supabase that receives Google's answer):
     ```
     https://<project-ref>.supabase.co/auth/v1/callback
     ```
   - **Create** → copy the **Client ID** and **Client secret** into Notepad.
5. **Audience** → **Publish app** → confirm. (While the app is "Testing", only test users you list can sign in.
   The app only asks for name, e-mail and picture, so Google doesn't require a review.)

### 2b. Supabase (turns Google on)

1. Supabase → **Authentication** → **Sign In / Providers** → **Google** → toggle **Enable**.
2. Paste the **Client ID** and **Client Secret** from 2a.4 → **Save**.
   (The *Callback URL* shown there should be the same `https://<project-ref>.supabase.co/auth/v1/callback`.)
3. **Authentication → URL Configuration**:
   - **Site URL**: `https://<your-app>.onrender.com`
   - **Redirect URLs** → **Add URL**, one at a time:
     ```
     https://<your-app>.onrender.com/**
     http://127.0.0.1:8000/**
     http://localhost:8000/**
     ```

### 2c. Email magic links

The **Email** provider is on by default. ⚠️ Supabase's built-in mailer is for testing only: it sends just a
few e-mails per hour and **only to members of your Supabase organisation**. For real users, add an e-mail
service: **Authentication → Emails → SMTP Settings → Enable custom SMTP** (for example
[Resend](https://resend.com), free for 3,000 e-mails/month; follow its "Supabase SMTP" guide).
Google sign-in works without this.

## Step 3 – Storage buckets

The app **creates both buckets automatically** on its first start (`resumes` and `exports`, both **private**).
To check, or to create them by hand: Supabase → **Storage** → **New bucket**:

- Name `resumes`, **Public bucket: OFF** → Create
- Name `exports`, **Public bucket: OFF** → Create

No storage policies are needed: only the server (with the service-role key) reads and writes these files, and
users download through signed links that expire after 10 minutes.

## Step 4 – Create the tables and copy your local data

Run these in **PowerShell** in the project folder (`C:\Users\SIS\Desktop\resume_parser`).
The `$env:` variables only exist in this PowerShell window, so your local `.env` stays in local mode.

```powershell
.venv\Scripts\activate
pip install -r requirements.txt -c constraints.txt

$env:DATABASE_URL = "postgresql://postgres.<project-ref>:YOUR-PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
$env:SUPABASE_URL = "https://<project-ref>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "<service_role key>"

# 4a. Create all tables (the app also does this on every start, so this is just a check)
alembic upgrade head
# -> ends with "Running upgrade 0002_multi_user -> 0003_row_level_security"
```

In Supabase → **Table Editor** you should now see `resumes`, `matches`, `tailored_resumes`, `comparisons`,
`comparison_jobs`, `profiles`, `usage`, each marked **RLS enabled**. (Row Level Security keeps the public
anon key from reading any table through Supabase's own API; the app is the only way in.)

**4b. Copy your local data to your account.** Do this *after* Step 6, once you have signed in on the live
site at least once with your Google account (that creates your account):

```powershell
python scripts/migrate_sqlite_to_postgres.py --email kevalhirpara2003@gmail.com --dry-run   # shows what would be copied
python scripts/migrate_sqlite_to_postgres.py --email kevalhirpara2003@gmail.com
```

It copies every resume, match, tailored resume and comparison from `data\resumes.db` to your account and
uploads the original PDFs it finds. Your local file is opened read-only, and running it twice is safe.

## Step 5 – Push the code to GitHub

1. Go to <https://github.com/new> (signed in as **keval025**):
   - Repository name: `resume-parser`
   - **Private** (recommended)
   - Do **not** add a README, .gitignore or licence (the project has them) → **Create repository**.
2. In PowerShell, in the project folder:

```powershell
git status                      # .env and data\ must NOT be listed
git remote add origin https://github.com/keval025/resume-parser.git
git push -u origin main
```

A browser window opens the first time ("Git Credential Manager"): sign in to GitHub and approve.
3. On GitHub, open the **Actions** tab: the **CI** workflow runs the tests (SQLite and Postgres), a
secret scan, and builds the Docker image. Wait for the green ✓ (about 8 minutes).

> Already committed a secret by mistake? Rotating it is the only real fix: generate a new key (Supabase:
> API Keys → Roll; Gemini: delete and create a key), update `.env` and Render, then remove it from the code.

## Step 6 – Create the Render web service

1. Go to <https://dashboard.render.com> → sign up with **GitHub**.
2. **New +** → **Blueprint** → connect your GitHub account → give Render access to the `resume-parser`
   repository → select it. Render reads `render.yaml` and shows the `resume-parser` web service.
3. Render asks for every value marked `sync: false`. Paste from your Notepad:

   | Key | Value |
   |---|---|
   | `APP_BASE_URL` | `https://<your-app>.onrender.com` (guess it from the service name; fix it in step 7 if needed) |
   | `DATABASE_URL` | the Session pooler URI (Step 1.5) |
   | `SUPABASE_URL` | `https://<project-ref>.supabase.co` |
   | `SUPABASE_ANON_KEY` | anon key |
   | `SUPABASE_SERVICE_ROLE_KEY` | service_role key |
   | `SUPABASE_JWT_SECRET` | legacy JWT secret, or leave empty (Step 1.4) |
   | `GEMINI_API_KEY` | your Gemini key |
   | `SENTRY_DSN` | empty, or your Sentry DSN (optional, <https://sentry.io>) |
   | `COMPANY_NAME` | your name or company (shown on Privacy / Terms) |
   | `CONTACT_EMAIL` | an address for privacy requests |

4. **Apply**. The first build takes ~10 minutes (it installs Chromium and Tesseract). Watch **Logs**:
   it ends with `Application startup complete` and the status becomes **Live**.
5. Open `https://<your-app>.onrender.com/health`: you want `"status":"ok"`, `"auth":"supabase"`,
   `"storage":"SupabaseStorage"`.

From now on every `git push` to `main` deploys automatically, but only after the GitHub checks pass.

> **Gemini and privacy:** for a public app, enable billing on the Google Cloud project of your Gemini key
> (<https://aistudio.google.com> → API keys → set up billing). On the free tier Google may use submitted
> content, which here means your users' resumes, to improve its products. The Privacy Policy assumes the paid tier.

## Step 7 – Connect the pieces

1. Copy the real URL from the top of the Render service page (e.g. `https://resume-parser-x1y2.onrender.com`).
   If it differs from what you guessed: Render → **Environment** → edit `APP_BASE_URL` → **Save, rebuild and deploy**.
2. Supabase → Authentication → **URL Configuration**: Site URL and Redirect URL = that address (Step 2b.3).
3. Google Cloud → Clients → your client → Authorized JavaScript origins = that address (Step 2a.4).
4. CORS needs nothing more: the app allows `APP_BASE_URL` (plus localhost) automatically.
   Extra origins (e.g. a `www.` domain) go in `CORS_ORIGINS`, comma separated.
5. Open the site → **Sign in with Google** → you land on `/app`. Then run Step 4b to bring your data over.

### Check that everything works

1. Sign in, then on `/app` press **F12** → **Console**, type `copy((await Auth.session()).access_token)` and
   press Enter. Your token is now in the clipboard.
2. In a private window, sign in with a **second** Google account and copy its token the same way.
3. In PowerShell:

```powershell
$env:TOKEN_A = "<paste your token>"
$env:TOKEN_B = "<paste the second account's token>"
python scripts/check_live.py https://<your-app>.onrender.com
```

It checks the public pages and the 401s, runs upload → match → tailor → compare → export on account A, and
proves that account B gets **404** on every one of A's records. Expect `0 failed`. Tokens expire after ~1 hour.

## Step 8 – Optional: your own domain

1. Render → your service → **Settings** → **Custom Domains** → **Add** → e.g. `app.example.com`.
2. At your domain registrar, add the DNS record Render shows (usually a **CNAME** → `<your-app>.onrender.com`).
   Render issues the HTTPS certificate automatically (minutes to an hour).
3. Update everything that mentions the old address:
   - Render env: `APP_BASE_URL=https://app.example.com` (and `CORS_ORIGINS=https://<your-app>.onrender.com` if you keep both)
   - Supabase URL Configuration: Site URL + `https://app.example.com/**`
   - Google client: JavaScript origin `https://app.example.com`; Branding: authorized domain `example.com`

## Running it on your PC

- **Single-user mode (no login)**: keep `SUPABASE_URL` empty in `.env` and run `run.bat`, same as before.
- **Against Supabase**: put the Step 1 values in `.env` (keep `APP_ENV=development`) and run `run.bat`;
  sign in at <http://127.0.0.1:8000>.

---

## Troubleshooting

**Login redirect loop / sent back to the home page / `?error=...` in the URL**
- Supabase → URL Configuration: the Site URL must be your real Render URL, and the Redirect URLs must include
  `https://<your-app>.onrender.com/**` (with `/**`).
- Google client: the redirect URI must be exactly `https://<project-ref>.supabase.co/auth/v1/callback`.
- "redirect_uri_mismatch" from Google → that redirect URI is missing or has a typo.
- Magic link opens but you're not signed in → open the link in the **same browser** you requested it from
  (the secure PKCE login only works there).

**401 "Your session is invalid or has expired" right after signing in**
- `SUPABASE_URL` on Render must be exactly `https://<project-ref>.supabase.co` (no trailing slash, no `/rest/v1`).
- Legacy (HS256) projects: `SUPABASE_JWT_SECRET` must be the *JWT secret*, not the anon or service key.
- New-signing-key projects: leave the secret empty; check the Render logs for `signing key not found`
  (it means the server couldn't reach `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`).
- Your PC's clock must be correct (tokens are time-limited).

**Deploy fails: "APP_ENV=production but these settings are missing: …"**
The app refuses to run publicly without login. Add the listed variables in Render → Environment.

**Database errors: `password authentication failed`, `Network is unreachable`, `prepared statement … already exists`**
Use the **Session pooler** URI (Step 1.5) with the real password, not the direct connection or transaction pooler.

**"File storage error during upload (HTTP 400/403)"**
`SUPABASE_SERVICE_ROLE_KEY` is wrong. It must be the **service_role** key, not the anon key.

**Playwright / Chromium crash on Render (PDF export fails, "Target closed", "browserType.launch")**
Usually out of memory on the free 512 MB instance (see the next item). Word (.docx) and CSV exports don't use
Chromium and keep working. The CI job "Chromium and Tesseract work inside the image" proves the image itself is fine.

**Out of memory ("Ran out of memory (used over 512MB)", instance restarts)**
Chromium needs ~200–300 MB per PDF render. Options: Render → Settings → **Instance type** → *Standard* (2 GB);
avoid many parallel PDF exports; set `COMPARE_CONCURRENCY=2`.

**Slow first request (30–60 s) / "cold starts" on the free plan**
Free services sleep after 15 minutes without traffic, and the next visitor waits for a restart. Upgrade to a paid
instance (always on), or accept it for testing. Long background jobs can also be cut off when the instance sleeps;
just retry them.

**429 "You've used all 10 free matches this month."**
That's the free plan working. Change limits with `FREE_*_PER_MONTH` env vars, or give an account unlimited use:
Supabase → Table Editor → `profiles` → set `plan` to `pro`.

**429 "Too many requests"**
The per-user rate limit (`RATE_LIMIT_HEAVY=20/minute`). Wait a minute.

**"Couldn't read this link, please paste the JD text"**
Some sites (LinkedIn, Glassdoor) need a login, so paste the JD text instead.

**Gemini errors: "daily free-tier quota … used up" / rate limit**
Enable billing on the Gemini key's project, or switch `GEMINI_MODEL` to a `-lite` model.

## Plans and costs

| Service | Free tier | When to upgrade |
|---|---|---|
| Supabase | 500 MB database, 1 GB storage, 50,000 monthly users; **pauses after 1 week without activity** | Pro ($25/mo) for production: no pausing, daily backups |
| Render | 512 MB, sleeps after 15 min | Starter/Standard for always-on and more RAM for PDF export |
| Gemini API | Free tier with daily limits; content may be used by Google | Billing enabled for user data (pay per use, cheap with Flash-Lite) |
| Sentry | 5,000 errors/month | rarely needed |

## What runs where (reference)

- **Startup**: `alembic upgrade head` (Dockerfile CMD, and again inside the app), creates the storage buckets if missing,
  starts the daily clean-up (03:15 UTC: deletes resumes older than each user's auto-delete setting, and export files older than 1 day).
- **Security**: every API route needs a valid Supabase token; every query filters by the signed-in user; other users'
  records answer 404; RLS is on for all tables; files are private and downloaded through 10-minute signed links;
  the service-role key exists only on the server; logs contain ids and counts, never resume text, names or e-mails.
