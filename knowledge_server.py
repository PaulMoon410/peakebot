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
import subprocess
import time
import threading
import logging
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# Configure logging to stderr with detailed format
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger('knowledge_server')

# ---------------------------------------------------------------------------
# Python memory engine configuration
# ---------------------------------------------------------------------------

KNOWLEDGE_ENGINE = os.environ.get("KNOWLEDGE_ENGINE", "memory-retrieval")
ENABLE_CROSS_VERIFY = os.environ.get("ENABLE_CROSS_VERIFY", "true").lower() != "false"
BOT_NAME = os.environ.get("BOT_NAME", "Chessie")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PYTHON_PORT = int(os.environ.get("PYTHON_PORT", 5001))
PYTHON_HOST = os.environ.get("PYTHON_HOST", "127.0.0.1")

FTP_HOST = os.environ.get("FTP_HOST", "ftp.geocities.ws")
FTP_USER = os.environ.get("FTP_USER", "PeakeCoin")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "Peake410")
FTP_BRAIN_DIR = os.environ.get("FTP_BRAIN_DIR", "/ai/brain")
SEARCH_MAX_FILES = int(os.environ.get("SEARCH_MAX_FILES", "0"))
FTP_AI_KNOW_DIR = os.environ.get("FTP_AI_KNOW_DIR", FTP_BRAIN_DIR)
TERM_KNOWLEDGE_FILE = os.environ.get("TERM_KNOWLEDGE_FILE", "term_knowledge.json")
TERM_CACHE_TTL_SECONDS = int(os.environ.get("TERM_CACHE_TTL_SECONDS", "300"))
SEARCH_CACHE_TTL_SECONDS = int(os.environ.get("SEARCH_CACHE_TTL_SECONDS", "120"))
CHAT_SEARCH_RETRIES = int(os.environ.get("CHAT_SEARCH_RETRIES", "3"))
KNOWLEDGE_MAX_FILE_MB = int(os.environ.get("KNOWLEDGE_MAX_FILE_MB", "10"))
KNOWLEDGE_MAX_FILE_BYTES = max(1, KNOWLEDGE_MAX_FILE_MB) * 1024 * 1024
FACTCHECK_ENABLED = os.environ.get("FACTCHECK_ENABLED", "true").lower() != "false"
FACTCHECK_INTERVAL_SECONDS = int(os.environ.get("FACTCHECK_INTERVAL_SECONDS", "1800"))
FACTCHECK_MAX_ITEMS_PER_RUN = int(os.environ.get("FACTCHECK_MAX_ITEMS_PER_RUN", "5"))
FACTCHECK_MAX_RESULTS = int(os.environ.get("FACTCHECK_MAX_RESULTS", "3"))
FACTCHECK_IDLE_SECONDS = int(os.environ.get("FACTCHECK_IDLE_SECONDS", "180"))
FACTCHECK_WHITELIST_HOSTS = os.environ.get(
    "FACTCHECK_WHITELIST_HOSTS",
    "wikipedia.org,britannica.com,congress.gov,state.gov,nih.gov,cdc.gov,nps.gov,edu,gov",
)
FACTCHECK_HEARTBEAT_PATH = os.environ.get(
    "FACTCHECK_HEARTBEAT_PATH",
    f"{FTP_BRAIN_DIR.rstrip('/')}/factcheck_heartbeat.json",
)
FACTCHECK_WORKER_STATUS_PATH = os.environ.get(
    "FACTCHECK_WORKER_STATUS_PATH",
    "/tmp/factcheck_worker_status.json",
)

# In-memory cache to reduce FTP round-trips
_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()

# In-memory cache for FTP-driven term aliases/definitions.
_term_knowledge_cache: Dict[str, object] = {
    "loaded_at": 0.0,
    "data": {"aliases": {}, "definitions": {}},
}
_term_knowledge_lock = threading.Lock()

# Cache parsed knowledge corpus used by search to avoid re-downloading FTP files
# on every chat request.
_search_corpus_cache: Dict[str, dict] = {}
_search_corpus_lock = threading.Lock()

_factcheck_process: Optional[subprocess.Popen] = None
_factcheck_lock = threading.Lock()

# Responses matching these patterns are considered low-quality memory and
# should not be selected as knowledge answers.
LOW_QUALITY_PATTERNS = [
    "i found a closely related conversation in memory",
    "i found multiple related conversations in memory",
    "i do not have a close prior conversation",
    "node proxy error",
    "python memory engine unavailable",
    "ai unavailable",
    "knowledge base loaded with comprehensive facts",
    "this principle is widely taught in foundational materials",
    "this point is often treated as a core concept",
]

