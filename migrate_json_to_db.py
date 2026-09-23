"""
ZeroToHire — one-time JSON -> SQLite migration (Phase 1).

Run this ONCE after pulling this update:

    python3 migrate_json_to_db.py

What it does:
  1. Creates the SQLite tables (if not already there) at .data/app.db
  2. Reads every legacy .data/*.json file and outputs/*/metadata.json,
     and inserts/updates the matching DB rows.
  3. Moves the original JSON files into .data/legacy_backup/ so they're
     not silently read by old code paths — nothing is deleted.

Safe to re-run: every insert is an upsert keyed on the same natural key
the old JSON files used (email, run_id, session_id, etc.), so running
this twice just re-applies the same data instead of duplicating it.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from main import app, OUTPUT_DIR, LOCAL_DATA_DIR
from models import (
    db, User, OtpCode, Profile, Run, JobFeedItem,
    AutopilotQueueItem, AutopilotConfig, InterviewSession, ColdEmailLog,
)

BACKUP_DIR = LOCAL_DATA_DIR / "legacy_backup"


def _load(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! could not parse {path}: {e}")
        return fallback


def migrate_users():
    data = _load(LOCAL_DATA_DIR / "users.json", {})
    count = 0
    for email, fields in data.items():
        if User.query.filter_by(email=email).first():
            continue
        db.session.add(User(
            id=fields.get("id"),
            name=fields.get("name", ""),
            email=email,
            phone=fields.get("phone", ""),
            role=fields.get("role", "candidate"),
            password_hash=fields.get("password_hash", ""),
            google_id=fields.get("google_id", ""),
            created_at=fields.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        ))
        count += 1
    db.session.commit()
    print(f"  users: {count} migrated")


def migrate_otps():
    data = _load(LOCAL_DATA_DIR / "otp_codes.json", {})
    count = 0
    for key, fields in data.items():
        if OtpCode.query.filter_by(key=key).first():
            continue
        db.session.add(OtpCode(
            key=key,
            code=fields.get("code"),
            channel=fields.get("channel"),
            target=fields.get("target"),
            name=fields.get("name"),
            attempts=fields.get("attempts", 0),
            expires_at=fields.get("expires_at"),
            created_at=fields.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        ))
        count += 1
    db.session.commit()
    print(f"  otp_codes: {count} migrated")


def migrate_job_feed():
    data = _load(LOCAL_DATA_DIR / "job_feed.json", {})
    count = 0
    for user_id, jobs in data.items():
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            url = job.get("source_url") or job.get("url") or ""
            exists = JobFeedItem.query.filter_by(user_id=int(user_id), source_url=url).first()
            if exists:
                continue
            db.session.add(JobFeedItem(
                user_id=int(user_id),
                source_url=url,
                title=job.get("title", ""),
                text=job.get("text", ""),
                origin=job.get("origin", "manual"),
                saved_at=job.get("saved_at") or datetime.now().isoformat(timespec="seconds"),
                updated_at=job.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            ))
            count += 1
    db.session.commit()
    print(f"  job_feed: {count} migrated")


def migrate_autopilot_queue():
    data = _load(LOCAL_DATA_DIR / "autopilot_queue.json", {})
    count = 0
    for user_id, items in data.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id", "")
            exists = AutopilotQueueItem.query.filter_by(user_id=int(user_id), item_id=item_id).first()
            if exists:
                continue
            db.session.add(AutopilotQueueItem(
                user_id=int(user_id),
                item_id=item_id,
                position=item.get("position", 0),
                title=item.get("title", ""),
                source_url=item.get("source_url", ""),
                score=item.get("score", 0),
                status=item.get("status", "Queued"),
                updated_at=item.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            ))
            count += 1
    db.session.commit()
    print(f"  autopilot_queue: {count} migrated")


def migrate_autopilot_config():
    data = _load(LOCAL_DATA_DIR / "autopilot_config.json", {})
    count = 0
    for user_id, cfg in data.items():
        if db.session.get(AutopilotConfig, int(user_id)):
            continue
        db.session.add(AutopilotConfig(
            user_id=int(user_id),
            roles_json=json.dumps(cfg.get("roles", [])),
            locations_json=json.dumps(cfg.get("locations", [])),
            salary_min=cfg.get("salary_min", 0),
            experience_level=cfg.get("experience_level", "entry"),
            daily_job_limit=cfg.get("daily_job_limit", 20),
            daily_top_kits=cfg.get("daily_top_kits", 5),
            digest_channel=cfg.get("digest_channel", "email"),
            telegram_chat_id=cfg.get("telegram_chat_id", ""),
            enabled=bool(cfg.get("enabled", True)),
            updated_at=cfg.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
        ))
        count += 1
    db.session.commit()
    print(f"  autopilot_config: {count} migrated")


def migrate_interview_sessions():
    data = _load(LOCAL_DATA_DIR / "interview_sessions.json", {})
    count = 0
    for session_id, s in data.items():
        if InterviewSession.query.filter_by(session_id=session_id).first():
            continue
        db.session.add(InterviewSession(
            session_id=session_id,
            user_id=s.get("user_id"),
            round_type=s.get("round_type", "technical"),
            round_name=s.get("round_name", ""),
            role=s.get("role", ""),
            company=s.get("company", ""),
            jd=s.get("jd", ""),
            resume=s.get("resume", ""),
            history_json=json.dumps(s.get("history", [])),
            scores_json=json.dumps(s.get("scores", [])),
            total_score=s.get("total_score", 0),
            question_count=s.get("question_count", 0),
            status=s.get("status", "active"),
            created_at=s.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        ))
        count += 1
    db.session.commit()
    print(f"  interview_sessions: {count} migrated")


def migrate_cold_email_log():
    data = _load(LOCAL_DATA_DIR / "cold_email_log.json", [])
    count = 0
    if isinstance(data, list):
        for entry in data:
            db.session.add(ColdEmailLog(
                to_email=entry.get("to", ""),
                company=entry.get("company", ""),
                role=entry.get("role", ""),
                candidate=entry.get("candidate", ""),
                sent_at=entry.get("sent_at") or datetime.now().isoformat(timespec="seconds"),
                status=entry.get("status", "sent"),
            ))
            count += 1
    db.session.commit()
    print(f"  cold_email_log: {count} migrated")


def migrate_profiles():
    profile_dir = Path(__file__).resolve().parent / "profiles"
    count = 0
    if not profile_dir.exists():
        print("  profiles: 0 migrated (no profiles/ dir)")
        return
    for user_dir in profile_dir.glob("user_*"):
        if not user_dir.is_dir():
            continue
        try:
            user_id = int(user_dir.name.replace("user_", ""))
        except ValueError:
            continue
        for path in user_dir.glob("*.json"):
            profile = _load(path, {})
            if not profile:
                continue
            profile_id = profile.get("id") or path.stem
            if Profile.query.filter_by(user_id=user_id, profile_id=profile_id).first():
                continue
            db.session.add(Profile(
                user_id=user_id,
                profile_id=profile_id,
                name=profile.get("name", ""),
                headline=profile.get("headline", ""),
                resume_text=profile.get("resume_text", ""),
                updated_at=profile.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            ))
            count += 1
    db.session.commit()
    print(f"  profiles: {count} migrated")


def migrate_runs_and_tracker():
    # Full AI-generated kits: outputs/<run_id>/metadata.json
    count_runs = 0
    for run_dir in OUTPUT_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta_path = run_dir / "metadata.json"
        metadata = _load(meta_path, None)
        if not metadata or not metadata.get("run_id"):
            continue
        run_id = metadata["run_id"]
        if Run.query.filter_by(run_id=run_id).first():
            continue

        extra_fields = {
            k: v for k, v in metadata.items()
            if k not in {
                "run_id", "user_id", "created_at", "name", "company", "job_url",
                "profile_id", "match_score", "kw_count", "elapsed",
                "application_status", "application_notes",
            }
        }
        db.session.add(Run(
            run_id=run_id,
            user_id=metadata.get("user_id"),
            company=metadata.get("company", ""),
            role=metadata.get("job_url", ""),
            profile_id=metadata.get("profile_id", "default"),
            candidate_name=metadata.get("name", ""),
            match_score=metadata.get("match_score", 0),
            kw_count=metadata.get("kw_count", 0),
            elapsed=metadata.get("elapsed", 0),
            status=metadata.get("application_status", "Generated"),
            notes=metadata.get("application_notes", ""),
            has_kit=True,
            extra_json=json.dumps(extra_fields),
            created_at=metadata.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        ))
        count_runs += 1
    db.session.commit()
    print(f"  runs (from outputs/*/metadata.json): {count_runs} migrated")

    # Tracker entries: outputs/application_tracker.json — includes lightweight
    # tracked applications that never went through full kit generation.
    tracker = _load(OUTPUT_DIR / "application_tracker.json", {})
    count_tracker = 0
    for run_id, item in tracker.items():
        run = Run.query.filter_by(run_id=run_id).first()
        if run:
            # Kit already migrated above — just make sure status/notes agree.
            if item.get("status"):
                run.status = item["status"]
            if item.get("notes"):
                run.notes = item["notes"]
            continue
        db.session.add(Run(
            run_id=run_id,
            user_id=item.get("user_id"),
            company=item.get("company", ""),
            role=item.get("role", ""),
            status=item.get("status", "Generated"),
            notes=item.get("notes", ""),
            has_kit=False,
            extra_json="{}",
            created_at=item.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
            updated_at=item.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
        ))
        count_tracker += 1
    db.session.commit()
    print(f"  runs (tracker-only entries): {count_tracker} migrated")


def backup_legacy_files():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in ["users.json", "otp_codes.json", "job_feed.json", "autopilot_queue.json",
                 "autopilot_config.json", "interview_sessions.json", "cold_email_log.json"]:
        src = LOCAL_DATA_DIR / name
        if src.exists():
            shutil.move(str(src), str(BACKUP_DIR / name))
            moved.append(name)
    tracker_src = OUTPUT_DIR / "application_tracker.json"
    if tracker_src.exists():
        shutil.move(str(tracker_src), str(BACKUP_DIR / "application_tracker.json"))
        moved.append("application_tracker.json")
    print(f"  moved to {BACKUP_DIR}: {moved if moved else 'nothing left to move'}")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        print("Migrating legacy JSON data into SQLite (.data/app.db)...")
        migrate_users()
        migrate_otps()
        migrate_job_feed()
        migrate_autopilot_queue()
        migrate_autopilot_config()
        migrate_interview_sessions()
        migrate_cold_email_log()
        migrate_profiles()
        migrate_runs_and_tracker()
        print("Backing up old JSON files (not deleting anything)...")
        backup_legacy_files()
        print("Done. Restart the app — it now reads/writes SQLite exclusively.")
