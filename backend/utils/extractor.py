import json
import os
import re
import time

from utils.gemini_shared import (
    cap_text_tokens,
    extract_retry_seconds,
    is_model_not_found_error,
    is_retryable_error,
    rate_limit_sleep,
)


def _restore_acronyms(text):
    if not text:
        return text

    acronyms = ["api", "qa", "ui", "ux", "faq", "kpi", "okr", "sla"]
    normalized = text
    for acronym in acronyms:
        normalized = re.sub(
            rf"\b{acronym}\b",
            acronym.upper(),
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def _extract_json_block(content):
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    return stripped


def _split_sentences(text):
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]


def _extract_decisions(sentences):
    decision_markers = [
        "decide",
        "decided",
        "decision",
        "agreed",
        "approved",
        "finalized",
        "confirmed",
    ]

    decisions = []
    for sentence in sentences:
        lower_sentence = sentence.lower()
        if any(marker in lower_sentence for marker in decision_markers):
            decisions.append(sentence)
    return decisions[:8]


def _normalize_decisions(decisions):
    normalized = []
    seen = set()

    for decision in decisions:
        if not isinstance(decision, str):
            continue

        text = decision.strip()
        if not text:
            continue

        # Remove transcript-title prefixes before a speaker label, e.g.
        # "Product Readiness Sync - April 10 Nina: ..." -> "Nina: ..."
        speaker_label_match = re.search(r"\b([A-Z][a-zA-Z]+:\s*)", text)
        if speaker_label_match and speaker_label_match.start() > 0:
            text = text[speaker_label_match.start() :]

        # Remove speaker labels like "Mina:" or "Aisha:".
        text = re.sub(r"^[A-Z][a-zA-Z]+:\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = text.rstrip(".")

        lowered = text.lower()
        reviewed_agreed_match = re.match(r"^we reviewed .* and agreed to (.+)$", text, flags=re.IGNORECASE)
        if reviewed_agreed_match:
            agreed_tail = reviewed_agreed_match.group(1).strip()
            text = f"The team agreed to {agreed_tail}"
            lowered = text.lower()

        if lowered.startswith("we decided"):
            text = "The team decided" + text[10:]
        elif lowered.startswith("we agreed"):
            text = "The team agreed" + text[9:]
        elif lowered.startswith("decided"):
            text = "The team decided " + text[7:].lstrip()
        elif lowered.startswith("agreed"):
            text = "The team agreed " + text[6:].lstrip()

        if text:
            text = text[0].upper() + text[1:]
            text = _restore_acronyms(text)
            text = f"{text}."

        key = text.lower()
        if not text or key in seen:
            continue

        seen.add(key)
        normalized.append(text)

    return normalized


def _extract_action_items(sentences):
    action_items = []
    seen = set()

    def clean_task(task_text):
        cleaned = task_text.strip().rstrip(".")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned

    def extract_deadline(task_text):
        deadline_match = re.search(r"\bby\s+([^.,;]+)", task_text, flags=re.IGNORECASE)
        if not deadline_match:
            return "Not specified"
        deadline_raw = deadline_match.group(1).strip()
        deadline_raw = re.split(r"\s+(and|but)\s+", deadline_raw, maxsplit=1, flags=re.IGNORECASE)[0]

        common_day_match = re.match(
            r"^(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            deadline_raw,
            flags=re.IGNORECASE,
        )
        if common_day_match:
            return common_day_match.group(1).capitalize()

        return deadline_raw

    for sentence in sentences:
        text = sentence.strip()

        speaker_match = re.match(r"^([A-Z][a-zA-Z]+):\s*(.+)$", text)
        if not speaker_match:
            continue

        speaker = speaker_match.group(1)
        body = speaker_match.group(2).strip()

        owner = None
        task_text = None

        action_prefix_match = re.match(
            r"^Action\s*item\s*:\s*([A-Z][a-zA-Z]+)\s+will\s+(.+)$",
            body,
            flags=re.IGNORECASE,
        )
        if action_prefix_match:
            owner = action_prefix_match.group(1)
            task_text = action_prefix_match.group(2)
        else:
            i_will_match = re.match(r"^I\s+will\s+(.+)$", body, flags=re.IGNORECASE)
            if i_will_match:
                owner = speaker
                task_text = i_will_match.group(1)
            else:
                named_will_match = re.match(r"^([A-Z][a-zA-Z]+)\s+will\s+(.+)$", body)
                if named_will_match:
                    owner = named_will_match.group(1)
                    task_text = named_will_match.group(2)

        if not owner or not task_text:
            continue

        deadline = extract_deadline(task_text)
        task = task_text
        if deadline != "Not specified":
            task = re.sub(
                rf"\s+by\s+{re.escape(deadline)}\b",
                "",
                task,
                count=1,
                flags=re.IGNORECASE,
            )
        task = clean_task(task)

        signature = (owner.lower(), task.lower(), deadline.lower())
        if signature in seen:
            continue
        seen.add(signature)

        action_items.append(
            {
                "person": owner,
                "task": task,
                "deadline": deadline,
            }
        )

    return action_items[:12]


def _normalize_action_items(action_items):
    normalized = []

    def sentence_case(text):
        if not text:
            return text
        return text[0].upper() + text[1:]

    def normalize_deadline(deadline_text):
        text = str(deadline_text or "Not specified").strip()
        if not text:
            return "Not specified"

        text = re.sub(r"^by\s+", "", text, flags=re.IGNORECASE)
        text = text.rstrip(" .;,")

        common_day_match = re.match(
            r"^(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text,
            flags=re.IGNORECASE,
        )
        if common_day_match:
            return common_day_match.group(1).capitalize()

        return text or "Not specified"

    for item in action_items or []:
        person = str((item or {}).get("person") or "Unassigned").strip() or "Unassigned"
        task = str((item or {}).get("task") or "").strip()
        deadline = normalize_deadline((item or {}).get("deadline"))

        task = re.sub(r"\s+", " ", task).strip().rstrip(" .;,")
        if not task:
            continue
        task = sentence_case(task)
        task = _restore_acronyms(task)

        normalized.append(
            {
                "person": person,
                "task": task,
                "deadline": deadline,
            }
        )

    return normalized


def _fallback_extract(text):
    sentences = _split_sentences(text)
    return {
        "decisions": _extract_decisions(sentences),
        "action_items": _extract_action_items(sentences),
        "source": "heuristic",
    }


def _llm_extract(text):
    return _gemini_extract(text)


def extract_insights_fast(text):
    """Fast path for uploads: return heuristic draft immediately."""
    fallback = _fallback_extract(text)
    fallback["decisions"] = _normalize_decisions(fallback.get("decisions", []))
    fallback["action_items"] = _normalize_action_items(fallback.get("action_items", []))
    fallback["source"] = "heuristic"
    return fallback


def enhance_insights_with_gemini(text):
    """Background path: try Gemini only, return None when unavailable."""
    llm_result = None
    try:
        llm_result = _llm_extract(text)
    except Exception:
        llm_result = None

    if not llm_result:
        return None

    llm_result["decisions"] = _normalize_decisions(llm_result.get("decisions", []))
    llm_result["action_items"] = _normalize_action_items(llm_result.get("action_items", []))
    return llm_result


def _model_candidates():
    configured = os.getenv("GEMINI_MODEL_CANDIDATES", "").strip()
    if configured:
        models = [model.strip() for model in configured.split(",") if model.strip()]
        if models:
            return models

    primary = os.getenv("GEMINI_CHAT_MODEL", "gemini-1.5-flash")
    return [
        primary,
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
    ]


def _gemini_extract(text):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
    except Exception:
        return None

    client = genai.Client(api_key=api_key)
    retries_per_model = int(os.getenv("GEMINI_RETRIES_PER_MODEL", "5"))
    retry_backoff_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_SECONDS", "1.0"))
    inter_request_delay = float(os.getenv("GEMINI_INTER_REQUEST_DELAY_SECONDS", "0.25"))
    max_input_tokens = int(os.getenv("GEMINI_MAX_INPUT_TOKENS", "1500"))
    max_output_tokens = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "512"))
    max_transcript_chars = int(os.getenv("GEMINI_EXTRACT_MAX_CHARS", "6000"))

    prompt = (
        "Extract decisions and action items from this meeting transcript. "
        "Return strict JSON only with schema: "
        "{\"decisions\": [string], \"action_items\": [{\"person\": string, \"task\": string, \"deadline\": string}]}.\n\n"
        f"Transcript:\n{text[:max_transcript_chars]}"
    )
    prompt = cap_text_tokens(prompt, max_input_tokens)

    for model in _model_candidates():
        for attempt in range(1, retries_per_model + 1):
            last_error = None
            try:
                rate_limit_sleep(inter_request_delay)
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"max_output_tokens": max_output_tokens},
                )
                raw_content = response.text or "{}"
                parsed = json.loads(_extract_json_block(raw_content))

                parsed.setdefault("decisions", [])
                parsed.setdefault("action_items", [])
                parsed["source"] = "gemini"
                parsed["model"] = model
                return parsed
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:500]}"

            if is_model_not_found_error(last_error):
                break

            if attempt < retries_per_model and is_retryable_error(last_error):
                provider_retry = extract_retry_seconds(last_error or "")
                wait_seconds = provider_retry if provider_retry is not None else (retry_backoff_seconds * (2 ** (attempt - 1)))
                time.sleep(max(0.2, wait_seconds))
                continue

            break

    return None


def extract_insights(text):
    llm_result = enhance_insights_with_gemini(text)
    if llm_result:
        return llm_result
    return extract_insights_fast(text)
