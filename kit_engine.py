"""Everything about turning (resume + job description) into a trustworthy kit.

Sections
  1. Resume intake      - read PDF / DOCX / TXT, reject empty or garbage input
  2. Keyword matching   - whole-word + synonym matching, score = real coverage
  3. Output cleaning    - strip model chatter ("Sure! Here is...")
  4. Document export    - markdown -> a real DOCX and PDF (no stray ** or ##)
  5. Prompts            - one place for every prompt the pipeline sends

Nothing in here talks to the network or the database, so it is easy to test.
"""
import io
import re
from functools import lru_cache
from xml.sax.saxutils import escape as xml_escape

# ════════════════════════════════════════════════════════════════════
# 1. RESUME INTAKE
# ════════════════════════════════════════════════════════════════════
MIN_RESUME_WORDS = 40
MIN_RESUME_CHARS = 200
MAX_RESUME_CHARS = 20000
MAX_PDF_PAGES = 10


class ResumeError(ValueError):
    """A problem with the resume that is safe to show the user as-is."""


def extract_resume_from_file(filename: str, data: bytes) -> str:
    """Return plain text from an uploaded PDF, DOCX, TXT or MD file."""
    name = (filename or "").lower()
    if not data:
        raise ResumeError("The uploaded file is empty.")

    # Sniff the real format: a renamed file must not be decoded as text.
    if data[:4] == b"%PDF" or name.endswith(".pdf"):
        return _pdf_text(data)
    if data[:4] == b"PK\x03\x04" or name.endswith(".docx"):
        return _docx_text(data)
    if name.endswith(".doc") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ResumeError("Old .doc files are not supported. Please save it as .docx or PDF and upload again.")
    if name.endswith((".txt", ".md", ".text")) or not name:
        return _decode_text(data)
    raise ResumeError("Unsupported file type. Please upload a PDF, DOCX or TXT file, or paste your resume text.")


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _pdf_text(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        raise ResumeError("PDF upload is not available on this server. Please paste your resume text instead.")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages[:MAX_PDF_PAGES]
            text = "\n".join((p.extract_text() or "") for p in pages)
    except Exception:
        raise ResumeError("Could not read this PDF. Try a different export, or paste your resume text.")
    if len(text.strip()) < 30:
        raise ResumeError("This PDF has no readable text (it may be a scan or an image). Please upload a text-based PDF or paste your resume.")
    return text


def _docx_text(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        raise ResumeError("DOCX upload is not available on this server. Please paste your resume text instead.")
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise ResumeError("Could not read this DOCX file. Re-save it as .docx or PDF, or paste your resume text.")
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                t = " ".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
                if t and t not in cells:
                    cells.append(t)
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def clean_resume_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[^\S\n]+", " ", text)            # collapse runs of spaces/tabs
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def validate_resume_text(text: str) -> str:
    """Return the cleaned resume or raise ResumeError with a helpful message."""
    text = clean_resume_text(text)
    if not text:
        raise ResumeError("Please add your resume (paste the text or upload a PDF/DOCX). Kits are built from your real experience.")
    bad = sum(1 for ch in text if ch == "�" or (ord(ch) < 32 and ch not in "\n\t"))
    if bad / max(len(text), 1) > 0.05:
        raise ResumeError("The resume could not be read properly (the file looks corrupted). Please paste the text instead.")
    words = len(text.split())
    if words < MIN_RESUME_WORDS or len(text) < MIN_RESUME_CHARS:
        raise ResumeError(
            f"Your resume looks too short ({words} words). Add your education, skills, projects or experience "
            f"(at least {MIN_RESUME_WORDS} words) so the kit is based on real details."
        )
    if len(text) > MAX_RESUME_CHARS:
        text = text[:MAX_RESUME_CHARS]
    return text


# ════════════════════════════════════════════════════════════════════
# 2. KEYWORD MATCHING + HONEST SCORING
# ════════════════════════════════════════════════════════════════════
# Each group = terms that mean the same thing. Matching a keyword matches
# any member of its group, as a WHOLE word (so "java" never matches
# "javascript" and "sql" never matches "postgresql").
_SYN_GROUPS = [
    ["javascript", "js", "ecmascript"],
    ["typescript", "ts"],
    ["node.js", "nodejs", "node js", "node"],
    ["react", "react.js", "reactjs"],
    ["react native", "reactnative"],
    ["vue", "vue.js", "vuejs"],
    ["angular", "angularjs", "angular.js"],
    ["next.js", "nextjs"],
    ["express", "express.js", "expressjs"],
    ["postgresql", "postgres"],
    ["mongodb", "mongo"],
    ["kubernetes", "k8s"],
    ["machine learning", "ml"],
    ["artificial intelligence", "ai"],
    ["natural language processing", "nlp"],
    ["large language model", "llm"],
    ["retrieval augmented generation", "rag"],
    ["ci/cd", "cicd", "ci cd", "continuous integration", "continuous delivery", "continuous deployment"],
    ["aws", "amazon web services"],
    ["gcp", "google cloud", "google cloud platform"],
    ["azure", "microsoft azure"],
    ["rest", "restful", "rest api", "restful api"],
    ["c++", "cpp"],
    ["c#", "csharp", "c sharp"],
    [".net", "dotnet"],
    ["go", "golang"],
    ["scikit-learn", "sklearn", "scikit learn"],
    ["html", "html5"],
    ["css", "css3"],
    ["object oriented programming", "oop", "oops"],
    ["data structures and algorithms", "data structures", "dsa"],
    ["ui/ux", "ux/ui", "ui ux"],
    ["git", "version control"],
    ["sql", "structured query language"],
    ["nosql", "no-sql"],
    ["python", "python3"],
    ["pytorch", "torch"],
    ["power bi", "powerbi"],
    ["excel", "ms excel", "microsoft excel"],
    ["fastapi", "fast api"],
    ["spring boot", "springboot"],
    ["unit testing", "unit tests"],
    ["problem solving", "problem-solving"],
    ["data analysis", "data analytics"],
    ["hugging face", "huggingface"],
    ["github actions", "gh actions"],
]

# Terms we scan the JD for even if the AI forgot to list them. Deliberately
# excludes words that are also ordinary English (rest, go, r, c, node, git).
_SCAN_TERMS = {
    "python", "java", "javascript", "typescript", "c++", "c#", "kotlin", "swift", "php", "ruby", "rust", "scala",
    "react", "react native", "vue", "angular", "next.js", "node.js", "express", "django", "flask", "fastapi",
    "spring boot", ".net", "html", "css", "tailwind", "bootstrap", "jquery", "redux", "webpack",
    "sql", "mysql", "postgresql", "mongodb", "redis", "sqlite", "oracle", "firebase", "nosql", "graphql",
    "docker", "kubernetes", "terraform", "jenkins", "ci/cd", "github actions", "linux", "bash", "nginx",
    "aws", "azure", "gcp", "lambda", "serverless", "microservices", "kafka", "rabbitmq",
    "machine learning", "deep learning", "artificial intelligence", "natural language processing",
    "large language model", "llm", "retrieval augmented generation", "rag", "prompt engineering",
    "langchain", "openai", "hugging face", "tensorflow", "pytorch", "keras", "scikit-learn", "opencv",
    "pandas", "numpy", "matplotlib", "tableau", "power bi", "excel", "etl", "spark", "hadoop", "airflow",
    "snowflake", "bigquery", "data analysis", "data science", "statistics",
    "android", "ios", "flutter", "selenium", "cypress", "jest", "pytest", "junit", "postman", "swagger",
    "oauth", "jwt", "websockets", "agile", "scrum", "tdd", "system design", "data structures and algorithms",
    "object oriented programming", "figma", "jira", "seo", "cybersecurity",
}

_STOP_KEYWORDS = {
    "the", "and", "or", "with", "experience", "skills", "ability", "knowledge", "strong", "good", "work",
    "team", "role", "years", "year", "etc", "other", "including", "understanding", "familiarity",
}

# Words that are ordinary English, so they only count as a skill when written
# the way the skill is written (case-sensitive, standalone).
_CASE_SENSITIVE = {
    "go": re.compile(r"(?<![A-Za-z0-9])Go(?![A-Za-z0-9])"),
    "r": re.compile(r"(?<![A-Za-z0-9&/])R(?![A-Za-z0-9&/+#])"),
    "c": re.compile(r"(?<![A-Za-z0-9&/])C(?![A-Za-z0-9&/+#])"),
}


def _norm_term(term: str) -> str:
    return re.sub(r"\s+", " ", (term or "").strip().lower())


@lru_cache(maxsize=1)
def _group_index():
    index = {}
    for gi, group in enumerate(_SYN_GROUPS):
        for term in group:
            index[_norm_term(term)] = gi
    return index


def _lookup_group(t: str):
    """Group id for a normalised term; tolerates a plural ("restful apis")."""
    index = _group_index()
    for cand in (t, t[:-2] if t.endswith("es") else None, t[:-1] if t.endswith("s") else None):
        if cand and cand in index:
            return index[cand]
    return None


def _variants(term: str):
    t = _norm_term(term)
    gi = _lookup_group(t)
    if gi is None:
        return [t]
    return [_norm_term(x) for x in _SYN_GROUPS[gi]]


@lru_cache(maxsize=4096)
def _compile_term(term: str):
    if term in _CASE_SENSITIVE:
        return _CASE_SENSITIVE[term]
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term) if p]
    body = r"[\s\-]+".join(parts)
    # whole word; optional plural; letters/digits/+/# next to it means it is part of a longer word
    return re.compile(r"(?<![a-z0-9+#.])" + body + r"(?:s|es)?(?![a-z0-9+#])", re.IGNORECASE)


