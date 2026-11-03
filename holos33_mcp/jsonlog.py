"""Eén JSONL-regel per toolcall. Geen IP-adres. Argumenten alleen als hash.

Uitzondering: een zoekvraag zonder treffers wordt ingekort bewaard, omdat dat precies
de contentgaten zijn die Stefan wil zien.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def args_hash(args: dict) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_call(path: Path, **fields) -> None:
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        # Loggen mag een antwoord nooit breken.
        pass
