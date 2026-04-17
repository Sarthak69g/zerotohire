from datetime import datetime
import html
from pathlib import Path
import json
import os
import re
import tempfile
from urllib.parse import urlparse

from crewai import Agent, Crew, LLM, Task
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, session, stream_with_context
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

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

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
PROFILE_DIR = BASE_DIR / "profiles"
TRACKER_FILE = OUTPUT_DIR / "application_tracker.json"
LOCAL_DATA_DIR = BASE_DIR / ".data"
LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = LOCAL_DATA_DIR / "users.json"
OUTPUT_DIR.mkdir(exist_ok=True)
PROFILE_DIR.mkdir(exist_ok=True)
if not TRACKER_FILE.exists():
    TRACKER_FILE.write_text("{}", encoding="utf-8")
if not USERS_FILE.exists():
    USERS_FILE.write_text("{}", encoding="utf-8")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = os.getenv("SESSION_SECRET", "change-me-in-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

allowed_origin = os.getenv("FRONTEND_ORIGIN", "*")
CORS(app, resources={r"/*": {"origins": allowed_origin}}, supports_credentials=True)


def wants(field, default=True):
    value = request.form.get(field)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def read_users():
    data = read_json(USERS_FILE, {})
    return data if isinstance(data, dict) else {}


def write_users(data):
    write_json(USERS_FILE, data)


def next_user_id(users):
    if not users:
        return 1
    return max(int(user.get("id", 0)) for user in users.values()) + 1


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
                "created_at": user.get("created_at"),
            }
    return None


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


def read_tracker():
    data = read_json(TRACKER_FILE, {})
    return data if isinstance(data, dict) else {}


