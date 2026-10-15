# ZeroToHire (AutoApply AI)

AutoApply AI turns a job description and resume into a targeted application kit:

- job description and ATS analysis
- honest keyword-match score (counted from the job description, never guessed)
- rewritten resume
- cover letter
- interview prep kit
- real DOCX/PDF/TXT exports (generated on demand)
- mock interview simulator (fixed number of questions, scored in code, final report)
- saved run history
- local candidate profiles
- job URL import

The app uses Flask and Groq through one thin client (`llm_client.py`) with one default model. The frontend is a single `index.html` file that streams each result from the Flask backend as it finishes.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Add your real Groq key to `.env`:

```env
GROQ_API_KEY=your_real_key_here
```

## Run

```powershell
python main.py
```

Then open:

- `http://127.0.0.1:5000` (served by Flask), or
- `index.html` directly.

## How It Works

1. **Resume intake** (`kit_engine.py`): PDF, DOCX or TXT is read and checked. A resume that is empty or too short is rejected with a clear message instead of producing a made-up kit.
2. **JD Reader** runs first and extracts role, skills and keywords.
3. **Keyword match** is computed in code: whole-word matching with synonyms. If the job description gives fewer than 5 usable keywords, no score is shown (no default 7/10).
4. **Resume rewrite, cover letter and interview prep** run in parallel and each appears on screen as soon as it finishes. If any stage fails, nothing is saved and no quota is used.
5. The kit text is stored in the database (`Run.kit_json`). DOCX/PDF/TXT files are built from it when you download, with model preambles stripped and markdown turned into real headings and bullets.

## Code Map

| File | Job |
|---|---|
| `main.py` | Flask app, auth, billing, `/run` streaming pipeline, downloads |
| `kit_engine.py` | Resume intake, keyword scoring, prompts, output cleaning, DOCX/PDF builders |
| `llm_client.py` | The only place that talks to Groq (retries, JSON mode, safe errors) |
| `interview_engine.py` | Interview rounds, prompts, strict reply validation, score and report maths |
| `automation.py` | Job scraping, autopilot, interview routes |
| `models.py` | SQLAlchemy models |
| `tests/` | Test harness (AI is faked, no network needed) |

## Product Features

- Save candidate profiles locally in `profiles/`.
- Import public job pages with the Job URL Import panel.
- Review previous runs from the Run History panel.
- Inspect the ATS keyword heatmap after each run.
- Download TXT, DOCX or PDF for each part of a kit (made from the saved kit text).

## Output Files

Kit text lives in the database, so it survives redeploys on hosts with a disk that resets. Files are generated on request; `outputs/` is only a local cache.

## Notes

- PDF upload requires `pdfplumber`, included in `requirements.txt`.
- `.env` is ignored by Git so your API key stays local.
- `profiles/` is ignored by Git because it can contain personal resume data.
- Set `FLASK_DEBUG=true` only while developing.

## Deployment

The project is ready for single-service deployment (backend + frontend in one Flask app).

### Required environment variables

- `GROQ_API_KEY`
- `GROQ_MODEL` (optional, default `llama-3.3-70b-versatile`)
- `DATABASE_URL` (Postgres in production; SQLite is used locally if empty)
- `FRONTEND_ORIGIN` (use your deployed domain, or `*` if needed)
- `SESSION_SECRET` (required for login sessions)
- `SESSION_COOKIE_SECURE` (`true` in production over HTTPS)
- `PORT` (set by platform automatically)

### Optional OTP providers

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` for email/Gmail OTP delivery
- `OTP_DEV_MODE=true` to always return OTP in API response for local testing

### Optional Google OAuth + role routing

- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (optional; defaults to `<your-domain>/auth/google/callback`)
- `ADMIN_EMAILS` comma-separated list for admin dashboard widgets
- `RECRUITER_EMAILS` comma-separated list for recruiter dashboard widgets

### Render / Railway style deploy

This repo includes `render.yaml` for a Render blueprint deploy.

1. Push this folder to GitHub.
2. Create a new Web Service from the repo.
3. Build command:

```bash
pip install -r requirements.txt
```

4. Start command:

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 8 --timeout 180
```

5. Add environment variables in the platform dashboard.
6. Deploy and open the service URL.

### Staying online and keeping data

Render's free web service sleeps after about 15 minutes idle (first visit then takes ~30 seconds), and its free Postgres is deleted after 90 days. Before real users arrive, pick one:

- **Cheapest that works:** keep the free Render web service, point `DATABASE_URL` at a free Neon or Supabase Postgres (no expiry), and ping `/health` every 5 minutes with a free uptime monitor.
- **Simplest paid:** Render Starter web service plus Render Postgres paid plan (always on, no expiry).

## Tests

```bash
pip install -r requirements.txt
python tests/run_audit_tests.py
```
