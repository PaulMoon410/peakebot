#!/usr/bin/env python3
"""
Background fact-check worker.

Periodically scans FTP conversation logs, verifies question-like prompts against
safe sources, and stores verified facts on FTP so future chats do not repeat the
same verification work.
"""

import ftplib
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

FTP_HOST = os.environ.get("FTP_HOST", "ftp.geocities.ws")
FTP_USER = os.environ.get("FTP_USER", "PeakeCoin")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "Peake410")
FTP_BRAIN_DIR = os.environ.get("FTP_BRAIN_DIR", "/ai/brain")

FACTCHECK_INTERVAL_SECONDS = int(os.environ.get("FACTCHECK_INTERVAL_SECONDS", "1800"))
FACTCHECK_IDLE_SECONDS = int(os.environ.get("FACTCHECK_IDLE_SECONDS", "180"))
FACTCHECK_MAX_ITEMS_PER_RUN = int(os.environ.get("FACTCHECK_MAX_ITEMS_PER_RUN", "5"))
FACTCHECK_MAX_RESULTS = int(os.environ.get("FACTCHECK_MAX_RESULTS", "3"))
FACTCHECK_WHITELIST_HOSTS = [h.strip().lower() for h in os.environ.get("FACTCHECK_WHITELIST_HOSTS", "wikipedia.org,gov,edu").split(",") if h.strip()]
FACTCHECK_HEARTBEAT_PATH = os.environ.get("FACTCHECK_HEARTBEAT_PATH", f"{FTP_BRAIN_DIR.rstrip('/')}/factcheck_heartbeat.json")
FACTCHECK_STATE_PATH = os.environ.get("FACTCHECK_STATE_PATH", f"{FTP_BRAIN_DIR.rstrip('/')}/factcheck_state.json")
FACTCHECK_FACTS_PATH = os.environ.get("FACTCHECK_FACTS_PATH", f"{FTP_BRAIN_DIR.rstrip('/')}/factcheck_facts.json")


def _ftp_connect() -> ftplib.FTP:  # type: ignore[type-arg]
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, 21, timeout=20)
    ftp.login(FTP_USER, FTP_PASSWORD)
    ftp.set_pasv(True)
    return ftp


def _ensure_ftp_dir(ftp: ftplib.FTP, directory: str) -> None:  # type: ignore[type-arg]
    parts = [p for p in directory.split("/") if p]
    current = "/"
    for part in parts:
        current = f"{current}/{part}" if current != "/" else f"/{part}"
        try:
            ftp.cwd(current)
        except ftplib.error_perm:
            ftp.mkd(current)
            ftp.cwd(current)


def ftp_read_json(path: str, default):
    ftp = _ftp_connect()
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {path}", buf.write)
        buf.seek(0)
        return json.loads(buf.read().decode("utf-8"))
    except Exception:
        return default
    finally:
        ftp.quit()


def ftp_write_json(path: str, payload) -> bool:
    ftp = _ftp_connect()
    try:
        _ensure_ftp_dir(ftp, os.path.dirname(path) or "/")
        raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        ftp.storbinary(f"STOR {path}", io.BytesIO(raw))
        return True
    except Exception:
        return False
    finally:
        ftp.quit()


def is_whitelisted(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for allowed in FACTCHECK_WHITELIST_HOSTS:
        if host == allowed or host.endswith(f".{allowed}"):
            return True
    return False


def extract_query_terms(prompt: str) -> str:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9\-]+", prompt)]
    stop = {
        "what", "who", "where", "when", "why", "how", "is", "are", "the", "a", "an", "of", "in", "on", "for", "about", "to", "and", "or", "can", "you", "tell", "me"
    }
    filtered = [t for t in tokens if len(t) > 2 and t not in stop]
    return " ".join(filtered[:6]).strip()


def fetch_wikipedia_fact(query: str) -> Tuple[str, str]:
    if not query:
        return "", ""

    search_url = (
        "https://en.wikipedia.org/w/api.php?action=opensearch&search="
        + quote(query)
        + f"&limit={FACTCHECK_MAX_RESULTS}&namespace=0&format=json"
    )
    req = Request(search_url, headers={"User-Agent": "PeakeBot-FactCheck/1.0"})
    with urlopen(req, timeout=20) as resp:
        search_data = json.loads(resp.read().decode("utf-8"))

    titles = search_data[1] if isinstance(search_data, list) and len(search_data) > 1 else []
    if not titles:
        return "", ""

    title = str(titles[0]).strip()
    summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title)
    if not is_whitelisted(summary_url):
        return "", ""

    req2 = Request(summary_url, headers={"User-Agent": "PeakeBot-FactCheck/1.0"})
    with urlopen(req2, timeout=20) as resp2:
        summary = json.loads(resp2.read().decode("utf-8"))

    extract = str(summary.get("extract") or "").strip()
    page_url = str(summary.get("content_urls", {}).get("desktop", {}).get("page") or "").strip()
    if not extract:
        return "", ""
    if page_url and not is_whitelisted(page_url):
        return "", ""
    return extract, (page_url or summary_url)