def has_term(text: str, term: str) -> bool:
    """True if `term` (or a synonym) appears in `text` as a whole word."""
    if not text or not term:
        return False
    return any(_compile_term(v).search(text) for v in _variants(term) if v)


def _group_key(term: str):
    t = _norm_term(term)
    gi = _lookup_group(t)
    return ("g", gi) if gi is not None else ("t", t)


def _as_list(value):
    if isinstance(value, str):
        value = re.split(r"[\n;,]", value)
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if isinstance(item, str):
            item = re.sub(r"^[\s\-*•\d.)]+", "", item).strip(" .;:")
            if item:
                out.append(item)
    return out


def build_keyword_list(jd_data: dict, jd_text: str, limit: int = 30):
    """Pick the keywords a resume is scored against.

    Rules (so nothing is invented):
      * a keyword must actually appear in the job description (synonyms count)
      * must-have skills weigh double
      * well-known tech terms in the JD are added even if the AI missed them
    Returns [{"keyword", "weight"}].
    """
    jd_text = jd_text or ""
    chosen, seen = [], set()

    def add(term, weight):
        term = (term or "").strip()
        if not term or len(term) > 32 or len(term.split()) > 4:
            return
        if _norm_term(term) in _STOP_KEYWORDS:
            return
        key = _group_key(term)
        if key in seen or not has_term(jd_text, term):
            return
        seen.add(key)
        chosen.append({"keyword": term, "weight": weight})

    for term in _as_list(jd_data.get("must_have_skills")):
        add(term, 2)
    for term in _as_list(jd_data.get("ats_keywords")):
        add(term, 1)
    for term in _as_list(jd_data.get("nice_to_have_skills")):
        add(term, 1)
    for term in sorted(_SCAN_TERMS):
        if len(chosen) >= limit:
            break
        add(term, 1)
    return chosen[:limit]


