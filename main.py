from datetime import datetime, timedelta
import html
import ipaddress
import secrets
import socket
from pathlib import Path
import json
import os
import random
import re
import smtplib
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, urljoin, urlparse

from crewai import Agent, Crew, LLM, Task
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, request, send_file, session, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from email.message import EmailMessage
from automation import register_automation_routes, start_scheduler
from models import (
    db,
    User,
    OtpCode,
    Profile as ProfileModel,
    Run,
    JobFeedItem,
    AutopilotQueueItem,
    AutopilotConfig,
    Payment,
)

# ── PDF support (optional but recommended) ──────────────
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False 

try:
    import requests
    from bs4 import BeautifulSoup

    URL_FETCH_SUPPORT = True
except ImportError:
    URL_FETCH_SUPPORT = False

try:
    from docx import Document

    DOCX_SUPPORT = True
except ImportError:
    DOCX_SUPPORT = False

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    PDF_EXPORT_SUPPORT = True
except ImportError:
    PDF_EXPORT_SUPPORT = False

try:
    import razorpay

    RAZORPAY_SUPPORT = True
except ImportError:
    RAZORPAY_SUPPORT = False

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
PROFILE_DIR = BASE_DIR / "profiles"
LOCAL_DATA_DIR = BASE_DIR / ".data"
LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
PROFILE_DIR.mkdir(exist_ok=True)

DB_PATH = LOCAL_DATA_DIR / "app.db"


def resolve_database_uri():
    """Prefer a real persistent database (Postgres) in production so
    user/run data survives redeploys. Falls back to local SQLite only
    when DATABASE_URL isn't set — i.e. local dev on your machine.
    Render/Railway/Heroku-style platforms inject DATABASE_URL automatically
    once you attach a Postgres instance; some providers still hand out
    the old `postgres://` scheme, which SQLAlchemy's psycopg2 driver
    rejects, so it's normalized to `postgresql://` here.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DB_PATH}"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
db.init_app(app)


def ensure_schema_up_to_date():
    """Lightweight, dependency-free 'migration' step.

    The project doesn't use Alembic/Flask-Migrate, and `db.create_all()`
    ONLY creates tables that don't exist yet — it silently does nothing to
    a table that already exists, even if the model gained new columns
    since that table was created. That's exactly what caused the
    `no such column: users.plan` 500 error: the Phase 3 billing update
    added `plan` / `plan_expires_at` to the User model, but any local or
    deployed DB created before that change still has the old `users`
    table shape, so every query through it (login, /auth/me, the Google
    OAuth callback) blew up.

    This walks every table SQLAlchemy knows about, diffs its real columns
    against the model's expected columns, and ALTERs in whatever's
    missing. Cheap, safe (additive only, never drops/renames), and works
    the same way on SQLite (local dev) and Postgres (Render).
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table — db.create_all() already handles it
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            col_type = col.type.compile(dialect=db.engine.dialect)
            ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}'
            with db.engine.begin() as conn:
                conn.execute(text(ddl))
            print(f"[migration] {table.name}: added missing column '{col.name}'")


