#!/usr/bin/env python3
"""
Knowledge Server - Python companion to the Node.js peakebot server.
Provides a dedicated REST API for storing and retrieving AI conversation
knowledge using the same FTP backend.

Run with:
    python3 knowledge_server.py
or with custom settings:
    FTP_HOST=... FTP_USER=... FTP_PASSWORD=... PYTHON_PORT=5001 python3 knowledge_server.py
"""

import os
import json
import ftplib
import io
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Python memory engine configuration
# ---------------------------------------------------------------------------

KNOWLEDGE_ENGINE = os.environ.get("KNOWLEDGE_ENGINE", "memory-retrieval")
ENABLE_CROSS_VERIFY = os.environ.get("ENABLE_CROSS_VERIFY", "true").lower() != "false"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PYTHON_PORT = int(os.environ.get("PYTHON_PORT", 5001))

FTP_HOST = os.environ.get("FTP_HOST", "ftp.geocities.ws")
FTP_USER = os.environ.get("FTP_USER", "PeakeCoin")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "Peake410")
FTP_BRAIN_DIR = "/ai/brain"

# In-memory cache to reduce FTP round-trips
_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()

# ---------------------------------------------------------------------------
# FTP helpers
# ---------------------------------------------------------------------------

def _ftp_connect() -> ftplib.FTP:  # type: ignore[type-arg]
    """Open and return an authenticated FTP connection."""
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21, timeout=15)
    ftp.login(FTP_USER, FTP_PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _ensure_ftp_dir(ftp: ftplib.FTP, directory: str) -> None:  # type: ignore[type-arg]
    """Create remote directory if it doesn't exist."""
    parts = [p for p in directory.split("/") if p]
    current = "/"
    for part in parts:
        current = f"{current}/{part}" if current != "/" else f"/{part}"
        try:
            ftp.cwd(current)
        except ftplib.error_perm:
            ftp.mkd(current)
            ftp.cwd(current)


def ftp_list_knowledge() -> List[dict]:
    """Return list of knowledge file metadata from FTP."""
    ftp = _ftp_connect()
    try:
        try:
            ftp.cwd(FTP_BRAIN_DIR)
        except ftplib.error_perm:
            return []
        entries = ftp.nlst()
        files = [e for e in entries if e.endswith(".json")]
        return [{"name": f, "path": f"{FTP_BRAIN_DIR}/{f}"} for f in sorted(files, reverse=True)]
    finally:
        ftp.quit()


def ftp_download_file(filename: str) -> Optional[dict]:
    """Download and parse a single knowledge JSON file from FTP."""
    # Sanitize filename — allow only safe characters
    if not re.fullmatch(r"[\w\-\.]+\.json", filename):
        return None

    ftp = _ftp_connect()
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {FTP_BRAIN_DIR}/{filename}", buf.write)
        buf.seek(0)
        return json.loads(buf.read().decode("utf-8"))
    except ftplib.error_perm:
        return None
    finally:
        ftp.quit()


def ftp_get_daily_filename() -> str:
    """Return the daily knowledge file path based on current UTC date."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{FTP_BRAIN_DIR}/{date_str}.json"


def ftp_load_daily_knowledge() -> List[dict]:
    """Load today's conversation array from FTP. Returns empty list if file doesn't exist."""
    try:
        ftp = _ftp_connect()
        filename = ftp_get_daily_filename()
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {filename}", buf.write)
        except ftplib.error_perm:
            ftp.quit()
            return []
        
        ftp.quit()
        buf.seek(0)
        data = json.loads(buf.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def ftp_append_conversation(user_message: str, ai_response: str, memory_state: dict) -> bool:
    """
    Append a conversation to today's daily file on FTP.
    Creates the file if it doesn't exist.
    """
    try:
        # Load today's conversations
        conversations = ftp_load_daily_knowledge()
        
        # Append new conversation
        conversations.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_message": user_message,
            "ai_response": ai_response,
            "memory_state": memory_state,
        })
        
        # Upload back to FTP
        ftp = _ftp_connect()
        _ensure_ftp_dir(ftp, FTP_BRAIN_DIR)
        filename = ftp_get_daily_filename()
        payload = json.dumps(conversations, indent=2, ensure_ascii=False).encode("utf-8")
        ftp.storbinary(f"STOR {filename}", io.BytesIO(payload))
        ftp.quit()
        
        # Clear cache for this file
        with _cache_lock:
            _cache.pop(filename, None)
        
        return True
    except Exception as exc:
        print(f"[knowledge_server] ERROR appending conversation: {exc}")
        return False


def ftp_search_relevant_knowledge(query: str, max_results: int = 5) -> List[dict]:
    """
    Search conversations in today's daily file.
    Uses keyword importance scoring with TF-IDF-like weighting.
    """
    # Common stop words to ignore in matching
    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by",
        "for", "from", "if", "in", "is", "it", "of", "on", "or",
        "that", "the", "to", "was", "what", "when", "where", "which",
        "who", "will", "with", "how", "many", "can", "do", "does",
        "did", "have", "has", "this", "these", "those", "you", "your",
        "i", "me", "my", "we", "us", "our", "just", "very", "more", "most",
        "then", "than", "there", "here", "other", "such", "no", "not", "so"
    }
    
    try:
        conversations = ftp_load_daily_knowledge()
        
        # Extract keywords from query (longer words, meaningful words weighted higher)
        query_words_all = [w.lower() for w in re.findall(r"\w+", query.lower()) if w not in stop_words and len(w) > 2]
        if not query_words_all:
            return []
        
        # Prioritize longer, more specific keywords
        query_keywords = {}
        for word in query_words_all:
            # Weight longer words more heavily (they're more specific)
            weight = len(word) / 10.0 + 1.0
            query_keywords[word] = query_keywords.get(word, 0) + weight
        
        scored = []
        
        for conv in conversations:
            user_msg = conv.get("user_message", "")
            ai_msg = conv.get("ai_response", "")
            combined = (user_msg + " " + ai_msg).lower()
            
            # Extract keywords from conversation (same method as query)
            conv_words = [w.lower() for w in re.findall(r"\w+", combined) if w not in stop_words and len(w) > 2]
            
            # Calculate match score - emphasis on matching important keywords
            score = 0.0
            matches = []
            for keyword, weight in query_keywords.items():
                if keyword in conv_words:
                    # Count frequency in conversation (up to 3x multiplier)
                    freq = min(conv_words.count(keyword), 3)
                    score += weight * freq
                    matches.append(keyword)
            
            # Only include if at least 2 key query words matched or >40% of unique query keywords
            min_matches = max(2, len(query_keywords) // 2)
            if len(set(matches)) >= min_matches:
                scored.append((score, len(set(matches)), conv))
        
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [entry[2] for entry in scored[:max_results]]
    except Exception as exc:
        print(f"[knowledge_server] ERROR searching daily knowledge: {exc}")
        return []


# ---------------------------------------------------------------------------
# Memory response helpers
# ---------------------------------------------------------------------------

def _word_set(text: str) -> set:
    return set(re.findall(r"\w+", text.lower()))


def generate_memory_response(prompt: str, memory: dict, relevant: List[dict]) -> str:
    """Generate a response from memory and relevant prior conversations.
    Can combine multiple relevant results for a more complete answer."""
    lower = prompt.strip().lower()

    if "what is today" in lower or "what's today" in lower or "what day is it" in lower:
        return datetime.now(timezone.utc).strftime("Today is %A, %B %d, %Y (UTC).")

    if relevant:
        # Check if we have multiple complementary results to combine
        if len(relevant) >= 2:
            first = relevant[0]
            second = relevant[1]
            
            first_q = first.get("user_message", "").strip()
            first_a = first.get("ai_response", "").strip()
            second_q = second.get("user_message", "").strip()
            second_a = second.get("ai_response", "").strip()
            
            # If both are relevant and different enough, combine them
            if first_a and second_a and first_a.lower() != second_a.lower():
                return (
                    "I found multiple related conversations in memory.\n\n"
                    f"First: \"{first_q}\" → {first_a}\n\n"
                    f"Also related: \"{second_q}\" → {second_a}"
                )
        
        # Use single best result
        best = relevant[0]
        best_q = best.get("user_message", "").strip()
        best_a = best.get("ai_response", "").strip()

        if best_a:
            if best_q and best_q.lower() != lower:
                return (
                    "I found a closely related conversation in memory.\n\n"
                    f"Closest prior question: \"{best_q}\"\n"
                    f"Closest prior answer: {best_a}"
                )
            return best_a

    facts = memory.get("facts") if isinstance(memory, dict) else []
    if isinstance(facts, list) and facts:
        latest_facts = [
            f"{item.get('subject', '').strip()} = {item.get('value', '').strip()}"
            for item in facts[-3:]
            if item.get("subject") and item.get("value")
        ]
        if latest_facts:
            fact_list = "; ".join(latest_facts)
            return (
                "I do not have a close prior conversation match yet, "
                f"but I remember these recent facts: {fact_list}."
            )

    return (
        "I do not have a close prior conversation for that yet. "
        "Tell me details and I will store this for future recall."
    )


def verify_response(prompt: str, response: str, relevant: List[dict]) -> dict:
    """Basic cross-verification step to score consistency with relevant memory."""
    issues: List[str] = []
    score = 1.0

    if not response.strip():
        issues.append("Empty response")
        score -= 0.8

    if relevant:
        best = relevant[0].get("ai_response", "")
        if best:
            overlap = len(_word_set(best).intersection(_word_set(response)))
            if overlap < 2:
                issues.append("Low overlap with closest stored answer")
                score -= 0.45

    if len(response) > 1400:
        issues.append("Response is unusually long")
        score -= 0.1

    score = max(0.0, min(1.0, score))
    return {
        "enabled": ENABLE_CROSS_VERIFY,
        "passed": score >= 0.35,
        "score": round(score, 2),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# FTP memory (simple key-value store for the conversation memory object)
# ---------------------------------------------------------------------------

FTP_MEMORY_PATH = "/ai/memory.json"


def ftp_load_memory() -> dict:
    """Download memory.json from FTP. Returns empty default on failure."""
    try:
        ftp = _ftp_connect()
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {FTP_MEMORY_PATH}", buf.write)
        ftp.quit()
        buf.seek(0)
        data = json.loads(buf.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ftp_save_memory(memory: dict) -> bool:
    """Upload memory.json to FTP. Returns True on success."""
    try:
        ftp = _ftp_connect()
        _ensure_ftp_dir(ftp, "/ai")
        payload = json.dumps(memory, indent=2, ensure_ascii=False).encode("utf-8")
        ftp.storbinary(f"STOR {FTP_MEMORY_PATH}", io.BytesIO(payload))
        ftp.quit()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json; charset=utf-8",
    }


class KnowledgeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server with JSON API for knowledge storage."""

    # Silence default access log — use explicit print for important events
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in _cors_headers().items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        # Reject excessively large payloads (10 MB limit)
        if length > 10 * 1024 * 1024:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        for k, v in _cors_headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")

        # --- Health check ---
        if path == "/health":
            self._send(200, {
                "ok": True,
                "service": "knowledge-server",
                "engine": KNOWLEDGE_ENGINE,
                "cross_verify": ENABLE_CROSS_VERIFY,
                "ftp_host": FTP_HOST,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return

        # --- List knowledge ---
        if path == "/knowledge":
            try:
                limit = min(int(qs.get("limit", [50])[0]), 200)
                files = ftp_list_knowledge()[:limit]
                self._send(200, {"files": files, "count": len(files)})
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        # --- Fetch single knowledge entry  /knowledge/<filename> ---
        match = re.fullmatch(r"/knowledge/([\w\-\.]+\.json)", path)
        if match:
            filename = match.group(1)
            # Check cache first
            with _cache_lock:
                cached = _cache.get(filename)
            if cached:
                self._send(200, cached)
                return
            try:
                data = ftp_download_file(filename)
                if data is None:
                    self._send(404, {"error": "Not found"})
                else:
                    with _cache_lock:
                        _cache[filename] = data
                    self._send(200, data)
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        # --- Search knowledge (simple substring match on cached/recent entries) ---
        if path == "/knowledge/search":
            query = qs.get("q", [""])[0].strip().lower()
            if not query:
                self._send(400, {"error": "Missing query parameter 'q'"})
                return
            try:
                limit = min(int(qs.get("limit", [5])[0]), 20)
                results = ftp_search_relevant_knowledge(query, max_results=limit)
                self._send(200, {"results": results, "count": len(results)})
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        # --- Memory GET: load from FTP ---
        if path == "/memory":
            try:
                memory = ftp_load_memory()
                self._send(200, memory)
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        self._send(404, {"error": "Not found"})

    def do_PUT(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # --- Memory PUT: save to FTP ---
        if path == "/memory":
            body = self._read_body()
            if body is None:
                self._send(400, {"error": "Invalid or oversized JSON body"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "Memory must be a JSON object"})
                return
            body["profile"] = body.get("profile", {})
            body["profile"]["updatedAt"] = datetime.now(timezone.utc).isoformat()
            ok = ftp_save_memory(body)
            if ok:
                self._send(200, {"ok": True, "updatedAt": body["profile"]["updatedAt"]})
            else:
                self._send(500, {"error": "Failed to save memory to FTP"})
            return

        self._send(404, {"error": "Not found"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # --- Store new knowledge entry ---
        if path == "/knowledge":
            body = self._read_body()
            if body is None:
                self._send(400, {"error": "Invalid or oversized JSON body"})
                return

            user_message = body.get("user_message", "").strip()
            ai_response = body.get("ai_response", "").strip()
            memory_state = body.get("memory_state", {})

            if not user_message or not ai_response:
                self._send(400, {"error": "Fields 'user_message' and 'ai_response' are required"})
                return

            try:
                # Append to today's daily file on FTP
                ok = ftp_append_conversation(user_message, ai_response, memory_state)
                
                if ok:
                    daily_file = ftp_get_daily_filename()
                    print(f"[knowledge_server] Knowledge appended to {daily_file}")
                    self._send(201, {"ok": True, "daily_file": daily_file})
                else:
                    raise Exception("Failed to append to FTP")
            except Exception as exc:
                print(f"[knowledge_server] ERROR saving knowledge: {exc}")
                self._send(500, {"error": str(exc)})
            return

        # --- Sync: refresh the in-memory cache from FTP ---
        if path == "/sync":
            try:
                with _cache_lock:
                    _cache.clear()
                files = ftp_list_knowledge()
                self._send(200, {"ok": True, "message": "Cache cleared", "available": len(files)})
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        # --- Chat: receive a user message, call Llama, store result on FTP ---
        if path == "/chat":
            body = self._read_body()
            if body is None:
                self._send(400, {"error": "Invalid or oversized JSON body"})
                return

            prompt = body.get("prompt", "").strip()
            memory = body.get("memory", {})

            if not prompt:
                self._send(400, {"error": "Field 'prompt' is required"})
                return

            # Search FTP knowledge base for relevant past exchanges
            relevant = ftp_search_relevant_knowledge(prompt, max_results=5)
            ai_response = generate_memory_response(prompt, memory, relevant)
            verification = verify_response(prompt, ai_response, relevant) if ENABLE_CROSS_VERIFY else {
                "enabled": False,
                "passed": True,
                "score": 1.0,
                "issues": [],
            }

            # If verification fails and we have a stronger prior answer, use it directly.
            if not verification.get("passed", True) and relevant:
                fallback = relevant[0].get("ai_response", "").strip()
                if fallback:
                    ai_response = fallback
                    verification = verify_response(prompt, ai_response, relevant) if ENABLE_CROSS_VERIFY else verification

            # Store the conversation to today's daily file on FTP
            ftp_saved = False
            daily_file = None
            ftp_error = None
            try:
                ok = ftp_append_conversation(prompt, ai_response, memory)
                if ok:
                    ftp_saved = True
                    daily_file = ftp_get_daily_filename()
                    print(f"[knowledge_server] Chat appended to {daily_file}")
                else:
                    ftp_error = "Failed to append to FTP"
                    print(f"[knowledge_server] WARNING: could not append to FTP")
            except Exception as exc:
                ftp_error = str(exc)
                print(f"[knowledge_server] WARNING: FTP append failed: {exc}")

            self._send(200, {
                "response": ai_response,
                "source": "python-memory-engine",
                "relevant_count": len(relevant),
                "verification": verification,
                "ftp_saved": ftp_saved,
                "daily_file": daily_file,
                "ftp_error": ftp_error,
            })
            return

        self._send(404, {"error": "Not found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PYTHON_PORT), KnowledgeHandler)
    print(f"[knowledge_server] Python knowledge server running on port {PYTHON_PORT}")
    print(f"[knowledge_server] FTP host: {FTP_HOST}  |  brain dir: {FTP_BRAIN_DIR}")
    print(f"[knowledge_server] Engine: {KNOWLEDGE_ENGINE}  |  Cross verify: {ENABLE_CROSS_VERIFY}")
    print(f"[knowledge_server] Endpoints:")
    print(f"  GET    /health")
    print(f"  GET    /memory")
    print(f"  PUT    /memory")
    print(f"  GET    /knowledge?limit=50")
    print(f"  GET    /knowledge/<filename>")
    print(f"  GET    /knowledge/search?q=<query>")
    print(f"  POST   /chat  {{prompt, memory}}")
    print(f"  POST   /knowledge  {{user_message, ai_response, memory_state}}")
    print(f"  POST   /sync")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[knowledge_server] Shutting down.")
        server.server_close()
