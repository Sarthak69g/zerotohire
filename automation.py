# ============================================================
#  ZeroToHire — Automation Stack v2.0
#  Features:
#   1. Job Scraper        (Internshala, Adzuna, RemoteOK, Instahyre, Shine)
#   2. Telegram Bot       (approve jobs from phone)
#   3. Cold Email Sender  (auto-email HR after kit generation)
#   4. Interview Simulator (real-time mock interview AI)
#   5. Scheduled Daily Run (APScheduler every morning)
# ============================================================

import json
import logging
import os
import re
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from models import db, InterviewSession, ColdEmailLog, AutopilotConfig, JobFeedItem

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
# 2. COLD EMAIL SENDER
# ============================================================

def find_hr_email(company_name: str, job_url: str = "") -> str | None:
    """Look for a real HR/careers email actually published on the job page.
    Returns None if nothing real is found — we never guess an address anymore.
    A guessed address (e.g. hr@company.com) is more likely to bounce or reach
    the wrong inbox than to help, so it's not worth the risk."""
    if job_url:
        try:
            resp = requests.get(job_url, headers=HEADERS, timeout=10)
            emails = re.findall(
                r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
                resp.text
            )
            for email in emails:
                if not any(x in email.lower() for x in ["noreply", "no-reply", "support", "info@"]):
                    return email
        except Exception:
            pass
    return None


def build_cold_email_body(candidate_name: str, candidate_email: str, company: str, role: str, cover_letter_snippet: str = "") -> str:
    snippet = cover_letter_snippet[:400] if cover_letter_snippet else (
        f"I'm deeply interested in the {role} role at {company}. "
        f"I believe my background in Python, AI, and web development makes me a strong fit."
    )
    return f"""Hi there,

I came across the {role} opening at {company} and wanted to reach out directly.

{snippet}

I'd love the opportunity to discuss how I can contribute to your team. I've attached my resume and would be happy to share more details at your convenience.

Looking forward to hearing from you.

Best regards,
{candidate_name}
{candidate_email}
"""


def send_cold_email(
    hr_email: str,
    candidate_name: str,
    candidate_email: str,
    company: str,
    role: str,
    cover_letter_snippet: str = "",
) -> tuple[bool, str]:
    """Actually sends via SMTP. Only ever call this with a real, known-good
    hr_email — either found on the job page or explicitly supplied by the
    user. Never call this with a guessed address."""
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "")

    if not (smtp_host and smtp_user and smtp_password):
        return False, "SMTP not configured. Add SMTP_HOST, SMTP_USER, SMTP_PASSWORD to .env"

    body = build_cold_email_body(candidate_name, candidate_email, company, role, cover_letter_snippet)

    try:
        msg = EmailMessage()
        msg["Subject"] = f"Application: {role} — {candidate_name}"
        msg["From"] = f"{candidate_name} <{smtp_from}>"
        msg["To"] = hr_email
        msg["Reply-To"] = candidate_email
        msg.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        db.session.add(ColdEmailLog(
            to_email=hr_email,
            company=company,
            role=role,
            candidate=candidate_name,
            sent_at=datetime.now().isoformat(timespec="seconds"),
            status="sent",
        ))
        db.session.commit()

        log.info(f"Cold email sent to {hr_email} for {role} at {company}")
        return True, f"Cold email sent to {hr_email}"

    except Exception as e:
        log.error(f"Cold email failed: {e}")
        return False, str(e)


def get_cold_email_log() -> list[dict]:
    rows = ColdEmailLog.query.order_by(ColdEmailLog.id.desc()).all()
    return [r.to_dict() for r in rows]


# ============================================================
# 3. INTERVIEW SIMULATOR — FIXED: No more *laughs* or stage directions
# ============================================================

NO_STAGE_DIRECTIONS = (
    "CRITICAL: Do NOT use action markers like *laughs*, *smiles*, *nods*, (pauses), (chuckles), "
    "[leans forward], or ANY stage directions. Express your personality, warmth, and humor purely "
    "through your WORDS. Never write text meant to describe actions rather than speech."
)

