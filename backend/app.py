import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, jsonify, render_template, request

from utils.analytics import analyze_sentiment, extract_speaker_metadata
from utils.extractor import enhance_insights_with_gemini, extract_insights_fast
from utils.parser import parse_uploaded_file
from utils.rag import RAGEngine
from utils.storage import MeetingStorage


def _load_env_file(file_path):
    if not file_path.exists():
        return

    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_environment():
    project_root = Path(__file__).resolve().parent.parent
    # Prefer .env for local secrets; fallback to .env.example for quick demos.
    _load_env_file(project_root / ".env")
    _load_env_file(project_root / ".env.example")


load_environment()

app = Flask(__name__, template_folder="templates", static_folder="static")
rag_engine = RAGEngine()
meetings = {}
meeting_insights = {}
meeting_enhancement_state = {}
enhancement_lock = threading.Lock()
enhancement_workers = int(os.getenv("GEMINI_ENHANCEMENT_WORKERS", "2"))
enhancement_executor = ThreadPoolExecutor(max_workers=max(1, enhancement_workers))
project_root = Path(__file__).resolve().parent.parent
storage = MeetingStorage(project_root / "data" / "meetings.db")


def _set_enhancement_state(meeting_id, status, error=None):
    with enhancement_lock:
        meeting_enhancement_state[meeting_id] = {
            "status": status,
            "error": error,
            "updated_at": int(time.time()),
        }


def _run_gemini_enhancement(meeting_id, transcript_text):
    _set_enhancement_state(meeting_id, "processing")

    enhanced = enhance_insights_with_gemini(transcript_text)
    if not enhanced:
        _set_enhancement_state(meeting_id, "failed", error="Gemini unavailable or quota exhausted")
        return

    with enhancement_lock:
        meeting_insights[meeting_id] = enhanced
    _set_enhancement_state(meeting_id, "completed")


def _schedule_gemini_enhancement(meeting_id, transcript_text):
    _set_enhancement_state(meeting_id, "queued")
    enhancement_executor.submit(_run_gemini_enhancement, meeting_id, transcript_text)


def build_metadata(text, filename, speaker_meta=None):
    words = [w for w in text.split() if w.strip()]
    lines = [line for line in text.splitlines() if line.strip()]
    metadata = {
        "filename": filename,
        "word_count": len(words),
        "line_count": len(lines),
        "character_count": len(text),
    }

    if speaker_meta:
        metadata["speaker_count"] = speaker_meta.get("speaker_count", 0)
        metadata["speakers"] = speaker_meta.get("speakers", [])

    return metadata


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/meetings", methods=["GET"])
def list_meetings():
    project = (request.args.get("project") or "").strip() or None
    meeting_date = (request.args.get("meeting_date") or "").strip() or None
    ordered = storage.list_meetings(project=project, meeting_date=meeting_date)
    return jsonify({"meetings": ordered})


@app.route("/api/insights_status", methods=["GET"])
def insights_status():
    raw_ids = (request.args.get("meeting_ids") or "").strip()
    if not raw_ids:
        return jsonify({"updates": {}})

    meeting_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    updates = {}
    with enhancement_lock:
        for meeting_id in meeting_ids:
            state = meeting_enhancement_state.get(meeting_id, {"status": "unknown", "error": None, "updated_at": int(time.time())})
            updates[meeting_id] = {
                "meeting_id": meeting_id,
                "enhancement_status": state.get("status", "unknown"),
                "enhancement_error": state.get("error"),
                "updated_at": state.get("updated_at"),
                "insights": meeting_insights.get(meeting_id),
            }

    return jsonify({"updates": updates})


