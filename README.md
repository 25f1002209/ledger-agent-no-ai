# Ledger Agent without AI token

This project uses Python rules and regular expressions only. No OpenAI, AI Pipe, or other LLM token is needed.

## Configure
Copy `.env.example` to `.env`. Set `LEDGER_API_URL` to the complete assignment-specific `questionData` URL with `path=/`. Keep its query parameters intact. Do not commit `.env`.

## Run locally (Windows PowerShell)
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python explore_api.py
uvicorn app:app --reload
```
Test `http://127.0.0.1:8000/docs` with `POST /answer`.

## Deploy on Render
Push the six project files plus `render.yaml` to GitHub, excluding `.env`. Create a Render Web Service connected to the repo.
Build command: `pip install -r requirements.txt`
Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
Add environment variable `LEDGER_API_URL` with the complete assignment URL, and `UPSTREAM_TIMEOUT_SECONDS=8`.
After deploy, test `/health`, then `/docs`.

## Limits
A rule-based parser cannot understand every unseen paraphrase. The API field names, pagination, currency-rate conventions, and refund structure must be verified against your own assignment API output. The parser recognizes common month/year and region phrasing and matches product/customer names present in fetched orders. Extend rules after testing.
