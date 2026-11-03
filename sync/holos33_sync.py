"""Bouwt een gevalideerde momentopname van holos33.com voor de MCP-server.

Alleen stdlib. Haalt alleen URL's onder https://holos33.com/ op. Schrijft een nieuwe
release-map en verzet de symlink ``current`` pas als de validatie slaagt. Bij een fout
blijft de vorige release live en eindigt het proces met exitcode 1.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://holos33.com"
UA = "holos33-mcp-sync/1.0 (+https://holos33.com)"
LANGS = ("nl", "en")
MAX_FETCH_BYTES = 3 * 1024 * 1024
KEEP_RELEASES = 5
ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")

# Paginasleutels zijn de Engelse slugs. Onbekende pagina's krijgen soort "other" en een waarschuwing.
PAGE_KINDS = {
    "home": "core",
    "how-it-works": "core",
    "ai-coach": "core",
    "biofeedback": "core",
    "modules": "core",
    "science": "core",
    "mission-vision": "core",
    "pricing": "access",
    "faq": "faq",
    "contact": "contact",
    "trust-center": "privacy",
    "sub-processors": "privacy",
    "data-management": "privacy",
    "privacy-policy": "legal",
    "terms-and-conditions": "legal",
    "early-access-terms": "legal",
    "disclaimer": "legal",
    "cookie-policy": "legal",
    "for-professionals": "partner",
    "for-businesses": "partner",
    "research-partnerships": "partner",
    "investors": "partner",
}
EXCLUDED_PAGES = {"brand-guidelines", "knowledge-base"}

# Minimale aantallen per taal. Minder betekent dat de bron kapot is, niet dat de site kromp.
MIN_COUNTS = {"pages": 15, "modules": 4, "articles": 5, "faq": 5}
MAX_DROP_RATIO = 0.4
# Pagina's waar de tools op leunen moeten echte tekst hebben; FAQ-items moeten een echte vraag en antwoord zijn.
MIN_PAGE_WORDS_KINDS = ("core", "access", "privacy")
MIN_PAGE_WORDS = 60


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- ophalen


def assert_allowed(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "holos33.com":
        raise SyncError(f"url buiten holos33.com geweigerd: {url}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Volgt geen redirects: een verhuisde bron is een fout, geen stille sprong naar elders."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError(f"redirect {code} {newurl}")


class Fetcher:
    def __init__(self, delay: float = 0.7, retries: int = 2, timeout: float = 20.0):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.count = 0
        self._last = 0.0

    def get(self, url: str) -> tuple[str, dict]:
        assert_allowed(url)
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
            try:
                # SyncError (redirect, te groot) is geen URLError/OSError en wordt dus niet opnieuw geprobeerd.
                with urllib.request.build_opener(_NoRedirect).open(request, timeout=self.timeout) as response:
                    final = response.geturl()
                    assert_allowed(final)
                    body = response.read(MAX_FETCH_BYTES + 1)
                    if len(body) > MAX_FETCH_BYTES:
                        raise SyncError(f"antwoord te groot: {url}")
                    self.count += 1
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    return body.decode("utf-8", errors="replace"), headers
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error = exc
                time.sleep(1.5 * (attempt + 1))
        raise SyncError(f"ophalen mislukt: {url}: {error}")

    def json(self, url: str):
        body, _ = self.get(url)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SyncError(f"geen json: {url}") from exc


PER_PAGE = 100
MAX_API_PAGES = 10


def fetch_all(fetcher, url_without_page: str) -> list:
    """Haalt alle batches van een WP-REST-lijst op (``url`` bevat al ``per_page=100``).

    Stopt op de header X-WP-TotalPages of op een korte batch; bij precies 100 items vraagt WordPress
    anders een pagina die niet bestaat (HTTP 400).
    """
    items: list = []
    for page in range(1, MAX_API_PAGES + 1):
        url = f"{url_without_page}&page={page}"
        body, headers = fetcher.get(url)
        try:
            batch = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SyncError(f"geen json: {url}") from exc
        if not isinstance(batch, list):
            raise SyncError(f"geen lijst: {url}")
        items.extend(batch)
        try:
            total_pages = int(headers.get("x-wp-totalpages", "1"))
        except (TypeError, ValueError):
            total_pages = 1
        if len(batch) < PER_PAGE or page >= total_pages:
            return items
    raise SyncError(f"meer dan {MAX_API_PAGES} pagina's: {url_without_page}")


# ---------------------------------------------------------------- html naar markdown


def clean_text(text: str) -> str:
    return ZERO_WIDTH.sub("", html.unescape(text or ""))


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))


SKIP_TAGS = {
    "script", "style", "svg", "noscript", "form", "nav", "footer", "header", "iframe", "template",
    "button", "select", "textarea", "dialog", "canvas", "video", "audio", "picture", "head",
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "aside", "blockquote", "figure", "figcaption",
    "dl", "dt", "dd", "details", "summary", "table",
}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
SKIP_MARKERS = ("cmplz", "breadcrumb", "cookie", "skip-link", "screen-reader-text")


class Markdownizer(HTMLParser):
    """Zet de leesbare inhoud van een pagina om naar eenvoudige Markdown.

    Navigatie, footer, formulieren, cookiebanners en verborgen elementen vallen weg.
    """

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.stack: list[str] = []
        self.skip_at: int | None = None
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.prefix = ""
        self.lists: list[list] = []
        self.link: tuple[str | None, int] | None = None
        self.table: list[str] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.h1: str | None = None
        self._in_h1 = False
        self._h1_buf: list[str] = []

    def _should_skip(self, tag: str, attrs: dict) -> bool:
        if tag in SKIP_TAGS or "hidden" in attrs or attrs.get("aria-hidden") == "true":
            return True
        ident = f"{attrs.get('id') or ''} {attrs.get('class') or ''}".lower()
        return any(marker in ident for marker in SKIP_MARKERS)

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split())
        if text:
            self.blocks.append(self.prefix + text)
        self.buf = []
        self.prefix = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if self.skip_at is not None:
            if tag not in VOID_TAGS:
                self.stack.append(tag)
            return
        if self._should_skip(tag, attrs_d):
            if tag not in VOID_TAGS:
                self.stack.append(tag)
                self.skip_at = len(self.stack)
            return
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        if tag in HEADINGS:
            self._flush()
            self.prefix = "#" * HEADINGS[tag] + " "
            if tag == "h1" and self.h1 is None:
                self._in_h1 = True
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                mark = f"{self.lists[-1][1]}. "
            else:
                mark = "- "
            self.prefix = "  " * depth + mark
        elif tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "br":
            self._data(" ")
        elif tag == "a":
            href = (attrs_d.get("href") or "").strip()
            target = None
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                absolute = urllib.parse.urljoin(self.base_url, href)
                if absolute.startswith(("http://", "https://")):
                    target = clean_url(absolute)
            self.link = (target, len(self.buf))
        elif tag in BLOCK_TAGS:
            self._flush()

    def _close(self, tag: str) -> None:
        if tag in HEADINGS:
            if tag == "h1" and self._in_h1:
                self.h1 = " ".join("".join(self._h1_buf).split()) or None
                self._in_h1 = False
            self._flush()
        elif tag == "li":
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag in ("td", "th"):
            if self.row is not None and self.cell is not None:
                self.row.append(" ".join("".join(self.cell).split()).replace("|", "/"))
            self.cell = None
        elif tag == "tr":
            if self.table is not None and self.row:
                self.table.append("| " + " | ".join(self.row) + " |")
                if len(self.table) == 1:
                    self.table.append("|" + " --- |" * len(self.row))
            self.row = None
        elif tag == "table":
            if self.table:
                self.blocks.append("\n".join(self.table))
            self.table = None
        elif tag == "a":
            if self.link is not None:
                target, start = self.link
                text = " ".join("".join(self.buf[start:]).split())
                if target and text and start <= len(self.buf):
                    self.buf[start:] = [f" [{text}]({target}) "]
            self.link = None
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            current = self.stack.pop()
            if self.skip_at is not None:
                if len(self.stack) < self.skip_at:
                    self.skip_at = None
            else:
                self._close(current)
            if current == tag:
                break

    def _data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buf.append(data)
        if self._in_h1:
            self._h1_buf.append(data)

    def handle_data(self, data):
        if self.skip_at is None:
            self._data(ZERO_WIDTH.sub("", data))

    def markdown(self) -> str:
        self._flush()
        out: list[str] = []
        for block in self.blocks:
            block = block.rstrip()
            core = block.strip().lstrip("-#0123456789. ")
            # Losse emoji, pijltjes en cijfersnippers ("5/5", "→") zeggen een agent niets.
            if len(core) <= 3 or not re.search(r"[^\W\d_]{2}", core):
                continue
            if not out or out[-1] != block:
                out.append(block)
        return "\n\n".join(out)


def html_to_markdown(source: str, base_url: str) -> tuple[str, str | None]:
    parser = Markdownizer(base_url)
    body_start = source.find("<body")
    parser.feed(source[body_start:] if body_start >= 0 else source)
    parser.close()
    return parser.markdown(), parser.h1


# ---------------------------------------------------------------- metadata uit html


def hreflang_map(source: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in re.findall(r"<link\b[^>]*>", source, flags=re.I):
        lang = re.search(r'hreflang="([^"]+)"', tag)
        href = re.search(r'href="([^"]+)"', tag)
        if lang and href and 'rel="alternate"' in tag:
            found[lang.group(1).lower()] = clean_url(html.unescape(href.group(1)))
    return found


def meta_description(source: str) -> str | None:
    match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', source, flags=re.I)
    return clean_text(match.group(1)).strip() if match else None


def json_ld_nodes(source: str) -> list[dict]:
    nodes: list[dict] = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', source, flags=re.S | re.I):
        try:
            data = json.loads(ZERO_WIDTH.sub("", block))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                nodes.extend(n for n in item["@graph"] if isinstance(n, dict))
            elif isinstance(item, dict):
                nodes.append(item)
    return nodes


def faq_from_html(source: str) -> list[dict]:
    items: list[dict] = []
    for node in json_ld_nodes(source):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "FAQPage" not in types:
            continue
        for entry in node.get("mainEntity") or []:
            question = clean_text(entry.get("name", "")).strip()
            answer = entry.get("acceptedAnswer") or {}
            answer_text = clean_text(answer.get("text", "") if isinstance(answer, dict) else "")
            answer_text = " ".join(re.sub(r"<[^>]+>", " ", answer_text).split())
            if question and answer_text:
                items.append({"question": question, "answer": answer_text})
    return items


def iso_gmt(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.endswith("Z") else value + "Z"


def slug_of(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.strip("/").split("/")
    return path[-1] if path and path[-1] not in LANGS else "home"


# ---------------------------------------------------------------- regels uit het feitenbestand


DEFAULT_RULE_SCOPE = ("faq", "pages", "modules")


def compile_rules(facts: dict, scope: str) -> list[tuple[re.Pattern, str]]:
    """Regels voor één soort content. Blogartikelen vallen standaard buiten de regels."""
    rules = []
    for rule in (facts.get("content_rules") or {}).get("blocked_patterns", []):
        if scope in rule.get("scope", DEFAULT_RULE_SCOPE):
            rules.append((re.compile(rule["pattern"], flags=re.I), rule["reason"]))
    return rules


def apply_rules(markdown: str, rules, where: str, warnings: list[dict], notice: str) -> str:
    """Verwijdert blokken die het feitenbestand tegenspreken en laat een zichtbare melding achter."""
    if not rules:
        return markdown
    kept = []
    for block in markdown.split("\n\n"):
        hit = next(((p, r) for p, r in rules if p.search(block)), None)
        if hit:
            warnings.append({"where": where, "reason": hit[1], "excerpt": block[:160]})
            if not kept or kept[-1] != notice:
                kept.append(notice)
        else:
            kept.append(block)
    return "\n\n".join(kept)


def blocked(text: str, rules) -> str | None:
    for pattern, reason in rules:
        if pattern.search(text):
            return reason
    return None


def safe_description(text: str | None, rules, where: str, warnings: list[dict]) -> str | None:
    """Een beschrijving die een regel raakt, valt weg (None) met een waarschuwing."""
    reason = blocked(text, rules) if text else None
    if reason:
        warnings.append({"where": where, "reason": reason, "excerpt": text[:160]})
        return None
    return text


def safe_title(candidates: list[str | None], key: str, rules, where: str, warnings: list[dict]) -> str | None:
    """Eerste titel-kandidaat (in de gewone volgorde) die geen regel raakt.

    Keuze: een titel moet leesbaar blijven, dus geen removed_notice. Raakt een kandidaat een regel,
    dan waarschuwing en door naar de volgende (h1 of WordPress-titel); raken ze allemaal, dan de key
    met spaties ("ai-coach" -> "Ai coach").
    """
    options = [c for c in candidates if c]
    for option in options:
        reason = blocked(option, rules)
        if reason is None:
            return option
        warnings.append({"where": where, "reason": reason, "excerpt": option[:160]})
    # Ook zonder kandidaten nooit None: de index en de tools rekenen op een string.
    return key.split(":")[-1].replace("-", " ").capitalize()


# ---------------------------------------------------------------- bouwen


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def build(fetcher: Fetcher, facts: dict[str, dict]) -> tuple[dict, list[dict]]:
    warnings: list[dict] = []
    out: dict = {}
    en_url_to_key: dict[str, str] = {}

    for lang in ("en", "nl"):
        lang_facts = facts.get(lang, {})
        page_rules = compile_rules(lang_facts, "pages")
        faq_rules = compile_rules(lang_facts, "faq")
        module_rules = compile_rules(lang_facts, "modules")
        article_rules = compile_rules(lang_facts, "articles")
        notice = (facts.get(lang, {}).get("content_rules") or {}).get("removed_notice", "[removed]")

        pages_raw = fetch_all(fetcher, f"{BASE}/{lang}/wp-json/wp/v2/pages?per_page={PER_PAGE}&_fields=id,slug,link,modified_gmt,title")
        pages = []
        faq: list[dict] = []
        for item in pages_raw:
            link = clean_url(item["link"])
            source, _ = fetcher.get(link)
            alternates = hreflang_map(source)
            if lang == "en":
                key = slug_of(link)
                en_url_to_key[link] = key
            else:
                key = en_url_to_key.get(alternates.get("en", ""), f"nl-only:{item['slug']}")
            if key in EXCLUDED_PAGES:
                continue
            kind = PAGE_KINDS.get(key, "other")
            if kind == "other":
                warnings.append({"where": f"pages.{lang}.{key}", "reason": "onbekende pagina, soort other"})
            markdown, h1 = html_to_markdown(source, link)
            markdown = apply_rules(markdown, page_rules, f"pages.{lang}.{key}", warnings, notice)
            if word_count(markdown) < 30:
                warnings.append({"where": f"pages.{lang}.{key}", "reason": "pagina bijna leeg na extractie"})
            other = "nl" if lang == "en" else "en"
            if key == "faq":
                for entry in faq_from_html(source):
                    reason = blocked(entry["question"] + " " + entry["answer"], faq_rules)
                    if reason:
                        warnings.append({"where": f"faq.{lang}", "reason": reason, "excerpt": entry["question"][:160]})
                        continue
                    faq.append({"id": f"faq-{len(faq) + 1}", **entry, "source_url": link})
            pages.append(
                {
                    "key": key,
                    "kind": kind,
                    "lang": lang,
                    "title": safe_title([h1, clean_text(item["title"]["rendered"]).strip()], key, page_rules,
                                        f"pages.{lang}.{key}.title", warnings),
                    "description": safe_description(meta_description(source), page_rules, f"pages.{lang}.{key}.description", warnings),
                    "url": link,
                    "alternate_url": alternates.get(other),
                    "last_modified": iso_gmt(item.get("modified_gmt")),
                    "markdown": markdown,
                    "words": word_count(markdown),
                }
            )

        modules_raw = fetch_all(fetcher, f"{BASE}/{lang}/wp-json/wp/v2/modules?per_page={PER_PAGE}&_fields=id,slug,link,modified_gmt,title,menu_order")
        modules = []
        for item in modules_raw:
            link = clean_url(item["link"])
            source, _ = fetcher.get(link)
            alternates = hreflang_map(source)
            key = slug_of(link) if lang == "en" else slug_of(alternates.get("en", link))
            markdown, h1 = html_to_markdown(source, link)
            markdown = apply_rules(markdown, module_rules, f"modules.{lang}.{key}", warnings, notice)
            modules.append(
                {
                    "key": key,
                    "lang": lang,
                    "slug": item["slug"],
                    "title": safe_title([clean_text(item["title"]["rendered"]).strip(), h1], key, module_rules,
                                        f"modules.{lang}.{key}.title", warnings),
                    "summary": safe_description(meta_description(source), module_rules, f"modules.{lang}.{key}.summary", warnings),
                    "url": link,
                    "alternate_url": alternates.get("nl" if lang == "en" else "en"),
                    "last_modified": iso_gmt(item.get("modified_gmt")),
                    "markdown": markdown,
                    "words": word_count(markdown),
                }
            )
        modules.sort(key=lambda m: m["key"])

        cats = fetch_all(fetcher, f"{BASE}/{lang}/wp-json/wp/v2/categories?per_page={PER_PAGE}&_fields=id,slug,name")
        cat_names = {c["id"]: clean_text(c["name"]) for c in cats}
        posts_raw = fetch_all(
            fetcher,
            f"{BASE}/{lang}/wp-json/wp/v2/posts?per_page={PER_PAGE}&_fields=id,slug,link,date_gmt,modified_gmt,title,excerpt,content,categories",
        )
        articles = []
        for item in posts_raw:
            link = clean_url(item["link"])
            source, _ = fetcher.get(link)
            alternates = hreflang_map(source)
            markdown, _ = html_to_markdown(item["content"]["rendered"], link)
            markdown = apply_rules(markdown, article_rules, f"articles.{lang}.{item['slug']}", warnings, notice)
            excerpt = " ".join(re.sub(r"<[^>]+>", " ", clean_text(item["excerpt"]["rendered"])).split())
            articles.append(
                {
                    "key": item["slug"],
                    "lang": lang,
                    "title": safe_title([clean_text(item["title"]["rendered"]).strip()], item["slug"], article_rules,
                                        f"articles.{lang}.{item['slug']}.title", warnings),
                    "summary": safe_description(meta_description(source) or excerpt[:300], article_rules,
                                                f"articles.{lang}.{item['slug']}.summary", warnings),
                    "categories": [cat_names.get(c, str(c)) for c in item.get("categories", [])],
                    "url": link,
                    "alternate_url": alternates.get("nl" if lang == "en" else "en"),
                    "date_published": iso_gmt(item.get("date_gmt")),
                    "last_modified": iso_gmt(item.get("modified_gmt")),
                    "markdown": markdown,
                    "words": word_count(markdown),
                }
            )
        articles.sort(key=lambda a: a["date_published"] or "", reverse=True)

        out[lang] = {"pages": sorted(pages, key=lambda p: p["key"]), "modules": modules, "articles": articles, "faq": faq}
    return out, warnings


def validate(data: dict, facts: dict[str, dict], previous: dict | None) -> None:
    for lang in LANGS:
        if lang not in facts:
            raise SyncError(f"feitenbestand ontbreekt voor {lang}")
        for required in ("facts_version", "brand", "what_it_is", "status", "entity", "access", "links", "disclaimer"):
            if required not in facts[lang]:
                raise SyncError(f"facts.{lang}.json mist veld {required}")
        for kind, minimum in MIN_COUNTS.items():
            count = len(data[lang][kind])
            if count < minimum:
                raise SyncError(f"{kind}.{lang}: {count} items, minimaal {minimum}")
            if previous:
                before = previous.get("counts", {}).get(lang, {}).get(kind, 0)
                if before and count < before * (1 - MAX_DROP_RATIO):
                    raise SyncError(f"{kind}.{lang}: van {before} naar {count} items, te grote daling")
        for module in data[lang]["modules"]:
            if module["words"] < 80:
                raise SyncError(f"module {lang}/{module['key']} heeft te weinig tekst ({module['words']} woorden)")
        for page in data[lang]["pages"]:
            if page.get("kind") in MIN_PAGE_WORDS_KINDS and page.get("words", 0) < MIN_PAGE_WORDS:
                raise SyncError(f"pagina {lang}/{page['key']} ({page['kind']}) heeft te weinig tekst ({page.get('words', 0)} woorden)")
        for item in data[lang]["faq"]:
            if word_count(item.get("question", "")) < 3 or word_count(item.get("answer", "")) < 5:
                raise SyncError(f"faq {lang}/{item.get('id')}: vraag of antwoord te kort")
    if facts["nl"]["facts_version"] != facts["en"]["facts_version"]:
        raise SyncError("facts_version verschilt tussen nl en en")


def load_facts(facts_dir: Path) -> dict[str, dict]:
    facts = {}
    for lang in LANGS:
        path = facts_dir / f"facts.{lang}.json"
        if path.is_file():
            facts[lang] = json.loads(path.read_text(encoding="utf-8"))
    return facts


def _write_synced(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_release(root: Path, data: dict, facts: dict[str, dict], warnings: list[dict], fetch_count: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / stamp
    tmp = releases / f".{stamp}.tmp"
    # Halve releases van afgebroken runs (ook die van eerdere tijdstempels) opruimen.
    for leftover in releases.iterdir():
        if leftover.name.startswith(".") and leftover.name.endswith(".tmp") and leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover)
    tmp.mkdir()
    for lang in LANGS:
        for kind in ("pages", "modules", "articles", "faq"):
            _write_synced(tmp / f"{kind}.{lang}.json", json.dumps(data[lang][kind], ensure_ascii=False, indent=1))
        _write_synced(tmp / f"facts.{lang}.json", json.dumps(facts[lang], ensure_ascii=False, indent=1))
    manifest = {
        "release": stamp,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": BASE,
        "facts_version": facts["en"]["facts_version"],
        "counts": {lang: {kind: len(data[lang][kind]) for kind in ("pages", "modules", "articles", "faq")} for lang in LANGS},
        "fetches": fetch_count,
        "warning_count": len(warnings),
        "warnings": warnings,
    }
    _write_synced(tmp / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    for path in [tmp, *tmp.iterdir()]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    tmp.rename(target)
    # Bestanden en mapingangen op schijf vóór ``current`` verspringt (stroomuitval mag geen lege release live zetten).
    _fsync_dir(target)
    _fsync_dir(releases)

    link_tmp = root / ".current.tmp"
    if link_tmp.is_symlink() or link_tmp.exists():
        link_tmp.unlink()
    link_tmp.symlink_to(Path("releases") / stamp)
    os.replace(link_tmp, root / "current")

    old = sorted(p for p in releases.iterdir() if p.is_dir() and not p.name.startswith("."))
    for path in old[:-KEEP_RELEASES]:
        shutil.rmtree(path, ignore_errors=True)
    return target


def previous_manifest(root: Path) -> dict | None:
    path = root / "current" / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def log(path: Path | None, row: dict) -> None:
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **row}
    line = json.dumps(row, ensure_ascii=False)
    print(line)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="holos33.com naar MCP-momentopname")
    parser.add_argument("--content", default=os.environ.get("HOLOS33_MCP_CONTENT", "/srv/holos33-mcp-content"))
    parser.add_argument("--facts", default=os.environ.get("HOLOS33_FACTS", "/opt/holos33-mcp/app/facts"))
    parser.add_argument("--log", default=os.environ.get("HOLOS33_SYNC_LOG", "/var/log/holos33-mcp/sync.jsonl"))
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--dump", help="alleen ophalen en als json naar dit pad schrijven, geen release")
    args = parser.parse_args(argv)
    os.umask(0o022)

    root = Path(args.content)
    log_path = Path(args.log) if args.log else None
    started = time.monotonic()
    fetcher = Fetcher(delay=args.delay)
    try:
        facts = load_facts(Path(args.facts))
        data, warnings = build(fetcher, facts)
        if args.dump:
            Path(args.dump).write_text(json.dumps({"data": data, "warnings": warnings}, ensure_ascii=False, indent=1), encoding="utf-8")
            log(None, {"status": "dumped", "path": args.dump, "fetches": fetcher.count})
            return 0
        validate(data, facts, previous_manifest(root))
        target = write_release(root, data, facts, warnings, fetcher.count)
    except Exception as exc:  # elke fout houdt de vorige release live
        log(log_path, {"status": "error", "error": f"{exc.__class__.__name__}: {exc}", "fetches": fetcher.count,
                       "duration_s": round(time.monotonic() - started, 1)})
        return 1
    log(
        log_path,
        {
            "status": "ok",
            "release": target.name,
            "counts": {lang: {k: len(v) for k, v in data[lang].items()} for lang in LANGS},
            "warnings": len(warnings),
            "fetches": fetcher.count,
            "duration_s": round(time.monotonic() - started, 1),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
