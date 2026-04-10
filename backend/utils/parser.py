import io
import re


def _read_text(stream):
    raw = stream.read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="ignore")
    return raw


def parse_vtt(text):
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper() == "WEBVTT":
            continue
        if "-->" in stripped:
            continue
        if re.fullmatch(r"\d+", stripped):
            continue
        cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines)


def parse_uploaded_file(file_stream, extension):
    if isinstance(file_stream, io.BytesIO):
        file_stream.seek(0)
    text = _read_text(file_stream)

    if extension == "vtt":
        return parse_vtt(text)

    return text
