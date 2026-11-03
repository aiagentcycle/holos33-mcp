"""Leest de momentopname die de sync maakt. Geen paden of URL's uit toolargumenten.

De symlink ``current`` wijst naar de actieve release. Verspringt hij, dan laadt de server
de nieuwe release bij de volgende call. Een kapotte nieuwe release vervangt nooit een
werkende oude.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from holos33_mcp.search import Bm25

LANGS = ("nl", "en")
KINDS = ("pages", "modules", "articles", "faq")
RECHECK_SECONDS = 5.0


class ContentUnavailable(Exception):
    pass


@dataclass
class LangContent:
    facts: dict
    pages: dict[str, dict]
    modules: dict[str, dict]
    module_aliases: dict[str, str]
    articles: dict[str, dict]
    faq: list[dict]
    index: Bm25
    docs: dict[str, dict] = field(default_factory=dict)


@dataclass
class Loaded:
    release: str
    path: str
    manifest: dict
    langs: dict[str, LangContent]


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _index_docs(content: dict[str, list | dict]) -> tuple[list[tuple[str, str]], dict[str, dict]]:
    docs: list[tuple[str, str]] = []
    meta: dict[str, dict] = {}

    def add(doc_id: str, kind: str, item: dict, title: str, summary: str, body: str, url: str | None):
        # De (Engelse) key telt mee, zodat "pricing" de pagina met titel "Start for free" vindt.
        key_words = str(item.get("key") or "").replace("-", " ")
        docs.append((doc_id, f"{title} {title} {title} {key_words} {key_words} {summary} {summary} {body}"))
        meta[doc_id] = {
            "type": kind,
            "key": item.get("key") or item.get("id"),
            "title": title,
            "summary": summary,
            "url": url,
            "last_modified": item.get("last_modified"),
        }

    for item in content["articles"]:
        add(f"article:{item['key']}", "article", item, item["title"], item.get("summary") or "", item["markdown"], item["url"])
    for item in content["modules"]:
        add(f"module:{item['key']}", "module", item, item["title"], item.get("summary") or "", item["markdown"], item["url"])
    for item in content["pages"]:
        add(f"page:{item['key']}", "page", item, item["title"], item.get("description") or "", item["markdown"], item["url"])
    for item in content["faq"]:
        add(f"faq:{item['id']}", "faq", item, item["question"], "", item["answer"], item.get("source_url"))
    return docs, meta


def load_release(path: Path) -> Loaded:
    manifest = _read(path / "manifest.json")
    langs: dict[str, LangContent] = {}
    for lang in LANGS:
        raw = {kind: _read(path / f"{kind}.{lang}.json") for kind in KINDS}
        facts = _read(path / f"facts.{lang}.json")
        if not isinstance(facts, dict) or facts.get("lang") != lang:
            raise ContentUnavailable(f"facts.{lang}.json ongeldig")
        for kind in KINDS:
            if not isinstance(raw[kind], list) or not raw[kind]:
                raise ContentUnavailable(f"{kind}.{lang}.json leeg of ongeldig")
        modules = {item["key"]: item for item in raw["modules"]}
        aliases = {}
        for item in raw["modules"]:
            aliases[item["key"]] = item["key"]
            aliases[item["slug"]] = item["key"]
        docs, meta = _index_docs(raw)
        langs[lang] = LangContent(
            facts=facts,
            pages={item["key"]: item for item in raw["pages"]},
            modules=modules,
            module_aliases=aliases,
            articles={item["key"]: item for item in raw["articles"]},
            faq=raw["faq"],
            index=Bm25(docs),
            docs=meta,
        )
    return Loaded(release=manifest.get("release", path.name), path=str(path), manifest=manifest, langs=langs)


class Snapshot:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._loaded: Loaded | None = None
        self._checked = 0.0
        self.last_error: str | None = None
        self._failed_target: str | None = None

    def get(self) -> Loaded:
        with self._lock:
            now = time.monotonic()
            if self._loaded is not None and now - self._checked < RECHECK_SECONDS:
                return self._loaded
            self._checked = now
            try:
                target = os.path.realpath(self.root / "current")
            except OSError as exc:
                target = None
                self.last_error = f"current onleesbaar: {exc.__class__.__name__}"
            # Een mislukte release wordt niet elke 5 s opnieuw geladen; pas als ``current`` verspringt.
            if target and target != self._failed_target and (self._loaded is None or target != self._loaded.path):
                try:
                    self._loaded = load_release(Path(target))
                    self.last_error = None
                    self._failed_target = None
                except Exception as exc:
                    # Oude release blijft actief; de fout is zichtbaar via /healthz.
                    self._failed_target = target
                    self.last_error = f"release {Path(target).name} niet geladen: {exc.__class__.__name__}: {exc}"
            if self._loaded is None:
                raise ContentUnavailable(self.last_error or "geen release beschikbaar")
            return self._loaded


def split_parts(markdown: str, size: int) -> list[str]:
    """Knipt op alinea-grenzen in delen van ongeveer ``size`` tekens."""
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for block in markdown.split("\n\n"):
        while len(block) > size:
            if current:
                parts.append("\n\n".join(current))
                current, length = [], 0
            parts.append(block[:size])
            block = block[size:]
        if length + len(block) > size and current:
            parts.append("\n\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts or [""]
