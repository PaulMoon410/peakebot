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
import hashlib
import re
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Llama / AI configuration
# ---------------------------------------------------------------------------

LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "http://74.208.146.37:8080")
LLAMA_MODEL = os.environ.get("LLAMA_MODEL", "qwen2.5")

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


def ftp_upload_knowledge(data: dict) -> str:
    """Upload a knowledge entry to FTP and return the filename."""
    ftp = _ftp_connect()
    try:
        _ensure_ftp_dir(ftp, FTP_BRAIN_DIR)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        # Short hash for uniqueness without random state issues
        digest = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:7]
        filename = f"conversation-{ts}-{digest}.json"
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        ftp.storbinary(f"STOR {FTP_BRAIN_DIR}/{filename}", io.BytesIO(payload))
        return filename
    finally:
        ftp.quit()


def ftp_check_duplicate(ai_response: str) -> bool:
    """Return True if an identical AI response already exists on FTP."""
    try:
        files = ftp_list_knowledge()
        ftp = _ftp_connect()
        try:
            for entry in files[:50]:  # Check most recent 50 to limit FTP traffic
                buf = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {FTP_BRAIN_DIR}/{entry['name']}", buf.write)
                    buf.seek(0)
                    data = json.loads(buf.read().decode("utf-8"))
                    if data.get("ai_response") == ai_response:
                        return True
                except Exception:
                    continue
        finally:
            ftp.quit()
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Llama AI helpers
# ---------------------------------------------------------------------------

def call_llama(messages: list, timeout: int = 25) -> str:
    """
    POST to the Llama /v1/chat/completions endpoint.
    Returns the assistant message string, or raises on failure.
    """
    payload = json.dumps({
        "messages": messages,
        "model": LLAMA_MODEL,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LLAMA_SERVER}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise ValueError(f"Unexpected Llama response shape: {data}")


def llama_health() -> bool:
    """Return True if Llama server responds to a minimal probe."""
    try:
        call_llama([{"role": "user", "content": "ping"}], timeout=5)
        return True
    except Exception:
        return False


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
                "ftp_host": FTP_HOST,
                "llama_server": LLAMA_SERVER,
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
                files = ftp_list_knowledge()[:100]
                results = []
                ftp = _ftp_connect()
                try:
                    for entry in files:
                        buf = io.BytesIO()
                        try:
                            ftp.retrbinary(f"RETR {FTP_BRAIN_DIR}/{entry['name']}", buf.write)
                            buf.seek(0)
                            data = json.loads(buf.read().decode("utf-8"))
                            user_msg = data.get("user_message", "").lower()
                            ai_msg = data.get("ai_response", "").lower()
                            if query in user_msg or query in ai_msg:
                                results.append(data)
                        except Exception:
                            continue
                finally:
                    ftp.quit()
                self._send(200, {"results": results, "count": len(results)})
            except Exception as exc:
                self._send(500, {"error": str(exc)})
            return

        self._send(404, {"error": "Not found"})

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
                # Optional duplicate check (caller can skip with check_duplicate=false)
                check_dup = str(body.get("check_duplicate", "true")).lower() != "false"
                is_duplicate = False
                if check_dup:
                    is_duplicate = ftp_check_duplicate(ai_response)

                if is_duplicate:
                    self._send(200, {"ok": True, "duplicate": True, "filename": None})
                    return

                entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "user_message": user_message,
                    "ai_response": ai_response,
                    "memory_state": memory_state,
                }
                filename = ftp_upload_knowledge(entry)

                # Populate cache
                with _cache_lock:
                    _cache[filename] = entry

                print(f"[knowledge_server] Saved: {filename}")
                self._send(201, {"ok": True, "duplicate": False, "filename": filename})
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

            # Build conversation history from memory (last 10 turns)
            history = []
            for conv in (memory.get("conversations") or [])[-10:]:
                history.append({"role": "user", "content": conv.get("user", "")})
                history.append({"role": "assistant", "content": conv.get("ai", "")})
            messages = history + [{"role": "user", "content": prompt}]

            try:
                ai_response = call_llama(messages)
            except Exception as exc:
                print(f"[knowledge_server] Llama error: {exc}")
                self._send(502, {"error": f"Llama server error: {exc}"})
                return

            # Store the conversation in FTP /ai/brain (non-blocking best-effort)
            try:
                entry = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "user_message": prompt,
                    "ai_response": ai_response,
                    "memory_state": memory,
                }
                filename = ftp_upload_knowledge(entry)
                with _cache_lock:
                    _cache[filename] = entry
                print(f"[knowledge_server] Chat saved: {filename}")
            except Exception as exc:
                print(f"[knowledge_server] WARNING: could not save to FTP: {exc}")
                filename = None

            self._send(200, {
                "response": ai_response,
                "source": "python-knowledge-server",
                "filename": filename,
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
    print(f"[knowledge_server] Llama: {LLAMA_SERVER}")
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
