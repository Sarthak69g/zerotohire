# AutoApply AI

AutoApply AI turns a job description and resume into a targeted application kit:

- job description and ATS analysis
- resume match score
- rewritten resume
- cover letter
- interview prep kit
- DOCX/PDF exports
- saved run history
- local candidate profiles
- job URL import

The app uses Flask, CrewAI, and Groq. The frontend is a single `index.html` file that streams progress from the Flask backend.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Add your real Groq key to `.env`:

```env
GROQ_API_KEY=your_real_key_here
```

## Run

```powershell
python main.py
```

Then open:

- `http://127.0.0.1:5000` (served by Flask), or
- `index.html` directly.

## How It Works

The backend always runs the first two stages:

1. JD Reader
2. Resume Analyser

Then it runs only the outputs selected in the UI:

3. Resume Rewriter
4. Cover Letter Writer
5. Interview Coach

Each stage receives the previous stage output, so the agents now work as a real pipeline instead of isolated one-shot calls.

## Product Features

- Save candidate profiles locally in `profiles/`.
- Import public job pages with the Job URL Import panel.
- Review previous runs from the Run History panel.
- Inspect the ATS keyword heatmap after each run.
- Download TXT files in the browser, or DOCX/PDF files from the Flask backend.

## Output Files

Each completed run is saved under `outputs/` in a timestamped folder with `metadata.json`, TXT outputs, and generated DOCX/PDF exports when the export libraries are installed.

## Notes

- PDF upload requires `pdfplumber`, included in `requirements.txt`.
- `.env` is ignored by Git so your API key stays local.
- `profiles/` is ignored by Git because it can contain personal resume data.
- Set `FLASK_DEBUG=true` only while developing.

## Deployment

The project is ready for single-service deployment (backend + frontend in one Flask app).

### Required environment variables

- `GROQ_API_KEY`
- `GROQ_MODEL` (optional, default already set in code)
- `FRONTEND_ORIGIN` (use your deployed domain, or `*` if needed)
- `PORT` (set by platform automatically)

### Render / Railway style deploy

This repo now includes [render.yaml](C:/Users/SARTHAK/OneDrive/AutoApplyAI/render.yaml) for Render blueprint deploy.

1. Push this folder to GitHub.
2. Create a new Web Service from the repo.
3. Build command:

```bash
pip install -r requirements.txt
```

4. Start command:

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 8 --timeout 180
```

5. Add environment variables in the platform dashboard.
6. Deploy and open the service URL.
