# Meeting Intelligence Hub (MVP)

A lightweight AI system that transforms meeting transcripts into actionable insights and enables intelligent search through conversational Q&A.

## Features

- Upload `.txt` or `.vtt` transcript files
- Automatic extraction of:
  - Decisions
  - Action items (person, task, deadline)
- RAG chatbot over uploaded transcript content
- Source citations returned with each answer

## Tech

- Backend: Flask
- Retrieval: Sentence Transformers + FAISS (with keyword fallback)
- LLM (optional): Gemini API for extraction and answer generation
- Frontend: HTML, CSS, vanilla JS

## Architecture

1. Transcript is uploaded and parsed
2. Text is chunked into smaller segments
3. Embeddings are generated using Sentence Transformers
4. Stored in FAISS vector index for similarity search
5. User query is embedded and matched with relevant chunks
6. Retrieved context is passed to LLM (or fallback logic)
7. Final answer is generated with source citations

Why this matters:
- Recruiters scan for system thinking
- Shows you understand RAG pipeline clearly

## Design Decisions

- Used FAISS for fast local vector search instead of external DB
- SQLite used for lightweight metadata storage in MVP
- Heuristic extraction used for fast fallback without LLM
- Gemini integration is optional to handle cost and quota limits

## Limitations

- In-memory FAISS index (not persistent across restarts)
- Basic sentiment and extraction without fine-tuned models
- Not optimized for large-scale production workloads

## Quick Start

1. Create a virtual environment and activate it.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Optional: Configure API key for stronger extraction and responses.

```bash
cp .env.example .env
```

Then set `GEMINI_API_KEY` in `.env`. The backend auto-loads `.env` on startup.

Optional reliability settings for Gemini:

- `GEMINI_CHAT_MODEL=gemini-1.5-flash`
- `GEMINI_MODEL_CANDIDATES=gemini-1.5-flash,gemini-1.5-pro,gemini-2.0-flash,gemini-2.5-flash`
- `GEMINI_RETRIES_PER_MODEL=5`
- `GEMINI_RETRY_BACKOFF_SECONDS=1.0`
- `GEMINI_INTER_REQUEST_DELAY_SECONDS=0.25`
- `GEMINI_MAX_INPUT_TOKENS=1500`
- `GEMINI_MAX_OUTPUT_TOKENS=512`
- `GEMINI_CONTEXT_CHUNKS=2`
- `GEMINI_MAX_CHARS_PER_CHUNK=260`
- `GEMINI_EXTRACT_MAX_CHARS=6000`
- `GEMINI_RESPONSE_CACHE_ENABLED=true`
- `GEMINI_RESPONSE_CACHE_TTL_SECONDS=90`
- `GEMINI_RESPONSE_CACHE_MAX_ENTRIES=128`
- `GEMINI_ENHANCEMENT_WORKERS=2`
- `GEMINI_FAST_FALLBACK_ON_QUOTA=true`
- `GEMINI_QUOTA_COOLDOWN_SECONDS=120`

Chat requests include only compact recent history, and the backend summarizes it before Gemini calls to avoid prompt bloat.
Repeated questions within the cache window can be served from an in-memory response cache to reduce duplicate Gemini calls.
Upload returns fast heuristic draft insights first, then upgrades them asynchronously with Gemini in the background.
Chat can skip expensive provider retries during quota exhaustion and fall back immediately during a short cooldown window.
Set cooldown to `60` for aggressive retry back to Gemini, or `120-300` for stable fallback behavior under prolonged quota exhaustion.

4. Run the app:

```bash
cd backend
python app.py
```

5. Open:

- `http://127.0.0.1:5000`

## API Endpoints

- `GET /health`
- `POST /api/upload` (form-data: `file`)
- `POST /api/chat` (JSON: `question`, optional `meeting_id`)
- `GET /api/meetings`
- `GET /api/insights_status?meeting_ids=<id1,id2,...>`

## Demo Flow

1. Upload transcript
2. Show extracted decisions and action items
3. Ask a question like: `Why was launch delayed?`
4. Show answer and source chunks

## Notes

- If no LLM key is set, the app still works with heuristic extraction and retrieval-based responses.
- First embedding call may download model files.
