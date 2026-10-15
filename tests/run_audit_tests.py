"""ZeroToHire test harness. Real app code; only the AI, email and network edges are faked.

Run from the project root:   python tests/run_audit_tests.py
It copies the project to a temp folder first, so your real database, outputs and
profiles are never touched.
"""
import os, shutil, sys, tempfile
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="zth_test_")
shutil.copytree(_ROOT, os.path.join(_TMP, "app"), ignore=shutil.ignore_patterns(
    ".data", "outputs", "profiles", "tests", "__pycache__", ".git", ".venv", "venv", ".env", "*.db"))
os.chdir(os.path.join(_TMP, "app"))
sys.path.insert(0, os.getcwd())
import hashlib, hmac, io, json, os, sys, threading, http.server, traceback
from datetime import datetime, timedelta

os.environ.update(
    GROQ_API_KEY="dummy", RAZORPAY_KEY_ID="rzp_test_dummy", RAZORPAY_KEY_SECRET="topsecret",
    FLASK_DEBUG="false", OTP_DEV_MODE="false",
)
os.environ.pop("SESSION_SECRET", None)   # simulate a deploy that forgot it (render.yaml doesn't set it)

import kit_engine
import main, automation
from models import db, User, Payment, Run, ColdEmailLog, JobFeedItem, InterviewSession

app = main.app
app.config["TESTING"] = True

RESULTS = []
def report(tag, status, detail=""):
    RESULTS.append((tag, status, detail))
    print(f"[{status}] {tag}" + (f"\n        {detail}" if detail else ""), flush=True)

