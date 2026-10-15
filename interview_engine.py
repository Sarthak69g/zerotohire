"""Mock-interview engine: prompts, strict parsing, honest scoring, final report.

Design rules (Phase 1 - Trust)
  * A FIXED number of questions per round, so every interview has an end and a report.
  * The AI must answer in JSON. A reply we cannot parse is retried, then reported
    as an error. It is NEVER turned into a made-up score.
  * Numbers (average, grade) are computed here in code, not by the model.
  * Every answer belongs to a real, stored question (no "opener" the user answers blindly).

Nothing here touches the network or the database, so it is easy to test.
"""
import re

# ── Rounds ──────────────────────────────────────────────────────────────
ROUNDS = {
    "technical": {
        "name": "Technical Round",
        "questions": 5,
        "persona": (
            "You are a sharp but friendly senior developer interviewing a candidate. "
            "Ask practical technical questions based on the skills in the job description. "
            "Mix concept questions with 'how would you build/debug' questions. One question at a time."
        ),
        "intro": "Hi! This is a technical round. I'll ask 5 questions based on the role. Answer like you would in a real interview.",
    },
    "behavioral": {
        "name": "Behavioral Round",
        "questions": 5,
        "persona": (
            "You are a perceptive HR interviewer running a behavioral round. "
            "Ask for real past situations (teamwork, deadlines, conflict, failure, initiative). "
            "Encourage the STAR structure (Situation, Task, Action, Result). One question at a time."
        ),
        "intro": "Hi! This is a behavioral round: 5 questions about real situations. Use the STAR structure (Situation, Task, Action, Result).",
    },
    "hr": {
        "name": "HR Round",
        "questions": 5,
        "persona": (
            "You are a warm but sharp HR manager. Cover motivation, career goals, culture fit, "
            "strengths and weaknesses, availability and expectations. One question at a time."
        ),
        "intro": "Hi! This is an HR round: 5 questions about you, your goals and what you want from the role.",
    },
    "rapid": {
        "name": "Rapid Fire Round",
        "questions": 8,
        "persona": (
            "You run a rapid-fire round. Ask SHORT, direct questions that can be answered in one or two sentences "
            "(definitions, quick comparisons, 'what would you use for...'). One question at a time."
        ),
        "intro": "Rapid fire! 8 short questions, answer each in a sentence or two.",
    },
}

DEFAULT_ROUND = "technical"

NO_ACTIONS = (
    "Never write stage directions or action markers such as *laughs*, (pauses) or [smiles]. "
    "Plain spoken words only."
)

MIN_WORDS_FOR_FULL_SCORE = 3     # an answer shorter than this can never score above MAX_SCORE_TINY
MAX_SCORE_TINY = 2


class InterviewFormatError(ValueError):
    """The AI's reply was not in the required structure."""


def round_config(round_type: str) -> dict:
    return ROUNDS.get(round_type) or ROUNDS[DEFAULT_ROUND]


def total_questions(round_type: str) -> int:
    return round_config(round_type)["questions"]


