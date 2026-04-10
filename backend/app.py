import os
import threading
import time
import io
import csv
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file

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
    storage.update_meeting_insights(meeting_id, enhanced)
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


def _collect_export_items(meeting_id=None):
    if meeting_id:
        ids = [meeting_id] if meeting_id in meeting_insights else []
    else:
        ids = list(meeting_insights.keys())

    decisions = []
    action_items = []
    for mid in ids:
        insights = meeting_insights.get(mid) or {}
        record = meetings.get(mid) or {}
        filename = record.get("filename") or mid

        for decision in insights.get("decisions", []):
            decisions.append({"meeting_id": mid, "filename": filename, "decision": decision})

        for item in insights.get("action_items", []):
            action_items.append(
                {
                    "meeting_id": mid,
                    "filename": filename,
                    "person": item.get("person", ""),
                    "task": item.get("task", ""),
                    "deadline": item.get("deadline", ""),
                }
            )

    return decisions, action_items


def _build_csv_export(meeting_id=None):
    decisions, action_items = _collect_export_items(meeting_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["Decisions"])
    writer.writerow(["meeting_id", "filename", "decision"])
    for row in decisions:
        writer.writerow([row["meeting_id"], row["filename"], row["decision"]])

    writer.writerow([])
    writer.writerow(["Action Items"])
    writer.writerow(["meeting_id", "filename", "person", "task", "deadline"])
    for row in action_items:
        writer.writerow([row["meeting_id"], row["filename"], row["person"], row["task"], row["deadline"]])

    csv_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    csv_bytes.seek(0)
    return csv_bytes


def _build_pdf_export(meeting_id=None):
    try:
        pagesizes = importlib.import_module("reportlab.lib.pagesizes")
        pdfgen = importlib.import_module("reportlab.pdfgen")
        LETTER = pagesizes.LETTER
        canvas = pdfgen.canvas
    except Exception:
        decisions, action_items = _collect_export_items(meeting_id)
        lines = ["Meeting Intelligence Hub - Export", "", "Decisions"]
        lines.extend([f"[{row['filename']}] {row['decision']}" for row in decisions])
        lines.extend(["", "Action Items"])
        lines.extend(
            [
                f"[{row['filename']}] {row['person']} | {row['task']} | {row['deadline']}"
                for row in action_items
            ]
        )

        # Minimal PDF generator fallback (single page, Helvetica).
        escaped_lines = []
        for line in lines[:42]:
            escaped = (
                str(line)
                .replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
            )
            escaped_lines.append(escaped[:110])

        stream_parts = ["BT", "/F1 11 Tf", "40 770 Td", "14 TL"]
        first = True
        for line in escaped_lines:
            if first:
                stream_parts.append(f"({line}) Tj")
                first = False
            else:
                stream_parts.append("T*")
                stream_parts.append(f"({line}) Tj")
        stream_parts.append("ET")
        stream = "\n".join(stream_parts).encode("utf-8")

        objs = []
        objs.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
        objs.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
        objs.append(
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        )
        objs.append(b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
        objs.append(
            f"5 0 obj << /Length {len(stream)} >> stream\n".encode("utf-8")
            + stream
            + b"\nendstream endobj\n"
        )

        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objs:
            offsets.append(len(pdf))
            pdf.extend(obj)
        xref_start = len(pdf)
        pdf.extend(f"xref\n0 {len(offsets)}\n".encode("utf-8"))
        pdf.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            pdf.extend(f"{off:010d} 00000 n \n".encode("utf-8"))
        pdf.extend(
            f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode(
                "utf-8"
            )
        )
        return io.BytesIO(pdf)

    decisions, action_items = _collect_export_items(meeting_id)
    packet = io.BytesIO()
    pdf = canvas.Canvas(packet, pagesize=LETTER)
    width, height = LETTER
    y = height - 40

    def write_line(text, step=16):
        nonlocal y
        if y < 50:
            pdf.showPage()
            y = height - 40
        pdf.drawString(40, y, text[:120])
        y -= step

    pdf.setFont("Helvetica-Bold", 14)
    write_line("Meeting Intelligence Hub - Export", step=22)

    pdf.setFont("Helvetica-Bold", 12)
    write_line("Decisions")
    pdf.setFont("Helvetica", 10)
    for row in decisions:
        write_line(f"[{row['filename']}] {row['decision']}")

    y -= 8
    pdf.setFont("Helvetica-Bold", 12)
    write_line("Action Items")
    pdf.setFont("Helvetica", 10)
    for row in action_items:
        write_line(f"[{row['filename']}] {row['person']} | {row['task']} | {row['deadline']}")

    pdf.save()
    packet.seek(0)
    return packet


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


@app.route("/api/dashboard", methods=["GET"])
def dashboard_stats():
    summary = storage.dashboard_summary()

    return jsonify(
        {
            "stats": {
                "total_meetings": summary["total_meetings"],
                "total_projects": summary["total_projects"],
                "total_words": summary["total_words"],
                "total_action_items": summary["total_action_items"],
            },
            "meetings": summary["recent_meetings"],
        }
    )


@app.route("/api/export", methods=["GET"])
def export_insights():
    export_format = (request.args.get("format") or "csv").strip().lower()
    meeting_id = (request.args.get("meeting_id") or "").strip() or None

    if meeting_id and meeting_id not in meeting_insights:
        return jsonify({"error": "Unknown meeting_id for export"}), 404

    if export_format == "csv":
        payload = _build_csv_export(meeting_id)
        filename = f"meeting_insights_{meeting_id or 'all'}.csv"
        return send_file(payload, as_attachment=True, download_name=filename, mimetype="text/csv")

    if export_format == "pdf":
        payload = _build_pdf_export(meeting_id)
        if payload is None:
            return jsonify({"error": "PDF export requires reportlab package"}), 400
        filename = f"meeting_insights_{meeting_id or 'all'}.pdf"
        return send_file(payload, as_attachment=True, download_name=filename, mimetype="application/pdf")

    return jsonify({"error": "Unsupported export format. Use csv or pdf"}), 400


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
            "insights": insights,
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