def write_tracker(data):
    write_json(TRACKER_FILE, data)


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
def get_llm():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to .env and restart Flask.")

    return LLM(
        model=os.getenv("GROQ_MODEL", "groq/llama-3.3-70b-versatile"),
        api_key=api_key,
        temperature=0.35,
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

    match_score = data.get("match_score", 7)
    kw_count = data.get("ats_keywords_found", 12)

    if isinstance(match_score, str) and match_score.isdigit():
        match_score = int(match_score)
    elif isinstance(match_score, float):
        match_score = round(match_score)
    elif not isinstance(match_score, int):
        m = re.search(r'(\d+)\s*/\s*10', text, re.IGNORECASE)
        match_score = int(m.group(1)) if m else 7

    if isinstance(kw_count, str) and kw_count.isdigit():
        kw_count = int(kw_count)
    elif isinstance(kw_count, float):
        kw_count = round(kw_count)
    elif not isinstance(kw_count, int):
        k = re.search(r'ATS Keywords Found[:\s]+(\d+)', text, re.IGNORECASE)
        kw_count = int(k.group(1)) if k else 12

    return max(0, min(int(match_score), 10)), max(0, int(kw_count))


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
    if render_docx(kit_docx, 'AutoApply AI Application Kit', kit_sections):
        saved['application_kit_docx'] = str(kit_docx)
    if render_pdf(kit_pdf, 'AutoApply AI Application Kit', kit_sections):
        saved['application_kit_pdf'] = str(kit_pdf)

    metadata['saved_files'] = saved
    write_json(run_dir / 'metadata.json', metadata)

    tracker = read_tracker()
    tracker[run_id] = {
        'run_id': run_id,
        'user_id': metadata.get('user_id'),
        'company': metadata.get('company', company),
        'role': metadata.get('job_url', ''),
        'status': tracker.get(run_id, {}).get('status', 'Generated'),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'notes': tracker.get(run_id, {}).get('notes', ''),
    }
    write_tracker(tracker)
    return saved


def list_runs(user_id):
    tracker = read_tracker()
    runs = []
    for path in OUTPUT_DIR.iterdir():
        if not path.is_dir():
            continue
        metadata = read_json(path / 'metadata.json', None)
        if metadata and metadata.get('user_id') == user_id:
            tracked = tracker.get(metadata.get('run_id', ''), {})
            metadata['application_status'] = tracked.get('status', 'Generated')
            metadata['application_notes'] = tracked.get('notes', '')
            runs.append(metadata)
    return sorted(runs, key=lambda item: item.get('created_at', ''), reverse=True)


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

    metadata = read_json(run_dir / 'metadata.json', {})
    if metadata.get('user_id') != user_id:
        return None
    tracker = read_tracker().get(run_id, {})
    metadata['application_status'] = tracker.get('status', metadata.get('application_status', 'Generated'))
    metadata['application_notes'] = tracker.get('notes', metadata.get('application_notes', ''))
    files = {}
    for path in run_dir.glob('*.txt'):
        files[path.stem] = path.read_text(encoding='utf-8')
    return {'metadata': metadata, 'files': files}


def profile_path(user_id, profile_id):
    user_dir = PROFILE_DIR / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_id = secure_filename(profile_id or 'default') or 'default'
    return user_dir / f'{safe_id}.json'


def fetch_job_text(url):
    if not URL_FETCH_SUPPORT:
        raise RuntimeError('Job URL import needs requests and beautifulsoup4. Install requirements and restart Flask.')

    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        raise RuntimeError('Paste a full http or https job URL.')

    response = requests.get(
        url,
        headers={'User-Agent': 'AutoApplyAI/1.0 (+local job description importer)'},
        timeout=12,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript']):
        tag.decompose()
    title = clean_text(soup.title.string if soup.title else '')
    body = clean_text(soup.get_text(' '))
    return {'title': title, 'text': body[:12000], 'source_url': url}


@app.route('/auth/register', methods=['POST'])
def auth_register():
    data = request.get_json(silent=True) or {}
    name = clean_text(data.get('name')) or 'Candidate'
    email = clean_text(data.get('email')).lower()
    password = str(data.get('password') or '')

    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    users = read_users()
    if email in users:
        return jsonify({'error': 'Email already registered'}), 409

    user_id = next_user_id(users)
    users[email] = {
        "id": user_id,
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now().isoformat(timespec='seconds'),
    }
    write_users(users)

    session['user_id'] = user_id
    return jsonify({'user': {'id': user_id, 'name': name, 'email': email}})


@app.route('/auth/login', methods=['POST'])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = clean_text(data.get('email')).lower()
    password = str(data.get('password') or '')
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    user = read_users().get(email)
    if not user or not check_password_hash(user.get('password_hash', ''), password):
        return jsonify({'error': 'Invalid credentials'}), 401

    session['user_id'] = user['id']
    return jsonify({'user': {'id': user['id'], 'name': user['name'], 'email': user['email']}})


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
def run_pipeline():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    jd = request.form.get('jd', '').strip()
    name = request.form.get('name', 'Candidate').strip() or 'Candidate'
    company = request.form.get('company', 'the Company').strip() or 'the Company'
    job_url = request.form.get('job_url', '').strip()
    profile_id = request.form.get('profile_id', 'default').strip() or 'default'

    if not jd:
        return jsonify({'error': 'Job description is required'}), 400

    try:
        resume_text = read_resume_from_request()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 400

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

    metadata = read_json(run_dir / 'metadata.json', {})
    if metadata.get('user_id') != user_id:
        return jsonify({'error': 'File not found'}), 404
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

    user_dir = PROFILE_DIR / f"user_{user_id}"
    data = []
    for path in user_dir.glob('*.json'):
        profile = read_json(path, {})
        if profile:
            data.append(profile)
    return jsonify({'profiles': sorted(data, key=lambda item: item.get('updated_at', ''), reverse=True)})


@app.route('/profiles', methods=['POST'])
def save_profile():
    user_id, error_resp, status = require_user()
    if error_resp:
        return error_resp, status

    data = request.get_json(silent=True) or {}
    name = clean_text(data.get('name')) or 'Default Profile'
    profile_id = secure_filename(data.get('id') or name.lower().replace(' ', '-')) or 'default'
    profile = {
        'id': profile_id,
        'name': name,
        'headline': clean_text(data.get('headline')),
        'resume_text': str(data.get('resume_text') or '').strip(),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }
    write_json(profile_path(user_id, profile_id), profile)
    return jsonify({'profile': profile})


@app.route('/fetch-job', methods=['POST'])
def fetch_job():
    data = request.get_json(silent=True) or {}
    url = clean_text(data.get('url'))
    if not url:
        return jsonify({'error': 'Job URL is required'}), 400
    try:
        return jsonify(fetch_job_text(url))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


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
    run_id = clean_text(data.get('run_id'))
    if not run_id:
        return jsonify({'error': 'run_id is required'}), 400

    tracker = read_tracker()
    current = tracker.get(run_id, {})
    if current and current.get('user_id') != user_id:
        return jsonify({'error': 'Run not found'}), 404
    item = {
        'run_id': run_id,
        'user_id': user_id,
        'company': clean_text(data.get('company')) or current.get('company', ''),
        'role': clean_text(data.get('role')) or current.get('role', ''),
        'status': clean_text(data.get('status')) or current.get('status', 'Generated'),
        'notes': str(data.get('notes') or current.get('notes', '')).strip(),
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


# ── HEALTH CHECK ───────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'pdf_support': PDF_SUPPORT,
        'docx_support': DOCX_SUPPORT,
        'pdf_export_support': PDF_EXPORT_SUPPORT,
        'url_fetch_support': URL_FETCH_SUPPORT,
        'model': os.getenv('GROQ_MODEL', 'groq/llama-3.3-70b-versatile'),
        'outputs': str(OUTPUT_DIR),
    })


@app.route('/', methods=['GET'])
def home():
    return send_file(BASE_DIR / 'index.html')


if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, port=int(os.getenv('PORT', '5000')), threaded=True)