def conversation_key(item: dict) -> str:
    ts = str(item.get("timestamp") or "")
    user = str(item.get("user_message") or item.get("user") or "")
    source = str(item.get("source_file") or "")
    return hashlib.sha256(f"{ts}|{user}|{source}".encode("utf-8")).hexdigest()


def load_conversations() -> List[dict]:
    conversations: List[dict] = []
    ftp = _ftp_connect()
    try:
        try:
            ftp.cwd(FTP_BRAIN_DIR)
        except ftplib.error_perm:
            return []

        names = [n for n in ftp.nlst() if n.endswith(".json")]
        for filename in sorted(names, reverse=True):
            buf = io.BytesIO()
            try:
                ftp.retrbinary(f"RETR {filename}", buf.write)
            except Exception:
                continue
            buf.seek(0)
            try:
                data = json.loads(buf.read().decode("utf-8"))
            except Exception:
                continue

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        copy = dict(item)
                        copy["source_file"] = filename
                        conversations.append(copy)
            elif isinstance(data, dict):
                flat_user = str(data.get("user_message") or data.get("user") or "").strip()
                flat_ai = str(data.get("ai_response") or data.get("ai") or "").strip()
                if flat_user or flat_ai:
                    conversations.append({
                        "timestamp": data.get("timestamp"),
                        "user_message": flat_user,
                        "ai_response": flat_ai,
                        "source_file": filename,
                    })

                file_conversations = data.get("conversations", [])
                if isinstance(file_conversations, list):
                    for entry in file_conversations:
                        if not isinstance(entry, dict):
                            continue
                        conversations.append({
                            "timestamp": entry.get("timestamp"),
                            "user_message": str(entry.get("user_message") or entry.get("user") or ""),
                            "ai_response": str(entry.get("ai_response") or entry.get("ai") or ""),
                            "source_file": filename,
                        })
    finally:
        ftp.quit()

    return conversations


def is_idle() -> bool:
    heartbeat = ftp_read_json(FACTCHECK_HEARTBEAT_PATH, {})
    last_chat = str(heartbeat.get("last_chat_at") or "").strip()
    if not last_chat:
        return True
    try:
        dt = datetime.fromisoformat(last_chat.replace("Z", "+00:00"))
    except Exception:
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age >= FACTCHECK_IDLE_SECONDS


def run_once() -> None:
    if not is_idle():
        return

    state = ftp_read_json(FACTCHECK_STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))

    facts_payload = ftp_read_json(FACTCHECK_FACTS_PATH, {
        "profile": {
            "siteOrigin": "factcheck_worker",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "facts": [],
        "notes": [],
        "conversations": [],
        "remoteSources": [],
    })

    existing_ids = set()
    for f in facts_payload.get("facts", []):
        if isinstance(f, dict):
            existing_ids.add(str(f.get("id") or ""))

    candidates = []
    for item in load_conversations():
        user_msg = str(item.get("user_message") or "").strip()
        if not user_msg or "?" not in user_msg:
            continue
        key = conversation_key(item)
        if key in processed:
            continue
        candidates.append((key, user_msg))

    if not candidates:
        return

    added = 0
    for key, user_msg in candidates[:FACTCHECK_MAX_ITEMS_PER_RUN]:
        query = extract_query_terms(user_msg)
        try:
            fact_text, source_url = fetch_wikipedia_fact(query)
        except Exception:
            fact_text, source_url = "", ""

        processed.add(key)

        if not fact_text:
            continue

        fact_hash = hashlib.sha256(f"{query}|{fact_text}".encode("utf-8")).hexdigest()[:12]
        fact_id = f"factcheck_{fact_hash}"
        if fact_id in existing_ids:
            continue

        facts_payload.setdefault("facts", []).append({
            "id": fact_id,
            "category": "factcheck",
            "fact": fact_text.split(".")[0].strip(),
            "context": fact_text,
            "confidence": 0.9,
            "sources": [source_url] if source_url else ["wikipedia.org"],
            "query": user_msg,
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
        })
        existing_ids.add(fact_id)
        added += 1

    state["processed"] = list(processed)[-5000:]
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()
    facts_payload["profile"]["updatedAt"] = datetime.now(timezone.utc).isoformat()

    ftp_write_json(FACTCHECK_STATE_PATH, state)
    if added > 0:
        ftp_write_json(FACTCHECK_FACTS_PATH, facts_payload)


def main() -> None:
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(max(30, FACTCHECK_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