def mkuser(name):
    c = app.test_client()
    r = c.post("/auth/register", json={"name": name, "email": f"{name.lower()}@example.com", "password": "password123"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return c, r.get_json()["user"]["id"], r

def make_pro(uid, days=30):
    with app.app_context():
        u = db.session.get(User, uid)
        u.plan = "pro"
        u.plan_expires_at = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
        db.session.commit()

def sse(resp):
    out = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out

A, a_id, a_resp = mkuser("Alice")
B, b_id, _ = mkuser("Bob")
make_pro(a_id)

# ---- T1: boot, cookie flags, CORS, unauthenticated info ------------------------------------
try:
    sc = a_resp.headers.get("Set-Cookie", "")
    report("T1a Session cookie flags", "INFO", f"Set-Cookie = {sc.split(';')[0][:30]}...; Secure present: {'Secure' in sc}; HttpOnly: {'HttpOnly' in sc}; SameSite: {'Lax' in sc}")
    r = app.test_client().get("/health", headers={"Origin": "https://evil.example"})
    report("T1b CORS with credentials", "CONFIRMED" if r.headers.get("Access-Control-Allow-Origin") == "https://evil.example" and r.headers.get("Access-Control-Allow-Credentials") == "true" else "NOT REPRODUCED",
           f"ACAO={r.headers.get('Access-Control-Allow-Origin')} ACAC={r.headers.get('Access-Control-Allow-Credentials')} (FRONTEND_ORIGIN defaults to '*')")
    r = app.test_client().get("/automation/status")
    report("T1c /automation/status without login", "CONFIRMED" if r.status_code == 200 else "NOT REPRODUCED", f"{r.status_code} {r.get_data(as_text=True).strip()[:140]}")
    r = app.test_client().get("/health")
    report("T1d /health leaks server path + config", "CONFIRMED" if "outputs" in r.get_json() else "NOT REPRODUCED", f"keys: {list(r.get_json().keys())}")
except Exception:
    report("T1", "ERROR", traceback.format_exc()[-400:])

# ---- T2: forged session using the default secret ---------------------------------------------
try:
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    fake = Flask("attacker"); fake.secret_key = "change-me-in-production"
    token = SecureCookieSessionInterface().get_signing_serializer(fake).dumps({"user_id": a_id})
    atk = app.test_client()
    atk.set_cookie("autoapply_session", token)
    r = atk.get("/auth/me")
    report("T2 Forged login cookie via default SESSION_SECRET", "CONFIRMED" if r.status_code == 200 else "NOT REPRODUCED",
           f"/auth/me as attacker with NO password -> {r.status_code} {r.get_data(as_text=True).strip()[:120]}")
except Exception:
    report("T2", "ERROR", traceback.format_exc()[-400:])

# ---- T3: billing replay ----------------------------------------------------------------------
try:
    C, c_id, _ = mkuser("Carol")
    with app.app_context():
        db.session.add(Payment(user_id=c_id, razorpay_order_id="order_TEST1", amount_inr=199, status="created"))
        db.session.commit()
    sig = hmac.new(b"topsecret", b"order_TEST1|pay_TEST1", hashlib.sha256).hexdigest()
    body = {"razorpay_order_id": "order_TEST1", "razorpay_payment_id": "pay_TEST1", "razorpay_signature": sig}
    expiries = []
    for _ in range(4):
        r = C.post("/billing/verify", json=body)
        expiries.append((r.status_code, (r.get_json() or {}).get("plan_expires_at")))
    ok = all(s == 200 for s, _ in expiries) and len({e for _, e in expiries}) == 4
    report("T3 /billing/verify replay extends Pro each call (ONE payment)", "CONFIRMED" if ok else "NOT REPRODUCED", f"4 calls, 1 payment -> {expiries}")
    with app.app_context():
        n = Payment.query.filter_by(user_id=c_id).count()
    report("T3b Payment rows recorded", "INFO", f"{n} row(s) for 4 verify calls")
except Exception:
    report("T3", "ERROR", traceback.format_exc()[-500:])

# ---- fake interview LLM (Phase 1): controls what the "AI" returns ----------------------------------------
IV = {"scores": [], "calls": 0, "mode": "ok", "narrative_fail": False, "stage": False}
def fake_interview_json(messages, **kw):
    sys_ = messages[0]["content"]
    IV["calls"] += 1
    if '{"question"' in sys_:
        return {"question": "Question one: explain how you would design a REST API?"}
    if '"reaction"' in sys_:
        if IV["mode"] == "malformed":
            return {"reaction": "hmm", "feedback": "no score here"}
        if IV["mode"] == "string_score":
            return {"reaction": "ok", "score": "8/10", "feedback": "Good.", "model_answer_hint": "h", "next_question": "Next question number two please?"}
        score = IV["scores"].pop(0) if IV["scores"] else 7
        last = "FINAL question" in sys_
        react = "*laughs* Nice answer." if IV["stage"] else "Nice answer."
        return {"reaction": react, "score": score, "feedback": f"Feedback for score {score}.", "model_answer_hint": "A strong answer names trade-offs.",
                "next_question": "" if last else "Another follow up question about databases and indexes?"}
    if '"summary"' in sys_:
        if IV["narrative_fail"]:
            raise automation.LLMError("busy")
        return {"summary": "Honest summary from the coach.", "strengths": ["Clear structure"], "improvements": ["Add examples"], "next_steps": ["Practise STAR"]}
    return {}
automation.chat_json = fake_interview_json

# ---- T4: interview-session IDOR + cold-email-log leak ------------------------------------------
try:
    r = A.post("/automation/interview/start", json={"round_type": "hr", "role": "Backend Dev", "company": "SecretCorp", "jd": "Backend developer role with Python and SQL experience. " * 2, "resume": "my resume"})
    sid = r.get_json()["session_id"]
    with app.app_context():
        row = db.session.get(InterviewSession, sid)
        row.history_json = json.dumps([{"n": 1, "question": "Why?", "answer": "My current salary is 18 LPA and I'm leaving my employer.", "score": 5, "feedback": "ok", "hint": ""}])
        db.session.commit()
    r = B.get(f"/automation/interview/summary/{sid}")      # Bob is a FREE user and not the owner
    leaked = "18 LPA" in r.get_data(as_text=True)
    report("T4a Interview transcript readable by another user", "CONFIRMED" if leaked else "NOT REPRODUCED", f"Bob (free, id={b_id}) GET summary of Alice's session -> {r.status_code}, contains Alice's answer: {leaked}")
    with app.app_context():
        db.session.add(ColdEmailLog(to_email="hr@victim-company.com", company="VictimCo", role="SDE", candidate="Alice", status="sent"))
        db.session.commit()
    r = B.get("/automation/cold-email/log")
    leaked = "hr@victim-company.com" in r.get_data(as_text=True)
    report("T4b Cold-email log shows ALL users' emails", "CONFIRMED" if leaked else "NOT REPRODUCED", f"Bob (free) sees Alice's log entry: {leaked}")
except Exception:
    report("T4", "ERROR", traceback.format_exc()[-500:])

# ---- T5: scrape endpoint ungated + wipes manual job feed ----------------------------------------
try:
    B.post("/job-feed", json={"jobs": [{"source_url": "https://example.com/my-saved-job", "title": "Saved by me", "text": "full jd " * 20, "origin": "manual"}]})
    before = len(B.get("/job-feed").get_json()["jobs"])
    automation.run_job_scraper = lambda roles, locs: [{"title": "Scraped Role", "company": "X", "location": "Remote", "source": "RemoteOK", "source_url": "https://remoteok.com/1", "text": "Scraped Role at X. Remote."}]
    r = B.post("/automation/scrape", json={"roles": ["python"], "locations": ["delhi"]})
    after = [j["title"] for j in B.get("/job-feed").get_json()["jobs"]]
    codes = [r.status_code] + [B.post("/automation/scrape", json={"roles": ["python"], "locations": ["delhi"]}).status_code for _ in range(7)]
    report("T5a Scraper has no abuse limit (free user can hammer it)", "CONFIRMED" if 429 not in codes else "NOT REPRODUCED", f"8 rapid calls -> {codes}")
    report("T5b Scrape wipes the user's manually saved jobs", "CONFIRMED" if "Saved by me" not in after else "NOT REPRODUCED", f"feed before={before} item(s); after scrape titles={after}")
except Exception:
    report("T5", "ERROR", traceback.format_exc()[-500:])

# ---- T6: SSRF in cold-email (hardened /fetch-job vs unhardened find_hr_email) ----------------------
try:
    hits = []
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b"<html>internal admin</html>")
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/internal-only"
    r1 = A.post("/fetch-job", json={"url": url})
    hits_after_fetchjob = len(hits)
    r2 = A.post("/automation/cold-email", json={"company": "Acme", "role": "Dev", "job_url": url})
    report("T6a /fetch-job blocks internal URL (hardened)", "OK" if r1.status_code == 400 and hits_after_fetchjob == 0 else "UNEXPECTED", f"status {r1.status_code}, server hits={hits_after_fetchjob}")
    report("T6b /automation/cold-email fetches internal URL (SSRF)", "CONFIRMED" if len(hits) > hits_after_fetchjob else "NOT REPRODUCED", f"Pro user supplied job_url=127.0.0.1 -> server made the request, internal hits={len(hits)}")
except Exception:
    report("T6", "ERROR", traceback.format_exc()[-500:])

# ---- T7: cold email is draft-only now ----------------------------------------------------
try:
    r = B.post("/automation/cold-email", json={"company": "Co", "role": "Dev", "candidate_name": "Totally Not Elon", "candidate_email": "me@x.com", "hr_email": "hr@co.com", "job_url": "http://127.0.0.1:1/x"})
    j = r.get_json(); body = j["draft"]["body"]
    report("T7a Draft claims an attachment that doesn't exist", "CONFIRMED" if "I've attached my resume" in body else "NOT REPRODUCED", "draft tells user to attach it themselves")
    report("T7b Hardcoded 'Python, AI, and web development' claim", "CONFIRMED" if "Python, AI, and web development" in body else "NOT REPRODUCED")
    report("T7c Server can send email as the platform account", "CONFIRMED" if hasattr(automation, "send_cold_email") or j.get("mode") != "draft_only" else "NOT REPRODUCED", f"mode={j.get('mode')}")
    report("T7d No server-side fetch of user-supplied job_url", "CONFIRMED" if hasattr(automation, "find_hr_email") else "NOT REPRODUCED")
except Exception:
    report("T7", "ERROR", traceback.format_exc()[-500:])

# ---- T8: Telegram send/digest routes removed ----------------------------------------------
try:
    codes = [B.post("/automation/telegram/send", json={"text": "x", "chat_id": "1"}).status_code, A.post("/automation/telegram/digest", json={}).status_code]
    report("T8 Telegram send/digest routes still exist", "CONFIRMED" if any(c != 404 for c in codes) else "NOT REPRODUCED", f"status codes {codes}")
except Exception:
    report("T8", "ERROR", traceback.format_exc()[-400:])

def docx_text(b):
    return _re.sub(r"<[^>]+>", " ", zipfile.ZipFile(io.BytesIO(b)).read("word/document.xml").decode())

# ---- T9: scoring (Phase 1: kit_engine) -----------------------------------------------------------------
import kit_engine as ke, time as _time, zipfile, re as _re
try:
    kws = [{"keyword": k_, "weight": 1} for k_ in ["Java", "Go", "CI", "SQL", "Rust"]]
    sc = ke.score_resume(kws, "JavaScript developer at Google with specific MySQL and PostgreSQL experience. We go to market fast.")
    falsepos = [h["keyword"] for h in sc["heatmap"] if h["found"]]
    report("T9a Keyword matching has substring false positives", "CONFIRMED" if falsepos else "NOT REPRODUCED", f"'found': {falsepos}")
    syn = ke.score_resume([{"keyword": "JavaScript", "weight": 1}, {"keyword": "PostgreSQL", "weight": 1}, {"keyword": "machine learning", "weight": 1}, {"keyword": "CI/CD", "weight": 1}, {"keyword": "REST", "weight": 1}],
                          "Built JS apps, Postgres, machine-learning coursework, continuous integration, RESTful APIs")
    report("T9c Synonyms are matched (js/postgres/continuous integration/restful)", "OK" if syn["kw_found"] == 5 else "BROKEN", f"found {syn['kw_found']}/5, score {syn['match_score']}")
    none_sc = ke.score_resume(ke.build_keyword_list({}, "We are hiring."), "resume")
    report("T9b A score is invented when there is no signal", "CONFIRMED" if none_sc["match_score"] is not None or hasattr(main, "parse_scores") else "NOT REPRODUCED", f"score={none_sc['match_score']}, parse_scores exists={hasattr(main, 'parse_scores')}")
except Exception:
    report("T9", "ERROR", traceback.format_exc()[-400:])

# ---- T10: full /run pipeline with a fake LLM -----------------------------------------------------------------
JD_JSON = {"role_title": "Backend Developer", "role_summary": "Build APIs. Work with the team.", "must_have_skills": ["Python", "SQL", "Docker"],
           "nice_to_have_skills": ["Kubernetes"], "ats_keywords": ["Python", "SQL", "Docker", "REST", "Git", "Linux"],
           "responsibilities": ["build APIs"], "vibe_signals": ["fast"], "resume_must_prove": ["shipped a backend"]}
CALLS = {"n": 0, "fail": set(), "sleep": 0.0}
def fake_chat_json(messages, **kw):
    return dict(JD_JSON)
def fake_chat(messages, **kw):
    CALLS["n"] += 1
    u = messages[-1]["content"]
    _time.sleep(CALLS["sleep"])
    if "Write a cover letter" in u:
        if "cover" in CALLS["fail"]: raise main.LLMError("AI busy")
        return "Sure! Here is the letter:\n\nDear Hiring Team,\n\nHello.\n\nSincerely,\nBob"
    if "Rewrite the resume" in u: return "Sure, here is your resume:\n\n# Bob Builder\nbob@example.com | [GitHub](https://github.com/bob)\n## Skills\n- **Python**, Flask\n- Docker\n\nLet me know if you want changes!"
    if "Build an interview prep kit" in u: return "## Technical Questions\n1. What is Flask?\n   Answer."
    if "Compare the resume" in u: return "## Strongest Matches\n- Python\n## Gaps\n- SQL"
    return "x"
main.llm_chat_json = fake_chat_json
main.llm_chat = fake_chat
RESUME = ("Bob Builder - Python developer from Delhi. Built Flask apps with Docker and Git. Wrote REST APIs, used Linux daily, "
          "completed a BCA at GGSIPU and an internship building dashboards. Skills: Python, Flask, Docker, Git, Linux, REST, HTML, CSS. "
          "Projects: a job tracker web app with login and email alerts, and a chat bot that answers student questions using an LLM API.")
try:
    main.limiter.enabled = False
    form = lambda resume, **extra: {"jd": "We need a backend developer with Python, SQL, Docker, REST, Git and Linux experience. " * 2, "name": "Bob", "company": "Acme", "resume_text": resume, "include_resume": "true", "include_cover": "true", "include_interview": "true", **extra}
    run_ids, statuses, events = [], [], []
    for i in range(4):
        r = B.post("/run", data=form(RESUME))
        ev = sse(r); events.append(ev)
        statuses.append(r.status_code)
        done = [e for e in ev if e.get("type") == "complete"]
        run_ids.append(done[0]["run_id"] if done else None)
        if r.status_code == 402: break
        _time.sleep(1.1)   # run_ids are second-resolution
    report("T10a /run pipeline end-to-end (fake LLM) + saves kit to DB", "OK" if run_ids[0] else "BROKEN", f"statuses={statuses} run_ids={run_ids}")
    report("T10b Free quota enforced at 3 kits/month", "OK" if statuses[:3] == [200, 200, 200] and statuses[-1] == 402 else "UNEXPECTED", f"statuses={statuses}")
    rid = run_ids[0]; c0 = [e for e in events[0] if e.get("type") == "complete"][0]
    h = B.get("/history").get_json()["runs"]
    d = B.get(f"/history/{rid}")
    dl = B.get(f"/download/{rid}/rewritten_resume_docx")
    report("T10c History list / detail / DOCX download work", "OK" if len(h) == 3 and d.status_code == 200 and dl.status_code == 200 else "BROKEN", f"history={len(h)} detail={d.status_code} download={dl.status_code}")
    # empty / tiny resume
    r_empty = A.post("/run", data=form(""))
    r_tiny = A.post("/run", data=form("Bob - dev"))
    report("T10d Pipeline accepts an EMPTY/tiny resume (AI then invents one)", "CONFIRMED" if r_empty.status_code == 200 or r_tiny.status_code == 200 else "NOT REPRODUCED", f"empty -> {r_empty.status_code}, tiny -> {r_tiny.status_code}: {r_tiny.get_json().get('error','')[:80]}")
    # exports
    t = docx_text(dl.data)
    kit = B.get(f"/download/{rid}/application_kit_docx")
    pdf = B.get(f"/download/{rid}/rewritten_resume_pdf")
    pdf_ok = pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    report("T10e DOCX export contains raw markdown / model chatter", "CONFIRMED" if ("**" in t or "](" in t or "# Bob" in t or "Sure" in t or "Let me know" in t) else "NOT REPRODUCED", f"docx text starts: {t.strip()[:60]!r}; kit.docx={kit.status_code}; resume.pdf valid={pdf_ok}")
    txtdl = B.get(f"/download/{rid}/cover_letter").get_data(as_text=True)
    report("T10f Plain-text download is clean (no preamble)", "OK" if txtdl.startswith("Dear Hiring Team") else "BROKEN", repr(txtdl[:50]))
    # kit text lives in DB: wipe disk outputs and re-read
    import shutil
    shutil.rmtree(main.OUTPUT_DIR, ignore_errors=True); main.OUTPUT_DIR.mkdir(exist_ok=True)
    d2 = B.get(f"/history/{rid}"); dl2 = B.get(f"/download/{rid}/application_kit_pdf")
    report("T10g Kit survives a wiped disk (stored in DB, files on demand)", "OK" if d2.status_code == 200 and d2.get_json()["files"].get("cover_letter") and dl2.status_code == 200 else "BROKEN", f"detail={d2.status_code} kit.pdf={dl2.status_code}")
    # score is real: Bob has python/docker/git/linux/rest, no SQL -> not 7 and not fake
    report("T10h Score is computed from real coverage", "OK" if c0["match_score"] is not None and c0["kw_total"] >= 5 and c0["match_score"] != 7 and any(not x["found"] for x in c0["heatmap"]) else "CHECK", f"score={c0['match_score']} found {c0['kw_count']}/{c0['kw_total']}; missing={[x['keyword'] for x in c0['heatmap'] if not x['found']]}")
    # parallel + streaming
    CALLS["sleep"] = 1.0
    t0 = _time.time(); r = A.post("/run", data=form(RESUME)); ev = sse(r); el = _time.time() - t0
    partials = [e["stage"] for e in ev if e.get("type") == "partial"]
    order_ok = [e["type"] for e in ev][:1] == ["agent_start"]
    report("T10i Stages run in parallel and stream results as they finish", "OK" if el < 3.6 and len(partials) >= 5 else "BROKEN", f"4 stages x 1s took {el:.1f}s (serial would be >=4s); partial events={partials}")
    CALLS["sleep"] = 0.0
    # failure of one stage: error, nothing saved, nothing counted
    with app.app_context():
        before = Run.query.filter_by(user_id=a_id, has_kit=True).count()
    CALLS["fail"] = {"cover"}
    r = A.post("/run", data=form(RESUME)); ev = sse(r)
    CALLS["fail"] = set()
    with app.app_context():
        after = Run.query.filter_by(user_id=a_id, has_kit=True).count()
    types = [e.get("type") for e in ev]
    report("T10j A failed stage does not save a kit or use quota", "OK" if "error" in types and "complete" not in types and before == after else "BROKEN", f"events={sorted(set(types))} kits before/after={before}/{after}")
    # IDOR on kit downloads
    idor = A.get(f"/download/{rid}/cover_letter").status_code, A.get(f"/history/{rid}").status_code
    report("T10k Another user cannot read/download Bob's kit", "OK" if idor == (404, 404) else "BROKEN", f"Alice -> download {idor[0]}, history {idor[1]}")
    main.limiter.enabled = True
except Exception:
    report("T10", "ERROR", traceback.format_exc()[-900:])

# ---- T11: build-resume -----------------------------------------------------------------------------------------------
try:
    main.limiter.enabled = False
    main.llm_chat = lambda messages, **kw: "Sure! Here you go:\n\n# Alice A\nALICE PRIVATE RESUME phone 9999999999\n## Skills\n- **Python**"
    r = A.post("/build-resume", json={"name": "Alice", "jd": "backend developer " * 5, "phone": "9999999999"})
    rid2 = r.get_json()["run_id"]
    r2 = B.get(f"/build-resume/download/{rid2}?fmt=txt")
    own = A.get(f"/build-resume/download/{rid2}?fmt=docx")
    docx_clean = "**" not in docx_text(own.data) and "Sure" not in docx_text(own.data) if own.status_code == 200 else False
    report("T11a Another user can download Alice's built resume (IDOR)", "CONFIRMED" if r2.status_code == 200 else "NOT REPRODUCED", f"Bob got HTTP {r2.status_code}; Alice's own docx clean={docx_clean}")
    codes = [B.post("/build-resume", json={"name": "Bob", "jd": "backend developer " * 5}).status_code for _ in range(5)]
    report("T11b /build-resume ignores the free-plan quota", "CONFIRMED" if all(c == 200 for c in codes) else "NOT REPRODUCED", f"Bob (free) 5 builds -> {codes}")
    main.limiter.enabled = True
except Exception:
    report("T11", "ERROR", traceback.format_exc()[-500:])

# ---- T12: resume uploads --------------------------------------------------------------------------------------------
try:
    from docx import Document
    buf = io.BytesIO(); d = Document(); d.add_paragraph("Sarthak Sharma - Python Developer. " + "Built Flask projects with React and Docker. " * 8); d.save(buf)
    with app.test_request_context("/run", method="POST", data={"resume": (io.BytesIO(buf.getvalue()), "cv.docx")}, content_type="multipart/form-data"):
        txt = main.read_resume_from_request()
    report("T12a A .docx resume is decoded as binary garbage", "CONFIRMED" if "Sarthak" not in txt else "NOT REPRODUCED", f"extracted starts with {txt[:30]!r}")
    main.limiter.enabled = False
    r = A.post("/run", data={**form(""), "resume": (io.BytesIO(buf.getvalue()), "cv.docx")}, content_type="multipart/form-data")
    ok = any(e.get("type") == "complete" for e in sse(r))
    r_bad = A.post("/run", data={**form(""), "resume": (io.BytesIO(b"\x00\x01\x02garbage" * 50), "cv.pdf")}, content_type="multipart/form-data")
    r_old = A.post("/run", data={**form(""), "resume": (io.BytesIO(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 200), "cv.doc")}, content_type="multipart/form-data")
    main.limiter.enabled = True
    report("T12b DOCX upload runs end-to-end; bad/old files give a clear 400", "OK" if ok and r_bad.status_code == 400 and r_old.status_code == 400 else "BROKEN", f"docx run ok={ok}; garbage pdf={r_bad.status_code}; .doc={r_old.status_code}: {r_old.get_json().get('error','')[:60]}")
except Exception:
    report("T12", "ERROR", traceback.format_exc()[-400:])

# ---- T13: pre-registration takeover + OTP hygiene ---------------------------------------------------
try:
    main.limiter.enabled = False
    ATT = app.test_client()
    r = ATT.post("/auth/register", json={"name": "Mallory", "email": "victim@example.com", "password": "attackerpass1"})
    os.environ["OTP_DEV_MODE"] = "true"
    V = app.test_client()
    ro = V.post("/auth/request-otp", json={"channel": "email", "target": "victim@example.com", "name": "Victim"})
    code = ro.get_json().get("dev_otp")
    rv = V.post("/auth/verify-otp", json={"channel": "email", "target": "victim@example.com", "code": code, "name": "Victim"})
    again = app.test_client().post("/auth/login", json={"email": "victim@example.com", "password": "attackerpass1"})
    report("T13a Attacker's pre-set password still works after the real owner verifies", "CONFIRMED" if again.status_code == 200 else "NOT REPRODUCED", f"owner OTP login={rv.status_code}; attacker login after={again.status_code}")
    os.environ["OTP_DEV_MODE"] = "true"
    V.post("/auth/request-otp", json={"channel": "email", "target": "spam@example.com", "name": "S"})
    ro2 = V.post("/auth/request-otp", json={"channel": "email", "target": "spam@example.com", "name": "S"})
    report("T13b OTP can be re-requested instantly (inbox spam)", "CONFIRMED" if ro2.status_code == 200 else "NOT REPRODUCED", f"2nd request within 1s -> {ro2.status_code}")
    main.limiter.enabled = True
    os.environ["OTP_DEV_MODE"] = "false"
    codes = [app.test_client().post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 9}).status_code for _ in range(15)]
    report("T13c Login can be brute-forced (no rate limit)", "CONFIRMED" if 429 not in codes else "NOT REPRODUCED", f"15 rapid wrong logins -> {sorted(set(codes))}")
    h = app.test_client().get("/health").get_data(as_text=True)
    report("T13d /health leaks config", "CONFIRMED" if "outputs" in h or "model" in h else "NOT REPRODUCED")
