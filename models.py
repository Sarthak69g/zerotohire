# ============================================================
#  ZeroToHire — Database Models (Phase 1: SQLite migration)
#
#  Replaces the old flat-JSON files in .data/ with real tables.
#  Why this matters: JSON files do read-modify-write on the whole
#  file for every single update. Two requests landing at the same
#  moment (two users, or a browser double-click) can stomp on each
#  other and silently lose data. SQLite gives us row-level writes
#  and atomic commits instead.
#
#  Design notes:
#   - `Run` and the old `Tracker` are merged into ONE table. In the
#     old code these were two files that both kept a status/notes/
#     company/role per run_id — pure duplication. Now there's one
#     row per application, whether it came from a full AI-generated
#     kit (has files on disk) or just a manually tracked entry from
#     the Kanban/autopilot flow (no files).
#   - Generated documents (docx/pdf/txt) STAY on disk under outputs/
#     <run_id>/ — no reason to stuff binary files into SQLite. The
#     Run row just points at that folder via run_id.
#   - Blob-ish fields that don't need to be queried on (heatmap,
#     chat history, keyword lists) are kept as JSON text columns
#     rather than being fully normalized — that would be a lot of
#     extra tables for zero real query benefit right now.
# ============================================================

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), default="")
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(50), default="")
    role = db.Column(db.String(50), default="candidate")
    password_hash = db.Column(db.String(255), default="")
    google_id = db.Column(db.String(255), default="")
    # False = signed up with a password but never proved they own the email
    # (set True on first successful email-OTP or Google login). NULL = legacy row.
    email_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String(40), default=now_iso)
    # ── Subscription (Phase 3) ──────────────────────────────
    # plan: "free" or "pro". plan_expires_at: ISO date the current paid
    # period ends — checked on every gated request rather than trusting
    # `plan` alone, so an expired subscription can't quietly stay "pro"
    # forever if a renewal payment is ever missed.
    plan = db.Column(db.String(20), default="free")
    plan_expires_at = db.Column(db.String(40), default="")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name or "",
            "email": self.email,
            "phone": self.phone or "",
            "role": self.role or "candidate",
            "password_hash": self.password_hash or "",
            "google_id": self.google_id or "",
            "email_verified": self.email_verified,
            "created_at": self.created_at,
            "plan": self.plan or "free",
            "plan_expires_at": self.plan_expires_at or "",
        }