MIN_KEYWORDS_FOR_SCORE = 5


def score_resume(keywords, resume_text: str) -> dict:
    """Score = weighted share of JD keywords the resume really contains.

    Returns heatmap rows plus numbers. If there are too few keywords to say
    anything meaningful, the score is None (the UI shows a dash) - never a
    made-up default.
    """
    items, total_w, found_w, found = [], 0, 0, 0
    for k in keywords:
        ok = has_term(resume_text, k["keyword"])
        items.append({
            "keyword": k["keyword"],
            "found": ok,
            "status": "found" if ok else "missing",
            "weight": k["weight"],
        })
        total_w += k["weight"]
        if ok:
            found_w += k["weight"]
            found += 1
    if len(items) < MIN_KEYWORDS_FOR_SCORE or total_w == 0:
        return {"heatmap": items, "match_score": None, "coverage_pct": None,
                "kw_found": found, "kw_total": len(items)}
    coverage = found_w / total_w
    return {
        "heatmap": items,
        "match_score": round(coverage * 10, 1),
        "coverage_pct": round(coverage * 100),
        "kw_found": found,
        "kw_total": len(items),
    }


# ════════════════════════════════════════════════════════════════════
# 3. OUTPUT CLEANING
# ════════════════════════════════════════════════════════════════════
_PREAMBLE_RE = re.compile(
    r"^\s*(?:sure|certainly|absolutely|of course|okay|ok|great|here(?:'s| is| are)|below is|"
    r"i(?:'ve| have) (?:rewritten|written|prepared|created|put together|drafted))\b[^\n]*\n+",
    re.IGNORECASE,
)
_POSTAMBLE_RE = re.compile(
    r"\n+\s*(?:let me know|feel free|i hope this|hope this helps|would you like|if you(?:'d| would) like|"
    r"please let me know|good luck)[\s\S]*$",
    re.IGNORECASE,
)