except Exception:
    report("T13", "ERROR", traceback.format_exc()[-500:])

# ---- T14: interview simulator (Phase 1) ------------------------------------------------------------------
try:
    main.limiter.enabled = False
    JD = "Backend developer role. You will build REST APIs with Python, SQL and Docker. " * 2
    def start(client=A, **over):
        body = {"round_type": "technical", "company": "Acme", "jd": JD, "resume": "Bob, python dev"}
        body.update(over)
        return client.post("/automation/interview/start", json=body)
    # a: pro gate, b: needs a JD
    ra = start(B)
    rb = start(A, jd="short")
    report("T14a Free user blocked / JD required", "OK" if ra.status_code == 402 and rb.status_code == 400 else "BROKEN", f"free={ra.status_code}, short jd={rb.status_code}")
    # c: start returns question 1 of 5 and no private text
    rc = start(A); jc = rc.get_json(); sid = jc["session_id"]
    private = "jd" in jc or "resume" in jc or "Bob, python dev" in json.dumps(jc)
    report("T14b Start gives a real first question, fixed length, no JD/resume echo", "OK" if jc.get("pending_question") and jc.get("total_questions") == 5 and jc.get("question_number") == 1 and not private else "BROKEN", f"q={jc.get('pending_question')!r} total={jc.get('total_questions')} private_leak={private}")
    # d: five answers, scores computed in code, report at the end
    IV["scores"] = [8, 6, 4, 10, 2]
    outs = []
    for n in range(1, 6):
        r = A.post("/automation/interview/answer", json={"session_id": sid, "answer": "I would design it with clear resources, pagination and auth, then test it carefully.", "question_number": n})
        outs.append((r.status_code, r.get_json()))
    last = outs[-1][1]
    ok_flow = all(c == 200 for c, _ in outs) and [o[1]["score"] for o in outs] == [8, 6, 4, 10, 2] and outs[3][1]["next_question"] and last["next_question"] is None and last["status"] == "completed"
    rep = last.get("report") or {}
    report("T14c Fixed 5 questions then a final report", "OK" if ok_flow and rep.get("answered") == 5 else "BROKEN", f"statuses={[c for c,_ in outs]} final status={last.get('status')}")
    report("T14d Average + grade computed in code (not by the AI)", "OK" if rep.get("average_score") == 6.0 and rep.get("grade") == "Good" and last.get("average_score") == 6.0 else "BROKEN", f"avg={rep.get('average_score')} grade={rep.get('grade')} summary={rep.get('summary')!r}")
    r6 = A.post("/automation/interview/answer", json={"session_id": sid, "answer": "one more answer please"})
    report("T14e Cannot answer after the interview is over", "OK" if r6.status_code == 400 else "BROKEN", f"status {r6.status_code}")
    # f: malformed AI reply -> error, no fake score, session unchanged
    s2 = start(A).get_json()["session_id"]
    IV["mode"] = "malformed"; before = IV["calls"]
    rf = A.post("/automation/interview/answer", json={"session_id": s2, "answer": "A reasonably long answer about the topic at hand."})
    IV["mode"] = "ok"
    state = A.get(f"/automation/interview/summary/{s2}").get_json()
    report("T14f Unreadable AI reply = clear error, retried, no invented score", "OK" if rf.status_code == 502 and IV["calls"] - before == 2 and state["answered"] == 0 and state["scores"] == [] else "BROKEN", f"status={rf.status_code} ai_calls={IV['calls']-before} answered={state['answered']}")
    # g: tiny answer cannot earn real credit even if the model is generous
    IV["scores"] = [9]
    rg = A.post("/automation/interview/answer", json={"session_id": s2, "answer": "idk"})
    report("T14g A 1-word answer is capped (max 2/10)", "OK" if rg.get_json().get("score") == 2 else "BROKEN", f"score={rg.get_json().get('score')}")
    # h: '8/10' string accepted; stage directions stripped
    IV["mode"] = "string_score"
    rh = A.post("/automation/interview/answer", json={"session_id": s2, "answer": "A properly long answer that explains the approach step by step.", "question_number": 2})
    IV["mode"] = "ok"
    report("T14h Score like '8/10' is parsed", "OK" if rh.get_json().get("score") == 8 else "BROKEN", f"score={rh.get_json().get('score')}")
    IV["stage"] = True; IV["scores"] = [7]
    ri = A.post("/automation/interview/answer", json={"session_id": s2, "answer": "Another detailed answer covering indexes and query plans clearly.", "question_number": 3})
    IV["stage"] = False
    report("T14i Stage directions (*laughs*) are stripped", "OK" if "*" not in (ri.get_json().get("reaction") or "x*") and ri.get_json().get("reaction") else "BROKEN", f"reaction={ri.get_json().get('reaction')!r}")
    # j: out-of-sync (double click / stale tab)
    rj = A.post("/automation/interview/answer", json={"session_id": s2, "answer": "A stale answer for an old question number.", "question_number": 1})
    report("T14j Stale/double submit is rejected (409), nothing recorded", "OK" if rj.status_code == 409 else "BROKEN", f"status {rj.status_code}")
    # k: finish early -> report from answers so far; owner-only
    rk_b = B.post("/automation/interview/finish", json={"session_id": s2})
    rk = A.post("/automation/interview/finish", json={"session_id": s2}); jk = rk.get_json()
    report("T14k End early keeps answers, writes a report; other users get 404", "OK" if rk_b.status_code == 404 and jk.get("status") == "ended" and jk.get("answered") == 3 and (jk.get("report") or {}).get("average_score") == jk.get("average_score") else "BROKEN", f"other user={rk_b.status_code} status={jk.get('status')} answered={jk.get('answered')} avg={jk.get('average_score')}")
    # l: report wording fails -> still completed with a built-in report
    s3 = start(A).get_json()["session_id"]; IV["scores"] = [5, 5, 5, 5, 5]; IV["narrative_fail"] = True
    for n in range(1, 6):
        r = A.post("/automation/interview/answer", json={"session_id": s3, "answer": "A detailed answer with enough words to count properly.", "question_number": n})
    IV["narrative_fail"] = False
    jl = r.get_json()
    report("T14l If the AI report fails, a built-in report is still produced", "OK" if jl.get("status") == "completed" and (jl.get("report") or {}).get("average_score") == 5.0 and (jl["report"].get("summary")) else "BROKEN", f"status={jl.get('status')} report={jl.get('report')}")
    # m: other user cannot answer Alice's session
    s4 = start(A).get_json()["session_id"]
    make_pro(b_id)
    rm = B.post("/automation/interview/answer", json={"session_id": s4, "answer": "I am answering someone else's interview."})
    report("T14m Another user cannot answer your session", "OK" if rm.status_code == 404 else "BROKEN", f"status {rm.status_code}")
    main.limiter.enabled = True
except Exception:
    report("T14", "ERROR", traceback.format_exc()[-900:])

print("\n===== SUMMARY =====")
for tag, st, _ in RESULTS:
    print(f"{st:<14} {tag}")
os._exit(0)
