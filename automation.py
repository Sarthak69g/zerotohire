# ============================================================
#  ZeroToHire — Automation Stack v2.0
#  Features:
#   1. Job Scraper        (Internshala, Adzuna, RemoteOK, Instahyre, Shine)
#   2. Cold Email Drafts  (draft only - user sends from their own inbox)
#   3. (Telegram bot removed in Phase 0 - it could message any chat_id)
#   4. Interview Simulator (real-time mock interview AI)
#   5. Scheduled Daily Run (APScheduler every morning)
# ============================================================

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from models import db, InterviewSession, ColdEmailLog, AutopilotConfig, JobFeedItem
import interview_engine as ie
from llm_client import LLMError, chat_json

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def _safe_str(value, max_len=2000, default=''):
    if value is None or isinstance(value, (dict, list)):
        return default
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return text[:max_len]


def _safe_long_text(value, max_len=20000, default=''):
    if value is None or isinstance(value, (dict, list)):
        return default
    return str(value).strip()[:max_len]


def _is_valid_email(value):
    return bool(EMAIL_RE.match(_safe_str(value, max_len=255)))

load_dotenv()
log = logging.getLogger("automation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / ".data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SCRAPER_CACHE_FILE = DATA_DIR / "scraped_jobs.json"
# Interview sessions and cold-email log now live in SQLite (see models.py).
# Scraped-job cache stays as a JSON file on purpose — it's a disposable,
# regenerated-on-every-scrape cache, not user data, so there's nothing to
# protect by moving it into the DB.

# ── helpers ────────────────────────────────────────────────

def _read(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ============================================================
# 1. JOB SCRAPER
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Rough honesty labels, not a guarantee — RemoteOK is a real JSON API so it
# rarely breaks silently. Internshala/Adzuna fall back to raw HTML selectors
# only when structured data isn't present. Instahyre/Shine have no structured
# fallback at all, so they're the two most likely to quietly return nothing
# the moment either site tweaks its markup.
SOURCE_RELIABILITY = {
    "RemoteOK": "high",
    "Internshala": "medium",
    "Adzuna": "medium",
    "Instahyre": "low",
    "Shine": "low",
}

def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def scrape_internshala(role: str, location: str = "") -> list[dict]:
    """Scrape internships from Internshala."""
    jobs = []
    try:
        keyword = role.replace(" ", "-").lower()
        url = f"https://internshala.com/internships/{keyword}-internship"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select(".internship_meta") or soup.select(".individual_internship") or soup.select(".internship-heading-container")
        for card in cards[:15]:
            title_el = card.select_one(".profile") or card.select_one("h3") or card.select_one(".heading_4_5")
            company_el = card.select_one(".company_name") or card.select_one(".company-name") or card.select_one(".heading_6")
            location_el = card.select_one(".locations") or card.select_one(".location_link") or card.select_one("#location_names")
            stipend_el = card.select_one(".stipend") or card.select_one(".salary") or card.select_one(".desktop-text")
            link_el = card.select_one("a[href]")

            title = _clean(title_el.text) if title_el else role
            company = _clean(company_el.text) if company_el else "Unknown"
            loc = _clean(location_el.text) if location_el else "Remote"
            stipend = _clean(stipend_el.text) if stipend_el else "Unpaid"
            href = link_el["href"] if link_el else ""
            source_url = f"https://internshala.com{href}" if href.startswith("/") else href

            if title and company:
                jobs.append({
                    "title": title,
                    "company": company,
                    "location": loc,
                    "stipend": stipend,
                    "source": "Internshala",
                    "source_url": source_url,
                    "text": f"{title} at {company}. Location: {loc}. Stipend: {stipend}.",
                    "scraped_at": datetime.now().isoformat(timespec="seconds"),
                })
        log.info(f"Internshala: scraped {len(jobs)} jobs for '{role}'")
    except requests.exceptions.Timeout:
        log.warning(f"Internshala timeout for '{role}'")
    except requests.exceptions.ConnectionError:
        log.warning(f"Internshala connection error for '{role}'")
    except Exception as e:
        log.error(f"Internshala scrape error: {e}")
    return jobs


def scrape_adzuna(role: str, location: str = "india") -> list[dict]:
    """Adzuna — tries JSON-LD structured data, falls back to HTML cards."""
    jobs = []
    try:
        keyword = requests.utils.quote(role)
        loc_q = requests.utils.quote(location)
        url = f"https://www.adzuna.in/search?q={keyword}&where={loc_q}"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        title = _clean(item.get("title", role))
                        company = _clean((item.get("hiringOrganization") or {}).get("name", "Unknown"))
                        loc_text = _clean((item.get("jobLocation") or {}).get("address", {}).get("addressLocality", location))
                        jobs.append({"title": title, "company": company, "location": loc_text,
                            "source": "Adzuna", "source_url": item.get("url", url), "type": "fulltime",
                            "text": f"{title} at {company}. {loc_text}.",
                            "scraped_at": datetime.now().isoformat(timespec="seconds")})
            except Exception:
                pass
        if not jobs:
            for card in (soup.select(".result") or soup.select("article"))[:10]:
                t = card.select_one("h2 a") or card.select_one("h2")
                c = card.select_one(".company") or card.select_one("[class*=company]")
                l = card.select_one(".location") or card.select_one("[class*=location]")
                a = card.select_one("a[href]")
                title, company, loc_text = _clean(t.text if t else role), _clean(c.text if c else "Unknown"), _clean(l.text if l else location)
                href = (a["href"] if a else url)
                if not href.startswith("http"): href = "https://www.adzuna.in" + href
                if title and company != "Unknown":
                    jobs.append({"title": title, "company": company, "location": loc_text,
                        "source": "Adzuna", "source_url": href, "type": "fulltime",
                        "text": f"{title} at {company}. {loc_text}.",
                        "scraped_at": datetime.now().isoformat(timespec="seconds")})
        log.info(f"Adzuna: {len(jobs)} jobs for '{role}'")
    except Exception as e:
        log.error(f"Adzuna error: {e}")
    return jobs[:12]


def scrape_remoteok(role: str, location: str = "") -> list[dict]:
    """RemoteOK public JSON API — no auth needed, works reliably."""
    jobs = []
    try:
        api_url = f"https://remoteok.com/api?tag={requests.utils.quote(role)}"
        resp = requests.get(api_url, headers={**HEADERS, "Accept": "application/json"}, timeout=12)
        data = resp.json()
        listings = [d for d in data if isinstance(d, dict) and d.get("company")]
        for item in listings[:12]:
            title = _clean(item.get("position", role))
            company = _clean(item.get("company", "Unknown"))
            salary = item.get("salary_min")
            sal_text = f"${salary:,}+" if salary else None
            tags = ", ".join((item.get("tags") or [])[:5])
            jobs.append({"title": title, "company": company, "location": "Remote",
                "stipend": sal_text, "source": "RemoteOK",
                "source_url": item.get("url", "https://remoteok.com"), "type": "remote",
                "text": f"{title} at {company}. Remote. Tags: {tags}.",
                "scraped_at": datetime.now().isoformat(timespec="seconds")})
        log.info(f"RemoteOK: {len(jobs)} jobs for '{role}'")
    except Exception as e:
        log.error(f"RemoteOK error: {e}")
    return jobs


def scrape_instahyre(role: str, location: str = "delhi") -> list[dict]:
    """Instahyre — Indian startup jobs platform."""
    jobs = []
    try:
        keyword = requests.utils.quote(role)
        url = f"https://www.instahyre.com/search-jobs/?q={keyword}"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        title = _clean(item.get("title", role))
                        company = _clean((item.get("hiringOrganization") or {}).get("name", "Unknown"))
                        loc_text = _clean((item.get("jobLocation") or {}).get("address", {}).get("addressLocality", location))
                        if title:
                            jobs.append({"title": title, "company": company, "location": loc_text,
                                "source": "Instahyre", "source_url": item.get("url", url), "type": "fulltime",
                                "text": f"{title} at {company}. {loc_text}.",
                                "scraped_at": datetime.now().isoformat(timespec="seconds")})
            except Exception:
                pass
        if not jobs:
            for card in (soup.select(".opportunity-card") or soup.select(".job-card") or soup.select(".job-listing"))[:10]:
                t = card.select_one("h2") or card.select_one("h3")
                c = card.select_one(".company-name") or card.select_one(".employer")
                l = card.select_one(".location")
                a = card.select_one("a[href]")
                title, company = _clean(t.text if t else role), _clean(c.text if c else "Unknown")
                loc_text = _clean(l.text if l else location)
                href = a["href"] if a else ""
                if not href.startswith("http"): href = "https://www.instahyre.com" + href
                if title and company != "Unknown":
                    jobs.append({"title": title, "company": company, "location": loc_text,
                        "source": "Instahyre", "source_url": href, "type": "fulltime",
                        "text": f"{title} at {company}. {loc_text}.",
                        "scraped_at": datetime.now().isoformat(timespec="seconds")})
        log.info(f"Instahyre: {len(jobs)} jobs for '{role}'")
    except Exception as e:
        log.error(f"Instahyre error: {e}")
    return jobs[:12]


def scrape_shine(role: str, location: str = "delhi") -> list[dict]:
    """Shine.com — major Indian job portal."""
    jobs = []
    try:
        r = role.replace(" ", "-").lower()
        l = location.replace(" ", "-").lower()
        url = f"https://www.shine.com/job-search/{r}-jobs-in-{l}"
        resp = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else data.get("@graph", [data])
                for item in items:
                    if item.get("@type") == "JobPosting":
                        title = _clean(item.get("title", role))
                        company = _clean((item.get("hiringOrganization") or {}).get("name", "Unknown"))
                        loc_text = _clean((item.get("jobLocation") or {}).get("address", {}).get("addressLocality", location))
                        sal = item.get("baseSalary", {})
                        sal_text = None
                        if isinstance(sal, dict):
                            mn = sal.get("value", {}).get("minValue", "")
                            mx = sal.get("value", {}).get("maxValue", "")
                            if mn: sal_text = f"₹{mn}–{mx}" if mx else f"₹{mn}"
                        if title:
                            jobs.append({"title": title, "company": company, "location": loc_text,
                                "stipend": sal_text, "source": "Shine", "source_url": item.get("url", url),
                                "type": "fulltime", "text": f"{title} at {company}. {loc_text}.",
                                "scraped_at": datetime.now().isoformat(timespec="seconds")})
            except Exception:
                pass
        if not jobs:
            for card in (soup.select(".job-listing") or soup.select(".jobCard") or soup.select("[class*=job-card]"))[:10]:
                t = card.select_one("h2") or card.select_one("h3")
                c = card.select_one(".company") or card.select_one("[class*=company]")
                l_el = card.select_one(".location") or card.select_one("[class*=location]")
                s_el = card.select_one(".salary") or card.select_one("[class*=salary]")
                a = card.select_one("a[href]")
                title, company = _clean(t.text if t else role), _clean(c.text if c else "Unknown")
                loc_text = _clean(l_el.text if l_el else location)
                sal_text = _clean(s_el.text if s_el else "")
                href = a["href"] if a else url
                if not href.startswith("http"): href = "https://www.shine.com" + href
                if title and company != "Unknown":
                    jobs.append({"title": title, "company": company, "location": loc_text,
                        "stipend": sal_text or None, "source": "Shine", "source_url": href,
                        "type": "fulltime", "text": f"{title} at {company}. {loc_text}.",
                        "scraped_at": datetime.now().isoformat(timespec="seconds")})
        log.info(f"Shine: {len(jobs)} jobs for '{role}'")
    except Exception as e:
        log.error(f"Shine error: {e}")
    return jobs[:12]


def run_job_scraper(roles: list[str], locations: list[str]) -> list[dict]:
    """
    Main scraper — runs all 5 sources in parallel threads for speed.
    Sources: Internshala, Adzuna, RemoteOK, Instahyre, Shine
    (LinkedIn and Naukri were removed — both require JS-rendered pages that
    plain requests+BeautifulSoup cannot see, so they returned empty results.)
    """
    all_jobs = []
    loc = locations[0] if locations else "delhi"
    sources_hit = set()

    for role in roles[:3]:  # max 3 roles to keep it fast
        results: dict[str, list] = {}

        def _run(fn, name, _role=role, _loc=loc):
            try:
                results[name] = fn(_role, _loc)
            except Exception as e:
                log.error(f"{name} thread error: {e}")
                results[name] = []

        threads = [
            threading.Thread(target=_run, args=(scrape_internshala, "Internshala")),
            threading.Thread(target=_run, args=(scrape_adzuna,      "Adzuna")),
            threading.Thread(target=_run, args=(scrape_remoteok,    "RemoteOK")),
            threading.Thread(target=_run, args=(scrape_instahyre,   "Instahyre")),
            threading.Thread(target=_run, args=(scrape_shine,       "Shine")),
        ]
        for t in threads: t.start()
        for t in threads: t.join(timeout=15)

        for name, source_jobs in results.items():
            if source_jobs:
                sources_hit.add(name)
            all_jobs.extend(source_jobs)

    # Deduplicate by title+company
    seen = set()
    unique = []
    for job in all_jobs:
        key = f"{job['title'].lower()}|{job['company'].lower()}"
        if key not in seen:
            # 'reliability' — RemoteOK is a real JSON API, so it almost never
            # silently breaks. Internshala/Adzuna use structured JSON-LD when
            # the site provides it. Instahyre/Shine lean on raw CSS selectors
            # with no structured fallback, so they're the first to go quiet
            # when either site changes its markup. This doesn't change what
            # gets returned — it's an extra field for the frontend/analytics
            # to eventually show "source may be stale" instead of pretending
            # every source is equally trustworthy.
            job['reliability'] = SOURCE_RELIABILITY.get(job.get('source', ''), 'unknown')
            seen.add(key)
            unique.append(job)

    # cache result
    _write(SCRAPER_CACHE_FILE, {"jobs": unique, "scraped_at": datetime.now().isoformat(), "sources": list(sources_hit)})
    log.info(f"Scraper total: {len(unique)} unique jobs from {sources_hit}")
    return unique


# ============================================================
# 2. COLD EMAIL DRAFTS  (Phase 0: the server no longer sends email)
# ============================================================
# Why: sending from one shared platform Gmail with a user-controlled display
# name was spoofable, claimed an attachment that never existed, and the
# job-URL fetch to look for an HR address was an SSRF hole. Users now get a
# draft they send from their OWN inbox (mailto / copy).

def build_cold_email_body(candidate_name: str, candidate_email: str, company: str, role: str, cover_letter_snippet: str = "") -> str:
    snippet = (cover_letter_snippet or "").strip()[:600]
    if not snippet:
        snippet = f"[Add 1-2 lines on why you're a good fit for the {role} role - mention one real project or skill.]"
    contact = f"{candidate_name}\n{candidate_email}".strip()
    return f"""Hi there,

I came across the {role} opening at {company} and wanted to reach out directly.

{snippet}

I'd love the opportunity to discuss how I can contribute to your team. [Attach your resume before sending.]

Looking forward to hearing from you.

Best regards,
{contact}
"""


def get_cold_email_log(user_id: int) -> list[dict]:
    """Owner-scoped. Old rows saved before user_id existed are not shown."""
    rows = ColdEmailLog.query.filter_by(user_id=user_id).order_by(ColdEmailLog.id.desc()).all()
    return [r.to_dict() for r in rows]


# ============================================================
# 3. INTERVIEW SIMULATOR  (Phase 1: fixed length, structured replies, real report)
#    Prompts / parsing / scoring live in interview_engine.py.
# ============================================================

def _load_turns(row) -> list[dict]:
    """Stored turns: [{n, question, answer|None, score, feedback, hint, reaction}].
    Sessions saved before Phase 1 used a chat-message list; they have no turns."""
    try:
        data = json.loads(row.history_json or "[]")
    except ValueError:
        data = []
    return [t for t in data if isinstance(t, dict) and "question" in t]


def _call_json(messages, **kwargs) -> dict:
    return chat_json(messages, **kwargs)


def _ask_structured(build_messages, parse, attempts=2, **kwargs):
    """Ask for JSON and validate it; retry once if the structure is wrong.
    A reply that never validates becomes an LLMError, never a made-up value."""
    last = None
    for _ in range(attempts):
        try:
            return parse(_call_json(build_messages(), **kwargs))
        except ie.InterviewFormatError as exc:
            last = exc
            log.warning("Interview reply had the wrong structure: %s", exc)
    raise LLMError("The interviewer gave an unreadable reply. Please try again.")


def _session_view(row) -> dict:
    """What the browser may see about a session (never the JD/resume text)."""
    turns = _load_turns(row)
    scores = json.loads(row.scores_json or "[]")
    pending = next((t for t in turns if t.get("answer") is None), None)
    avg = ie.average(scores)
    return {
        "session_id": row.session_id,
        "round_type": row.round_type,
        "round_name": row.round_name,
        "role": row.role,
        "company": row.company,
        "status": row.status,
        "total_questions": ie.total_questions(row.round_type),
        "answered": len(scores),
        "average_score": avg,
        "grade": ie.grade_for(avg),
        "scores": scores,
        "pending_question": pending["question"] if (pending and row.status == "active") else None,
        "question_number": pending["n"] if (pending and row.status == "active") else None,
        "turns": [
            {"n": t["n"], "question": t["question"], "answer": t.get("answer"), "score": t.get("score"),
             "feedback": t.get("feedback", ""), "hint": t.get("hint", "")}
            for t in turns if t.get("answer") is not None
        ],
        "report": json.loads(row.report_json) if row.report_json else None,
    }


def create_interview_session(user_id: int, round_type: str, jd: str, resume: str, role: str, company: str) -> dict:
    cfg = ie.round_config(round_type)
    round_type = round_type if round_type in ie.ROUNDS else ie.DEFAULT_ROUND
    role = role or ie.guess_role(jd)
    # Ask the AI for question 1 BEFORE saving anything: a failed start leaves no junk session.
    question = _ask_structured(
        lambda: ie.first_question_messages(round_type, role, company, jd, resume),
        ie.parse_question, temperature=0.6, max_tokens=300, timeout=40,
    )
    turns = [{"n": 1, "question": question, "answer": None}]
    row = InterviewSession(
        session_id=f"iv-{user_id}-{int(time.time())}-{secrets.token_hex(6)}",
        user_id=user_id,
        round_type=round_type,
        round_name=cfg["name"],
        role=role[:300],
        company=company[:300],
        jd=jd[:6000],
        resume=resume[:6000],
        history_json=json.dumps(turns),
        scores_json="[]",
        total_score=0,
        question_count=0,
        status="active",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.session.add(row)
    db.session.commit()
    log.info("Interview session created: %s", row.session_id)
    view = _session_view(row)
    view["intro"] = cfg["intro"]
    return view


def get_interview_session(session_id: str, user_id: int) -> dict | None:
    """Owner-scoped: someone else's session looks exactly like a missing one."""
    row = InterviewSession.query.filter_by(session_id=session_id, user_id=user_id).first()
    return _session_view(row) if row else None


def _finalize(row, turns) -> dict:
    """Build + store the final report. The AI writes the wording (best effort);
    numbers always come from the stored scores."""
    narrative = None
    try:
        narrative = _call_json(ie.report_messages(row.round_type, row.role, row.company, turns),
                               temperature=0.3, max_tokens=700, timeout=40)
    except LLMError as exc:
        log.warning("Interview report narrative failed (using built-in wording): %s", exc)
    report = ie.build_report(turns, narrative)
    row.report_json = json.dumps(report)
    return report


def answer_interview_question(session_id: str, user_id: int, user_answer: str, expected_n: int | None = None) -> dict:
    row = InterviewSession.query.filter_by(session_id=session_id, user_id=user_id).first()
    if not row:
        return {"error": "Session not found", "code": "not_found"}
    if row.status != "active":
        return {"error": "This interview is already finished. Start a new one.", "code": "finished"}

    turns = _load_turns(row)
    pending = turns[-1] if turns and turns[-1].get("answer") is None else None
    if not pending:
        return {"error": "This session has no open question. Start a new interview.", "code": "finished"}
    if expected_n is not None and expected_n != pending["n"]:
        # a double-click or a stale tab: tell the browser where we really are
        return {"error": "That answer was for a different question.", "code": "out_of_sync",
                "question_number": pending["n"], "question": pending["question"]}

    total = ie.total_questions(row.round_type)
    is_last = pending["n"] >= total
    answered_before = row.question_count or 0

    evaluation = _ask_structured(
        lambda: ie.answer_messages(row.round_type, row.role, row.company, row.jd, row.resume, turns, user_answer, is_last),
        lambda data: ie.parse_evaluation(data, user_answer, is_last),
        temperature=0.4, max_tokens=700, timeout=45,
    )

    # The AI call is slow: make sure nothing else answered this question meanwhile.
    db.session.refresh(row)
    if (row.question_count or 0) != answered_before or row.status != "active":
        return {"error": "That answer was for a different question.", "code": "out_of_sync"}

    pending.update(answer=user_answer, score=evaluation["score"], feedback=evaluation["feedback"],
                   hint=evaluation["hint"], reaction=evaluation["reaction"])
    scores = json.loads(row.scores_json or "[]") + [evaluation["score"]]
    if not is_last:
        turns.append({"n": pending["n"] + 1, "question": evaluation["next_question"], "answer": None})

    row.history_json = json.dumps(turns)
    row.scores_json = json.dumps(scores)
    row.question_count = len(scores)
    row.total_score = ie.average(scores) or 0
    report = None
    if is_last:
        row.status = "completed"
        report = _finalize(row, turns)
    db.session.commit()

    return {
        "reaction": evaluation["reaction"],
        "score": evaluation["score"],
        "feedback": evaluation["feedback"],
        "model_answer_hint": evaluation["hint"],
        "next_question": evaluation["next_question"] or None,
        "question_number": None if is_last else pending["n"] + 1,
        "answered": len(scores),
        "total_questions": total,
        "average_score": row.total_score,
        "status": row.status,
        "report": report,
        "session_id": session_id,
    }


def finish_interview(session_id: str, user_id: int) -> dict:
    """End early. Keeps every answer given so far and writes a report from them."""
    row = InterviewSession.query.filter_by(session_id=session_id, user_id=user_id).first()
    if not row:
        return {"error": "Session not found", "code": "not_found"}
    if row.status == "active":
        row.status = "ended"
        turns = _load_turns(row)
        if any(t.get("answer") is not None for t in turns):
            _finalize(row, turns)
        db.session.commit()
    return _session_view(row)


def get_interview_summary(session_id: str, user_id: int) -> dict:
    view = get_interview_session(session_id, user_id)
    return view or {"error": "Session not found"}


def list_user_interview_sessions(user_id: int) -> list[dict]:
    rows = (
        InterviewSession.query.filter_by(user_id=user_id)
        .order_by(InterviewSession.created_at.desc())
        .all()
    )
    return [
        {
            "session_id": r.session_id,
            "round_name": r.round_name,
            "role": r.role,
            "company": r.company,
            "total_score": r.total_score or 0,
            "answered": r.question_count or 0,
            "total_questions": ie.total_questions(r.round_type),
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def merge_scraped_jobs(user_id: int, jobs: list[dict], origin: str = "scraped") -> int:
    """Save scraped jobs WITHOUT touching what the user saved by hand.
    Only previous scraped/autopilot rows are replaced; manual items stay.
    Needs an app context. Returns number of rows added."""
    JobFeedItem.query.filter(
        JobFeedItem.user_id == user_id,
        JobFeedItem.origin.in_(["scraped", "autopilot"]),
    ).delete(synchronize_session=False)
    kept = {r.source_url for r in JobFeedItem.query.filter_by(user_id=user_id).all()}
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for job in jobs:
        url = (job.get("source_url") or "")[:1000]
        if not url or url in kept:
            continue  # unique (user, url); a URL-less job can't be opened anyway
        kept.add(url)
        db.session.add(JobFeedItem(
            user_id=user_id, source_url=url,
            title=(job.get("title") or "")[:500], text=job.get("text", ""),
            origin=origin, saved_at=now, updated_at=now,
        ))
        added += 1
    db.session.commit()
    return added


# ============================================================
# 5. SCHEDULER (APScheduler)
# ============================================================

_scheduler_lock_fh = None


def start_scheduler(app_context_fn=None):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler not installed. Run: pip install apscheduler")
        log.warning("Scheduler will not run.")
        return None

    # gunicorn runs several workers and each imports this module. Without a
    # guard every worker starts its own scheduler and the daily run executes
    # N times. An exclusive file lock lets exactly one worker own it.
    global _scheduler_lock_fh
    try:
        import fcntl, tempfile
        _scheduler_lock_fh = open(os.path.join(tempfile.gettempdir(), "zerotohire_scheduler.lock"), "w")
        fcntl.flock(_scheduler_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # Windows local dev: single process anyway
    except OSError:
        log.info("Scheduler already running in another worker; skipping.")
        return None

    scheduler = BackgroundScheduler(timezone=os.getenv("SCHEDULER_TZ", "Asia/Kolkata"))

    def _run_daily_job_body():
        for cfg_row in AutopilotConfig.query.filter_by(enabled=True).all():
            cfg = cfg_row.to_dict()
            user_id = cfg_row.user_id
            roles = cfg.get("roles", [])
            locations = cfg.get("locations", ["delhi"])

            if not roles:
                continue

            log.info(f"Running scraper for user {user_id}: {roles}")
            jobs = run_job_scraper(roles, locations)

            merge_scraped_jobs(user_id, jobs, origin="autopilot")

            log.info(f"Daily run complete for user {user_id}: {len(jobs)} jobs found")

    def daily_job_run():
        log.info("Scheduled daily job run starting...")
        try:
            # DB access needs a Flask app context — the scheduler runs this
            # on its own background thread, outside any request, so we push
            # one manually using the factory main.py hands us.
            if app_context_fn:
                with app_context_fn():
                    _run_daily_job_body()
            else:
                log.warning("No app context available for scheduler; skipping daily run.")
        except Exception as e:
            log.error(f"Scheduled daily run error: {e}")

    scheduler.add_job(
        daily_job_run,
        CronTrigger(hour=8, minute=0),
        id="daily_job_run",
        replace_existing=True,
    )

    scheduler.start()
    log.info("Scheduler started — daily job run at 8:00 AM (%s)", os.getenv("SCHEDULER_TZ", "Asia/Kolkata"))
    return scheduler


# ============================================================
# FLASK ROUTES
# ============================================================

def register_automation_routes(app, require_user_fn, read_autopilot_config_fn, write_job_feed_fn, limiter=None, require_pro_fn=None):
    from flask import jsonify, request, session

    def _require_pro(user_id):
        """Falls back to allowing everyone through if main.py doesn't pass
        a require_pro_fn — keeps this module usable/testable standalone
        without hard-depending on the billing system existing."""
        if not require_pro_fn:
            return None, None
        _, error_resp, status = require_pro_fn()
        return error_resp, status

    def _maybe_limit(rule):
        """Apply limiter.limit(rule) if a limiter was passed in, otherwise no-op."""
        def decorator(fn):
            return limiter.limit(rule)(fn) if limiter else fn
        return decorator

    @app.route("/automation/scrape", methods=["POST"])
    @_maybe_limit("5 per hour; 15 per day")
    def automation_scrape():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status

        data = request.get_json(silent=True) or {}
        roles = [_safe_str(r, max_len=100) for r in (data.get("roles") or []) if isinstance(r, str) and r.strip()][:10]
        locations = [_safe_str(l, max_len=100) for l in (data.get("locations") or []) if isinstance(l, str) and l.strip()][:10]

        if not roles:
            cfg = read_autopilot_config_fn().get(str(user_id), {})
            roles = cfg.get("roles", ["python developer"])
            locations = locations or cfg.get("locations", ["delhi"])

        def scrape_and_respond():
            jobs = run_job_scraper(roles, locations)
            merge_scraped_jobs(user_id, jobs, origin="scraped")
            return jobs

        result = {"jobs": [], "error": None}
        def worker():
            # This runs in a background thread with no Flask app context of
            # its own — write_job_feed_fn() hits the database, and SQLAlchemy
            # needs an active app context for that. Without pushing one here,
            # every scrape silently 500s with "working outside of application
            # context" the moment it tries to save results.
            with app.app_context():
                try:
                    result["jobs"] = scrape_and_respond()
                except Exception as e:
                    result["error"] = str(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=45)

        if result["error"]:
            return jsonify({"error": result["error"]}), 500

        jobs = result["jobs"]
        sources = list({j["source"] for j in jobs}) if jobs else []
        return jsonify({
            "jobs": jobs,
            "count": len(jobs),
            "sources": sources,
            "message": f"Scraped {len(jobs)} jobs" + (f" from {', '.join(sources)}" if sources else ". Some sources may be blocked — try again later."),
        })

    @app.route("/automation/cold-email", methods=["POST"])
    @_maybe_limit("20 per hour")
    def automation_cold_email():
        """Returns a DRAFT only. The user sends it from their own inbox."""
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status

        data = request.get_json(silent=True) or {}
        company = _safe_str(data.get("company"), max_len=200)
        role = _safe_str(data.get("role"), max_len=200)
        candidate_name = _safe_str(data.get("candidate_name", ""), max_len=150) or ""
        candidate_email = _safe_str(data.get("candidate_email"), max_len=255)
        cover_snippet = _safe_long_text(data.get("cover_snippet"), max_len=3000)
        hr_email = _safe_str(data.get("hr_email"), max_len=255)

        if not company or not role:
            return jsonify({"error": "company and role are required"}), 400
        if candidate_email and not _is_valid_email(candidate_email):
            return jsonify({"error": "candidate_email looks invalid"}), 400
        if hr_email and not _is_valid_email(hr_email):
            return jsonify({"error": "hr_email looks invalid"}), 400

        body = build_cold_email_body(candidate_name, candidate_email, company, role, cover_snippet)
        return jsonify({
            "ok": True,
            "mode": "draft_only",
            "message": "Draft ready. Review it, attach your resume and send it from your own email.",
            "to": hr_email,
            "draft": {
                "subject": f"Application: {role}" + (f" - {candidate_name}" if candidate_name else ""),
                "body": body,
            },
        })

    @app.route("/automation/cold-email/log", methods=["GET"])
    def automation_cold_email_log():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify({"log": get_cold_email_log(user_id)})

    @app.route("/automation/interview/start", methods=["POST"])
    @_maybe_limit("10 per hour; 30 per day")
    def automation_interview_start():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        pro_error, pro_status = _require_pro(user_id)
        if pro_error:
            return pro_error, pro_status

        data = request.get_json(silent=True) or {}
        round_type = _safe_str(data.get("round_type", "technical"), max_len=30, default="technical")
        jd = _safe_long_text(data.get("jd"), max_len=20000)
        resume = _safe_long_text(data.get("resume"), max_len=20000)
        role = _safe_str(data.get("role"), max_len=150)
        company = _safe_str(data.get("company", "the company"), max_len=200, default="the company") or "the company"

        if round_type not in ie.ROUNDS:
            return jsonify({"error": f"round_type must be one of: {list(ie.ROUNDS.keys())}"}), 400
        if len(jd.strip()) < 30:
            return jsonify({"error": "Paste the job description first (at least a few lines) so the questions match the role."}), 400

        try:
            view = create_interview_session(user_id, round_type, jd, resume, role, company)
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502
        view["rounds_available"] = list(ie.ROUNDS.keys())
        return jsonify(view)

    @app.route("/automation/interview/answer", methods=["POST"])
    @_maybe_limit("20 per minute; 100 per hour")
    def automation_interview_answer():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        pro_error, pro_status = _require_pro(user_id)
        if pro_error:
            return pro_error, pro_status

        data = request.get_json(silent=True) or {}
        session_id = _safe_str(data.get("session_id"), max_len=120)
        user_answer = _safe_long_text(data.get("answer"), max_len=5000).strip()
        try:
            expected_n = int(data.get("question_number")) if data.get("question_number") is not None else None
        except (TypeError, ValueError):
            expected_n = None

        if not session_id or not user_answer:
            return jsonify({"error": "session_id and answer are required"}), 400

        try:
            result = answer_interview_question(session_id, user_id, user_answer, expected_n)
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502
        code = result.get("code")
        if code == "not_found":
            return jsonify(result), 404
        if code == "out_of_sync":
            return jsonify(result), 409
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/automation/interview/summary/<session_id>", methods=["GET"])
    def automation_interview_summary(session_id):
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        result = get_interview_summary(session_id, user_id)
        if result.get("error"):
            return jsonify(result), 404
        return jsonify(result)

    @app.route("/automation/interview/finish", methods=["POST"])
    @_maybe_limit("20 per hour")
    def automation_interview_finish():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        data = request.get_json(silent=True) or {}
        result = finish_interview(_safe_str(data.get("session_id"), max_len=120), user_id)
        if result.get("error"):
            return jsonify(result), 404
        return jsonify(result)

    @app.route("/automation/interview/sessions", methods=["GET"])
    def automation_interview_sessions():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify({"sessions": list_user_interview_sessions(user_id)})

    @app.route("/automation/status", methods=["GET"])
    def automation_status():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify({
            "scraper": True,
            "cold_email": "draft_only",
            "interview_simulator": bool(os.getenv("GROQ_API_KEY")),
            "scheduler": True,
        })

    log.info("Automation routes registered")
