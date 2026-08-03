# Setup

Local development setup. All services run on the host; n8n runs in Docker and
reaches them via `host.docker.internal`.

## System dependencies

Ubuntu / WSL2:

```bash
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-tur poppler-utils libreoffice
```

- **Tesseract** — OCR. The `-tur` package supplies Turkish language data.
- **poppler-utils** — PDF rasterization, used by `pdf2image`.
- **LibreOffice** — DOCX → PDF conversion during assembly. The code looks for
  either `soffice` or `libreoffice` on PATH.

## Python environment

Python 3.11.

```bash
conda create -n abclink python=3.11
conda activate abclink
pip install -r requirements.txt
python -m spacy download en_core_web_lg
```

The spaCy model is a separate download and is required — Presidio's NER
recognizers will not load without it.

## Environment variables

Create a `.env` file (gitignored): GEMINI_API_KEY="your_key_here"

## Running the services

Each service runs independently. Five terminals, or a process manager:

```bash
uvicorn pii_service:app      --port 8001 --reload
uvicorn ocr_service:app      --port 8002 --reload
uvicorn redact_service:app   --port 8003 --reload
uvicorn anonymize_service:app --port 8004 --reload
uvicorn assemble_service:app --port 8005 --reload
```

Each exposes interactive docs at `http://localhost:<port>/docs`.

`scan_service.py` (port 8000) is not part of the active pipeline — see Phase 2
in the README.

## n8n

n8n Self-Hosted v2.27.4 in Docker. Import
`Legal Document Translation Workflow.json` via **Workflows → Import from File**.

Credentials are referenced by ID and are not included. You will need to supply
your own for Google Drive, Google Sheets, Gmail, and the Gemini API.

The workflow also references specific Google Drive folder and Sheet IDs. Replace
these with your own in the Drive, Sheets, and Gmail nodes.

## Verifying the install

```bash
tesseract --version
tesseract --list-langs      # should include 'tur'
soffice --version
python -c "import spacy; spacy.load('en_core_web_lg')"
```