@app.route("/api/upload", methods=["POST"])
def upload_transcript():
    incoming_files = request.files.getlist("files")
    if not incoming_files:
        incoming_files = request.files.getlist("file")

    incoming_files = [file_obj for file_obj in incoming_files if file_obj and file_obj.filename]
    if not incoming_files:
        return jsonify({"error": "No files selected. Use form-data field files"}), 400

    project = (request.form.get("project") or "General").strip() or "General"
    meeting_date = (request.form.get("meeting_date") or "").strip()
    if not meeting_date:
        meeting_date = time.strftime("%Y-%m-%d")

    uploads = []
    base_uploaded_at = int(time.time())
    inter_request_delay = float(os.getenv("GEMINI_INTER_REQUEST_DELAY_SECONDS", "0.2"))

    for index, incoming_file in enumerate(incoming_files):
        if index > 0 and inter_request_delay > 0:
            time.sleep(inter_request_delay)

        filename = incoming_file.filename
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext not in {"txt", "vtt"}:
            return jsonify({"error": f"Unsupported file type for {filename}. Only .txt and .vtt files are supported"}), 400

        try:
            transcript_text = parse_uploaded_file(incoming_file.stream, ext)
        except Exception as exc:
            return jsonify({"error": f"Failed to parse {filename}: {exc}"}), 400

        if not transcript_text.strip():
            return jsonify({"error": f"Transcript {filename} is empty after parsing"}), 400

        uploaded_at = base_uploaded_at + index
        meeting_id = f"{project.replace(' ', '_')}-{meeting_date}-{uploaded_at}-{filename.replace(' ', '_')}"

        speaker_meta = extract_speaker_metadata(transcript_text)
        metadata = build_metadata(transcript_text, filename, speaker_meta=speaker_meta)
        insights = extract_insights_fast(transcript_text)
        sentiment = analyze_sentiment(transcript_text)

        rag_engine.add_document(
            meeting_id=meeting_id,
            text=transcript_text,
            metadata={
                "filename": filename,
                "project": project,
                "meeting_date": meeting_date,
            },
        )

        meeting_record = {
            "meeting_id": meeting_id,
            "filename": filename,
            "project": project,
            "meeting_date": meeting_date,
            "uploaded_at": uploaded_at,
            "metadata": metadata,
        }
        meetings[meeting_id] = meeting_record
        with enhancement_lock:
            meeting_insights[meeting_id] = insights
        storage.save_meeting(meeting_record)

        _schedule_gemini_enhancement(meeting_id, transcript_text)

        uploads.append(
            {
                "meeting_id": meeting_id,
                "filename": filename,
                "project": project,
                "meeting_date": meeting_date,
                "metadata": metadata,
                "insights": insights,
                "enhancement_status": "queued",
                "sentiment": sentiment,
                "preview": transcript_text[:600],
            }
        )

    first_upload = uploads[0]
    return jsonify(
        {
            "uploads": uploads,
            "count": len(uploads),
            "project": project,
            "meeting_date": meeting_date,
            # Backward compatibility for existing single-file frontend behavior.
            "meeting_id": first_upload["meeting_id"],
            "metadata": first_upload["metadata"],
            "insights": first_upload["insights"],
            "enhancement_status": first_upload["enhancement_status"],
            "sentiment": first_upload["sentiment"],
            "preview": first_upload["preview"],
        }
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    meeting_id = (data.get("meeting_id") or "").strip() or None
    history = data.get("history") or []

    if not question:
        return jsonify({"error": "Question is required"}), 400

    if not rag_engine.has_documents():
        return jsonify({"error": "Upload a transcript first"}), 400

    if meeting_id and meeting_id not in meetings:
        return jsonify({"error": "Unknown meeting_id"}), 404

    if not isinstance(history, list):
        history = []

    # Keep only recent turns and avoid oversized payloads.
    compact_history = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        compact_history.append({"role": role, "content": content[:600]})

    answer = rag_engine.answer(
        question=question,
        meeting_filter=meeting_id,
        chat_history=compact_history,
    )
    return jsonify(answer)


if __name__ == "__main__":
    app.run(debug=True)