INTERVIEW_ROUNDS = {
    "technical": {
        "name": "Technical Round",
        "system": (
            "You are a sharp but chill senior developer interviewing a candidate. "
            "Talk like a friendly senior who genuinely wants the candidate to succeed, but doesn't let weak answers slide. "
            "Be conversational. React naturally to their answers, ask follow-ups based on what they said, and use light humor through words only. "
            "But when they give a vague or wrong answer, push back naturally: 'Hmm, can you be more specific?' or 'That's a start, but what about...?' "
            "Ask ONE focused technical question at a time based on the JD and their background. "
            "After each answer respond naturally first, THEN give your score. "
            "Format: [Natural conversational response]. SCORE: X/10 | FEEDBACK: [brief] | NEXT QUESTION: [question] "
            f"{NO_STAGE_DIRECTIONS}"
        ),
        "opener": "Hey! Let's get into it. I'll ask you some technical questions based on the role. Keep it real, don't overthink. Ready when you are!",
    },
    "behavioral": {
        "name": "Behavioral Round",
        "system": (
            "You are a chill but perceptive HR interviewer. Talk naturally like a person, not a robot. "
            "React to what they say. If something is interesting, say so. If an answer is too generic, push back warmly. "
            "Use STAR format guidance naturally in conversation. Ask ONE behavioral question at a time. "
            "After each answer: [Natural reaction + brief encouragement or gentle pushback]. SCORE: X/10 | FEEDBACK: [brief] | NEXT QUESTION: [question] "
            f"{NO_STAGE_DIRECTIONS}"
        ),
        "opener": "Hey, good to meet you! This is a pretty casual chat, just be yourself. I'll ask you some situational questions and we'll go from there. Sound good?",
    },
    "hr": {
        "name": "HR Round",
        "system": (
            "You are a warm, friendly HR person who's also sharp and reads between the lines. "
            "Make the candidate feel comfortable but ask the important stuff: salary expectations, motivation, culture fit, career goals. "
            "Respond like a real person having a genuine conversation. If they say something interesting, follow up on it naturally. "
            "After each answer: [Warm natural response]. SCORE: X/10 | FEEDBACK: [brief] | NEXT QUESTION: [question] "
            f"{NO_STAGE_DIRECTIONS}"
        ),
        "opener": "Hi! Super glad you made it. This is just a chill conversation, no trick questions, I promise. Let's just get to know each other a bit. Tell me, what made you interested in this role?",
    },
    "rapid": {
        "name": "Rapid Fire Round",
        "system": (
            "You are running a rapid-fire interview round. Keep it snappy and fun. "
            "React briefly to each answer: 'Nice!', 'Hmm interesting take', 'I'd push back on that a little', etc. "
            "Ask very short direct questions, expect 1-2 sentence answers. Keep the energy high. "
            "Format: [1-line reaction]. SCORE: X/10 | NEXT: [next question] "
            f"{NO_STAGE_DIRECTIONS}"
        ),
        "opener": "Alright, rapid fire! Short questions, short answers. Let's see how fast you think. No overthinking. Go!",
    },
}


