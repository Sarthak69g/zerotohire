# ZeroToHire - Phase 1 (Trust)

Gate for this phase: **5 outside people can generate a kit unaided, and nothing on screen is invented.**

## What changed

| Checklist item | What it does now |
|---|---|
| Resume required, DOCX accepted | Empty or tiny resumes are rejected with a clear message (min 40 words). PDF, DOCX and TXT uploads are read properly. Old `.doc` and scanned PDFs give a clear error, not garbage. |
| Clean exports | Model preambles/postambles are stripped. Markdown becomes real headings and bullets in DOCX and PDF. No `**` or `##` in any file. |
| Honest scoring | Whole-word matching (no more "java" inside "javascript"), synonym groups (js, postgres, CI, REST...). Score = weighted coverage. If the job description has fewer than 5 usable keywords, **no score is shown** (the 7/10 fallback is gone). Unscored runs show "-" in history and the dashboard. |
| Parallel stages, live results | JD reader first, then resume + cover + interview run together. Each result appears on screen as soon as it finishes. |
| Kits in the database | Kit text is stored in `Run.kit_json`. DOCX/PDF/TXT are built on download, so a redeploy cannot delete a user's kit. Old runs still download from their old files if those exist. New columns are added automatically at startup. |
| CrewAI removed | One thin client (`llm_client.py`), one default model (`llama-3.3-70b-versatile`, override with `GROQ_MODEL`). Retries on 429/5xx, clear error messages. `crewai` removed from requirements. |
| A failed stage is not charged | If any stage fails, nothing is saved and no free-plan quota is used. |
| Interview simulator | Fixed length (5 questions; rapid fire 8), structured AI replies, retry on a bad reply, **score and grade computed in code**, one-word answers capped at 2/10, final report, "End interview" button, double-click and stale-tab protection, owner-only access. |
| Bug found while testing | The **Run Kit button crashed** (`jobUrlInput` missing from the page) in every earlier version. Fixed: the Job URL field with Import button is back. Also fixed a dashboard crash (`dashboardGrid`). |
| Honest copy | Removed "CrewAI" mentions, "AI scored" (it is keyword overlap), "7 sources" (it is 5), "Files Saved" toast, and the dead bulk-import code. |

## Files

New: `kit_engine.py`, `llm_client.py`, `interview_engine.py`, `tests/run_audit_tests.py`
Changed: `main.py`, `automation.py`, `models.py`, `index.html`, `requirements.txt`, `render.yaml`, `.env.example`, `README.md`

## How to install (5 minutes)

1. Copy **all** files above into your repo, including the three new modules and the `tests/` folder. Missing a new module = the app will not start.
2. `pip install -r requirements.txt` (optional: `pip uninstall crewai` to free space).
3. In your local `.env` you can leave `GROQ_MODEL` as is (a leading `groq/` is ignored) or set `GROQ_MODEL=llama-3.3-70b-versatile`.
4. `python tests/run_audit_tests.py` should end with no `CONFIRMED`, `BROKEN` or `ERROR` lines. It runs on a temp copy and cannot touch your database.
5. `python main.py`, then try the checks below.

## Your 10-minute check with the real AI (I could not do this part)

I tested everything with the AI faked, so real Groq output quality is the one thing only you can confirm.
1. Paste a real job description and your real resume, click Run. Results should appear one by one.
2. Download Resume.docx and Kit.pdf. Open them: real headings and bullets, no `**`, no "Here is your resume".
3. Click Run with the resume box empty: you should get a clear error and no quota used.
4. Upload a `.docx` resume.
5. Interview: start a Technical round, answer 5 questions (try one answer of a single word), check the final report.
6. Groq free tier has request limits. One kit makes 4 AI calls. If several people run kits at once and you see "busy, try again", that is the limit, not a bug (the client already retries).

## Your decisions (cost involved, so they are yours)

**Always-on hosting + database that does not expire.** Today `render.yaml` uses Render free plans: the app sleeps after ~15 min idle (30-second first load) and the free Postgres is deleted after 90 days. Pick one before the outside-people test:
- **A. Cheapest:** keep the free Render web service, use a free Neon or Supabase Postgres (no expiry) via `DATABASE_URL`, and ping `/health` every 5 minutes with a free uptime monitor.
- **B. Simplest:** Render paid web service + paid Postgres.
Tell me which and I will adjust `render.yaml` and the README.

## The gate: 5 outside people

1. Send the link to 5 people who are not you (classmates, a LinkedIn contact). Give no instructions beyond "get a kit for a job you want".
2. Watch or ask: Did they finish? Where did they hesitate? Would they use the file? Did anything look wrong or made up?
3. Fix whatever 2+ people hit. When 5 of 5 finish unaided, Phase 1 is done.

## Known limits (planned for Phase 2)

- Built-from-scratch resumes (`/build-resume`) and their monthly counter still live on disk, so they reset when the host disk resets (this only favours the user).
- Rate limits are per worker until Redis is added (Phase 2).
- Not tested by me: live Razorpay, Google sign-in, Gmail OTP, live job scraping, the Render deployment.
