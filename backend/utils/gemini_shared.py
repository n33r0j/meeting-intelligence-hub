import re
import threading
import time


_LAST_CALL_AT = 0.0
_RATE_LIMIT_LOCK = threading.Lock()


def estimate_tokens(text):
    if not text:
        return 0
    return max(1, len(text) // 4)


def cap_text_tokens(text, max_tokens):
    if not text:
        return ""
    if max_tokens <= 0:
        return text

    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def rate_limit_sleep(min_delay_seconds):
    global _LAST_CALL_AT

    if min_delay_seconds <= 0:
        return

    with _RATE_LIMIT_LOCK:
        now = time.time()
        wait_for = (_LAST_CALL_AT + min_delay_seconds) - now
        if wait_for > 0:
            time.sleep(wait_for)
        _LAST_CALL_AT = time.time()


def extract_retry_seconds(error_text):
    if not error_text:
        return None

    retry_match = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", error_text, flags=re.IGNORECASE)
    if retry_match:
        try:
            return float(retry_match.group(1))
        except Exception:
            return None
    return None


def is_retryable_error(error_text):
    text = (error_text or "").lower()
    return any(marker in text for marker in ["429", "503", "resource_exhausted", "quota", "unavailable"])


def is_model_not_found_error(error_text):
    text = (error_text or "").lower()
    return "404" in text or "not found" in text or "unsupported model" in text