def create_interview_session(
    user_id: int,
    round_type: str,
    jd: str,
    resume: str,
    role: str,
    company: str,
) -> dict:
    session_id = f"iv-{user_id}-{int(time.time())}"
    config = INTERVIEW_ROUNDS.get(round_type, INTERVIEW_ROUNDS["technical"])

    row = InterviewSession(
        session_id=session_id,
        user_id=user_id,
        round_type=round_type,
        round_name=config["name"],
        role=role,
        company=company,
        jd=jd[:2000],
        resume=resume[:2000],
        history_json="[]",
        scores_json="[]",
        total_score=0,
        question_count=0,
        status="active",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.session.add(row)
    db.session.commit()
    log.info(f"Interview session created: {session_id}")
    return row.to_dict()


def get_interview_session(session_id: str) -> dict | None:
    row = InterviewSession.query.filter_by(session_id=session_id).first()
    return row.to_dict() if row else None


def answer_interview_question(
    session_id: str,
    user_answer: str,
    groq_api_key: str,
    groq_model: str = "openai/gpt-oss-120b",
) -> dict:
    row = InterviewSession.query.filter_by(session_id=session_id).first()
    if not row:
        return {"error": "Session not found"}
    session = row.to_dict()

    config = INTERVIEW_ROUNDS.get(session["round_type"], INTERVIEW_ROUNDS["technical"])

    system_prompt = (
        f"{config['system']}\n\n"
        f"Role: {session['role']} at {session['company']}\n"
        f"JD Summary: {session['jd'][:800]}\n"
        f"Candidate Background: {session['resume'][:800]}"
    )

    messages = [{"role": "system", "content": system_prompt}]

    for msg in session["history"]:
        messages.append(msg)

    if not session["history"]:
        messages.append({"role": "assistant", "content": config["opener"]})

    messages.append({"role": "user", "content": user_answer})

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": groq_model,
                "messages": messages,
                "temperature": 0.6,
                "max_tokens": 600,
            },
            timeout=20,
        )
        resp.raise_for_status()
        ai_reply = resp.json()["choices"][0]["message"]["content"]

        score_match = re.search(r"SCORE:\s*(\d+)\s*/\s*10", ai_reply, re.IGNORECASE)
        score = int(score_match.group(1)) if score_match else 7

        session["history"].append({"role": "user", "content": user_answer})
        session["history"].append({"role": "assistant", "content": ai_reply})
        session["scores"].append(score)
        session["question_count"] += 1
        session["total_score"] = round(sum(session["scores"]) / len(session["scores"]), 1)

        if session["question_count"] >= 5:
            session["status"] = "completed"

        row.history_json = json.dumps(session["history"])
        row.scores_json = json.dumps(session["scores"])
        row.question_count = session["question_count"]
        row.total_score = session["total_score"]
        row.status = session["status"]
        db.session.commit()

        return {
            "ai_reply": ai_reply,
            "score": score,
            "total_score": session["total_score"],
            "question_count": session["question_count"],
            "status": session["status"],
            "session_id": session_id,
        }

    except Exception as e:
        log.error(f"Interview AI error: {e}")
        return {"error": str(e)}


