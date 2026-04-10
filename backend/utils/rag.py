import os
import re
import time
import hashlib
import threading

import numpy as np

try:
    import faiss
except Exception:
    faiss = None

try:
    from sentence_transformers import SentenceTransformer
except Exception:
    SentenceTransformer = None

from utils.gemini_shared import (
    cap_text_tokens,
    extract_retry_seconds,
    is_model_not_found_error,
    is_retryable_error,
    rate_limit_sleep,
)


class RAGEngine:
    def __init__(self, model_name="all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.model = None
        self.documents = {}
        self.chunk_size = 80
        self.chunk_overlap = 20
        self.default_k = 4
        self.response_cache = {}
        self.cache_lock = threading.Lock()
        self.gemini_quota_blocked_until = 0.0

    def _quota_fast_fallback_settings(self):
        enabled = os.getenv("GEMINI_FAST_FALLBACK_ON_QUOTA", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        cooldown_seconds = float(os.getenv("GEMINI_QUOTA_COOLDOWN_SECONDS", "120"))
        return enabled, max(5.0, cooldown_seconds)

    def _is_quota_exceeded_error(self, error_text):
        text = (error_text or "").lower()
        markers = [
            "quota exceeded",
            "resource_exhausted",
            "free_tier_requests",
            "limit:",
            "429",
        ]
        return any(marker in text for marker in markers)

    def _is_quota_blocked(self):
        return time.time() < self.gemini_quota_blocked_until

    def _quota_cooldown_remaining_seconds(self):
        remaining = int(max(0, self.gemini_quota_blocked_until - time.time()))
        return remaining

    def _set_quota_block(self, cooldown_seconds):
        self.gemini_quota_blocked_until = time.time() + cooldown_seconds

    def _cache_settings(self):
        enabled = os.getenv("GEMINI_RESPONSE_CACHE_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        ttl_seconds = float(os.getenv("GEMINI_RESPONSE_CACHE_TTL_SECONDS", "90"))
        max_entries = int(os.getenv("GEMINI_RESPONSE_CACHE_MAX_ENTRIES", "128"))
        return enabled, max(1.0, ttl_seconds), max(16, max_entries)

    def _cache_get(self, key, ttl_seconds):
        now = time.time()
        with self.cache_lock:
            item = self.response_cache.get(key)
            if not item:
                return None
            if now - item["ts"] > ttl_seconds:
                self.response_cache.pop(key, None)
                return None
            item["ts"] = now
            return item

    def _cache_set(self, key, answer, model, max_entries):
        now = time.time()
        with self.cache_lock:
            self.response_cache[key] = {"answer": answer, "model": model, "ts": now}
            if len(self.response_cache) <= max_entries:
                return

            oldest_keys = sorted(
                self.response_cache.items(),
                key=lambda item: item[1].get("ts", 0.0),
            )
            remove_count = len(self.response_cache) - max_entries
            for idx in range(remove_count):
                self.response_cache.pop(oldest_keys[idx][0], None)

    def _model_candidates(self):
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

    def _summarize_history(self, chat_history):
        if not chat_history:
            return ""

        lines = []
        # Keep only the latest few turns to avoid oversized prompts.
        for item in chat_history[-4:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").strip().lower()
            content = str(item.get("content") or "").strip()
            if not content:
                continue

            label = "User" if role == "user" else "Assistant"
            compact = re.sub(r"\s+", " ", content)
            lines.append(f"- {label}: {compact[:180]}")

        return "\n".join(lines)

    def has_documents(self):
        return bool(self.documents)

    def _load_model(self):
        if self.model is not None:
            return self.model
        if SentenceTransformer is None:
            return None
        try:
            self.model = SentenceTransformer(self.model_name)
        except Exception:
            self.model = None
        return self.model

    def _chunk_text(self, text, chunk_size=None, overlap=None):
        chunk_size = chunk_size or self.chunk_size
        overlap = overlap or self.chunk_overlap
        words = text.split()
        if not words:
            return []

        chunks = []
        step = max(1, chunk_size - overlap)
        for start in range(0, len(words), step):
            chunk_words = words[start : start + chunk_size]
            if not chunk_words:
                continue
            chunks.append(" ".join(chunk_words))
        return chunks

    def _embed(self, texts):
        model = self._load_model()
        if model is None:
            return None

        vectors = model.encode(texts, normalize_embeddings=True)
        return np.asarray(vectors, dtype="float32")

    def add_document(self, meeting_id, text, metadata=None):
        chunks = self._chunk_text(text)
        embeddings = self._embed(chunks) if chunks else None

        self.documents[meeting_id] = {
            "meeting_id": meeting_id,
            "metadata": metadata or {},
            "chunks": chunks,
            "embeddings": embeddings,
        }

    def _collect_candidates(self, meeting_filter=None):
        if meeting_filter:
            document = self.documents.get(meeting_filter)
            return [document] if document else []
        return list(self.documents.values())

    def _retrieve_faiss(self, question, candidates, k):
        all_chunks = []
        all_vectors = []

        for doc in candidates:
            chunks = doc.get("chunks", [])
            embeddings = doc.get("embeddings")
            if not chunks or embeddings is None:
                continue
            all_chunks.extend(chunks)
            all_vectors.append(embeddings)

        if not all_chunks or not all_vectors or faiss is None:
            return []

        matrix = np.vstack(all_vectors).astype("float32")
        question_vector = self._embed([question])
        if question_vector is None:
            return []

        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)

        top_k = min(k, len(all_chunks))
        distances, indices = index.search(question_vector, top_k)

        results = []
        for rank, idx in enumerate(indices[0]):
            if idx < 0:
                continue
            results.append(
                {
                    "rank": rank + 1,
                    "score": float(distances[0][rank]),
                    "chunk": all_chunks[idx],
                }
            )
        return results

    def _retrieve_keyword(self, question, candidates, k):
        query_terms = set(re.findall(r"\w+", question.lower()))
        scored_chunks = []

        for doc in candidates:
            for chunk in doc.get("chunks", []):
                chunk_terms = set(re.findall(r"\w+", chunk.lower()))
                overlap = len(query_terms.intersection(chunk_terms))
                if overlap == 0:
                    continue
                scored_chunks.append((overlap, chunk))

        scored_chunks.sort(key=lambda item: item[0], reverse=True)
        results = []
        for rank, (score, chunk) in enumerate(scored_chunks[:k], start=1):
            results.append({"rank": rank, "score": float(score), "chunk": chunk})

        return results

    def _dedupe_retrieved_chunks(self, retrieved):
        # Remove duplicate citation chunks while preserving the best score order.
        deduped = []
        seen = set()

        for item in retrieved:
            chunk = (item.get("chunk") or "").strip()
            if not chunk:
                continue
            key = re.sub(r"\s+", " ", chunk.lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(
                {
                    "rank": len(deduped) + 1,
                    "score": item.get("score", 0.0),
                    "chunk": chunk,
                }
            )

        return deduped

    def _gemini_answer(self, question, retrieved_chunks, chat_history=None):
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            return None, None, None

        try:
            from google import genai
        except Exception:
            return None, None, None

        model_candidates = self._model_candidates()
        retries_per_model = int(os.getenv("GEMINI_RETRIES_PER_MODEL", "5"))
        retry_backoff_seconds = float(os.getenv("GEMINI_RETRY_BACKOFF_SECONDS", "1.0"))
        inter_request_delay = float(os.getenv("GEMINI_INTER_REQUEST_DELAY_SECONDS", "0.25"))
        max_input_tokens = int(os.getenv("GEMINI_MAX_INPUT_TOKENS", "1500"))
        max_output_tokens = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "512"))
        context_chunks_for_gemini = int(os.getenv("GEMINI_CONTEXT_CHUNKS", "2"))
        max_chars_per_chunk = int(os.getenv("GEMINI_MAX_CHARS_PER_CHUNK", "260"))

        compact_context_lines = []
        for item in retrieved_chunks[: max(1, context_chunks_for_gemini)]:
            chunk = (item.get("chunk") or "").strip()
            if len(chunk) > max_chars_per_chunk:
                chunk = chunk[:max_chars_per_chunk].rsplit(" ", 1)[0].strip() + " ..."
            compact_context_lines.append(f"[{item['rank']}] {chunk}")
        compact_context = "\n".join(compact_context_lines)
        history_summary = self._summarize_history(chat_history)

        history_block = ""
        if history_summary:
            history_block = f"Recent conversation summary:\n{history_summary}\n\n"

        prompt = (
            "Use only the context blocks below to answer the question. "
            "If uncertain, say so. Include citations like [1], [2]. "
            "If the question asks for agreements or decisions, list relevant points as short bullet points with citations. "
            "Keep the answer concise.\n\n"
            f"{history_block}"
            f"Context:\n{compact_context}\n\nQuestion: {question}"
        )
        prompt = cap_text_tokens(prompt, max_input_tokens)

        cache_enabled, cache_ttl_seconds, cache_max_entries = self._cache_settings()
        quota_fast_fallback_enabled, quota_cooldown_seconds = self._quota_fast_fallback_settings()
        cache_key = hashlib.sha256(prompt.encode("utf-8", errors="ignore")).hexdigest()
        if cache_enabled:
            cached = self._cache_get(cache_key, cache_ttl_seconds)
            if cached:
                return cached["answer"], cached.get("model") or model_candidates[0], None

        if quota_fast_fallback_enabled and self._is_quota_blocked():
            return None, None, "Gemini quota cooldown active. Skipping provider call and using fallback answer."

        client = genai.Client(api_key=gemini_api_key)
        last_error = None
        for model in model_candidates:
            for attempt in range(1, retries_per_model + 1):
                try:
                    rate_limit_sleep(inter_request_delay)
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config={"max_output_tokens": max_output_tokens},
                    )
                    if response.text and response.text.strip():
                        final_text = response.text.strip()
                        if cache_enabled:
                            self._cache_set(cache_key, final_text, model, cache_max_entries)
                        return final_text, model, None
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {str(exc)[:500]}"

                if quota_fast_fallback_enabled and self._is_quota_exceeded_error(last_error):
                    self._set_quota_block(quota_cooldown_seconds)
                    return None, None, last_error

                if is_model_not_found_error(last_error):
                    break

                if attempt < retries_per_model and is_retryable_error(last_error):
                    provider_retry = extract_retry_seconds(last_error or "")
                    wait_seconds = provider_retry if provider_retry is not None else (retry_backoff_seconds * (2 ** (attempt - 1)))
                    time.sleep(max(0.2, wait_seconds))
                    continue

                break

        return None, None, last_error

    def _generate_answer(self, question, retrieved_chunks, chat_history=None):
        if not retrieved_chunks:
            return (
                "I could not find enough context in uploaded transcripts to answer that.",
                "fallback",
                None,
                None,
            )

        gemini_answer, gemini_model, gemini_error = self._gemini_answer(question, retrieved_chunks, chat_history=chat_history)
        if gemini_answer:
            return gemini_answer, "gemini", gemini_model, None

        # Heuristic fallback: choose the most query-relevant sentence across retrieved chunks.
        stopwords = {
            "a",
            "an",
            "the",
            "is",
            "are",
            "was",
            "were",
            "why",
            "what",
            "when",
            "where",
            "how",
            "did",
            "do",
            "does",
            "to",
            "of",
            "in",
            "on",
            "for",
            "and",
            "or",
        }
        query_terms = {
            token for token in re.findall(r"\w+", question.lower()) if token not in stopwords
        }

        candidates = []
        for item in retrieved_chunks:
            for segment in re.split(r"(?<=[.!?])\s+", item["chunk"]):
                sentence = segment.strip()
                if not sentence:
                    continue
                # Remove typical transcript prefixes in fallback text.
                sentence = re.sub(r"^[A-Za-z ]+-\s+[A-Za-z0-9 ]+$", "", sentence).strip()
                if sentence:
                    candidates.append((item["rank"], sentence))

        if not candidates:
            top_chunk = retrieved_chunks[0]["chunk"]
            return f"{top_chunk[:260]} [1]", "fallback", None, gemini_error

        best_rank, best_sentence = candidates[0]
        best_score = -1
        for rank, sentence in candidates:
            sentence_terms = set(re.findall(r"\w+", sentence.lower()))
            overlap = len(query_terms.intersection(sentence_terms))
            if "because" in sentence.lower() or "so " in sentence.lower():
                overlap += 1
            if overlap > best_score:
                best_score = overlap
                best_rank = rank
                best_sentence = sentence

        # Clean transcript speaker prefix in fallback output for a more natural answer.
        best_sentence = re.sub(r"^[A-Z][a-zA-Z]+:\s*", "", best_sentence).strip()

        return f"{best_sentence} [{best_rank}]", "fallback", None, gemini_error

    def answer(self, question, meeting_filter=None, k=None, chat_history=None):
        k = k or self.default_k
        candidates = self._collect_candidates(meeting_filter)
        if not candidates:
            return {"answer": "No transcript data available.", "citations": []}

        retrieved = self._retrieve_faiss(question, candidates, k)
        retrieval_mode = "faiss"
        if not retrieved:
            retrieved = self._retrieve_keyword(question, candidates, k)
            retrieval_mode = "keyword"

        retrieved = self._dedupe_retrieved_chunks(retrieved)

        answer, generation_mode, generation_model, generation_error = self._generate_answer(
            question,
            retrieved,
            chat_history=chat_history,
        )

        citations = [
            {
                "rank": item["rank"],
                "score": item["score"],
                "text": item["chunk"],
            }
            for item in retrieved
        ]

        return {
            "answer": answer,
            "citations": citations,
            "retrieval_mode": retrieval_mode,
            "generation_mode": generation_mode,
            "generation_model": generation_model,
            "generation_error": generation_error,
            "gemini_quota_cooldown_active": self._is_quota_blocked(),
            "gemini_quota_cooldown_remaining_seconds": self._quota_cooldown_remaining_seconds(),
        }