def is_low_quality_ai_response(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(pattern in t for pattern in LOW_QUALITY_PATTERNS)


def normalize_term(term: str, aliases: Optional[Dict[str, str]] = None) -> str:
    """Normalize whitespace/case and apply optional alias mapping from FTP config."""
    cleaned = " ".join((term or "").strip().lower().split())
    if aliases:
        return aliases.get(cleaned, cleaned)
    return cleaned


def ftp_load_term_knowledge(force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
    """Load term aliases/definitions from FTP ai_know directory.

    Expected FTP JSON structure:
    {
      "aliases": {"constiution": "constitution"},
      "definitions": {"constitution": "..."}
    }
    """
    now = time.time()
    with _term_knowledge_lock:
        loaded_at = float(_term_knowledge_cache.get("loaded_at", 0.0) or 0.0)
        cached_data = _term_knowledge_cache.get("data", {"aliases": {}, "definitions": {}})
        if (
            not force_refresh
            and isinstance(cached_data, dict)
            and (now - loaded_at) < TERM_CACHE_TTL_SECONDS
        ):
            return cached_data  # type: ignore[return-value]

    remote_path = f"{FTP_AI_KNOW_DIR.rstrip('/')}/{TERM_KNOWLEDGE_FILE}"
    loaded = {"aliases": {}, "definitions": {}}
    ftp = _ftp_connect()
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {remote_path}", buf.write)
        buf.seek(0)
        data = json.loads(buf.read().decode("utf-8"))
        if isinstance(data, dict):
            raw_aliases = data.get("aliases", {})
            raw_definitions = data.get("definitions", {})

            if isinstance(raw_aliases, dict):
                aliases: Dict[str, str] = {}
                for k, v in raw_aliases.items():
                    key = str(k or "").strip().lower()
                    val = str(v or "").strip().lower()
                    if key and val:
                        aliases[key] = val
                loaded["aliases"] = aliases

            if isinstance(raw_definitions, dict):
                definitions: Dict[str, str] = {}
                for k, v in raw_definitions.items():
                    key = str(k or "").strip().lower()
                    val = str(v or "").strip()
                    if key and val:
                        definitions[key] = val
                loaded["definitions"] = definitions
    except Exception as exc:
        logger.warning(f"Could not load term knowledge from FTP: {exc}", exc_info=True)
    finally:
        ftp.quit()

    with _term_knowledge_lock:
        _term_knowledge_cache["loaded_at"] = now
        _term_knowledge_cache["data"] = loaded
    return loaded

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


def touch_factcheck_heartbeat() -> None:
    """Persist last chat activity marker so worker can crawl only when idle."""
    try:
        ftp = _ftp_connect()
        _ensure_ftp_dir(ftp, os.path.dirname(FACTCHECK_HEARTBEAT_PATH) or "/")
        payload = {
            "last_chat_at": datetime.now(timezone.utc).isoformat(),
            "source": "knowledge_server",
        }
        ftp.storbinary(
            f"STOR {FACTCHECK_HEARTBEAT_PATH}",
            io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        )
        ftp.quit()
    except Exception:
        # Heartbeat is best-effort and should not block chat flow.
        return


def start_factcheck_worker() -> None:
    """Launch the periodic fact-check worker subprocess if enabled."""
    global _factcheck_process
    if not FACTCHECK_ENABLED:
        logger.info("Fact-check worker disabled (FACTCHECK_ENABLED=false)")
        return

    worker_script = os.path.join(os.path.dirname(__file__), "factcheck_worker.py")
    if not os.path.exists(worker_script):
        logger.error(f"Fact-check worker not found at {worker_script}")
        return

    with _factcheck_lock:
        if _factcheck_process and _factcheck_process.poll() is None:
            return

        env = {
            **os.environ,
            "FACTCHECK_INTERVAL_SECONDS": str(FACTCHECK_INTERVAL_SECONDS),
            "FACTCHECK_MAX_ITEMS_PER_RUN": str(FACTCHECK_MAX_ITEMS_PER_RUN),
            "FACTCHECK_MAX_RESULTS": str(FACTCHECK_MAX_RESULTS),
            "FACTCHECK_IDLE_SECONDS": str(FACTCHECK_IDLE_SECONDS),
            "FACTCHECK_WHITELIST_HOSTS": FACTCHECK_WHITELIST_HOSTS,
            "FACTCHECK_HEARTBEAT_PATH": FACTCHECK_HEARTBEAT_PATH,
            "FACTCHECK_WORKER_STATUS_PATH": FACTCHECK_WORKER_STATUS_PATH,
        }

        _factcheck_process = subprocess.Popen(
            ["python3", worker_script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"Fact-check worker started: PID {_factcheck_process.pid}")


def stop_factcheck_worker() -> None:
    """Stop the periodic fact-check worker subprocess."""
    global _factcheck_process
    with _factcheck_lock:
        if not _factcheck_process:
            return
        try:
            _factcheck_process.terminate()
        except Exception:
            pass
        _factcheck_process = None

def extract_priority_terms(query: str, stop_words: set, aliases: Dict[str, str]) -> List[str]:
    """Extract important terms that should be matched before sentence-level scoring."""
    if not query:
        return []

    tokens_original = re.findall(r"[A-Za-z0-9\-]+", query)
    priority: List[str] = []

    for token in tokens_original:
        normalized = normalize_term(token, aliases)
        if len(normalized) < 3 or normalized in stop_words:
            continue

        # Prioritize likely entities/keywords: capitalized words, mixed tokens, and longer nouns.
        is_capitalized = token[:1].isupper()
        is_mixed = any(c.isdigit() for c in token) or "-" in token
        is_long = len(normalized) >= 7

        if is_capitalized or is_mixed or is_long:
            priority.append(normalized)

    # Keep order and remove duplicates.
    seen = set()
    deduped = []
    for term in priority:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped


def extract_query_phrases(query: str, stop_words: set, aliases: Dict[str, str]) -> List[str]:
    """Extract short normalized phrases to preserve sentence-level intent."""
    raw_tokens = [
        normalize_term(tok, aliases)
        for tok in re.findall(r"[A-Za-z0-9\-]+", query)
        if tok
    ]
    tokens = [t for t in raw_tokens if len(t) > 2 and t not in stop_words]
    phrases: List[str] = []

    # Build 2-3 word phrase windows for more precise intent matching.
    for i in range(len(tokens)):
        if i + 1 < len(tokens):
            phrases.append(f"{tokens[i]} {tokens[i+1]}")
        if i + 2 < len(tokens):
            phrases.append(f"{tokens[i]} {tokens[i+1]} {tokens[i+2]}")

    # De-duplicate preserving order.
    seen = set()
    deduped = []
    for phrase in phrases:
        if phrase in seen:
            continue
        seen.add(phrase)
        deduped.append(phrase)
    return deduped


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
    """Return first chunk path for today's daily knowledge file."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{FTP_BRAIN_DIR}/{date_str}-01.json"


def _daily_chunk_filename(date_str: str, chunk_index: int) -> str:
    return f"{FTP_BRAIN_DIR}/{date_str}-{chunk_index:02d}.json"


def _daily_chunk_index(filename: str, date_str: str) -> Optional[int]:
    # Accept legacy YYYY-MM-DD.json as chunk 1; prefer explicit -01 onward.
    m = re.fullmatch(rf"{re.escape(date_str)}(?:-(\d{{2,3}}))?\.json", filename)
    if not m:
        return None
    if m.group(1):
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return 1


def _ftp_download_json_list(ftp: ftplib.FTP, full_path: str) -> List[dict]:  # type: ignore[type-arg]
    buf = io.BytesIO()
    try:
        ftp.retrbinary(f"RETR {full_path}", buf.write)
    except ftplib.error_perm:
        return []
    buf.seek(0)
    try:
        data = json.loads(buf.read().decode("utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def ftp_load_daily_knowledge() -> List[dict]:
    """Load today's latest chunked conversation array from FTP."""
    try:
        ftp = _ftp_connect()
        try:
            ftp.cwd(FTP_BRAIN_DIR)
        except ftplib.error_perm:
            ftp.quit()
            return []

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        names = [n for n in ftp.nlst() if n.endswith(".json")]
        chunk_indices = [idx for idx in (_daily_chunk_index(n, date_str) for n in names) if idx is not None]

        if not chunk_indices:
            ftp.quit()
            return []

        latest_idx = max(chunk_indices)
        latest_file = _daily_chunk_filename(date_str, latest_idx)
        conversations = _ftp_download_json_list(ftp, latest_file)
        ftp.quit()
        return conversations
    except Exception:
        return []


def ftp_load_knowledge_for_search(max_files: Optional[int] = None) -> List[dict]:
    """Load knowledge entries from multiple FTP JSON files for recall search.

    Supports both formats:
    1) Daily conversation arrays with user_message/ai_response
    2) Fact-bank objects with profile/facts/notes/conversations
    """
    cache_key = str(max_files if max_files is not None else 0)
    now = time.time()
    with _search_corpus_lock:
        cache_entry = _search_corpus_cache.get(cache_key)
        if cache_entry and (now - float(cache_entry.get("loaded_at", 0.0))) < SEARCH_CACHE_TTL_SECONDS:
            cached = cache_entry.get("data", [])
            if isinstance(cached, list):
                return cached

    conversations: List[dict] = []
    ftp = _ftp_connect()
    try:
        try:

            ftp.cwd(FTP_BRAIN_DIR)
        except ftplib.error_perm:
            return []

        filenames = [name for name in ftp.nlst() if name.endswith(".json")]
        # Reverse sort gives newest-first for date-style names and still includes legacy names.
        filenames = sorted(filenames, reverse=True)
        if max_files is not None and max_files > 0:
            filenames = filenames[:max_files]

        for filename in filenames:
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {filename}", buf.write)
            except ftplib.error_perm:
                continue

            buf.seek(0)
            try:
                data = json.loads(buf.read().decode("utf-8"))
            except Exception:
                continue

            # Format A: list of conversation dicts
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        item_copy = dict(item)
                        # Expand searchable text using embedded memory_state facts.
                        memory_state = item.get("memory_state", {})
                        facts = memory_state.get("facts", []) if isinstance(memory_state, dict) else []
                        fact_fragments: List[str] = []
                        if isinstance(facts, list):
                            for fact in facts:
                                if not isinstance(fact, dict):
                                    continue
                                fact_text = str(fact.get("fact") or "").strip()
                                context_text = str(fact.get("context") or "").strip()
                                category = str(fact.get("category") or "").strip()
                                if fact_text:
                                    fact_fragments.append(" ".join(x for x in [category, fact_text, context_text] if x))

                        if fact_fragments:
                            base_user = str(item_copy.get("user_message") or "")
                            item_copy["search_text"] = (base_user + " " + " ".join(fact_fragments)).strip()

                        item_copy["source_file"] = filename
                        conversations.append(item_copy)

            # Format B: object containing facts/conversations arrays
            elif isinstance(data, dict):
                # Handle flat single-entry conversation JSON files.
                flat_user = str(data.get("user_message") or data.get("user") or "").strip()
                flat_ai = str(data.get("ai_response") or data.get("ai") or "").strip()
                if flat_user or flat_ai:
                    conversations.append({
                        "timestamp": data.get("timestamp"),
                        "user_message": flat_user,
                        "ai_response": flat_ai,
                        "source_file": filename,
                        "entry_type": "flat_conversation",
                    })

                # Convert conversation objects if present.
                file_conversations = data.get("conversations", [])
                if isinstance(file_conversations, list):
                    for entry in file_conversations:
                        if not isinstance(entry, dict):
                            continue
                        user_text = str(entry.get("user_message") or entry.get("user") or "").strip()
                        ai_text = str(entry.get("ai_response") or entry.get("ai") or "").strip()
                        if not user_text and not ai_text:
                            continue
                        conversations.append({
                            "timestamp": entry.get("timestamp"),
                            "user_message": user_text,
                            "ai_response": ai_text,
                            "source_file": filename,
                            "entry_type": "conversation",
                        })

                # Convert fact objects to searchable pseudo conversations.
                facts = data.get("facts", [])
                if isinstance(facts, list):
                    for fact in facts:
                        if not isinstance(fact, dict):
                            continue
                        fact_text = str(fact.get("fact") or "").strip()
                        context_text = str(fact.get("context") or "").strip()
                        category = str(fact.get("category") or "").strip()
                        fact_id = str(fact.get("id") or "").strip()
                        fact_query = str(fact.get("query") or "").strip()
                        sources = fact.get("sources") or []
                        source_text = " ".join(str(s).strip() for s in sources if str(s).strip()) if isinstance(sources, list) else ""

                        if not fact_text:
                            continue

                        # user_message holds searchable metadata; ai_response is the canonical fact.
                        searchable_prompt = " ".join(
                            part for part in [category, fact_id, fact_query, fact_text, context_text, source_text] if part
                        )

                        conversations.append({
                            "timestamp": data.get("profile", {}).get("updatedAt"),
                            "user_message": searchable_prompt,
                            "ai_response": fact_text if not context_text else f"{fact_text}. {context_text}",
                            "source_file": filename,
                            "entry_type": "fact",
                            "category": category,
                            "fact_id": fact_id,
                        })

        with _search_corpus_lock:
            _search_corpus_cache[cache_key] = {
                "loaded_at": now,
                "data": conversations,
            }
        return conversations
    finally:
        ftp.quit()


def ftp_append_conversation(user_message: str, ai_response: str, memory_state: dict) -> Optional[str]:
    """
    Append a conversation to today's daily file on FTP.
    Creates the file if it doesn't exist.
    """
    try:
        new_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_message": user_message,
            "ai_response": ai_response,
            "memory_state": memory_state,
        }

        ftp = _ftp_connect()
        _ensure_ftp_dir(ftp, FTP_BRAIN_DIR)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        names = [n for n in ftp.nlst() if n.endswith(".json")]
        chunk_indices = [idx for idx in (_daily_chunk_index(n, date_str) for n in names) if idx is not None]

        if chunk_indices:
            latest_idx = max(chunk_indices)
            target_filename = _daily_chunk_filename(date_str, latest_idx)
            existing = _ftp_download_json_list(ftp, target_filename)
        else:
            latest_idx = 1
            target_filename = _daily_chunk_filename(date_str, latest_idx)
            existing = []

        candidate = existing + [new_entry]
        payload = json.dumps(candidate, indent=2, ensure_ascii=False).encode("utf-8")

        if len(payload) > KNOWLEDGE_MAX_FILE_BYTES:
            latest_idx += 1
            target_filename = _daily_chunk_filename(date_str, latest_idx)
            payload = json.dumps([new_entry], indent=2, ensure_ascii=False).encode("utf-8")

        ftp.storbinary(f"STOR {target_filename}", io.BytesIO(payload))
        ftp.quit()

        # Clear cache for this file
        with _cache_lock:
            _cache.pop(target_filename, None)
        with _search_corpus_lock:
            _search_corpus_cache.clear()

        return target_filename
    except Exception as exc:
        print(f"[knowledge_server] ERROR appending conversation: {exc}")
        return None


def ftp_search_relevant_knowledge(
    query: str,
    max_results: int = 5,
    term_knowledge: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[dict]:
    """
    Search conversations across FTP knowledge files.
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
        term_knowledge = term_knowledge or ftp_load_term_knowledge()
        aliases = term_knowledge.get("aliases", {}) if isinstance(term_knowledge, dict) else {}
        query_is_code = bool(re.search(r"\b(code|python|javascript|js|html|css|sql|function|class|bug|error|api)\b", query.lower()))

        max_files = SEARCH_MAX_FILES if SEARCH_MAX_FILES > 0 else None
        conversations = ftp_load_knowledge_for_search(max_files=max_files)

        # Extract keywords from query (longer words, meaningful words weighted higher)
        query_words_all = [
            normalize_term(w, aliases)
            for w in re.findall(r"\w+", query.lower())
            if w not in stop_words and len(w) > 2
        ]
        if not query_words_all:
            return []

        # Keyword-first anchors: important terms should match before sentence-level similarity.
        priority_terms = extract_priority_terms(query, stop_words, aliases)
        query_phrases = extract_query_phrases(query, stop_words, aliases)

        # Prioritize longer, more specific keywords
        query_keywords = {}
        for word in query_words_all:
            # Weight longer words more heavily (they're more specific)
            weight = len(word) / 10.0 + 1.0
            query_keywords[word] = query_keywords.get(word, 0) + weight

        scored = []

        for conv in conversations:
            user_msg = conv.get("search_text", conv.get("user_message", ""))
            ai_msg = conv.get("ai_response", "")

            # Skip responses that are known fallback/error/recursive templates.
            if is_low_quality_ai_response(ai_msg):
                continue

            combined = (user_msg + " " + ai_msg).lower()
            combined_words = set(re.findall(r"\w+", combined))

            # Step 1: require at least one priority-term match when such terms exist.
            priority_matches = []
            if priority_terms:
                for term in priority_terms:
                    if term in combined_words or term in combined:
                        priority_matches.append(term)

            phrase_matches = []
            if query_phrases:
                phrase_matches = [phrase for phrase in query_phrases if phrase in combined]

            # For longer natural-language queries, require either anchor terms or phrase overlap.
            if len(query_words_all) >= 4 and not priority_matches and not phrase_matches:
                continue

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

            # Boost exact priority-term matches heavily so anchor concepts win first.
            if priority_matches:
                score += 8.0 * len(set(priority_matches))

            # Phrase overlap improves precision for sentence-structured prompts.
            if phrase_matches:
                score += 6.0 * len(set(phrase_matches))

            # Boost records that look code-oriented when the question is code-oriented.
            if query_is_code and re.search(r"(```|`|\bdef\b|\bfunction\b|\bclass\b|\bimport\b|\bconst\b|\blet\b|\breturn\b)", combined):
                score += 10.0

            # Allow one-keyword topic prompts (e.g., "Constitution", "America") to match.
            min_matches = 1 if len(query_keywords) <= 3 else max(2, len(query_keywords) // 2)
            if str(conv.get("entry_type") or "") == "fact":
                # Fact records are concise; allow retrieval on a single strong keyword hit.
                min_matches = 1
            if len(set(matches)) >= min_matches:
                scored.append((score, len(set(matches)), len(set(phrase_matches)), conv))

        scored.sort(key=lambda x: (x[0], x[2], x[1]), reverse=True)
        return [entry[3] for entry in scored[:max_results]]
    except Exception as exc:
        print(f"[knowledge_server] ERROR searching daily knowledge: {exc}")
        return []


# ---------------------------------------------------------------------------
# Memory response helpers
# ---------------------------------------------------------------------------

def _word_set(text: str) -> set:
    return set(re.findall(r"\w+", text.lower()))


def _truncate_answer(text: str, max_sentences: int = 3, max_chars: int = 320) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    shortened = " ".join(sentences[:max_sentences]).strip()
    if len(shortened) > max_chars:
        return shortened[:max_chars].rsplit(" ", 1)[0] + "..."
    return shortened


def _best_effort_answer(prompt: str, memory: dict) -> str:
    """Always return a usable answer, even when direct fact recall is weak."""
    normalized = " ".join((prompt or "").strip().split())
    if not normalized:
        return "Please share your question again and I will answer directly."

    # Code-learning branch: reflect code intent and ask for specifics.
    if re.search(r"\b(code|python|javascript|js|html|css|sql|function|class|bug|error|api)\b", normalized.lower()):
        return (
            f"Based on your request, this sounds code-related: '{_truncate_answer(normalized, max_sentences=1, max_chars=140)}'. "
            "I can learn from your examples and reply with cleaner code patterns. "
            "Share the language plus one sample input/output and I will build the exact structure."
        )

    # Conversation-learning branch: mirror user wording with concise guidance.
    facts = memory.get("facts") if isinstance(memory, dict) else []
    recent_fact = ""
    if isinstance(facts, list):
        for item in reversed(facts):
            if not isinstance(item, dict):
                continue
            candidate = str(item.get("fact") or item.get("value") or "").strip()
            if candidate:
                recent_fact = candidate
                break

    if recent_fact:
        return (
            f"You asked: '{_truncate_answer(normalized, max_sentences=1, max_chars=120)}'. "
            f"Closest known point: {_truncate_answer(recent_fact, max_sentences=1, max_chars=140)}. "
            "If you want, I can refine this into a more direct answer for your exact context."
        )

    return (
        f"I did not find a fully verified match yet for '{_truncate_answer(normalized, max_sentences=1, max_chars=120)}', "
        "but I can still help: give me one detail like location, goal, or timeframe and I will answer precisely."
    )


def _is_relevant_to_question(question: str, answer: str) -> bool:
    """Quick check: does the answer mention key concepts from the question?
    Prevents returning completely off-topic results."""
    stop = {"a", "an", "the", "is", "are", "be", "in", "on", "at", "to", "for", "of", "and", "or", "what", "where", "best", "can", "you", "about"}
    q_words = [w for w in re.findall(r"\w+", question.lower()) if w not in stop and len(w) > 3]
    if not q_words:
        return True  # Can't judge, assume OK
    
    a_lower = answer.lower()
    matching_keywords = sum(1 for w in q_words if w in a_lower)
    
    # Require at least 25% of significant keywords to match (or at least 1 if very few keywords)
    min_required = max(1, len(q_words) // 4)
    return matching_keywords >= min_required


def generate_memory_response(
    prompt: str,
    memory: dict,
    relevant: List[dict],
    term_knowledge: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """Generate a concise response from memory and relevant prior conversations."""
    lower = prompt.strip().lower()

    # Real-time date/time answers should bypass memory lookup.
    now_utc = datetime.now(timezone.utc)
    if (
        "what is today" in lower
        or "what's today" in lower
        or "what day is it" in lower
        or re.search(r"\b(today|date)\b", lower)
    ):
        return now_utc.strftime("Today is %A, %B %d, %Y (UTC).")

    if (
        "what time is it" in lower
        or "current time" in lower
        or re.search(r"\btime\b", lower)
    ):
        return now_utc.strftime("The current time is %H:%M:%S UTC on %A, %B %d, %Y.")

    if (
        "where are you from" in lower
        or "where were you founded" in lower
        or "where were you made" in lower
        or "where are you located" in lower
        or "where are you" == lower
        or "where are you?" == lower
        or "where you located" in lower
        or "your location" in lower
        or ("founded" in lower and "where" in lower)
    ):
        return "I am based in Maryland."

    if (
        "what is your name" in lower
        or "what's your name" in lower
        or "who are you" in lower
        or "your name" == lower
        or "your name?" == lower
    ):
        return f"My name is {BOT_NAME}."

    term_knowledge = term_knowledge or ftp_load_term_knowledge()
    aliases = term_knowledge.get("aliases", {}) if isinstance(term_knowledge, dict) else {}
    definitions = term_knowledge.get("definitions", {}) if isinstance(term_knowledge, dict) else {}

    # Handle common "what is <term>" prompts with concise factual definitions.
    term_match = re.fullmatch(r"\s*what\s+is\s+([a-z0-9\-\s\.]+)\??\s*", lower)
    if term_match:
        term = normalize_term(term_match.group(1), aliases)
        if term in definitions:
            return definitions[term]

    about_match = re.fullmatch(r"\s*(?:what\s+about|tell\s+me\s+about)\s+([a-z0-9\-\s\.]+)\??\s*", lower)
    if about_match:
        term = normalize_term(about_match.group(1), aliases)
        if term in definitions:
            return definitions[term]

    # Defensive cleanup in case a low-quality answer still slips in.
    relevant = [r for r in relevant if not is_low_quality_ai_response(r.get("ai_response", ""))]

    if relevant:
        # Use single best result only (no combining to keep responses concise)
        best = relevant[0]
        best_q = best.get("user_message", "").strip()
        best_a = best.get("ai_response", "").strip()

        if best_a:
            # Only return if actually relevant to the question
            if not _is_relevant_to_question(prompt, best_a):
                # Result is off-topic; fall through to fallback response
                pass
            elif best_q and best_q.lower() != lower:
                return _truncate_answer(best_a)
            else:
                return _truncate_answer(best_a)

    facts = memory.get("facts") if isinstance(memory, dict) else []
    if isinstance(facts, list) and facts:
        latest_facts = [
            f"{str(item.get('subject') or '').strip()} {str(item.get('fact') or item.get('value') or '').strip()}".strip()
            for item in facts[-3:]
            if isinstance(item, dict) and (item.get("fact") or item.get("value") or item.get("subject"))
        ]
        if latest_facts:
            fact_list = "; ".join(latest_facts)
            return _truncate_answer(f"Closest memory: {fact_list}.")

    return _best_effort_answer(prompt, memory)


def _load_worker_status() -> dict:
    """Read local worker heartbeat/status for health diagnostics."""
    try:
        with open(FACTCHECK_WORKER_STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
        try:
            self.send_response(status)
            for k, v in _cors_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected before response was fully written.
            return
        except OSError as exc:
            # Avoid noisy stack traces for transient socket shutdown races.
            print(f"[knowledge_server] WARN: response write failed: {exc}")
            return

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
        try:
            logger.debug(f"GET {self.path}")
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            path = parsed.path.rstrip("/")

            # --- Health check ---
            if path == "/health":
                worker_status = _load_worker_status()
                self._send(200, {
                    "ok": True,
                    "service": "knowledge-server",
                    "engine": KNOWLEDGE_ENGINE,
                    "cross_verify": ENABLE_CROSS_VERIFY,
                    "ftp_host": FTP_HOST,
                    "factcheck_enabled": FACTCHECK_ENABLED,
                    "factcheck_worker_running": bool(_factcheck_process and _factcheck_process.poll() is None),
                    "factcheck_interval_seconds": FACTCHECK_INTERVAL_SECONDS,
                    "factcheck_idle_seconds": FACTCHECK_IDLE_SECONDS,
                    "factcheck_worker_status": worker_status,
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
                    logger.error(f"Error listing knowledge: {exc}", exc_info=True)
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
                    logger.error(f"Error downloading knowledge file {filename}: {exc}", exc_info=True)
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
                    logger.error(f"Error searching knowledge: {exc}", exc_info=True)
                    self._send(500, {"error": str(exc)})
                return

            # --- Memory GET: load from FTP ---
            if path == "/memory":
                try:
                    memory = ftp_load_memory()
                    self._send(200, memory)
                except Exception as exc:
                    logger.error(f"Error loading memory: {exc}", exc_info=True)
                    self._send(500, {"error": str(exc)})
                return

            self._send(404, {"error": "Not found"})
        except Exception as exc:
            logger.error(f"Unhandled error in do_GET: {exc}", exc_info=True)
            self._send(500, {"error": "Internal server error"})

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
        try:
            logger.debug(f"POST {self.path}")
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

                touch_factcheck_heartbeat()

                try:
                    # Append to today's daily file on FTP
                    daily_file = ftp_append_conversation(user_message, ai_response, memory_state)

                    if daily_file:
                        logger.info(f"Knowledge appended to {daily_file}")
                        self._send(201, {"ok": True, "daily_file": daily_file})
                    else:
                        raise Exception("Failed to append to FTP")
                except Exception as exc:
                    logger.error(f"ERROR saving knowledge: {exc}", exc_info=True)
                    self._send(500, {"error": str(exc)})
                return

            # --- Sync: refresh the in-memory cache from FTP ---
            if path == "/sync":
                try:
                    with _cache_lock:
                        _cache.clear()
                    with _search_corpus_lock:
                        _search_corpus_cache.clear()
                    with _term_knowledge_lock:
                        _term_knowledge_cache["loaded_at"] = 0.0
                        _term_knowledge_cache["data"] = {"aliases": {}, "definitions": {}}
                    files = ftp_list_knowledge()
                    logger.info(f"Cache cleared: {len(files)} knowledge files available")
                    self._send(200, {"ok": True, "message": "Cache cleared", "available": len(files)})
                except Exception as exc:
                    logger.error(f"Error during sync: {exc}", exc_info=True)
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

                logger.debug(f"Chat request: {prompt[:100]}...")
                touch_factcheck_heartbeat()

                try:
                    term_knowledge = ftp_load_term_knowledge()

                    # Search FTP knowledge base with retries and progressive refresh.
                    relevant: List[dict] = []
                    attempts = max(1, min(CHAT_SEARCH_RETRIES, 5))
                    for attempt in range(attempts):
                        if attempt > 0:
                            with _search_corpus_lock:
                                _search_corpus_cache.clear()
                        if attempt >= 2:
                            term_knowledge = ftp_load_term_knowledge(force_refresh=True)

                        per_attempt_limit = 5 if attempt == 0 else 8
                        relevant = ftp_search_relevant_knowledge(
                            prompt,
                            max_results=per_attempt_limit,
                            term_knowledge=term_knowledge,
                        )
                        if relevant:
                            logger.debug(f"Chat search attempt {attempt+1}: found {len(relevant)} results")
                            break

                    ai_response = generate_memory_response(prompt, memory, relevant, term_knowledge=term_knowledge)
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
                        if is_low_quality_ai_response(ai_response):
                            ftp_error = "Skipped saving low-quality fallback response"
                            logger.debug("Skipped saving low-quality fallback response")
                        else:
                            daily_file = ftp_append_conversation(prompt, ai_response, memory)
                            if daily_file:
                                ftp_saved = True
                                logger.info(f"Chat appended to {daily_file}")
                            else:
                                ftp_error = "Failed to append to FTP"
                                logger.warning("Could not append to FTP")
                    except Exception as exc:
                        ftp_error = str(exc)
                        logger.warning(f"FTP append failed: {exc}", exc_info=True)

                    self._send(200, {
                        "response": ai_response,
                        "source": "python-memory-engine",
                        "relevant_count": len(relevant),
                        "verification": verification,
                        "ftp_saved": ftp_saved,
                        "daily_file": daily_file,
                        "ftp_error": ftp_error,
                    })
                except Exception as exc:
                    logger.error(f"Error processing chat: {exc}", exc_info=True)
                    self._send(500, {"error": str(exc)})
                return

            self._send(404, {"error": "Not found"})
        except Exception as exc:
            logger.error(f"Unhandled error in do_POST: {exc}", exc_info=True)
            self._send(500, {"error": "Internal server error"})
            return

        self._send(404, {"error": "Not found"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = HTTPServer((PYTHON_HOST, PYTHON_PORT), KnowledgeHandler)
    touch_factcheck_heartbeat()
    start_factcheck_worker()
    print(f"[knowledge_server] Python knowledge server running on {PYTHON_HOST}:{PYTHON_PORT}")
    print(f"[knowledge_server] FTP host: {FTP_HOST}  |  brain dir: {FTP_BRAIN_DIR}")
    print(f"[knowledge_server] Engine: {KNOWLEDGE_ENGINE}  |  Cross verify: {ENABLE_CROSS_VERIFY}")
    print(f"[knowledge_server] Fact-check worker enabled: {FACTCHECK_ENABLED}")
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
    finally:
        stop_factcheck_worker()
        server.server_close()