def get_interview_summary(session_id: str) -> dict:
    session = get_interview_session(session_id)
    if not session:
        return {"error": "Session not found"}

    scores = session.get("scores", [])
    avg = round(sum(scores) / len(scores), 1) if scores else 0

    grade = "Excellent" if avg >= 8 else "Good" if avg >= 6 else "Needs Work" if avg >= 4 else "Practice More"

    return {
        "session_id": session_id,
        "round_name": session.get("round_name"),
        "role": session.get("role"),
        "company": session.get("company"),
        "total_questions": session.get("question_count", 0),
        "average_score": avg,
        "grade": grade,
        "scores": scores,
        "status": session.get("status"),
        "history": session.get("history", []),
    }


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
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ============================================================
# 4. TELEGRAM BOT
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def tg_send(text: str, chat_id: str = "") -> bool:
    token = TELEGRAM_TOKEN
    cid = chat_id or TELEGRAM_CHAT_ID
    if not token or not cid:
        log.warning("Telegram not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False


def tg_send_job_digest(jobs: list[dict], chat_id: str = "") -> bool:
    if not jobs:
        return tg_send("No top jobs today. Try updating your roles/locations.", chat_id)

    lines = ["*ZeroToHire Daily Digest*", f"{datetime.now().strftime('%d %b %Y')}", ""]
    for i, job in enumerate(jobs[:5], 1):
        score = job.get("score", 0)
        verdict = job.get("verdict", "Go")
        emoji = "✅" if verdict == "Go" else "🟡" if verdict == "Maybe" else "❌"
        lines.append(
            f"{emoji} *{i}. {job.get('title', 'Job')}*\n"
            f"   {job.get('company', 'Company')} | {job.get('location', 'Remote')}\n"
            f"   Match: {score}% | Source: {job.get('source', 'Web')}\n"
            f"   {job.get('source_url', '')}\n"
        )

    lines.append("_Reply with job number to approve_")
    return tg_send("\n".join(lines), chat_id)


def tg_notify_kit_ready(job_title: str, company: str, run_id: str, chat_id: str = "") -> bool:
    msg = (
        f"*Kit Ready!*\n\n"
        f"*Role:* {job_title}\n"
        f"*Company:* {company}\n"
        f"*Run ID:* `{run_id}`\n\n"
        f"Your resume, cover letter, and interview prep are ready."
    )
    return tg_send(msg, chat_id)


def tg_notify_cold_email_sent(hr_email: str, company: str, role: str, chat_id: str = "") -> bool:
    msg = (
        f"*Cold Email Sent!*\n\n"
        f"*Role:* {role}\n"
        f"*Company:* {company}\n"
        f"*To:* {hr_email}\n\n"
        f"Your application email has been delivered."
    )
    return tg_send(msg, chat_id)


def get_telegram_updates(offset: int = 0) -> list[dict]:
    if not TELEGRAM_TOKEN:
        return []
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return resp.json().get("result", [])
    except Exception:
        return []


# ============================================================
# 5. SCHEDULER (APScheduler)
# ============================================================

def start_scheduler(app_context_fn=None):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler not installed. Run: pip install apscheduler")
        log.warning("Scheduler will not run.")
        return None

    scheduler = BackgroundScheduler()

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

            JobFeedItem.query.filter_by(user_id=user_id).delete()
            now = datetime.now().isoformat(timespec="seconds")
            for job in jobs:
                db.session.add(JobFeedItem(
                    user_id=user_id,
                    source_url=job.get("source_url", ""),
                    title=job.get("title", ""),
                    text=job.get("text", ""),
                    origin="autopilot",
                    saved_at=now,
                    updated_at=now,
                ))
            db.session.commit()

            top_jobs = jobs[:5]
            chat_id = cfg.get("telegram_chat_id") or TELEGRAM_CHAT_ID
            if chat_id:
                tg_send_job_digest(top_jobs, chat_id)

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
    log.info("Scheduler started — daily job run at 8:00 AM")
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
            write_job_feed_fn({str(user_id): jobs})
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
    def automation_cold_email():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        pro_error, pro_status = _require_pro(user_id)
        if pro_error:
            return pro_error, pro_status

        data = request.get_json(silent=True) or {}
        company = _safe_str(data.get("company"), max_len=200)
        role = _safe_str(data.get("role"), max_len=200)
        job_url = _safe_str(data.get("job_url"), max_len=2048)
        candidate_name = _safe_str(data.get("candidate_name", "Candidate"), max_len=150, default="Candidate") or "Candidate"
        candidate_email = _safe_str(data.get("candidate_email"), max_len=255)
        cover_snippet = _safe_long_text(data.get("cover_snippet"), max_len=3000)
        hr_email_override = _safe_str(data.get("hr_email"), max_len=255)

        if not company or not role:
            return jsonify({"error": "company and role are required"}), 400
        if candidate_email and not _is_valid_email(candidate_email):
            return jsonify({"error": "candidate_email looks invalid"}), 400
        if hr_email_override and not _is_valid_email(hr_email_override):
            return jsonify({"error": "hr_email override looks invalid"}), 400

        hr_email = hr_email_override or find_hr_email(company, job_url)

        if not hr_email:
            # No real, verified email — hand back a draft instead of guessing
            # an address and sending blind. The user reviews and sends it
            # themselves from their own inbox.
            draft_body = build_cold_email_body(candidate_name, candidate_email, company, role, cover_snippet)
            return jsonify({
                "ok": False,
                "mode": "draft_only",
                "message": "No verified HR email found for this company. Here's a ready-to-send draft — copy it into your own email client.",
                "draft": {
                    "subject": f"Application: {role} — {candidate_name}",
                    "body": draft_body,
                },
            })

        ok, msg = send_cold_email(hr_email, candidate_name, candidate_email, company, role, cover_snippet)

        if ok:
            chat_id = read_autopilot_config_fn().get(str(user_id), {}).get("telegram_chat_id", "")
            tg_notify_cold_email_sent(hr_email, company, role, chat_id)

        return jsonify({"ok": ok, "mode": "sent", "message": msg, "hr_email": hr_email})

    @app.route("/automation/cold-email/log", methods=["GET"])
    def automation_cold_email_log():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify({"log": get_cold_email_log()})

    @app.route("/automation/interview/start", methods=["POST"])
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
        role = _safe_str(data.get("role", "Software Developer"), max_len=150, default="Software Developer") or "Software Developer"
        company = _safe_str(data.get("company", "Company"), max_len=200, default="Company") or "Company"

        if round_type not in INTERVIEW_ROUNDS:
            return jsonify({"error": f"round_type must be one of: {list(INTERVIEW_ROUNDS.keys())}"}), 400

        session_obj = create_interview_session(user_id, round_type, jd, resume, role, company)
        config = INTERVIEW_ROUNDS[round_type]

        return jsonify({
            "session_id": session_obj["session_id"],
            "round_name": config["name"],
            "opener": config["opener"],
            "rounds_available": list(INTERVIEW_ROUNDS.keys()),
        })

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
        user_answer = _safe_long_text(data.get("answer"), max_len=5000)

        if not session_id or not user_answer:
            return jsonify({"error": "session_id and answer are required"}), 400

        groq_key = os.getenv("GROQ_API_KEY", "")
        groq_model = os.getenv("GROQ_MODEL", "groq/openai/gpt-oss-120b").replace("groq/", "")

        result = answer_interview_question(session_id, user_answer, groq_key, groq_model)
        return jsonify(result)

    @app.route("/automation/interview/summary/<session_id>", methods=["GET"])
    def automation_interview_summary(session_id):
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify(get_interview_summary(session_id))

    @app.route("/automation/interview/sessions", methods=["GET"])
    def automation_interview_sessions():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        return jsonify({"sessions": list_user_interview_sessions(user_id)})

    @app.route("/automation/telegram/send", methods=["POST"])
    def automation_telegram_send():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        pro_error, pro_status = _require_pro(user_id)
        if pro_error:
            return pro_error, pro_status

        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        chat_id = data.get("chat_id", "")

        if not text:
            return jsonify({"error": "text is required"}), 400

        ok = tg_send(text, chat_id)
        return jsonify({"ok": ok})

    @app.route("/automation/telegram/digest", methods=["POST"])
    def automation_telegram_digest():
        user_id, error_resp, status = require_user_fn()
        if error_resp:
            return error_resp, status
        pro_error, pro_status = _require_pro(user_id)
        if pro_error:
            return pro_error, pro_status

        data = request.get_json(silent=True) or {}
        chat_id = data.get("chat_id", "")
        rows = JobFeedItem.query.filter_by(user_id=user_id).order_by(JobFeedItem.updated_at.desc()).limit(5).all()
        jobs = [r.to_dict() for r in rows]

        ok = tg_send_job_digest(jobs, chat_id)
        return jsonify({"ok": ok, "jobs_sent": len(jobs)})

    @app.route("/automation/status", methods=["GET"])
    def automation_status():
        return jsonify({
            "scraper": True,
            "cold_email": bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER")),
            "telegram": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
            "interview_simulator": bool(os.getenv("GROQ_API_KEY")),
            "scheduler": True,
            "email_log_count": len(get_cold_email_log()),
        })

    log.info("Automation routes registered")