class OtpCode(db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(255), unique=True, nullable=False, index=True)  # "channel:target"
    code = db.Column(db.String(10))
    channel = db.Column(db.String(20))
    target = db.Column(db.String(255))
    name = db.Column(db.String(200))
    attempts = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.String(40))
    created_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        return {
            "code": self.code,
            "channel": self.channel,
            "target": self.target,
            "name": self.name,
            "attempts": self.attempts or 0,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


class Profile(db.Model):
    __tablename__ = "profiles"
    __table_args__ = (db.UniqueConstraint("user_id", "profile_id", name="uq_user_profile"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    profile_id = db.Column(db.String(120), nullable=False, default="default")
    name = db.Column(db.String(200), default="")
    headline = db.Column(db.String(300), default="")
    resume_text = db.Column(db.Text, default="")
    updated_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        return {
            "id": self.profile_id,
            "name": self.name or "",
            "headline": self.headline or "",
            "resume_text": self.resume_text or "",
            "updated_at": self.updated_at,
        }


class Run(db.Model):
    """One row per job application — whether it's a fully-generated
    AI kit (jd_analysis/resume/cover letter/etc. on disk) or a
    lightweight tracked entry from Kanban/autopilot with no files."""

    __tablename__ = "runs"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.String(160), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, index=True)
    company = db.Column(db.String(300), default="")
    role = db.Column(db.String(600), default="")  # job_url in the old schema
    profile_id = db.Column(db.String(120), default="default")
    candidate_name = db.Column(db.String(200), default="")
    match_score = db.Column(db.Float, default=0)
    kw_count = db.Column(db.Integer, default=0)
    elapsed = db.Column(db.Float, default=0)
    status = db.Column(db.String(50), default="Generated")
    notes = db.Column(db.Text, default="")
    has_kit = db.Column(db.Boolean, default=False)  # True once save_outputs() has run
    extra_json = db.Column(db.Text, default="{}")  # heatmap, selected, kw stats
    # The generated texts (jd_analysis, resume_analysis, rewritten_resume,
    # cover_letter, interview_prep) as JSON. Lives in the database so kits
    # survive redeploys; DOCX/PDF files are generated on demand from it.
    kit_json = db.Column(db.Text, default="")
    created_at = db.Column(db.String(40), default=now_iso)
    updated_at = db.Column(db.String(40), default=now_iso)

    def to_metadata_dict(self):
        import json
        extra = {}
        try:
            extra = json.loads(self.extra_json or "{}")
        except Exception:
            extra = {}
        base = {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "name": self.candidate_name or "",
            "company": self.company or "",
            "job_url": self.role or "",
            "profile_id": self.profile_id or "default",
            "match_score": self.match_score,  # None = not scored (never a fake default)
            "kw_count": self.kw_count or 0,
            "elapsed": self.elapsed or 0,
            "application_status": self.status or "Generated",
            "application_notes": self.notes or "",
        }
        base.update(extra)
        return base

    def to_tracker_dict(self):
        return {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "company": self.company or "",
            "role": self.role or "",
            "status": self.status or "Generated",
            "notes": self.notes or "",
            "updated_at": self.updated_at,
        }


class JobFeedItem(db.Model):
    __tablename__ = "job_feed"
    __table_args__ = (db.UniqueConstraint("user_id", "source_url", name="uq_user_source"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    source_url = db.Column(db.String(1000), default="")
    title = db.Column(db.String(500), default="")
    text = db.Column(db.Text, default="")
    origin = db.Column(db.String(50), default="manual")
    saved_at = db.Column(db.String(40), default=now_iso)
    updated_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        return {
            "source_url": self.source_url or "",
            "title": self.title or "",
            "text": self.text or "",
            "origin": self.origin or "manual",
            "saved_at": self.saved_at,
            "updated_at": self.updated_at,
        }


class AutopilotQueueItem(db.Model):
    __tablename__ = "autopilot_queue"
    __table_args__ = (db.UniqueConstraint("user_id", "item_id", name="uq_user_item"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    item_id = db.Column(db.String(200), nullable=False)
    position = db.Column(db.Integer, default=0)
    title = db.Column(db.String(500), default="")
    source_url = db.Column(db.String(1000), default="")
    score = db.Column(db.Float, default=0)
    status = db.Column(db.String(50), default="Queued")
    updated_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        return {
            "id": self.item_id,
            "position": self.position or 0,
            "title": self.title or "",
            "source_url": self.source_url or "",
            "score": self.score or 0,
            "status": self.status or "Queued",
            "updated_at": self.updated_at,
        }


class AutopilotConfig(db.Model):
    __tablename__ = "autopilot_config"

    user_id = db.Column(db.Integer, primary_key=True)
    roles_json = db.Column(db.Text, default="[]")
    locations_json = db.Column(db.Text, default="[]")
    salary_min = db.Column(db.Integer, default=0)
    experience_level = db.Column(db.String(50), default="entry")
    daily_job_limit = db.Column(db.Integer, default=20)
    daily_top_kits = db.Column(db.Integer, default=5)
    digest_channel = db.Column(db.String(20), default="email")
    telegram_chat_id = db.Column(db.String(100), default="")
    enabled = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        import json
        return {
            "roles": json.loads(self.roles_json or "[]"),
            "locations": json.loads(self.locations_json or "[]"),
            "salary_min": self.salary_min or 0,
            "experience_level": self.experience_level or "entry",
            "daily_job_limit": self.daily_job_limit or 20,
            "daily_top_kits": self.daily_top_kits or 5,
            "digest_channel": self.digest_channel or "email",
            "telegram_chat_id": self.telegram_chat_id or "",
            "enabled": bool(self.enabled),
            "updated_at": self.updated_at,
        }


class InterviewSession(db.Model):
    __tablename__ = "interview_sessions"

    session_id = db.Column(db.String(120), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    round_type = db.Column(db.String(50), default="technical")
    round_name = db.Column(db.String(120), default="")
    role = db.Column(db.String(300), default="")
    company = db.Column(db.String(300), default="")
    jd = db.Column(db.Text, default="")
    resume = db.Column(db.Text, default="")
    history_json = db.Column(db.Text, default="[]")
    scores_json = db.Column(db.Text, default="[]")
    report_json = db.Column(db.Text, default="")  # final report once the interview is completed
    total_score = db.Column(db.Float, default=0)
    question_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        import json
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "round_type": self.round_type,
            "round_name": self.round_name,
            "role": self.role,
            "company": self.company,
            "jd": self.jd or "",
            "resume": self.resume or "",
            "history": json.loads(self.history_json or "[]"),
            "scores": json.loads(self.scores_json or "[]"),
            "report": json.loads(self.report_json) if self.report_json else None,
            "total_score": self.total_score or 0,
            "question_count": self.question_count or 0,
            "status": self.status,
            "created_at": self.created_at,
        }


class Payment(db.Model):
    """One row per completed Razorpay payment. Kept even though `User.plan`
    is the source of truth for access control — this is the audit trail
    you'll need the first time someone emails asking 'did my payment go
    through' or wants a refund, and it makes webhook retries idempotent
    (checked by razorpay_payment_id before granting access again)."""

    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    razorpay_order_id = db.Column(db.String(120), default="")
    razorpay_payment_id = db.Column(db.String(120), unique=True, index=True)
    amount_inr = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="created")  # created | paid | failed
    plan_period_start = db.Column(db.String(40), default="")
    plan_period_end = db.Column(db.String(40), default="")
    created_at = db.Column(db.String(40), default=now_iso)

    def to_dict(self):
        return {
            "razorpay_order_id": self.razorpay_order_id or "",
            "razorpay_payment_id": self.razorpay_payment_id or "",
            "amount_inr": self.amount_inr or 0,
            "status": self.status or "created",
            "plan_period_start": self.plan_period_start or "",
            "plan_period_end": self.plan_period_end or "",
            "created_at": self.created_at,
        }


class ColdEmailLog(db.Model):
    __tablename__ = "cold_email_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True)
    to_email = db.Column(db.String(255), default="")
    company = db.Column(db.String(300), default="")
    role = db.Column(db.String(300), default="")
    candidate = db.Column(db.String(200), default="")
    sent_at = db.Column(db.String(40), default=now_iso)
    status = db.Column(db.String(20), default="sent")

    def to_dict(self):
        return {
            "to": self.to_email or "",
            "company": self.company or "",
            "role": self.role or "",
            "candidate": self.candidate or "",
            "sent_at": self.sent_at,
            "status": self.status or "sent",
        }
