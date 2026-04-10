# Meeting Intelligence Hub - Approach

## 1. Problem Statement
Organizations run many internal and client meetings each week, producing long transcripts that are difficult to navigate. Teams struggle to quickly find decisions, assigned actions, and reasoning behind key outcomes. This leads to repeated clarification requests, delayed execution, and reduced operational efficiency.

## 2. Solution Overview
Meeting Intelligence Hub transforms raw meeting transcripts into structured, searchable intelligence.

The system:
- Ingests transcript files (`.txt`, `.vtt`)
- Extracts decisions and action items
- Supports citation-backed Q&A over meeting content
- Preserves usability even when external LLM quota is limited (fallback mode)

The design goal is to reduce "double work" by making meeting knowledge instantly accessible.

## 3. System Design
### End-to-End Pipeline
1. Input: one or more transcript files are uploaded.
2. Preprocessing: transcripts are parsed, cleaned, and segmented.
3. Chunking: text is split into smaller overlapping chunks for retrieval.
4. Embedding: chunk vectors are generated using Sentence Transformers.
5. Indexing: vectors are stored in a FAISS index; metadata is stored in SQLite.
6. Retrieval: user query is matched against top-k relevant chunks.
7. Response Generation:
   - Fast heuristic path (always available), or
   - LLM-assisted generation using Gemini when available.
8. Output: concise answer with source citations.

### Processing Modes
- Upload-time insights: fast heuristic draft for responsiveness.
- Background enhancement: optional Gemini refinement when quota/service is available.
- Chat-time resilience: quota-aware cooldown and immediate fallback to avoid long waits.

## 4. Tech Stack
- Backend: Flask
- Embeddings: Sentence Transformers
- Vector Search: FAISS
- Metadata Store: SQLite
- LLM: Gemini API (optional)
- Frontend: HTML, CSS, JavaScript

## 5. Key Design Decisions
- FAISS over managed vector DB: zero external setup, low-latency local retrieval for MVP.
- SQLite over MongoDB: lightweight persistence suitable for single-node prototype workflows.
- Optional LLM integration: system remains functional without API key or during quota exhaustion.
- RAG-first architecture: improves answer grounding and enables source citations.
- Fast fallback strategy: prioritizes predictable response time under external model instability.

## 6. Core Features Delivered
- Multi-transcript ingestion (`.txt`, `.vtt`)
- Decision and action item extraction
- RAG-based chatbot with citations
- Speaker metadata extraction
- Sentiment timeline and speaker sentiment summary
- Cross-meeting context querying

## 7. Future Improvements
- Persistent vector storage (for example, Pinecone/Weaviate) and index reload on restart
- Transformer-based sentiment and richer tone classification
- Authentication and multi-user access control
- Background job queue hardening (Redis/Celery or equivalent)
- Containerized deployment and cloud-ready scaling
- Export features (CSV/PDF) for decisions and action items

## 8. Success Criteria
The approach is successful if teams can:
- Upload transcripts and receive structured insights quickly
- Ask operational questions and get grounded answers with citations
- Reduce clarification churn and improve execution speed after meetings