with app.app_context():
    db.create_all()
    ensure_schema_up_to_date()
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.getenv("SESSION_SECRET", "change-me-in-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
app.config["SESSION_COOKIE_NAME"] = "autoapply_session"
app.config["PERMANENT_SESSION_LIFETIME"] = 86400
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

allowed_origin = os.getenv("FRONTEND_ORIGIN", "*")
CORS(app, resources={r"/*": {"origins": allowed_origin}}, supports_credentials=True)


# ── RATE LIMITING ─────────────────────────────────────
# Keyed by logged-in user first (so one account can't be defeated by
# rotating IPs) and falls back to the client IP for pre-auth requests.
# In-memory storage is fine for a single-process/single-dyno deploy;
# swap storage_uri for Redis if this ever runs with >1 worker.
def rate_limit_key():
    user_id = session.get("user_id")
    return f"user:{user_id}" if user_id else f"ip:{get_remote_address()}"


# `memory://` storage keeps counters inside a single process. That's fine
# with 1 worker, but this app runs gunicorn with multiple workers/threads —
# each worker would keep its own separate counter, so a "5 per minute"
# limit silently becomes "5 per minute per worker". REDIS_URL (Render/
# Upstash/Railway all offer a free tier) gives one shared counter across
# every worker, which is what the limit numbers actually assume.
limiter_storage_uri = os.getenv("REDIS_URL", "").strip() or "memory://"

limiter = Limiter(
    app=app,
    key_func=rate_limit_key,
    default_limits=["200 per hour", "3000 per day"],
    storage_uri=limiter_storage_uri,
)


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({
        "error": "Too many requests. Slow down a bit and try again shortly.",
        "retry_after_seconds": getattr(e, "retry_after", None),
    }), 429


def wants(field, default=True):
    value = request.form.get(field)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


# ── DB-backed helpers ────────────────────────────────────────
# These keep the exact same call signature + return shape as the
# old JSON-file versions (dict keyed by email / user_id, etc.) so
# that every route below them didn't need to change. The actual
# storage underneath is now SQLite via SQLAlchemy — atomic,
# concurrency-safe writes instead of whole-file read-modify-write.

def read_users():
    return {u.email: u.to_dict() for u in User.query.all()}


def write_users(data):
    """Upserts every email in `data` into the users table. Callers
    build the full dict from read_users(), mutate it, then call this."""
    for email, fields in data.items():
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(email=email)
            db.session.add(user)
        user.name = fields.get("name", user.name or "")
        user.phone = fields.get("phone", user.phone or "")
        user.role = fields.get("role", user.role or "candidate")
        if fields.get("password_hash"):
            user.password_hash = fields["password_hash"]
        if fields.get("google_id"):
            user.google_id = fields["google_id"]
        if fields.get("id"):
            user.id = fields["id"]
        if fields.get("created_at"):
            user.created_at = fields["created_at"]
    db.session.commit()


def next_user_id(users):
    # Kept for compatibility with callers that pre-compute an id before
    # inserting; SQLite auto-increments the real primary key on commit,
    # so this is just a best-effort hint used before the row exists.
    if not users:
        return 1
    return max(int(user.get("id", 0)) for user in users.values()) + 1


def read_otps():
    return {o.key: o.to_dict() for o in OtpCode.query.all()}


def write_otps(data):
    for key, fields in data.items():
        otp = OtpCode.query.filter_by(key=key).first()
        if not otp:
            otp = OtpCode(key=key)
            db.session.add(otp)
        otp.code = fields.get("code", otp.code)
        otp.channel = fields.get("channel", otp.channel)
        otp.target = fields.get("target", otp.target)
        otp.name = fields.get("name", otp.name)
        otp.attempts = fields.get("attempts", otp.attempts or 0)
        otp.expires_at = fields.get("expires_at", otp.expires_at)
        otp.created_at = fields.get("created_at", otp.created_at or datetime.now().isoformat(timespec="seconds"))
    # Any key not present in `data` anymore means it was deleted by the caller
    existing_keys = set(data.keys())
    for otp in OtpCode.query.all():
        if otp.key not in existing_keys:
            db.session.delete(otp)
    db.session.commit()


def read_job_feed():
    store = {}
    for item in JobFeedItem.query.order_by(JobFeedItem.updated_at.desc()).all():
        store.setdefault(str(item.user_id), []).append(item.to_dict())
    return store


def write_job_feed(data):
    for user_id, jobs in data.items():
        JobFeedItem.query.filter_by(user_id=int(user_id)).delete()
        for job in jobs:
            db.session.add(JobFeedItem(
                user_id=int(user_id),
                source_url=job.get("source_url", ""),
                title=job.get("title", ""),
                text=job.get("text", ""),
                origin=job.get("origin", "manual"),
                saved_at=job.get("saved_at") or datetime.now().isoformat(timespec="seconds"),
                updated_at=job.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            ))
    db.session.commit()


def read_auto_queue():
    store = {}
    for item in AutopilotQueueItem.query.order_by(AutopilotQueueItem.position.asc()).all():
        store.setdefault(str(item.user_id), []).append(item.to_dict())
    return store


def write_auto_queue(data):
    for user_id, items in data.items():
        AutopilotQueueItem.query.filter_by(user_id=int(user_id)).delete()
        for item in items:
            db.session.add(AutopilotQueueItem(
                user_id=int(user_id),
                item_id=item.get("id", ""),
                position=item.get("position", 0),
                title=item.get("title", ""),
                source_url=item.get("source_url", ""),
                score=item.get("score", 0),
                status=item.get("status", "Queued"),
                updated_at=item.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            ))
    db.session.commit()


def read_autopilot_config():
    return {str(c.user_id): c.to_dict() for c in AutopilotConfig.query.all()}


def write_autopilot_config(data):
    for user_id, cfg in data.items():
        row = db.session.get(AutopilotConfig, int(user_id))
        if not row:
            row = AutopilotConfig(user_id=int(user_id))
            db.session.add(row)
        row.roles_json = json.dumps(cfg.get("roles", []))
        row.locations_json = json.dumps(cfg.get("locations", []))
        row.salary_min = cfg.get("salary_min", 0)
        row.experience_level = cfg.get("experience_level", "entry")
        row.daily_job_limit = cfg.get("daily_job_limit", 20)
        row.daily_top_kits = cfg.get("daily_top_kits", 5)
        row.digest_channel = cfg.get("digest_channel", "email")
        if cfg.get("telegram_chat_id") is not None:
            row.telegram_chat_id = cfg.get("telegram_chat_id", "")
        row.enabled = bool(cfg.get("enabled", True))
        row.updated_at = cfg.get("updated_at") or datetime.now().isoformat(timespec="seconds")
    db.session.commit()


def normalize_email(value):
    email = clean_text(value).lower()
    if not email or not EMAIL_RE.match(email):
        return None
    return email


def generate_otp_code():
    return f"{random.randint(100000, 999999)}"


def otp_target_key(channel, target):
    return f"{channel}:{target}"


def upsert_user_with_contact(name, channel, target):
    users = read_users()
    user_key = target if channel == 'email' else f"phone:{target}"
    existing = users.get(user_key)
    if existing:
        if name and not existing.get('name'):
            existing['name'] = name
            users[user_key] = existing
            write_users(users)
        return existing

    user_id = next_user_id(users)
    user = {
        "id": user_id,
        "name": name or "Candidate",
        "email": target if channel == 'email' else "",
        "phone": target if channel == 'phone' else "",
        "role": infer_user_role(target if channel == 'email' else ""),
        "password_hash": "",
        "created_at": datetime.now().isoformat(timespec='seconds'),
    }
    users[user_key] = user
    write_users(users)
    return user


def send_email_otp(target_email, otp_code):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM", smtp_user or "")

    if not (smtp_host and smtp_user and smtp_password and smtp_from):
        return False, "Email provider not configured. Set SMTP_HOST/SMTP_USER/SMTP_PASSWORD/SMTP_FROM."

    msg = EmailMessage()
    msg["Subject"] = "Your ZeroToHire OTP"
    msg["From"] = smtp_from
    msg["To"] = target_email
    msg.set_content(
        f"Your ZeroToHire OTP is {otp_code}. It expires in 5 minutes.\n"
        "If you did not request this, you can ignore this email."
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    return True, "OTP sent to email"


def current_user_id():
    return session.get("user_id")


def current_user():
    user_id = current_user_id()
    if not user_id:
        return None
    users = read_users()
    for user in users.values():
        if user.get("id") == user_id:
            return {
                "id": user.get("id"),
                "name": user.get("name"),
                "email": user.get("email"),
                "phone": user.get("phone", ""),
                "role": user.get("role", "candidate"),
                "created_at": user.get("created_at"),
                "plan": user.get("plan", "free"),
                "plan_expires_at": user.get("plan_expires_at", ""),
            }
    return None


# ── SUBSCRIPTION / BILLING (Phase 3) ────────────────────
FREE_MONTHLY_RUN_LIMIT = 3


def is_pro_user(user_id):
    """A user is Pro only while `plan == 'pro'` AND their period hasn't
    lapsed — checked fresh on every gated request instead of trusting a
    cached flag, so a missed renewal correctly falls back to Free rather
    than silently staying Pro forever."""
    row = db.session.get(User, user_id)
    if not row or row.plan != "pro":
        return False
    if not row.plan_expires_at:
        return False
    try:
        return datetime.fromisoformat(row.plan_expires_at) > datetime.now()
    except ValueError:
        return False


def require_pro():
    """Same shape as require_user() so routes can reuse the same
    `if error_resp: return error_resp, status` pattern."""
    user_id = current_user_id()
    if not user_id:
        return None, jsonify({"error": "Authentication required"}), 401
    if not is_pro_user(user_id):
        return None, jsonify({
            "error": "This feature is part of ZeroToHire Pro.",
            "upgrade_required": True,
        }), 402
    return user_id, None, None


def free_kits_used_this_month(user_id):
    start_of_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return Run.query.filter(
        Run.user_id == user_id,
        Run.has_kit.is_(True),
        Run.created_at >= start_of_month.isoformat(timespec="seconds"),
    ).count()


def check_run_quota(user_id):
    """Called right before /run actually spends Groq tokens. Pro users are
    unlimited; Free users get FREE_MONTHLY_RUN_LIMIT kits per calendar
    month. Returns an error response tuple, or (None, None) if the run
    is allowed to proceed."""
    if is_pro_user(user_id):
        return None, None
    used = free_kits_used_this_month(user_id)
    if used >= FREE_MONTHLY_RUN_LIMIT:
        return jsonify({
            "error": f"Free plan includes {FREE_MONTHLY_RUN_LIMIT} kits per month. "
                     f"You've used all {used}. Upgrade to Pro for unlimited kits.",
            "upgrade_required": True,
            "kits_used": used,
            "kits_limit": FREE_MONTHLY_RUN_LIMIT,
        }), 402
    return None, None


def infer_user_role(email):
    if not email:
        return "candidate"
    admins = {item.strip().lower() for item in os.getenv("ADMIN_EMAILS", "").split(",") if item.strip()}
    recruiters = {item.strip().lower() for item in os.getenv("RECRUITER_EMAILS", "").split(",") if item.strip()}
    email_lower = email.lower()
    if email_lower in admins:
        return "admin"
    if email_lower in recruiters:
        return "recruiter"
    return "candidate"


def oauth_redirect_uri():
    configured = clean_text(os.getenv("GOOGLE_REDIRECT_URI"))
    if configured:
        return configured
    return request.url_root.rstrip("/") + "/auth/google/callback"


def require_user():
    user_id = current_user_id()
    if not user_id:
        return None, jsonify({"error": "Authentication required"}), 401
    return user_id, None, None




def read_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return fallback


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def make_run_id(company):
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    safe_company = secure_filename(company or 'company') or 'company'
    return f'{stamp}-{safe_company}'


def clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


# ── INPUT VALIDATION HELPERS ────────────────────────────
# Every field pulled out of request.get_json()/request.form goes through
# one of these instead of a bare `.strip()`, so a caller sending the wrong
# JSON type (int, list, dict) can't crash a route with an AttributeError,
# and nobody can paste a 500KB blob into a field that was only ever meant
# to hold a short string.
def safe_str(value, max_len=2000, default=''):
    """Coerce to string, collapse whitespace, and hard-cap length."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return default
    text = re.sub(r'\s+', ' ', str(value)).strip()
    return text[:max_len]


def safe_long_text(value, max_len=20000, default=''):
    """Like safe_str but preserves newlines — for JD/resume/answer bodies."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return default
    text = str(value).strip()
    return text[:max_len]


EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def is_valid_email(value):
    return bool(EMAIL_RE.match(clean_text(value)))


def safe_int(value, default, min_val=None, max_val=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if min_val is not None:
        n = max(n, min_val)
    if max_val is not None:
        n = min(n, max_val)
    return n


def safe_url(value, max_len=2048):
    """Returns the URL string if it's http(s) and within length, else ''."""
    url = safe_str(value, max_len=max_len)
    if not url:
        return ''
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return ''
    return url


def read_tracker():
    # Tracker used to be its own JSON file duplicating company/role/status/
    # notes per run_id. That's now just a view over the `runs` table, so a
    # Kanban update and a generated-kit's metadata always agree on status.
    return {r.run_id: r.to_tracker_dict() for r in Run.query.all()}


def write_tracker(data):
    for run_id, item in data.items():
        run = Run.query.filter_by(run_id=run_id).first()
        if not run:
            run = Run(run_id=run_id, has_kit=False)
            db.session.add(run)
        run.user_id = item.get("user_id", run.user_id)
        run.company = item.get("company", run.company or "")
        run.role = item.get("role", run.role or "")
        run.status = item.get("status", run.status or "Generated")
        run.notes = item.get("notes", run.notes or "")
        run.updated_at = item.get("updated_at") or datetime.now().isoformat(timespec="seconds")
    db.session.commit()


def get_company_intel(jd_analysis):
    text = str(jd_analysis)

    def section(pattern):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return []
        rows = []
        for line in match.group(1).splitlines():
            clean = re.sub(r'^[\s\-*•\d.)]+', '', line).strip()
            if clean:
                rows.append(clean)
        return rows[:8]

    role_summary = section(r'Role Summary\s*:?\s*([\s\S]*?)(?:\n\s*\d+\.|\n\s*Must-Have|\Z)')
    must_have = section(r'Must-Have Skills\s*:?\s*([\s\S]*?)(?:\n\s*\d+\.|\n\s*Nice-to-Have|\Z)')
    culture = section(r'(?:Vibe Signals|Culture Signals)\s*:?\s*([\s\S]*?)(?:\n\s*\d+\.|\n\s*What The Resume Must Prove|\Z)')
    proof = section(r'What The Resume Must Prove\s*:?\s*([\s\S]*?)(?:\n\s*\d+\.|\Z)')

    return {
        'role_summary': role_summary[:3],
        'must_have': must_have[:6],
        'culture_signals': culture[:4],
        'proof_points': proof[:5],
    }

# ── LLM ────────────────────────────────────────────────
def get_llm(temperature=0.35):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to .env and restart Flask.")

    return LLM(
        model=os.getenv("GROQ_MODEL", "groq/openai/gpt-oss-120b"),
        api_key=api_key,
        temperature=temperature,
    )

# ── AGENTS ─────────────────────────────────────────────
def build_agents(llm):
    truth_rule = (
        "Never invent experience, employers, degrees, certifications, tools, numbers, "
        "or achievements. If evidence is missing, say so and improve only by honest reframing."
    )

    jd_reader = Agent(
        role="Job Description Analyst",
        goal="Extract the role requirements, hiring signals, and ATS keywords from a job description.",
        backstory=f"Senior HR analyst and ATS keyword specialist. {truth_rule}",
        llm=llm, verbose=False
    )

    resume_analyser = Agent(
        role="Resume Analyser",
        goal="Score resume fit against the job description and identify precise gaps.",
        backstory=f"Technical recruiter who gives direct, evidence-backed fit analysis. {truth_rule}",
        llm=llm, verbose=False
    )

    resume_rewriter = Agent(
        role="Professional Resume Rewriter",
        goal="Rewrite the resume for the target role using honest, ATS-friendly positioning.",
        backstory=f"Resume strategist for early-career tech candidates. {truth_rule}",
        llm=llm, verbose=False
    )

    cover_letter_writer = Agent(
        role="Expert Cover Letter Writer",
        goal="Write a concise, specific cover letter that sounds human and confident.",
        backstory=f"Cover letter specialist who avoids generic filler. {truth_rule}",
        llm=llm, verbose=False
    )

    interview_coach = Agent(
        role="Technical Interview Coach",
        goal="Prepare role-specific interview questions and strong answers from the candidate's actual background.",
        backstory=f"Senior interview coach for junior and internship tech candidates. {truth_rule}",
        llm=llm, verbose=False
    )

    return {
        "jd": jd_reader,
        "analysis": resume_analyser,
        "resume": resume_rewriter,
        "cover": cover_letter_writer,
        "interview": interview_coach,
    }


# ── TASKS ──────────────────────────────────────────────
def build_jd_task(agent, jd):
    return Task(
        description=f"""Analyze this job description with hiring precision.

Return these sections:
1. Role Summary: 2-3 lines
2. Must-Have Skills
3. Nice-to-Have Skills
4. ATS Keywords: at least 15 keywords and phrases
5. Responsibility Signals
6. Company / Team Vibe Signals
7. What The Resume Must Prove

Job Description:
{jd}""",
        expected_output="Structured job description analysis with ATS keywords and proof points.",
        agent=agent
    )


def build_analysis_task(agent, jd_analysis, resume):
    return Task(
        description=f"""Compare the resume against the job analysis below.

Return a short JSON block first, then a readable analysis.
JSON schema:
{{
  "match_score": 0-10,
  "ats_keywords_found": 0-50,
  "strongest_matches": ["..."],
  "biggest_gaps": ["..."],
  "top_recommendations": ["..."]
}}

After the JSON, include:
1. Matching skills and evidence
2. Missing skills / gaps
3. ATS keyword opportunities
4. Specific resume improvements

Job Analysis:
{jd_analysis}

Resume:
{resume}""",
        expected_output="JSON score block followed by practical fit analysis.",
        agent=agent
    )


def build_resume_task(agent, name, jd_analysis, resume_analysis, resume):
    return Task(
        description=f"""Rewrite the resume for {name}.

Use the job analysis and resume analysis below. Keep the resume honest.
Do:
1. Preserve real projects, education, skills, and timeline.
2. Add ATS keywords only where supported by the existing background.
3. Strengthen bullet points with clearer impact.
4. Use action verbs and concise bullets.
5. Include a tight summary, skills, projects, education, and certifications.

Do not fabricate metrics. If a metric is unknown, write the bullet without a fake number.

Job Analysis:
{jd_analysis}

Resume Analysis:
{resume_analysis}

Original Resume:
{resume}""",
        expected_output="A complete rewritten resume ready to copy-paste.",
        agent=agent
    )


def build_cover_task(agent, name, company, jd_analysis, resume_analysis, resume):
    return Task(
        description=f"""Write a cover letter for {name} applying to {company}.

Rules:
1. Under 300 words.
2. Open with a specific hook, not 'I am applying for'.
3. Connect actual projects and skills to the role.
4. Address one gap with confident learning momentum if useful.
5. Sound direct, warm, and human.
6. End with a clear call to action.

Job Analysis:
{jd_analysis}

Resume Analysis:
{resume_analysis}

Resume:
{resume}""",
        expected_output="A polished cover letter under 300 words.",
        agent=agent
    )


def build_interview_task(agent, name, company, jd, jd_analysis, resume_analysis, resume):
    return Task(
        description=f"""Build an interview prep kit for {name} applying to {company}.

Include:
1. 5 technical questions with strong answers.
2. 3 behavioral questions with STAR-style answers.
3. 2 tricky questions about gaps or weaknesses with honest answers.
4. 3 smart questions to ask the interviewer.
5. A 60-second self-introduction tailored to the role.

Every answer should reference the candidate's real background where possible.

Job Description:
{jd}

Job Analysis:
{jd_analysis}

Resume Analysis:
{resume_analysis}

Resume:
{resume}""",
        expected_output="A complete interview prep kit with tailored answers.",
        agent=agent
    )


def kickoff_single(agent, task):
    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    return str(crew.kickoff())


# ── PARSE SCORES FROM AGENT OUTPUT ─────────────────────
def extract_json(text):
    match = re.search(r"\{[\s\S]*?\}", str(text))
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def parse_scores(analyser_output):
    text = str(analyser_output)
    data = extract_json(text)

    # ── match_score ───────────────────────────────────────────
    match_score = data.get("match_score") or data.get("score") or data.get("overall_score")
    if match_score is None:
        # Try every possible pattern in the raw text
        for pat in [
            r'"match_score"\s*:\s*(\d+(?:\.\d+)?)',
            r'match.?score[^\d]*(\d+(?:\.\d+)?)\s*/\s*10',
            r'(\d+(?:\.\d+)?)\s*/\s*10',
            r'score[^\d]*(\d+(?:\.\d+)?)',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                match_score = float(m.group(1))
                break
        else:
            match_score = 7  # safe fallback

    try:
        match_score = max(0, min(10, round(float(str(match_score)))))
    except Exception:
        match_score = 7

    # ── kw_count ──────────────────────────────────────────────
    kw_count = data.get("ats_keywords_found") or data.get("keywords_found") or data.get("keyword_count")
    if kw_count is None:
        for pat in [
            r'"ats_keywords_found"\s*:\s*(\d+)',
            r'ATS Keywords Found[:\s]+(\d+)',
            r'keywords?\s+found[:\s]+(\d+)',
            r'(\d+)\s+keywords?',
        ]:
            k = re.search(pat, text, re.IGNORECASE)
            if k:
                kw_count = int(k.group(1))
                break
        else:
            kw_count = 12

    try:
        kw_count = max(0, int(kw_count))
    except Exception:
        kw_count = 12

    return match_score, kw_count


def extract_keywords(jd_analysis):
    text = str(jd_analysis)
    keywords = []

    section = re.search(
        r'ATS Keywords\s*:?\s*([\s\S]*?)(?:\n\s*\d+\.|\n\s*[A-Z][A-Za-z /]+:|\Z)',
        text,
        re.IGNORECASE,
    )
    source = section.group(1) if section else text

    for line in source.splitlines():
        line = re.sub(r'^[\s\-*•\d.)]+', '', line).strip()
        if not line:
            continue
        parts = [part.strip(" .;:") for part in re.split(r',|;', line)]
        keywords.extend(part for part in parts if 2 <= len(part) <= 40)

    seen = set()
    unique = []
    for keyword in keywords:
        key = keyword.lower()
        if key not in seen and len(unique) < 30:
            seen.add(key)
            unique.append(keyword)

    if unique:
        return unique

    fallback = re.findall(r'\b[A-Za-z][A-Za-z+#. -]{2,24}\b', text)
    stop = {'the', 'and', 'for', 'with', 'from', 'this', 'that', 'role', 'skills'}
    for item in fallback:
        key = clean_text(item).lower()
        if key and key not in stop and key not in seen and len(unique) < 20:
            seen.add(key)
            unique.append(clean_text(item))

    return unique


def build_heatmap(jd_analysis, resume_text):
    resume_lower = resume_text.lower()
    items = []
    for keyword in extract_keywords(jd_analysis):
        found = keyword.lower() in resume_lower
        items.append({
            'keyword': keyword,
            'found': found,
            'status': 'found' if found else 'missing',
        })
    return items


def tokenize_words(text):
    return re.findall(r'[a-zA-Z][a-zA-Z0-9+#.-]{1,30}', str(text or '').lower())


def top_keywords(text, limit=40):
    tokens = tokenize_words(text)
    stop = {
        'the', 'and', 'for', 'with', 'from', 'that', 'this', 'you', 'your', 'are', 'will', 'our', 'job',
        'role', 'team', 'skills', 'experience', 'years', 'have', 'has', 'all', 'not', 'into', 'their'
    }
    freq = {}
    for token in tokens:
        if token in stop or len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    ranked = sorted(freq.items(), key=lambda row: row[1], reverse=True)
    return [word for word, _ in ranked[:limit]]


def render_docx(path, title, sections):
    if not DOCX_SUPPORT:
        return False

    doc = Document()
    doc.add_heading(title, level=1)
    for heading, body in sections:
        if not body:
            continue
        doc.add_heading(heading, level=2)
        for line in str(body).splitlines():
            line = line.strip()
            if line:
                doc.add_paragraph(line)
    doc.save(path)
    return True


def render_pdf(path, title, sections):
    if not PDF_EXPORT_SUPPORT:
        return False

    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles['Title']), Spacer(1, 12)]
    for heading, body in sections:
        if not body:
            continue
        story.append(Paragraph(heading, styles['Heading2']))
        for block in str(body).splitlines():
            block = block.strip()
            if block:
                story.append(Paragraph(html.escape(block), styles['BodyText']))
        story.append(Spacer(1, 10))
    SimpleDocTemplate(str(path), pagesize=letter).build(story)
    return True


def read_resume_from_request():
    resume_text = request.form.get('resume_text', '').strip()

    if 'resume' not in request.files or not request.files['resume'].filename:
        return resume_text

    uploaded = request.files['resume']
    filename = secure_filename(uploaded.filename).lower()

    if filename.endswith('.pdf'):
        if not PDF_SUPPORT:
            raise RuntimeError('PDF upload needs pdfplumber. Install it with: pip install pdfplumber')

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                tmp_path = tmp.name
                uploaded.save(tmp_path)
            with pdfplumber.open(tmp_path) as pdf:
                return '\n'.join(page.extract_text() or '' for page in pdf.pages).strip()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return uploaded.read().decode('utf-8', errors='ignore').strip()


def save_outputs(company, payload, metadata):
    run_id = metadata['run_id']
    run_dir = OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    file_map = {
        'jd_analysis': 'jd_analysis.txt',
        'resume_analysis': 'resume_analysis.txt',
        'rewritten_resume': 'rewritten_resume.txt',
        'cover_letter': 'cover_letter.txt',
        'interview_prep': 'interview_prep.txt',
    }

    saved = {}
    for key, filename in file_map.items():
        value = payload.get(key, '')
        if value:
            path = run_dir / filename
            path.write_text(value, encoding='utf-8')
            saved[key] = str(path)

    kit_sections = [
        ('Rewritten Resume', payload.get('rewritten_resume', '')),
        ('Cover Letter', payload.get('cover_letter', '')),
        ('Interview Prep', payload.get('interview_prep', '')),
        ('ATS Report', (payload.get('jd_analysis', '') + '\n\n' + payload.get('resume_analysis', '')).strip()),
    ]
    resume_sections = [('Rewritten Resume', payload.get('rewritten_resume', ''))]

    if payload.get('rewritten_resume'):
        docx_path = run_dir / 'rewritten_resume.docx'
        pdf_path = run_dir / 'rewritten_resume.pdf'
        if render_docx(docx_path, 'Rewritten Resume', resume_sections):
            saved['rewritten_resume_docx'] = str(docx_path)
        if render_pdf(pdf_path, 'Rewritten Resume', resume_sections):
            saved['rewritten_resume_pdf'] = str(pdf_path)

    kit_docx = run_dir / 'application_kit.docx'
    kit_pdf = run_dir / 'application_kit.pdf'
    if render_docx(kit_docx, 'ZeroToHire Application Kit', kit_sections):
        saved['application_kit_docx'] = str(kit_docx)
    if render_pdf(kit_pdf, 'ZeroToHire Application Kit', kit_sections):
        saved['application_kit_pdf'] = str(kit_pdf)

    # Generated files (txt/docx/pdf) live on disk under outputs/<run_id>/ —
    # no reason to put binary documents in SQLite. Only the metadata that
    # the dashboard/history/tracker actually query on goes in the DB.
    metadata['saved_files'] = saved
    write_json(run_dir / 'metadata.json', metadata)  # kept as an on-disk backup / debugging aid

    extra_fields = {
        k: v for k, v in metadata.items()
        if k not in {
            'run_id', 'user_id', 'created_at', 'name', 'company', 'job_url',
            'profile_id', 'match_score', 'kw_count', 'elapsed',
            'application_status', 'application_notes',
        }
    }

    run = Run.query.filter_by(run_id=run_id).first()
    if not run:
        run = Run(run_id=run_id)
        db.session.add(run)
    run.user_id = metadata.get('user_id', run.user_id)
    run.company = metadata.get('company', company)
    run.role = metadata.get('job_url', '')
    run.profile_id = metadata.get('profile_id', 'default')
    run.candidate_name = metadata.get('name', '')
    run.match_score = metadata.get('match_score', 0)
    run.kw_count = metadata.get('kw_count', 0)
    run.elapsed = metadata.get('elapsed', 0)
    run.status = run.status or 'Generated'
    run.has_kit = True
    run.extra_json = json.dumps(extra_fields)
    run.created_at = metadata.get('created_at') or run.created_at or datetime.now().isoformat(timespec='seconds')
    run.updated_at = datetime.now().isoformat(timespec='seconds')
    db.session.commit()
    return saved


def list_runs(user_id):
    rows = (
        Run.query.filter_by(user_id=user_id, has_kit=True)
        .order_by(Run.created_at.desc())
        .all()
    )
    return [r.to_metadata_dict() for r in rows]


def get_run_dir(run_id):
    safe_run_id = secure_filename(run_id)
    run_dir = OUTPUT_DIR / safe_run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return None
    return run_dir


def load_run(user_id, run_id):
    run_dir = get_run_dir(run_id)
    if not run_dir:
        return None

    run = Run.query.filter_by(run_id=run_id, has_kit=True).first()
    if not run or run.user_id != user_id:
        return None
    metadata = run.to_metadata_dict()
    files = {}
    for path in run_dir.glob('*.txt'):
        files[path.stem] = path.read_text(encoding='utf-8')
    return {'metadata': metadata, 'files': files}


def read_profile(user_id, profile_id):
    safe_id = secure_filename(profile_id or 'default') or 'default'
    row = ProfileModel.query.filter_by(user_id=user_id, profile_id=safe_id).first()
    return row.to_dict() if row else {}


def write_profile(user_id, profile_id, data):
    safe_id = secure_filename(profile_id or 'default') or 'default'
    row = ProfileModel.query.filter_by(user_id=user_id, profile_id=safe_id).first()
    if not row:
        row = ProfileModel(user_id=user_id, profile_id=safe_id)
        db.session.add(row)
    row.name = data.get('name', row.name or '')
    row.headline = data.get('headline', row.headline or '')
    row.resume_text = data.get('resume_text', row.resume_text or '')
    row.updated_at = data.get('updated_at') or datetime.now().isoformat(timespec='seconds')
    db.session.commit()


def list_profiles(user_id):
    rows = ProfileModel.query.filter_by(user_id=user_id).order_by(ProfileModel.updated_at.desc()).all()
    return [r.to_dict() for r in rows]


def _is_blocked_ip(ip_str):
    """True if the IP is private/loopback/link-local/reserved — i.e. not a
    legitimate public job-posting host. Blocks localhost, LAN ranges, and
    cloud metadata endpoints like 169.254.169.254."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparsable -> treat as unsafe
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host_safely(hostname):
    """Resolves hostname to IPs and raises if any of them are non-public.
    Returns the list of resolved IPs on success."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise RuntimeError('Could not resolve that job URL\'s host.')
    ips = {info[4][0] for info in infos}
    if not ips or any(_is_blocked_ip(ip) for ip in ips):
        raise RuntimeError('That job URL points to a non-public or internal address, which isn\'t allowed.')
    return ips


def _assert_public_http_url(url):
    """Validates scheme + resolves host, rejecting private/internal targets.
    Call this on the original URL AND on every redirect hop."""
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        raise RuntimeError('Paste a full http or https job URL.')
    if not parsed.hostname:
        raise RuntimeError('Paste a full http or https job URL.')
    _resolve_host_safely(parsed.hostname)


def fetch_job_text(url):
    if not URL_FETCH_SUPPORT:
        raise RuntimeError('Job URL import needs requests and beautifulsoup4. Install requirements and restart Flask.')

    # Validate the URL itself, then manually follow redirects (max 3 hops),
    # re-validating each hop's target host. requests.get(..., allow_redirects=True)
    # would happily follow a public URL's redirect straight to an internal
    # IP or cloud metadata endpoint (169.254.169.254) — that's the classic
    # SSRF bypass, so redirects are never auto-followed here.
    current_url = url
    response = None
    for _ in range(4):
        _assert_public_http_url(current_url)
        response = requests.get(
            current_url,
            headers={'User-Agent': 'ZeroToHire/1.0 (+local job description importer)'},
            timeout=12,
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            next_url = response.headers.get('Location')
            if not next_url:
                break
            current_url = urljoin(current_url, next_url)
            continue
        break
    else:
        raise RuntimeError('That job URL redirected too many times.')

    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript']):
        tag.decompose()
    title = clean_text(soup.title.string if soup.title else '')
    body = clean_text(soup.get_text(' '))
    return {'title': title, 'text': body[:12000], 'source_url': current_url}


@app.route('/auth/register', methods=['POST'])
def auth_register():
    data = request.get_json(silent=True) or {}
    name = safe_str(data.get('name'), max_len=150, default='Candidate') or 'Candidate'
    email = normalize_email(data.get('email'))
    password = str(data.get('password') or '')

    if not email:
        return jsonify({'error': 'Valid email is required'}), 400
    if not (8 <= len(password) <= 128):
        return jsonify({'error': 'Password must be 8-128 characters'}), 400

    users = read_users()
    if email in users:
        return jsonify({'error': 'Email already registered'}), 409

    user_id = next_user_id(users)
    users[email] = {
        "id": user_id,
        "name": name,
        "email": email,
        "phone": "",
        "role": infer_user_role(email),
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now().isoformat(timespec='seconds'),
    }
    write_users(users)

    session['user_id'] = user_id
    return jsonify({'user': {'id': user_id, 'name': name, 'email': email, 'phone': '', 'role': infer_user_role(email)}})


@app.route('/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get('email'))
    password = str(data.get('password') or '')[:128]
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    user = read_users().get(email)
    if not user:
        return jsonify({'error': 'Invalid credentials'}), 401
    password_hash = user.get('password_hash', '')
    if not password_hash:
        return jsonify({'error': 'This account uses email OTP login. Use email OTP sign in.'}), 400
    if not check_password_hash(password_hash, password):
        return jsonify({'error': 'Invalid credentials'}), 401

    session['user_id'] = user['id']
    return jsonify({'user': {
        'id': user['id'],
        'name': user['name'],
        'email': user.get('email', ''),
        'phone': user.get('phone', ''),
        'role': user.get('role', infer_user_role(user.get('email', ''))),
    }})


@app.route('/auth/google/start', methods=['GET'])
def auth_google_start():
    client_id = clean_text(os.getenv("GOOGLE_CLIENT_ID"))
    if not client_id:
        return jsonify({'error': 'Google OAuth is not configured (GOOGLE_CLIENT_ID missing).'}), 503

    state = secrets.token_urlsafe(24)
    session.permanent = True
    session['google_oauth_state'] = state
    params = {
        "client_id": client_id,
        "redirect_uri": oauth_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@app.route('/auth/google/callback', methods=['GET'])
def auth_google_callback():
    expected_state = session.get('google_oauth_state')
    state = request.args.get('state', '')
    code = request.args.get('code', '')
    # Allow state mismatch on localhost (session cookie lost across redirect)
    is_local = request.host.startswith('127.0.0.1') or request.host.startswith('localhost')
    if not is_local:
        if not expected_state or not state or state != expected_state:
            return redirect("/login?error=google_state")
    session.pop('google_oauth_state', None)

    client_id = clean_text(os.getenv("GOOGLE_CLIENT_ID"))
    client_secret = clean_text(os.getenv("GOOGLE_CLIENT_SECRET"))
    if not client_id or not client_secret:
        return redirect("/login?error=google_config")
    if not URL_FETCH_SUPPORT:
        return redirect("/login?error=requests_missing")

    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": oauth_redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=12,
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return redirect("/login?error=google_token")

        profile_response = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=12,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()

        email = normalize_email(profile.get("email"))
        if not email:
            return redirect("/login?error=google_email")

        users = read_users()
        user = users.get(email)
        if not user:
            user_id = next_user_id(users)
            user = {
                "id": user_id,
                "name": clean_text(profile.get("name")) or "Candidate",
                "email": email,
                "phone": "",
                "role": infer_user_role(email),
                "password_hash": "",
                "created_at": datetime.now().isoformat(timespec='seconds'),
            }
            users[email] = user
            write_users(users)

        session['user_id'] = user['id']
        return redirect("/dashboard")
    except Exception:
        import traceback
        traceback.print_exc()  # was silently swallowed — logged now so this shows up in server logs / gunicorn output instead of leaving you guessing from just the redirect
        return redirect("/login?error=google_auth")


@app.route('/auth/request-otp', methods=['POST'])
def auth_request_otp():
    data = request.get_json(silent=True) or {}
    channel = clean_text(data.get('channel')).lower() or 'email'
    name = safe_str(data.get('name'), max_len=150, default='Candidate') or 'Candidate'
    target_raw = data.get('target')

    if channel != 'email':
        return jsonify({'error': 'Free mode supports email OTP only'}), 400

    target = normalize_email(target_raw)
    if not target:
        return jsonify({'error': 'Enter a valid email address'}), 400

    code = generate_otp_code()
    expires_at = datetime.now() + timedelta(minutes=5)
    key = otp_target_key(channel, target)
    otp_store = read_otps()
    otp_store[key] = {
        'code': code,
        'channel': channel,
        'target': target,
        'name': name,
        'attempts': 0,
        'expires_at': expires_at.isoformat(timespec='seconds'),
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    write_otps(otp_store)

    try:
        sent, message = send_email_otp(target, code)
    except Exception as exc:
        sent = False
        message = str(exc)

    debug_mode = os.getenv('FLASK_DEBUG', 'false').lower() == 'true' or os.getenv('OTP_DEV_MODE', 'false').lower() == 'true'

    if not sent and not debug_mode:
        # Delivery genuinely failed outside of a dev/local setup. Never leak
        # the code in the response — tell the user honestly instead of
        # returning ok:true with no way for them to actually get the code.
        return jsonify({
            'ok': False,
            'error': 'Could not send the verification code. Please try again in a moment.',
        }), 502

    response = {
        'ok': True,
        'channel': channel,
        'target': target,
        'expires_in_seconds': 300,
        'delivery': message if sent else f"{message} Using local dev OTP mode.",
    }
    if debug_mode:
        response['dev_otp'] = code
    return jsonify(response)


@app.route('/auth/verify-otp', methods=['POST'])
def auth_verify_otp():
    data = request.get_json(silent=True) or {}
    channel = clean_text(data.get('channel')).lower() or 'email'
    target_raw = data.get('target')
    code = clean_text(data.get('code'))
    name = safe_str(data.get('name'), max_len=150, default='Candidate') or 'Candidate'

    if channel != 'email':
        return jsonify({'error': 'Free mode supports email OTP only'}), 400
    if not code or not re.fullmatch(r'\d{6}', code):
        return jsonify({'error': 'Enter the 6-digit OTP'}), 400

    target = normalize_email(target_raw)
    if not target:
        return jsonify({'error': 'Invalid login target'}), 400

    key = otp_target_key(channel, target)
    otp_store = read_otps()
    entry = otp_store.get(key)
    if not entry:
        return jsonify({'error': 'OTP not found. Request a new code.'}), 404

    expires_at = datetime.fromisoformat(entry.get('expires_at'))
    if datetime.now() > expires_at:
        otp_store.pop(key, None)
        write_otps(otp_store)
        return jsonify({'error': 'OTP expired. Request a new code.'}), 400

    attempts = int(entry.get('attempts', 0))
    if attempts >= 5:
        otp_store.pop(key, None)
        write_otps(otp_store)
        return jsonify({'error': 'Too many attempts. Request a new OTP.'}), 429

    if entry.get('code') != code:
        entry['attempts'] = attempts + 1
        otp_store[key] = entry
        write_otps(otp_store)
        return jsonify({'error': 'Incorrect OTP'}), 401

    user = upsert_user_with_contact(name, channel, target)
    session['user_id'] = user['id']
    otp_store.pop(key, None)
    write_otps(otp_store)

    return jsonify({
        'ok': True,
        'user': {
            'id': user.get('id'),
            'name': user.get('name'),
            'email': user.get('email'),
            'phone': user.get('phone', ''),
            'role': user.get('role', infer_user_role(user.get('email', ''))),
        }
    })


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/auth/me', methods=['GET'])
def auth_me():
    user = current_user()
    if not user:
        return jsonify({'authenticated': False}), 401
    return jsonify({'authenticated': True, 'user': user})

 
# ── MAIN RUN ENDPOINT (SSE streaming) ──────────────────
@app.route('/run', methods=['POST'])
@limiter.limit("5 per minute; 30 per hour")
def run_pipeline():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    quota_error, quota_status = check_run_quota(user_id)
    if quota_error:
        return quota_error, quota_status

    jd = safe_long_text(request.form.get('jd', ''), max_len=20000)
    name = safe_str(request.form.get('name', 'Candidate'), max_len=150, default='Candidate') or 'Candidate'
    company = safe_str(request.form.get('company', 'the Company'), max_len=200, default='the Company') or 'the Company'
    job_url = safe_url(request.form.get('job_url', ''))
    profile_id = safe_str(request.form.get('profile_id', 'default'), max_len=120, default='default') or 'default'

    if not jd:
        return jsonify({'error': 'Job description is required'}), 400
    if len(jd) < 30:
        return jsonify({'error': 'Job description looks too short to analyze — paste the full listing.'}), 400

    try:
        resume_text = read_resume_from_request()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 400

    resume_text = safe_long_text(resume_text, max_len=20000)
    if not resume_text:
        resume_text = "No resume provided. Analyze only from the job description and clearly mention this limitation."

    selected = {
        'resume': wants('include_resume'),
        'cover': wants('include_cover'),
        'interview': wants('include_interview'),
        'ats': wants('include_ats'),
    }

    def generate():
        import time
        start = time.time()

        try:
            llm = get_llm()
            agents = build_agents(llm)
            results = {
                'jd_analysis': '',
                'resume_analysis': '',
                'rewritten_resume': '',
                'cover_letter': '',
                'interview_prep': '',
            }

            stages = [
                ('jd', 1, 'JD Reader', build_jd_task(agents['jd'], jd)),
                ('analysis', 2, 'Resume Analyser', None),
            ]
            if selected['resume']:
                stages.append(('resume', 3, 'Resume Rewriter', None))
            if selected['cover']:
                stages.append(('cover', 4, 'Cover Letter Writer', None))
            if selected['interview']:
                stages.append(('interview', 5, 'Interview Coach', None))

            total = len(stages)

            for index, (stage, agent_number, label, task) in enumerate(stages, start=1):
                if stage == 'analysis':
                    task = build_analysis_task(agents['analysis'], results['jd_analysis'], resume_text)
                elif stage == 'resume':
                    task = build_resume_task(
                        agents['resume'], name, results['jd_analysis'], results['resume_analysis'], resume_text
                    )
                elif stage == 'cover':
                    task = build_cover_task(
                        agents['cover'], name, company, results['jd_analysis'], results['resume_analysis'], resume_text
                    )
                elif stage == 'interview':
                    task = build_interview_task(
                        agents['interview'], name, company, jd, results['jd_analysis'], results['resume_analysis'], resume_text
                    )

                yield f"data: {json.dumps({'type': 'agent_start', 'agent': agent_number, 'label': label, 'step': index, 'total': total})}\n\n"
                output = kickoff_single(agents[stage], task)

                if stage == 'jd':
                    results['jd_analysis'] = output
                elif stage == 'analysis':
                    results['resume_analysis'] = output
                elif stage == 'resume':
                    results['rewritten_resume'] = output
                elif stage == 'cover':
                    results['cover_letter'] = output
                elif stage == 'interview':
                    results['interview_prep'] = output

                yield f"data: {json.dumps({'type': 'agent_done', 'agent': agent_number, 'label': label, 'step': index, 'total': total})}\n\n"

            match_score, kw_count = parse_scores(results['resume_analysis'])
            heatmap = build_heatmap(results['jd_analysis'], resume_text)
            company_intel = get_company_intel(results['jd_analysis'])
            elapsed = round(time.time() - start)
            run_id = make_run_id(company)

            payload = {
                'type': 'complete',
                'run_id': run_id,
                'match_score': match_score,
                'kw_count': kw_count,
                'elapsed': elapsed,
                'selected': selected,
                'heatmap': heatmap,
                'company_intel': company_intel,
                **results,
            }
            metadata = {
                'run_id': run_id,
                'user_id': user_id,
                'created_at': datetime.now().isoformat(timespec='seconds'),
                'name': name,
                'company': company,
                'job_url': job_url,
                'profile_id': profile_id,
                'match_score': match_score,
                'kw_count': kw_count,
                'elapsed': elapsed,
                'selected': selected,
                'heatmap': heatmap,
                'company_intel': company_intel,
            }
            payload['saved_files'] = save_outputs(company, payload, metadata)
            yield f"data: {json.dumps(payload)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/history', methods=['GET'])
def history():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    return jsonify({'runs': list_runs(user_id)})


@app.route('/history/<run_id>', methods=['GET'])
def history_detail(run_id):
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    run = load_run(user_id, run_id)
    if not run:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify(run)


@app.route('/download/<run_id>/<file_key>', methods=['GET'])
def download_run_file(run_id, file_key):
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    run_dir = get_run_dir(run_id)
    if not run_dir:
        return jsonify({'error': 'Run not found'}), 404

    run = Run.query.filter_by(run_id=secure_filename(run_id), has_kit=True).first()
    if not run or run.user_id != user_id:
        return jsonify({'error': 'File not found'}), 404
    metadata = run.to_metadata_dict()
    saved_files = metadata.get('saved_files', {})
    path = saved_files.get(file_key)
    if not path:
        return jsonify({'error': 'File not found'}), 404

    file_path = Path(path)
    resolved_run_dir = run_dir.resolve()
    if not file_path.exists() or resolved_run_dir not in file_path.resolve().parents:
        return jsonify({'error': 'File not found'}), 404

    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.route('/profiles', methods=['GET'])
def profiles():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    return jsonify({'profiles': list_profiles(user_id)})


@app.route('/profiles', methods=['POST'])
def save_profile():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    name = safe_str(data.get('name'), max_len=150, default='Default Profile') or 'Default Profile'
    profile_id = secure_filename(data.get('id') or name.lower().replace(' ', '-')) or 'default'
    profile = {
        'id': profile_id,
        'name': name,
        'headline': safe_str(data.get('headline'), max_len=300),
        'resume_text': safe_long_text(data.get('resume_text'), max_len=20000),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    write_profile(user_id, profile_id, profile)
    return jsonify({'profile': profile})


@app.route('/fetch-job', methods=['POST'])
def fetch_job():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    data = request.get_json(silent=True) or {}
    url = clean_text(data.get('url'))
    if not url:
        return jsonify({'error': 'Job URL is required'}), 400
    try:
        return jsonify(fetch_job_text(url))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/fetch-jobs-bulk', methods=['POST'])
def fetch_jobs_bulk():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    raw_urls = data.get('urls') or []
    if isinstance(raw_urls, str):
        raw_urls = [line.strip() for line in raw_urls.splitlines() if line.strip()]
    urls = [clean_text(url) for url in raw_urls if clean_text(url)]
    if not urls:
        return jsonify({'error': 'Provide at least one valid URL'}), 400

    urls = urls[:10]
    jobs = []
    errors = []

    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        future_map = {pool.submit(fetch_job_text, url): url for url in urls}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                payload = future.result()
                jobs.append({
                    'url': url,
                    'title': payload.get('title', ''),
                    'text': payload.get('text', ''),
                    'source_url': payload.get('source_url', url),
                })
            except Exception as exc:
                errors.append({'url': url, 'error': str(exc)})

    jobs.sort(key=lambda item: urls.index(item['url']))
    return jsonify({
        'requested': len(urls),
        'success': len(jobs),
        'failed': len(errors),
        'jobs': jobs,
        'errors': errors,
    })


@app.route('/job-feed', methods=['GET'])
def job_feed_list():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    store = read_job_feed()
    key = str(user_id)
    items = store.get(key, [])
    if not isinstance(items, list):
        items = []
    return jsonify({'jobs': items[:200]})


@app.route('/job-feed', methods=['POST'])
def job_feed_upsert():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    jobs = data.get('jobs') or []
    if not isinstance(jobs, list):
        return jsonify({'error': 'jobs must be a list'}), 400

    store = read_job_feed()
    key = str(user_id)
    existing = store.get(key, [])
    if not isinstance(existing, list):
        existing = []

    by_url = {}
    for row in existing:
        if isinstance(row, dict):
            url = clean_text(row.get('source_url') or row.get('url'))
            if url:
                by_url[url] = row

    now = datetime.now().isoformat(timespec='seconds')
    for raw in jobs[:80]:
        if not isinstance(raw, dict):
            continue
        url = safe_url(raw.get('source_url') or raw.get('url')) or safe_str(raw.get('source_url') or raw.get('url'), max_len=2048)
        if not url:
            continue
        prev = by_url.get(url, {})
        by_url[url] = {
            'source_url': url,
            'title': safe_str(raw.get('title'), max_len=200) or prev.get('title', 'Untitled Role'),
            'text': safe_long_text(raw.get('text') or prev.get('text'), max_len=15000),
            'origin': safe_str(raw.get('origin'), max_len=50) or prev.get('origin', 'manual'),
            'saved_at': prev.get('saved_at') or now,
            'updated_at': now,
        }

    merged = sorted(by_url.values(), key=lambda row: row.get('updated_at', ''), reverse=True)
    store[key] = merged[:200]
    write_job_feed(store)
    return jsonify({'jobs': store[key], 'count': len(store[key])})


@app.route('/tracker', methods=['GET'])
def tracker_get():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    entries = [item for item in read_tracker().values() if item.get('user_id') == user_id]
    entries.sort(key=lambda item: item.get('updated_at', ''), reverse=True)
    return jsonify({'items': entries})


@app.route('/tracker', methods=['POST'])
def tracker_update():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    run_id = safe_str(data.get('run_id'), max_len=160)
    if not run_id:
        return jsonify({'error': 'run_id is required'}), 400

    tracker = read_tracker()
    current = tracker.get(run_id, {})
    if current and current.get('user_id') != user_id:
        return jsonify({'error': 'Run not found'}), 404
    item = {
        'run_id': run_id,
        'user_id': user_id,
        'company': safe_str(data.get('company'), max_len=200) or current.get('company', ''),
        'role': safe_str(data.get('role'), max_len=200) or current.get('role', ''),
        'status': safe_str(data.get('status'), max_len=50) or current.get('status', 'Generated'),
        'notes': safe_long_text(data.get('notes'), max_len=3000) or current.get('notes', ''),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    tracker[run_id] = item
    write_tracker(tracker)
    return jsonify({'item': item})


@app.route('/analytics', methods=['GET'])
def analytics():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    runs = list_runs(user_id)
    tracker = read_tracker()
    count = len(runs)

    avg_score = round(sum(item.get('match_score', 0) for item in runs) / count, 2) if count else 0
    avg_keywords = round(sum(item.get('kw_count', 0) for item in runs) / count, 2) if count else 0
    avg_time = round(sum(item.get('elapsed', 0) for item in runs) / count, 2) if count else 0

    statuses = {}
    for run in runs:
        status = tracker.get(run.get('run_id', ''), {}).get('status', run.get('application_status', 'Generated'))
        statuses[status] = statuses.get(status, 0) + 1

    top_missing = {}
    for run in runs:
        for item in run.get('heatmap', []):
            if not item.get('found'):
                key = item.get('keyword', '').strip()
                if key:
                    top_missing[key] = top_missing.get(key, 0) + 1

    missing_sorted = sorted(top_missing.items(), key=lambda row: row[1], reverse=True)[:12]
    return jsonify({
        'runs': count,
        'avg_score': avg_score,
        'avg_keywords': avg_keywords,
        'avg_time_seconds': avg_time,
        'status_breakdown': statuses,
        'top_missing_keywords': [{'keyword': k, 'count': v} for k, v in missing_sorted],
    })


@app.route('/dashboard-data', methods=['GET'])
def dashboard_data():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    user = current_user() or {}
    runs = list_runs(user_id)
    role = user.get('role', 'candidate')
    latest = runs[0] if runs else {}
    score_series = []
    for run in reversed(runs[-12:]):
        score_series.append({
            'label': run.get('company', 'Run')[:14],
            'score': run.get('match_score', 0),
        })

    status_counts = {}
    for run in runs:
        key = run.get('application_status', 'Generated')
        status_counts[key] = status_counts.get(key, 0) + 1

    base_widgets = [
        {'id': 'runs', 'title': 'Total Runs', 'value': len(runs), 'subtitle': 'Saved in your account'},
        {'id': 'score', 'title': 'Latest Match Score', 'value': f"{latest.get('match_score', 0)}/10", 'subtitle': latest.get('company', 'No runs yet')},
    ]
    role_widget_map = {
        'candidate': [
            {'id': 'offers', 'title': 'Offers', 'value': status_counts.get('Offer', 0), 'subtitle': 'Tracked offers'},
            {'id': 'interviews', 'title': 'Interviews', 'value': status_counts.get('Interview', 0), 'subtitle': 'Interview pipeline'},
        ],
        'recruiter': [
            {'id': 'applied', 'title': 'Applied', 'value': status_counts.get('Applied', 0), 'subtitle': 'Applications sent'},
            {'id': 'to_apply', 'title': 'To Apply', 'value': status_counts.get('To Apply', 0), 'subtitle': 'Backlog queue'},
        ],
        'admin': [
            {'id': 'rejected', 'title': 'Rejected', 'value': status_counts.get('Rejected', 0), 'subtitle': 'Needs resume improvements'},
            {'id': 'avg_time', 'title': 'Avg Time', 'value': f"{round(sum(item.get('elapsed', 0) for item in runs) / len(runs), 1) if runs else 0}s", 'subtitle': 'Pipeline duration'},
        ],
    }
    widgets = base_widgets + role_widget_map.get(role, role_widget_map['candidate'])

    return jsonify({
        'role': role,
        'widgets': widgets,
        'latest': latest,
        'score_series': score_series,
        'status_counts': status_counts,
    })


def llm_rerank_top_jobs(ranked_jobs, resume_text, limit=8):
    """Keyword overlap alone can't tell 'Python for scripting' apart from
    'Python for backend APIs' — it just counts word matches. This takes the
    top N jobs by keyword score and sends them to the LLM in ONE batched
    call (not one call per job — that would be slow and expensive) to get
    a real contextual judgment layered on top.

    Deliberately fails soft: if GROQ_API_KEY is missing, the call times out,
    or the model returns something unparseable, the original keyword-based
    ranking is returned untouched. Autopilot should never break just because
    this enhancement layer had a bad moment.
    """
    if not ranked_jobs:
        return ranked_jobs

    candidates = ranked_jobs[:limit]
    try:
        llm = get_llm(temperature=0.2)
    except Exception:
        return ranked_jobs

    listing = "\n".join(
        f"{i}. {job.get('title', 'Untitled')} — overlap keywords: "
        f"{', '.join(job.get('overlap_keywords', [])[:6]) or 'none'}; "
        f"missing: {', '.join(job.get('missing_keywords', [])[:6]) or 'none'}"
        for i, job in enumerate(candidates)
    )

    prompt = f"""You are ranking job listings for a candidate by real fit, not just keyword overlap.

Candidate resume summary:
{resume_text[:1500]}

Jobs (numbered, with their keyword-overlap data already computed):
{listing}

For EACH numbered job, judge fit using the actual meaning of the missing/overlapping keywords,
not just counting them (e.g. missing a core requirement should hurt more than missing a nice-to-have).
Return ONLY a JSON array, one object per job, no other text:
[{{"index": 0, "smart_score": 0-100, "reasoning": "one short sentence"}}, ...]"""

    try:
        raw = str(llm.call(prompt)).strip()
        match = re.search(r'\[[\s\S]*\]', raw)
        results = json.loads(match.group(0)) if match else json.loads(raw)
        if not isinstance(results, list):
            return ranked_jobs

        for entry in results:
            if not isinstance(entry, dict):
                continue
            idx = entry.get('index')
            if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
                continue
            smart_score = entry.get('smart_score')
            try:
                smart_score = max(0, min(100, int(smart_score)))
            except (TypeError, ValueError):
                continue
            candidates[idx]['smart_score'] = smart_score
            reasoning = clean_text(entry.get('reasoning', ''))
            if reasoning:
                candidates[idx]['priority_reason'] = reasoning

        # Only the re-ranked slice gets reordered by smart_score; jobs beyond
        # `limit` keep their original keyword-based order and position.
        candidates.sort(key=lambda row: row.get('smart_score', row.get('score', 0)), reverse=True)
        return candidates + ranked_jobs[limit:]

    except Exception:
        return ranked_jobs


@app.route('/autopilot/plan', methods=['POST'])
def autopilot_plan():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    data = request.get_json(silent=True) or {}
    jobs = data.get('jobs') or []
    if not isinstance(jobs, list) or not jobs:
        return jsonify({'error': 'jobs list is required'}), 400

    resume_text = str(data.get('resume_text') or '')
    if not resume_text:
        profile_id = clean_text(data.get('profile_id')) or 'default'
        profile = read_profile(user_id, profile_id)
        resume_text = str(profile.get('resume_text') or '')

    resume_kw = set(top_keywords(resume_text, limit=60))
    ranked_jobs = []
    all_missing = {}
    for raw in jobs[:20]:
        title = clean_text(raw.get('title') if isinstance(raw, dict) else '')
        source_url = clean_text(raw.get('source_url') if isinstance(raw, dict) else '') or clean_text(raw.get('url') if isinstance(raw, dict) else '')
        text = str((raw.get('text') if isinstance(raw, dict) else '') or '')
        job_kw = top_keywords(text, limit=45)
        if not job_kw:
            continue
        job_kw_set = set(job_kw)
        overlap = resume_kw.intersection(job_kw_set)
        score = round((len(overlap) / max(len(job_kw_set), 1)) * 100)
        if score >= 45:
            verdict = 'Go'
        elif score >= 26:
            verdict = 'Maybe'
        else:
            verdict = 'Skip'

        missing = [kw for kw in job_kw if kw not in resume_kw][:8]
        for kw in missing:
            all_missing[kw] = all_missing.get(kw, 0) + 1
        ranked_jobs.append({
            'title': title or 'Untitled Role',
            'source_url': source_url,
            'score': score,
            'verdict': verdict,
            'overlap_keywords': sorted(list(overlap))[:8],
            'missing_keywords': missing,
            'priority_reason': (
                'Strong keyword alignment and low rewrite effort.'
                if verdict == 'Go' else
                'Moderate alignment; apply after quick resume tuning.'
                if verdict == 'Maybe' else
                'Low alignment; better to skip for now.'
            ),
        })

    ranked_jobs.sort(key=lambda row: row.get('score', 0), reverse=True)

    # Smart re-rank the top slice with the LLM by default — this is what
    # actually justifies calling it "Autopilot" instead of "keyword filter".
    # Callers can pass deep_rank: false to skip it (e.g. free-tier users,
    # or the frontend doing a quick preview before committing to the call).
    if data.get('deep_rank', True):
        ranked_jobs = llm_rerank_top_jobs(ranked_jobs, resume_text, limit=8)

    go_count = sum(1 for j in ranked_jobs if j['verdict'] == 'Go')
    maybe_count = sum(1 for j in ranked_jobs if j['verdict'] == 'Maybe')
    skip_count = sum(1 for j in ranked_jobs if j['verdict'] == 'Skip')

    missing_sorted = sorted(all_missing.items(), key=lambda row: row[1], reverse=True)[:8]
    top_missing = [kw for kw, _ in missing_sorted]

    action_center = {
        'this_week_focus': top_missing[:3],
        'next_actions': [
            'Apply immediately to top 3 Go jobs.',
            'Update one project bullet to include top missing skill cluster.',
            'Re-run analyzer after resume tuning before applying to Maybe jobs.',
        ],
        'resume_strategy_memory': [
            f"Repeated gap signal: '{kw}'" for kw in top_missing[:3]
        ],
    }

    return jsonify({
        'jobs_ranked': ranked_jobs,
        'summary': {'go': go_count, 'maybe': maybe_count, 'skip': skip_count},
        'action_center': action_center,
    })


@app.route('/autopilot/weekly-review', methods=['GET'])
def autopilot_weekly_review():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    runs = list_runs(user_id)
    tracker = read_tracker()
    statuses = {'Applied': 0, 'Interview': 0, 'Offer': 0, 'Rejected': 0, 'Generated': 0, 'To Apply': 0}
    score_total = 0
    kw_total = 0
    count = len(runs)
    for run in runs:
        score_total += float(run.get('match_score', 0) or 0)
        kw_total += float(run.get('kw_count', 0) or 0)
        status_key = tracker.get(run.get('run_id', ''), {}).get('status', run.get('application_status', 'Generated'))
        if status_key not in statuses:
            statuses[status_key] = 0
        statuses[status_key] += 1

    avg_score = round(score_total / count, 2) if count else 0
    avg_kw = round(kw_total / count, 2) if count else 0
    momentum = 'Rising' if avg_score >= 7 else 'Stable' if avg_score >= 5 else 'Needs Focus'

    return jsonify({
        'runs_total': count,
        'avg_score': avg_score,
        'avg_keywords': avg_kw,
        'status_breakdown': statuses,
        'momentum': momentum,
        'coach_notes': [
            'Prioritize Go jobs first, then retune resume for Maybe jobs.',
            'Track every application in Kanban to maintain follow-up rhythm.',
            'Close top 3 repeated keyword gaps this week.',
        ],
    })


@app.route('/autopilot/top-go-queue', methods=['POST'])
def autopilot_top_go_queue():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    data = request.get_json(silent=True) or {}
    ranked = data.get('jobs_ranked') or []
    if not isinstance(ranked, list) or not ranked:
        return jsonify({'error': 'jobs_ranked list is required'}), 400

    top_go = [job for job in ranked if isinstance(job, dict) and clean_text(job.get('verdict')).lower() == 'go'][:5]
    queue = []
    for idx, job in enumerate(top_go, start=1):
        queue.append({
            'position': idx,
            'title': clean_text(job.get('title')) or 'Untitled Role',
            'source_url': clean_text(job.get('source_url') or job.get('url')),
            'score': int(job.get('score') or 0),
            'status': 'Queued',
            'next_step': 'Open job and run pipeline with imported JD.',
        })

    return jsonify({
        'queued': queue,
        'count': len(queue),
        'message': 'Top Go queue created. Use this queue to generate kits in priority order.',
    })


@app.route('/autopilot/queue', methods=['GET'])
def autopilot_queue_list():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    store = read_auto_queue()
    rows = store.get(str(user_id), [])
    if not isinstance(rows, list):
        rows = []
    return jsonify({'items': rows})


@app.route('/autopilot/queue', methods=['POST'])
def autopilot_queue_save():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    data = request.get_json(silent=True) or {}
    queued = data.get('queued') or []
    if not isinstance(queued, list):
        return jsonify({'error': 'queued must be a list'}), 400

    now = datetime.now().isoformat(timespec='seconds')
    items = []
    for idx, row in enumerate(queued[:20], start=1):
        if not isinstance(row, dict):
            continue
        source_url = clean_text(row.get('source_url') or row.get('url'))
        title = clean_text(row.get('title')) or f"Queue Job {idx}"
        score = int(row.get('score') or 0)
        item_id = secure_filename(f"{source_url}-{idx}") or f"q-{idx}"
        item_id = f"{item_id[:64]}-{idx}"
        items.append({
            'id': item_id,
            'position': idx,
            'title': title,
            'source_url': source_url,
            'score': score,
            'status': clean_text(row.get('status')) or 'Queued',
            'updated_at': now,
        })

    store = read_auto_queue()
    store[str(user_id)] = items
    write_auto_queue(store)
    return jsonify({'items': items, 'count': len(items)})


@app.route('/autopilot/queue/<item_id>/advance', methods=['POST'])
def autopilot_queue_advance(item_id):
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    stages = ['Queued', 'In Progress', 'Kit Ready', 'Applied']
    store = read_auto_queue()
    items = store.get(str(user_id), [])
    if not isinstance(items, list):
        items = []

    target = None
    for item in items:
        if clean_text(item.get('id')) == clean_text(item_id):
            target = item
            break
    if not target:
        return jsonify({'error': 'Queue item not found'}), 404

    current = clean_text(target.get('status')) or 'Queued'
    if current not in stages:
        current = 'Queued'
    next_idx = min(stages.index(current) + 1, len(stages) - 1)
    target['status'] = stages[next_idx]
    target['updated_at'] = datetime.now().isoformat(timespec='seconds')

    store[str(user_id)] = items
    write_auto_queue(store)
    return jsonify({'item': target, 'items': items})


@app.route('/autopilot/setup', methods=['GET'])
def autopilot_setup_get():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status
    store = read_autopilot_config()
    cfg = store.get(str(user_id), {})
    return jsonify({'config': cfg})


@app.route('/autopilot/setup', methods=['POST'])
def autopilot_setup_save():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status
    data = request.get_json(silent=True) or {}
    store = read_autopilot_config()
    cfg = {
        'roles': [safe_str(x, max_len=100) for x in (data.get('roles') or []) if isinstance(x, str) and x.strip()][:8],
        'locations': [safe_str(x, max_len=100) for x in (data.get('locations') or []) if isinstance(x, str) and x.strip()][:8],
        'salary_min': safe_int(data.get('salary_min'), default=0, min_val=0, max_val=10_000_000),
        'experience_level': safe_str(data.get('experience_level'), max_len=30, default='entry') or 'entry',
        'daily_job_limit': safe_int(data.get('daily_job_limit'), default=20, min_val=5, max_val=50),
        'daily_top_kits': safe_int(data.get('daily_top_kits'), default=5, min_val=1, max_val=10),
        'digest_channel': safe_str(data.get('digest_channel'), max_len=30, default='email') or 'email',
        'enabled': bool(data.get('enabled', True)),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    store[str(user_id)] = cfg
    write_autopilot_config(store)
    return jsonify({'config': cfg})


@app.route('/autopilot/run-daily', methods=['POST'])
def autopilot_run_daily():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    data = request.get_json(silent=True) or {}
    cfg = read_autopilot_config().get(str(user_id), {})
    if not cfg.get('enabled', False):
        return jsonify({'error': 'Autopilot is disabled. Enable it in setup first.'}), 400

    feed = read_job_feed().get(str(user_id), [])
    if not isinstance(feed, list) or not feed:
        return jsonify({'error': 'Job feed is empty. Import jobs first.'}), 400

    profile_id = clean_text(data.get('profile_id')) or 'default'
    profile = read_profile(user_id, profile_id)
    resume_text = str(profile.get('resume_text') or '')
    if not resume_text:
        resume_text = str(data.get('resume_text') or '')

    jobs = []
    for row in feed[:cfg.get('daily_job_limit', 20)]:
        jobs.append({
            'title': row.get('title', ''),
            'text': row.get('text', ''),
            'source_url': row.get('source_url', ''),
        })

    resume_kw = set(top_keywords(resume_text, limit=60))
    ranked = []
    for raw in jobs:
        job_kw = top_keywords(raw.get('text', ''), limit=45)
        if not job_kw:
            continue
        overlap = resume_kw.intersection(set(job_kw))
        score = round((len(overlap) / max(len(set(job_kw)), 1)) * 100)
        verdict = 'Go' if score >= 45 else ('Maybe' if score >= 26 else 'Skip')
        ranked.append({
            'title': clean_text(raw.get('title')) or 'Untitled Role',
            'source_url': clean_text(raw.get('source_url')),
            'score': score,
            'verdict': verdict,
        })
    ranked.sort(key=lambda row: row.get('score', 0), reverse=True)
    top_go = [item for item in ranked if item.get('verdict') == 'Go'][:cfg.get('daily_top_kits', 5)]

    queue_items = []
    now = datetime.now().isoformat(timespec='seconds')
    for idx, item in enumerate(top_go, start=1):
        qid = secure_filename(f"{item.get('source_url')}-{idx}") or f"auto-{idx}"
        qid = f"{qid[:64]}-{idx}"
        queue_items.append({
            'id': qid,
            'position': idx,
            'title': item.get('title', ''),
            'source_url': item.get('source_url', ''),
            'score': item.get('score', 0),
            'status': 'Kit Ready',
            'updated_at': now,
        })

    queue_store = read_auto_queue()
    queue_store[str(user_id)] = queue_items
    write_auto_queue(queue_store)

    digest = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'jobs_scanned': len(jobs),
        'go_count': len([x for x in ranked if x.get('verdict') == 'Go']),
        'top_ready': queue_items,
        'message': 'Daily autopilot run complete. Review top jobs and approve to log applications.',
    }
    return jsonify({'ranked': ranked, 'queue': queue_items, 'digest': digest})


@app.route('/autopilot/digest', methods=['GET'])
def autopilot_digest_get():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status
    queue = read_auto_queue().get(str(user_id), [])
    top = queue[:5] if isinstance(queue, list) else []
    return jsonify({
        'date': datetime.now().strftime('%Y-%m-%d'),
        'items': top,
        'summary': f"{len(top)} top jobs ready for approval",
    })


@app.route('/autopilot/approve/<item_id>', methods=['POST'])
def autopilot_approve_item(item_id):
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status
    _, pro_error, pro_status = require_pro()
    if pro_error:
        return pro_error, pro_status

    store = read_auto_queue()
    items = store.get(str(user_id), [])
    if not isinstance(items, list):
        items = []
    target = None
    for item in items:
        if clean_text(item.get('id')) == clean_text(item_id):
            target = item
            break
    if not target:
        return jsonify({'error': 'Queue item not found'}), 404

    target['status'] = 'Applied'
    target['updated_at'] = datetime.now().isoformat(timespec='seconds')
    store[str(user_id)] = items
    write_auto_queue(store)

    run_id = f"auto-{secure_filename(item_id)[:32]}"
    tracker = read_tracker()
    tracker[run_id] = {
        'run_id': run_id,
        'user_id': user_id,
        'company': target.get('title', 'Autopilot Job'),
        'role': target.get('source_url', ''),
        'status': 'Applied',
        'notes': 'Applied via Autopilot approval',
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    write_tracker(tracker)
    return jsonify({'ok': True, 'item': target, 'tracker_run_id': run_id})


register_automation_routes(app, require_user, read_autopilot_config, write_job_feed, limiter=limiter, require_pro_fn=require_pro)
scheduler = start_scheduler(app.app_context)

# ── ZERO TO RESUME BUILDER ─────────────────────────────
@app.route('/build-resume', methods=['POST'])
@limiter.limit("5 per minute; 30 per hour")
def build_resume_from_scratch():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}

    # Collect user details — safe_str caps length and never throws on a
    # wrong JSON type (e.g. someone sends "year": 2026 as a number).
    name        = safe_str(data.get('name'), max_len=150)
    email       = safe_str(data.get('email'), max_len=200)
    phone       = safe_str(data.get('phone'), max_len=30)
    linkedin    = safe_str(data.get('linkedin'), max_len=300)
    github      = safe_str(data.get('github'), max_len=300)
    college     = safe_str(data.get('college'), max_len=200)
    degree      = safe_str(data.get('degree'), max_len=150)
    year        = safe_str(data.get('year'), max_len=20)
    cgpa        = safe_str(data.get('cgpa'), max_len=20)
    skills      = safe_str(data.get('skills'), max_len=1500)
    projects    = data.get('projects', [])   # list of {name, desc}
    certs       = safe_str(data.get('certifications'), max_len=1000)
    experience  = safe_long_text(data.get('experience'), max_len=3000)
    jd          = safe_long_text(data.get('jd'), max_len=20000)
    extra       = safe_long_text(data.get('extra'), max_len=1500)

    if not name or not jd:
        return jsonify({'error': 'name and jd are required'}), 400

    # Format projects nicely
    projects_text = ''
    if isinstance(projects, list):
        for i, p in enumerate(projects[:5], 1):
            if not isinstance(p, dict):
                continue
            pname = safe_str(p.get('name', f'Project {i}'), max_len=150) or f'Project {i}'
            pdesc = safe_str(p.get('desc', ''), max_len=500)
            ptech = safe_str(p.get('tech', ''), max_len=200)
            projects_text += f"\n{i}. {pname}"
            if ptech:
                projects_text += f" ({ptech})"
            if pdesc:
                projects_text += f": {pdesc}"
    elif isinstance(projects, str):
        projects_text = safe_long_text(projects, max_len=2500)

    prompt = f"""You are a professional resume writer specializing in ATS-optimized resumes for students and fresh graduates.

Build a complete, clean, professional resume for the candidate below, tailored specifically to the job description provided.

CANDIDATE DETAILS:
- Name: {name}
- Email: {email}
- Phone: {phone}
- LinkedIn: {linkedin}
- GitHub: {github}
- College: {college}
- Degree: {degree}
- Year/Graduation: {year}
- CGPA: {cgpa}
- Skills: {skills}
- Certifications: {certs}
- Experience/Internships: {experience if experience else 'None yet'}
- Extra Info: {extra if extra else 'None'}

PROJECTS:
{projects_text if projects_text else 'No projects listed.'}

JOB DESCRIPTION:
{jd[:3000]}

INSTRUCTIONS:
1. Create a complete resume with these sections IN ORDER:
   - Header (name, contact info, LinkedIn, GitHub)
   - Professional Summary (3 lines, tailored to JD)
   - Technical Skills (grouped by category, ATS keywords from JD injected naturally)
   - Projects (highlight relevance to JD, use action verbs, quantify where possible)
   - Education
   - Certifications (if any)
   - Experience/Internships (if any)

2. Rules:
   - NEVER fabricate experience, metrics, or achievements not provided
   - Use strong action verbs: Built, Developed, Implemented, Designed, Optimized
   - Inject ATS keywords from the JD naturally into skills and project descriptions
   - Keep bullet points concise and impactful (1 line each)
   - Professional Summary must reference the specific role from the JD
   - Format cleanly with clear section headers

3. Output the resume in plain text format with clear sections.
   Use ── as section dividers.
   Make it ready to copy-paste directly.

Build the best possible resume for this candidate given their actual background."""

    try:
        groq_key  = os.getenv('GROQ_API_KEY', '')
        groq_model = os.getenv('GROQ_MODEL', 'groq/openai/gpt-oss-120b').replace('groq/', '')

        import requests as req_lib
        resp = req_lib.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {groq_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': groq_model,
                'messages': [
                    {'role': 'system', 'content': 'You are a professional resume writer. Output only the resume, no preamble or commentary.'},
                    {'role': 'user', 'content': prompt},
                ],
                'temperature': 0.4,
                'max_tokens': 2500,
            },
            timeout=30,
        )
        resp.raise_for_status()
        resume_text = resp.json()['choices'][0]['message']['content']

        # Save as a file
        run_id = f"resume-build-{user_id}-{int(datetime.now().timestamp())}"
        out_dir = OUTPUT_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / 'built_resume.txt').write_text(resume_text, encoding='utf-8')

        # Also generate DOCX if supported
        docx_path = None
        if DOCX_SUPPORT:
            try:
                doc = Document()
                doc.add_heading(name, 0)
                for line in resume_text.split('\n'):
                    if line.strip().startswith('──'):
                        doc.add_paragraph('─' * 40)
                    elif line.strip():
                        doc.add_paragraph(line.strip())
                docx_path = str(out_dir / 'built_resume.docx')
                doc.save(docx_path)
            except Exception:
                docx_path = None

        return jsonify({
            'ok': True,
            'run_id': run_id,
            'resume': resume_text,
            'has_docx': docx_path is not None,
            'message': f'Resume built successfully for {name}',
        })

    except Exception as e:
        return jsonify({'error': f'Resume generation failed: {str(e)}'}), 500


@app.route('/build-resume/download/<run_id>', methods=['GET'])
def download_built_resume(run_id):
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    fmt = request.args.get('fmt', 'txt')
    out_dir = OUTPUT_DIR / run_id

    if fmt == 'docx':
        f = out_dir / 'built_resume.docx'
        if f.exists():
            return send_file(str(f), as_attachment=True, download_name='my_resume.docx')
    
    f = out_dir / 'built_resume.txt'
    if f.exists():
        return send_file(str(f), as_attachment=True, download_name='my_resume.txt')

    return jsonify({'error': 'File not found'}), 404


# ── LINKEDIN POST GENERATOR ─────────────────────────────
@app.route('/linkedin-post', methods=['POST'])
@limiter.limit("10 per minute; 60 per hour")
def linkedin_post():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    project_desc = safe_long_text(data.get('project'), max_len=4000)
    tone = safe_str(data.get('tone', 'professional'), max_len=20, default='professional')
    include_hashtags = bool(data.get('hashtags', True))

    if not project_desc:
        return jsonify({'error': 'project description is required'}), 400

    tone_guide = {
        'professional': 'professional, clear, inspiring — like a senior dev sharing a win',
        'casual': 'casual, friendly, relatable — like sharing with peers',
        'hype': 'hyped, energetic, punchy — short sentences, lots of impact',
    }.get(tone, 'professional')

    hashtag_instr = 'End with 5–8 relevant hashtags.' if include_hashtags else 'No hashtags.'

    prompt = f"""Write a viral LinkedIn post about this project/achievement:

{project_desc}

Tone: {tone_guide}
Format rules:
- Start with a strong hook (first line must stop the scroll)
- Use short paragraphs (2–3 lines max)
- Include what problem it solves, what you built, what tech you used
- End with a call to action (check it out, feedback welcome, etc.)
- {hashtag_instr}
- 150–250 words total

Write only the post. No preamble, no explanation."""

    try:
        llm = get_llm(temperature=0.8)
        post_text = str(llm.call(prompt)).strip()
        return jsonify({'post': post_text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── SALARY ESTIMATOR ────────────────────────────────────
@app.route('/salary-estimate', methods=['POST'])
@limiter.limit("10 per minute; 60 per hour")
def salary_estimate():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    jd = safe_long_text(data.get('jd'), max_len=20000)
    resume = safe_long_text(data.get('resume'), max_len=20000)
    location = safe_str(data.get('location'), max_len=100, default='India') or 'India'
    experience = safe_str(data.get('experience'), max_len=10, default='0') or '0'

    if not jd:
        return jsonify({'error': 'job description is required'}), 400

    prompt = f"""You are a salary intelligence expert for the Indian tech job market.

Job Description:
{jd[:2000]}

Candidate Resume Summary:
{resume[:1000] if resume else 'Not provided — estimate based on JD requirements'}

Location: {location}
Years of Experience: {experience}

Analyze and return a JSON object with exactly these fields:
{{
  "min_salary": <number in LPA for full-time, or monthly stipend in INR for internships>,
  "max_salary": <number>,
  "median_salary": <number>,
  "currency": "LPA" or "INR/month",
  "type": "full-time" or "internship",
  "confidence": "high" or "medium" or "low",
  "reasoning": "<2 sentences explaining the estimate>",
  "negotiation_tip": "<one specific tip to negotiate higher>",
  "market_demand": "high" or "medium" or "low",
  "skill_premium": ["<skill that adds salary premium>", ...]
}}

Base your estimate on real 2024–2025 Indian market data. Return only the JSON object."""

    try:
        llm = get_llm(temperature=0.3)
        raw = str(llm.call(prompt)).strip()
        # Parse JSON from response
        import json as _json
        try:
            result = _json.loads(raw)
        except Exception:
            m = re.search(r'\{[\s\S]+\}', raw)
            result = _json.loads(m.group(0)) if m else {'error': 'Could not parse estimate'}
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── BILLING / SUBSCRIPTION (Phase 3) ────────────────────
# One-time monthly order via Razorpay's Orders API (not the Subscriptions
# API) — simpler to run correctly as a solo dev: no recurring-mandate setup,
# no webhook-driven renewal logic to get right on day one. The tradeoff is
# renewal isn't automatic; a user pays again when plan_expires_at passes.
# That's a reasonable v1 — worth upgrading to real Subscriptions once there
# are enough paying users that manual renewal starts costing you revenue.
RAZORPAY_MONTHLY_PRICE_INR = int(os.getenv("RAZORPAY_MONTHLY_PRICE_INR", "199"))


def get_razorpay_client():
    if not RAZORPAY_SUPPORT:
        raise RuntimeError("razorpay package not installed. Run: pip install razorpay")
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not (key_id and key_secret):
        raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET missing. Add them to .env.")
    return razorpay.Client(auth=(key_id, key_secret))


@app.route('/billing/status', methods=['GET'])
def billing_status():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    row = db.session.get(User, user_id)
    pro = is_pro_user(user_id)
    used = free_kits_used_this_month(user_id)
    return jsonify({
        'plan': 'pro' if pro else 'free',
        'plan_expires_at': (row.plan_expires_at if row else '') or '',
        'free_kits_used': used,
        'free_kits_limit': FREE_MONTHLY_RUN_LIMIT,
        'free_kits_remaining': max(0, FREE_MONTHLY_RUN_LIMIT - used) if not pro else None,
        'monthly_price_inr': RAZORPAY_MONTHLY_PRICE_INR,
    })


@app.route('/billing/create-order', methods=['POST'])
@limiter.limit("10 per minute; 30 per hour")
def billing_create_order():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    try:
        client = get_razorpay_client()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    amount_paise = RAZORPAY_MONTHLY_PRICE_INR * 100
    try:
        order = client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': f'ztH-{user_id}-{int(datetime.now().timestamp())}',
            'notes': {'user_id': str(user_id), 'plan': 'pro_monthly'},
        })
    except Exception as e:
        return jsonify({'error': f'Could not create order: {e}'}), 502

    payment_row = Payment(
        user_id=user_id,
        razorpay_order_id=order.get('id', ''),
        amount_inr=RAZORPAY_MONTHLY_PRICE_INR,
        status='created',
    )
    db.session.add(payment_row)
    db.session.commit()

    return jsonify({
        'order_id': order.get('id'),
        'amount': amount_paise,
        'currency': 'INR',
        'key_id': os.getenv('RAZORPAY_KEY_ID', ''),
        'name': 'ZeroToHire Pro — Monthly',
    })


@app.route('/billing/verify', methods=['POST'])
@limiter.limit("10 per minute; 30 per hour")
def billing_verify():
    """Called by the frontend's Razorpay Checkout success handler with the
    three fields Razorpay hands back. Signature verification (not just
    trusting the response) is what stops someone from calling this route
    directly with a fake payment_id and getting Pro for free."""
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    order_id = safe_str(data.get('razorpay_order_id'), max_len=120)
    payment_id = safe_str(data.get('razorpay_payment_id'), max_len=120)
    signature = safe_str(data.get('razorpay_signature'), max_len=256)

    if not (order_id and payment_id and signature):
        return jsonify({'error': 'razorpay_order_id, razorpay_payment_id, and razorpay_signature are required'}), 400

    try:
        client = get_razorpay_client()
        client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503
    except Exception:
        return jsonify({'error': 'Payment signature verification failed. This payment could not be confirmed.'}), 400

    payment_row = Payment.query.filter_by(razorpay_order_id=order_id, user_id=user_id).order_by(Payment.id.desc()).first()
    if not payment_row:
        return jsonify({'error': 'No matching order found for this account.'}), 404

    # Idempotency: if this exact payment_id was already processed (e.g. the
    # frontend retried the verify call after a flaky network response),
    # don't extend the plan a second time for one payment.
    already_processed = Payment.query.filter_by(razorpay_payment_id=payment_id).first()
    if already_processed and already_processed.id != payment_row.id:
        return jsonify({'error': 'This payment has already been recorded.'}), 409

    now = datetime.now()
    period_end = now + timedelta(days=30)

    payment_row.razorpay_payment_id = payment_id
    payment_row.status = 'paid'
    payment_row.plan_period_start = now.isoformat(timespec='seconds')
    payment_row.plan_period_end = period_end.isoformat(timespec='seconds')

    user_row = db.session.get(User, user_id)
    if user_row:
        user_row.plan = 'pro'
        # Stack onto an existing unexpired period instead of overwriting it,
        # so paying early doesn't lose remaining days from a current plan.
        current_expiry = None
        if user_row.plan_expires_at:
            try:
                current_expiry = datetime.fromisoformat(user_row.plan_expires_at)
            except ValueError:
                current_expiry = None
        base = current_expiry if (current_expiry and current_expiry > now) else now
        user_row.plan_expires_at = (base + timedelta(days=30)).isoformat(timespec='seconds')

    db.session.commit()

    return jsonify({
        'ok': True,
        'plan': 'pro',
        'plan_expires_at': user_row.plan_expires_at if user_row else '',
        'message': 'Welcome to ZeroToHire Pro!',
    })


# ── HEALTH CHECK ───────────────────────────────────────
@app.route('/health', methods=['GET']) 
def health():
    otp_email_configured = bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))
    return jsonify({
        'status': 'ok',
        'pdf_support': PDF_SUPPORT, 
        'docx_support': DOCX_SUPPORT,
        'pdf_export_support': PDF_EXPORT_SUPPORT,
        'url_fetch_support': URL_FETCH_SUPPORT,
        'otp_email_configured': otp_email_configured,
        'otp_phone_configured': False,
        'google_oauth_configured': bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")),
        'model': os.getenv('GROQ_MODEL', 'groq/llama-3.3-70b-versatile'),
        'outputs': str(OUTPUT_DIR),
    })


@app.route('/terms', methods=['GET'])
def terms_page():
    return send_file(BASE_DIR / 'terms.html')


@app.route('/privacy', methods=['GET'])
def privacy_page():
    return send_file(BASE_DIR / 'privacy.html')


@app.route('/login', methods=['GET'])
def login_page():
    return send_file(BASE_DIR / 'index.html')


@app.route('/dashboard', methods=['GET'])
def dashboard_page():
    return send_file(BASE_DIR / 'index.html')


# ── Client-side pages ──────────────────────────────────────
# The frontend is one index.html that shows/hides a page based on the
# URL path (see routeTo()/applyRouteState() in index.html). Each of
# these paths needs its own Flask route so a direct visit or a page
# refresh serves the app shell instead of a 404.
@app.route('/jobs', methods=['GET'])
@app.route('/resume', methods=['GET'])
@app.route('/build', methods=['GET'])
@app.route('/applications', methods=['GET'])
@app.route('/autopilot', methods=['GET'])
@app.route('/interview', methods=['GET'])
@app.route('/tools', methods=['GET'])
def app_page():
    return send_file(BASE_DIR / 'index.html')


@app.route('/', methods=['GET'])
def home():
    return send_file(BASE_DIR / 'index.html')


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, port=int(os.getenv('PORT', '5000')), threaded=True)