def clean_ai_output(text: str) -> str:
    """Remove chatty lead-ins/outros and wrapping code fences from model output."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:markdown|md|text)?\s*\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n```\s*$", "", text)
    for _ in range(2):
        m = _PREAMBLE_RE.match(text)
        if m and len(m.group(0)) < 220:
            text = text[m.end():]
        else:
            break
    text = _POSTAMBLE_RE.sub("", text)
    text = re.sub(r"^\s*(?:---+|___+|\*\*\*+|──+|═+)\s*\n", "", text)   # leading rule
    text = re.sub(r"\n\s*(?:---+|___+|──+|═+)\s*$", "", text)           # trailing rule
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ════════════════════════════════════════════════════════════════════
# 4. DOCUMENT EXPORT (markdown -> DOCX / PDF / plain text)
# ════════════════════════════════════════════════════════════════════
_HR_RE = re.compile(r"^\s*(?:---+|___+|\*\*\*+|──+|═+|—{3,})\s*$")
_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*•–·]\s+(.*)$")
_NUM_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_BOLD_ONLY_RE = re.compile(r"^\s*\*\*([^*]{2,80})\*\*:?\s*$")
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|__.+?__|(?<![\w*])\*[^*\s][^*]*?\*(?![\w*])|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def parse_markdown(text: str):
    """-> list of (kind, text, level). kinds: h1 h2 h3 bullet num p hr"""
    blocks = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _HR_RE.match(line):
            blocks.append(("hr", "", 0))
            continue
        m = _HEAD_RE.match(line)
        if m:
            level = min(len(m.group(1)), 3)
            blocks.append((f"h{level}", m.group(2), 0))
            continue
        m = _BOLD_ONLY_RE.match(line)
        if m:
            blocks.append(("h3", m.group(1), 0))
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(("bullet", m.group(2), min(len(m.group(1)) // 2, 2)))
            continue
        m = _NUM_RE.match(line)
        if m:
            blocks.append(("num", m.group(3), int(m.group(2))))
            continue
        blocks.append(("p", line.strip(), 0))
    return blocks


def inline_runs(text: str):
    """-> list of (text, bold, italic) with markdown markers removed."""
    runs = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False, False))
        tok = m.group(0)
        if tok.startswith("**") or tok.startswith("__"):
            runs.append((tok[2:-2], True, False))
        elif tok.startswith("`"):
            runs.append((tok[1:-1], False, False))
        elif tok.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok).groups()
            if url.lower().startswith("mailto:") or label.strip() == url.strip():
                runs.append((label, False, False))
            else:
                runs.append((f"{label} ({url})", False, False))
        else:
            runs.append((tok[1:-1], False, True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False, False))
    return runs or [("", False, False)]


def markdown_to_plain(text: str) -> str:
    """Readable plain text (for .txt downloads and copy buttons)."""
    out = []
    for kind, body, level in parse_markdown(text):
        plain = "".join(r[0] for r in inline_runs(body))
        if kind == "h1":
            out.append(plain.upper())
        elif kind in ("h2", "h3"):
            out.append("")
            out.append(plain.upper() if kind == "h2" else plain)
        elif kind == "bullet":
            out.append("  " * level + "- " + plain)
        elif kind == "num":
            out.append(f"{level}. {plain}")
        elif kind == "hr":
            out.append("")
        else:
            out.append(plain)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---- DOCX -----------------------------------------------------------
def _docx_runs(paragraph, text, size=None, force_bold=False):
    for t, bold, italic in inline_runs(text):
        run = paragraph.add_run(t)
        run.bold = bool(bold or force_bold)
        run.italic = italic
        if size:
            from docx.shared import Pt
            run.font.size = Pt(size)


def _bottom_border(paragraph):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, val in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "888888")):
        bottom.set(qn(key), val)
    pbdr.append(bottom)
    pPr.append(pbdr)


def _new_document():
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt
    doc = Document()
    for section in doc.sections:
        section.left_margin = section.right_margin = Inches(0.8)
        section.top_margin = section.bottom_margin = Inches(0.7)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(3)
    return doc


def _docx_add_markdown(doc, text, resume_style=False, heading_offset=0):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor
    seen_h1 = False
    seen_h2 = False
    for kind, body, level in parse_markdown(text):
        if kind == "h1":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if resume_style else WD_ALIGN_PARAGRAPH.LEFT
            _docx_runs(p, body, size=20, force_bold=True)
            p.paragraph_format.space_after = Pt(2)
            seen_h1 = True
        elif kind == "h2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(3)
            p.paragraph_format.keep_with_next = True
            _docx_runs(p, body.upper() if resume_style else body, size=12, force_bold=True)
            _bottom_border(p)
            seen_h2 = True
        elif kind == "h3":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.keep_with_next = True
            _docx_runs(p, body, size=11, force_bold=True)
        elif kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.left_indent = Inches(0.25 + 0.25 * level)
            p.paragraph_format.space_after = Pt(1.5)
            _docx_runs(p, body)
        elif kind == "num":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.first_line_indent = Inches(-0.3)
            _docx_runs(p, f"{level}. " + body)
        elif kind == "hr":
            continue
        else:
            p = doc.add_paragraph()
            if resume_style and seen_h1 and not seen_h2:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER      # contact line under the name
            _docx_runs(p, body)


def markdown_to_docx(text: str, title: str = "", resume_style: bool = False) -> bytes:
    doc = _new_document()
    if title and not resume_style:
        from docx.shared import Pt
        p = doc.add_paragraph()
        _docx_runs(p, title, size=18, force_bold=True)
    _docx_add_markdown(doc, text, resume_style=resume_style)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def sections_to_docx(sections, title: str) -> bytes:
    """sections = [(heading, markdown)] -> one DOCX with a page per section."""
    doc = _new_document()
    from docx.shared import Pt
    p = doc.add_paragraph()
    _docx_runs(p, title, size=22, force_bold=True)
    first = True
    for heading, body in sections:
        if not body or not body.strip():
            continue
        if not first:
            doc.add_page_break()
        first = False
        h = doc.add_paragraph()
        _docx_runs(h, heading, size=16, force_bold=True)
        _bottom_border(h)
        _docx_add_markdown(doc, body, resume_style=(heading.lower().startswith("rewritten resume")))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- PDF ------------------------------------------------------------
_LATIN_MAP = {
    "₹": "Rs.", "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
    "•": "-", "·": "-", "→": "->", "✓": "", "✅": "", "★": "*", " ": " ", "​": "",
}


def _latin1_safe(text: str) -> str:
    for src, dst in _LATIN_MAP.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "ignore").decode("latin-1")


def _pdf_markup(text: str) -> str:
    parts = []
    for t, bold, italic in inline_runs(text):
        t = xml_escape(_latin1_safe(t))
        if bold:
            t = f"<b>{t}</b>"
        if italic:
            t = f"<i>{t}</i>"
        parts.append(t)
    return "".join(parts)


def _pdf_story(text, resume_style=False, base=None):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    normal = ParagraphStyle("zNormal", fontName="Helvetica", fontSize=10, leading=13.5, spaceAfter=3)
    h1 = ParagraphStyle("zH1", parent=normal, fontName="Helvetica-Bold", fontSize=19, leading=23,
                        alignment=TA_CENTER if resume_style else 0, spaceAfter=2)
    h2 = ParagraphStyle("zH2", parent=normal, fontName="Helvetica-Bold", fontSize=11.5, leading=14,
                        spaceBefore=9, spaceAfter=2, keepWithNext=1)
    h3 = ParagraphStyle("zH3", parent=normal, fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=5, keepWithNext=1)
    contact = ParagraphStyle("zContact", parent=normal, alignment=TA_CENTER)
    bullet = ParagraphStyle("zBullet", parent=normal, leftIndent=14, bulletIndent=3, spaceAfter=1.5)
    numbered = ParagraphStyle("zNum", parent=normal, leftIndent=16, firstLineIndent=-16)

    story, seen_h1, seen_h2 = [], False, False
    for kind, body, level in parse_markdown(text):
        if kind == "h1":
            story.append(Paragraph(_pdf_markup(body), h1))
            seen_h1 = True
        elif kind == "h2":
            story.append(Paragraph(_pdf_markup(body.upper() if resume_style else body), h2))
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#888888"), spaceAfter=3))
            seen_h2 = True
        elif kind == "h3":
            story.append(Paragraph(_pdf_markup(body), h3))
        elif kind == "bullet":
            st = ParagraphStyle("zB%d" % level, parent=bullet, leftIndent=14 + 12 * level, bulletIndent=3 + 12 * level)
            story.append(Paragraph(_pdf_markup(body), st, bulletText="-"))
        elif kind == "num":
            story.append(Paragraph(f"{level}. " + _pdf_markup(body), numbered))
        elif kind == "hr":
            story.append(Spacer(1, 4))
        else:
            style = contact if (resume_style and seen_h1 and not seen_h2) else normal
            story.append(Paragraph(_pdf_markup(body), style))
    return story


def _build_pdf(story) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
                      topMargin=0.7 * inch, bottomMargin=0.7 * inch).build(story)
    return buf.getvalue()


def markdown_to_pdf(text: str, title: str = "", resume_style: bool = False) -> bytes:
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph
    story = []
    if title and not resume_style:
        story.append(Paragraph(_pdf_markup(title), ParagraphStyle("zT", fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=8)))
    story += _pdf_story(text, resume_style=resume_style)
    return _build_pdf(story)


def sections_to_pdf(sections, title: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, PageBreak, Paragraph
    story = [Paragraph(_pdf_markup(title), ParagraphStyle("zT", fontName="Helvetica-Bold", fontSize=22, leading=26, spaceAfter=10))]
    first = True
    for heading, body in sections:
        if not body or not body.strip():
            continue
        if not first:
            story.append(PageBreak())
        first = False
        story.append(Paragraph(_pdf_markup(heading), ParagraphStyle("zS", fontName="Helvetica-Bold", fontSize=16, leading=20, spaceAfter=2)))
        story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#444444"), spaceAfter=6))
        story += _pdf_story(body, resume_style=heading.lower().startswith("rewritten resume"))
    return _build_pdf(story)


# ════════════════════════════════════════════════════════════════════
# 5. PROMPTS
# ════════════════════════════════════════════════════════════════════
TRUTH_RULES = (
    "Never invent experience, employers, degrees, certifications, tools, numbers or achievements. "
    "Use only facts present in the candidate's resume. If evidence is missing, say so; improve only by honest reframing."
)
OUTPUT_RULES = (
    "Output ONLY the requested document in clean Markdown. No introduction, no closing remarks, no notes to the reader, "
    "no placeholders like [Your Name]."
)


def jd_analysis_messages(jd: str):
    system = (
        "You are a senior recruiter and ATS keyword specialist. Read the job description and return ONLY a JSON object. "
        "Use only information present in the job description; do not guess skills it does not mention."
    )
    user = f"""Analyze this job description. Return a JSON object with exactly these keys:
{{
  "role_title": "string",
  "role_summary": "2-3 sentences",
  "must_have_skills": ["skills/tools the JD clearly requires"],
  "nice_to_have_skills": ["skills/tools marked as a plus"],
  "ats_keywords": ["15-30 tools, technologies, skills and domain terms, written exactly as in the JD"],
  "responsibilities": ["short phrases"],
  "vibe_signals": ["culture / team signals"],
  "resume_must_prove": ["what a resume must demonstrate to get shortlisted"]
}}