def guess_role(jd: str, fallback: str = "the role") -> str:
    """A readable role label from the first non-empty line of the JD."""
    for line in (jd or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip(" -*#:\t")
        if 3 <= len(line) <= 80:
            return line
    return fallback


# ── Text hygiene ────────────────────────────────────────────────────────
_ACTION_RE = re.compile(r"\*[^*\n]{1,40}\*|\[[a-zA-Z ]{2,30}\]|\((?:laughs|smiles|chuckles|pauses|nods|sighs)[^)]*\)", re.IGNORECASE)


def clean_speech(text) -> str:
    text = _ACTION_RE.sub("", str(text or ""))
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# ── Prompts ─────────────────────────────────────────────────────────────
def _context(role, company, jd, resume):
    return (
        f"Role: {role}\nCompany: {company}\n\n"
        f"JOB DESCRIPTION:\n{(jd or '')[:2200]}\n\n"
        f"CANDIDATE RESUME (may be empty):\n{(resume or 'not provided')[:1800]}"
    )


def first_question_messages(round_type, role, company, jd, resume):
    cfg = round_config(round_type)
    system = (
        f"{cfg['persona']} {NO_ACTIONS} "
        'Return ONLY a JSON object: {"question": "<your first question>"}.'
    )
    user = f"Start the interview with your first question.\n\n{_context(role, company, jd, resume)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _transcript(turns):
    lines = []
    for t in turns:
        if t.get("answer") is None:
            continue
        lines.append(f"Q{t['n']}: {t['question']}\nAnswer: {t['answer']}\n(score given: {t.get('score')}/10)")
    return "\n\n".join(lines) or "(no earlier questions)"


def answer_messages(round_type, role, company, jd, resume, turns, answer, is_last):
    """`turns` = earlier turns plus the pending one (the last, answer=None)."""
    cfg = round_config(round_type)
    pending = turns[-1]
    n, total = pending["n"], cfg["questions"]
    system = (
        f"{cfg['persona']} {NO_ACTIONS}\n"
        "You are evaluating the candidate's answer. Be honest and specific; do NOT inflate scores.\n"
        "Scoring: 0-2 empty, off-topic or no substance; 3-4 weak or mostly wrong; 5-6 partly right but vague or incomplete; "
        "7-8 good and mostly complete; 9-10 excellent, specific and well structured.\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '{"reaction": "1-2 natural spoken sentences reacting to the answer",'
        ' "score": <integer 0-10>,'
        ' "feedback": "1-2 sentences: what was good and what was missing",'
        ' "model_answer_hint": "1-2 sentences on what a strong answer would include",'
        ' "next_question": "<the next question>"}\n'
        + (
            'This was the FINAL question: set "next_question" to an empty string.'
            if is_last else
            f'Question {n} of {total} was just answered. "next_question" must be a NEW question on a different topic '
            "that does not repeat any earlier question."
        )
    )
    user = (
        f"{_context(role, company, jd, resume)}\n\n"
        f"EARLIER QUESTIONS:\n{_transcript(turns[:-1])}\n\n"
        f"CURRENT QUESTION (Q{n}): {pending['question']}\n"
        f"CANDIDATE'S ANSWER: {answer}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def report_messages(round_type, role, company, turns):
    cfg = round_config(round_type)
    lines = []
    for t in turns:
        if t.get("answer") is None:
            continue
        lines.append(f"Q{t['n']} ({t.get('score')}/10): {t['question']}\nAnswer: {t['answer']}\nFeedback: {t.get('feedback', '')}")
    system = (
        "You are an interview coach writing a candid end-of-interview report. Use ONLY what is in the transcript. "
        "Return ONLY a JSON object: "
        '{"summary": "3-4 honest sentences", "strengths": ["up to 3 short items"], '
        '"improvements": ["up to 3 short, specific items"], "next_steps": ["up to 3 concrete practice actions"]}'
    )
    user = f"{cfg['name']} for {role} at {company}.\n\n" + "\n\n".join(lines)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ── Parsing / validation ────────────────────────────────────────────────
def parse_question(data) -> str:
    q = clean_speech((data or {}).get("question"))
    if len(q) < 8:
        raise InterviewFormatError("missing question")
    return q[:600]


def parse_evaluation(data, answer, is_last) -> dict:
    """Validate the model's JSON. Raises InterviewFormatError - never invents a score."""
    if not isinstance(data, dict):
        raise InterviewFormatError("not an object")
    raw = data.get("score")
    if isinstance(raw, bool) or raw is None:
        raise InterviewFormatError("missing score")
    if isinstance(raw, str):
        m = re.match(r"\s*(\d+(?:\.\d+)?)", raw)
        if not m:
            raise InterviewFormatError("bad score")
        raw = float(m.group(1))
    try:
        score = int(round(float(raw)))
    except (TypeError, ValueError):
        raise InterviewFormatError("bad score")
    score = max(0, min(10, score))
    if len(str(answer or "").split()) < MIN_WORDS_FOR_FULL_SCORE:
        score = min(score, MAX_SCORE_TINY)   # a few words cannot earn real credit

    feedback = clean_speech(data.get("feedback"))
    if not feedback:
        raise InterviewFormatError("missing feedback")
    next_q = clean_speech(data.get("next_question"))
    if is_last:
        next_q = ""
    elif len(next_q) < 8:
        raise InterviewFormatError("missing next question")
    return {
        "reaction": clean_speech(data.get("reaction"))[:400],
        "score": score,
        "feedback": feedback[:500],
        "hint": clean_speech(data.get("model_answer_hint"))[:500],
        "next_question": next_q[:600],
    }


# ── Numbers computed in code ────────────────────────────────────────────
def average(scores):
    return round(sum(scores) / len(scores), 1) if scores else None


def grade_for(avg):
    if avg is None:
        return None
    return "Excellent" if avg >= 8 else "Good" if avg >= 6 else "Needs work" if avg >= 4 else "Keep practising"


def _short(text, n=140):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build_report(turns, narrative=None) -> dict:
    """Final report. Numbers come from `turns`; wording from `narrative` (may be None)."""
    answered = [t for t in turns if t.get("answer") is not None]
    scores = [t["score"] for t in answered if isinstance(t.get("score"), int)]
    avg = average(scores)
    narrative = narrative if isinstance(narrative, dict) else {}

    def items(key):
        value = narrative.get(key)
        if not isinstance(value, list):
            return []
        return [clean_speech(x)[:240] for x in value if isinstance(x, str) and clean_speech(x)][:3]

    strengths, improvements, next_steps = items("strengths"), items("improvements"), items("next_steps")
    summary = clean_speech(narrative.get("summary"))[:900]

    # Fallbacks built from the stored per-answer feedback, so a report always exists.
    ranked = sorted(answered, key=lambda t: t.get("score", 0), reverse=True)
    if not strengths:
        strengths = [f"Q{t['n']}: {_short(t.get('feedback'))}" for t in ranked if t.get("score", 0) >= 7][:3]
    if not improvements:
        improvements = [f"Q{t['n']}: {_short(t.get('hint') or t.get('feedback'))}" for t in reversed(ranked) if t.get("score", 10) <= 6][:3]
    if not next_steps and answered:
        weakest = min(answered, key=lambda t: t.get("score", 10))
        next_steps = [f"Re-answer Q{weakest['n']} out loud using the hint, then record yourself and compare."]
    if not summary:
        summary = (
            f"You answered {len(answered)} question{'s' if len(answered) != 1 else ''} "
            f"with an average score of {avg if avg is not None else '-'}/10."
        )

    return {
        "average_score": avg,
        "grade": grade_for(avg),
        "answered": len(answered),
        "summary": summary,
        "strengths": strengths,
        "improvements": improvements,
        "next_steps": next_steps,
    }