Job description:
{jd}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def jd_data_to_markdown(data: dict) -> str:
    def bullets(key):
        return "\n".join(f"- {x}" for x in _as_list(data.get(key))) or "- (not stated)"
    summary = str(data.get("role_summary") or "").strip() or "(not stated)"
    title = str(data.get("role_title") or "").strip()
    return (
        (f"## Role: {title}\n\n" if title else "")
        + f"## Role Summary\n{summary}\n\n"
        + f"## Must-Have Skills\n{bullets('must_have_skills')}\n\n"
        + f"## Nice-to-Have Skills\n{bullets('nice_to_have_skills')}\n\n"
        + f"## ATS Keywords\n{bullets('ats_keywords')}\n\n"
        + f"## Responsibilities\n{bullets('responsibilities')}\n\n"
        + f"## Team / Culture Signals\n{bullets('vibe_signals')}\n\n"
        + f"## What The Resume Must Prove\n{bullets('resume_must_prove')}"
    )


def company_intel_from_jd(data: dict) -> dict:
    summary = str(data.get("role_summary") or "").strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
    return {
        "role_summary": sentences[:3],
        "must_have": _as_list(data.get("must_have_skills"))[:6],
        "culture_signals": _as_list(data.get("vibe_signals"))[:4],
        "proof_points": _as_list(data.get("resume_must_prove"))[:5],
    }


def _keyword_report(heatmap) -> str:
    found = [h["keyword"] for h in heatmap if h["found"]]
    missing = [h["keyword"] for h in heatmap if not h["found"]]
    return (
        f"Keywords already in the resume: {', '.join(found) or 'none'}\n"
        f"Keywords from the JD missing in the resume: {', '.join(missing) or 'none'}"
    )


def _ctx(jd_markdown: str, heatmap, resume: str) -> str:
    return (
        f"JOB ANALYSIS:\n{jd_markdown}\n\nKEYWORD CHECK (computed by software, treat as fact):\n{_keyword_report(heatmap)}\n\n"
        f"CANDIDATE RESUME:\n{resume}"
    )


def analysis_messages(jd_markdown, heatmap, resume):
    system = f"You are a direct, evidence-based technical recruiter. {TRUTH_RULES} Do NOT give any numeric score. {OUTPUT_RULES}"
    user = f"""Compare the resume with the job. Write a short fit analysis in Markdown with these sections:
## Strongest Matches
(each with the evidence from the resume)
## Gaps
(requirements the resume does not show; be specific)
## Fastest Improvements
(5 concrete, honest edits the candidate can make today)

{_ctx(jd_markdown, heatmap, resume)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def resume_messages(name, jd_markdown, heatmap, resume):
    system = f"You are a resume strategist for early-career tech candidates. {TRUTH_RULES} {OUTPUT_RULES}"
    user = f"""Rewrite the resume for the job below.

Format (Markdown):
# Full Name
Contact line (email | phone | links, copied from the resume)
## Summary
2-3 lines
## Skills
## Projects
## Experience
## Education
## Certifications
(omit any section the original resume has no content for)

Rules:
- Keep every real project, role, date and qualification. Do not add any.
- Work the missing keywords in ONLY where the candidate's real background supports them.
- Bullets start with action verbs. No invented metrics: if a number is unknown, write the bullet without one.
- The candidate's name is "{name}" unless the resume states a different name.

{_ctx(jd_markdown, heatmap, resume)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def cover_messages(name, company, jd_markdown, heatmap, resume):
    system = f"You write concise, human cover letters. {TRUTH_RULES} {OUTPUT_RULES} Plain paragraphs only, no headings."
    who = company if company and company.lower() not in ("the company", "") else "the team"
    user = f"""Write a cover letter from {name} to {who}.

Rules:
- Under 280 words, 3-4 short paragraphs.
- Start with "Dear Hiring Team," and end with "Sincerely," then the name.
- Open with a specific hook, never "I am writing to apply".
- Connect 2 real projects/skills from the resume to the role. Mention one gap only as honest learning momentum.
- End with a clear call to action.

{_ctx(jd_markdown, heatmap, resume)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def interview_messages(name, company, jd_markdown, heatmap, resume):
    system = f"You are a senior interview coach for junior and internship tech candidates. {TRUTH_RULES} {OUTPUT_RULES}"
    user = f"""Build an interview prep kit for {name} (applying to {company}). Markdown with these sections:
## 60-Second Introduction
## Technical Questions
(5 questions, each with a strong answer based on the candidate's real background)
## Behavioral Questions
(3 questions with STAR-style answers from real experience)
## Tricky Questions
(2 questions about gaps or weaknesses, with honest answers)
## Questions To Ask The Interviewer
(3 smart questions)

Number the questions 1., 2., 3.

{_ctx(jd_markdown, heatmap, resume)}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
