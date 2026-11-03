"""Agent-leesbare bestanden uit dezelfde momentopname als de MCP-tools.

/llms.txt is een korte index, /llms-full.txt de volledige tekst, /md/... één document als Markdown.
Alles komt uit de actieve release; er wordt niets live van holos33.com gehaald.
"""

from __future__ import annotations

from holos33_mcp.content import Loaded

import hashlib
import json

from holos33_mcp import __version__

BASE = "https://mcp.holos33.com"
MCP_URL = f"{BASE}/mcp"
SERVER_NAME = "com.holos33/holos33"
# Registry en Server Card staan maximaal 100 tekens toe.
DESCRIPTION = "Read-only facts on Holos33, an EU privacy-first wellbeing platform: modules, Early Access, privacy."
PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
# Het logo van holos33.com, door deze server zelf geserveerd zodat een wijziging aan de site het niet breekt.
ICONS = [
    {"src": f"{BASE}/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]},
    {"src": f"{BASE}/icon-192.png", "mimeType": "image/png", "sizes": ["192x192"]},
]
# Publieke broncode (alleen de MCP-server, zonder beheerdocumenten); zie deploy/publish-github.sh.
REPOSITORY = {"url": "https://github.com/aiagentcycle/holos33-mcp", "source": "github"}


def server_card() -> dict:
    """SEP-2127 Server Card. Bewust zonder toollijst; die komt uit tools/list."""
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
        "name": SERVER_NAME,
        "title": "Holos33",
        "version": __version__,
        "description": DESCRIPTION,
        "websiteUrl": "https://holos33.com",
        "icons": ICONS,
        "remotes": [{"type": "streamable-http", "url": MCP_URL, "supportedProtocolVersions": PROTOCOL_VERSIONS}],
    }


def registry_server_json() -> dict:
    """server.json voor het officiële MCP Registry (schema 2025-12-11)."""
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": SERVER_NAME,
        "title": "Holos33",
        "description": DESCRIPTION,
        "version": __version__,
        "websiteUrl": "https://holos33.com",
        "icons": ICONS,
        "repository": REPOSITORY,
        "remotes": [{"type": "streamable-http", "url": MCP_URL}],
    }


def ai_catalog() -> dict:
    return {
        "specVersion": "1.0",
        "entries": [
            {
                "identifier": "urn:air:holos33.com:mcp:holos33",
                "type": "application/mcp-server-card+json",
                "url": f"{MCP_URL}/server-card",
            }
        ],
    }


def stable_json(value: dict) -> tuple[str, str]:
    body = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return body, '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'
FULL_TEXT_PAGE_KINDS = ("core", "access", "faq", "partner", "contact")


def md_path(lang: str, kind: str, key: str) -> str:
    return f"/md/{lang}/{kind}/{key}.md"


def document_markdown(item: dict, lang: str) -> str:
    lines = [f"# {item['title']}", "", f"Source: {item['url']}"]
    if item.get("alternate_url"):
        lines.append(f"Other language: {item['alternate_url']}")
    lines.append(f"Last modified: {item.get('last_modified') or 'unknown'}")
    lines += ["", item["markdown"].strip(), ""]
    return "\n".join(lines)


def _facts_block(facts: dict) -> list[str]:
    access = facts["access"]
    entity = facts["entity"]
    out = [
        facts["what_it_is"],
        "",
        f"- Status: {facts['status']['summary']}",
        f"- Price: Early Access €{access['early_access_price_eur']}, no credit card, valid through {access['valid_through']}. {access['paid_plans']}",
        f"- Who: {facts['eligibility']['min_age']}+ in {', '.join(facts['eligibility']['regions'])}.",
        f"- Wearables (optional): {', '.join(facts['wearables']['supported'])}.",
        f"- Company: {entity['legal_name']} (trade name {entity['trade_name']}), {entity['address']}, KvK {entity['kvk']}.",
        f"- Disclaimer: {facts['disclaimer']}",
    ]
    return out


def llms_index(data: Loaded) -> str:
    en = data.langs["en"]
    nl = data.langs["nl"]
    out = [
        "# Holos33",
        "",
        f"> {en.facts['what_it_is']}",
        "",
        *_facts_block(en.facts),
        "",
        "## For AI agents",
        f"- [MCP server]({MCP_URL}): read-only, Streamable HTTP, no login. Tools for overview, modules, search, articles, pages, FAQ, pricing, privacy, partners and contact.",
        f"- [Full text]({BASE}/llms-full.txt): every module, key page and article in one file.",
        f"- Facts version {en.facts['facts_version']}, snapshot {data.release}.",
        "",
        "## Modules",
    ]
    for module in en.modules.values():
        out.append(f"- [{module['title']}]({BASE}{md_path('en', 'module', module['key'])}): {module.get('summary') or ''}".rstrip(": "))
    out += ["", "## Pages"]
    for page in sorted(en.pages.values(), key=lambda p: (p["kind"], p["key"])):
        out.append(f"- [{page['title']}]({BASE}{md_path('en', 'page', page['key'])}) ({page['kind']})")
    out += ["", "## Knowledge base"]
    for article in en.articles.values():
        out.append(f"- [{article['title']}]({BASE}{md_path('en', 'article', article['key'])})")
    out += ["", "## Nederlands", f"- {nl.facts['what_it_is']}"]
    for module in nl.modules.values():
        out.append(f"- [{module['title']}]({BASE}{md_path('nl', 'module', module['key'])})")
    for article in nl.articles.values():
        out.append(f"- [{article['title']}]({BASE}{md_path('nl', 'article', article['key'])})")
    out.append("")
    return "\n".join(out)


def llms_full(data: Loaded) -> str:
    out = [f"# Holos33: full text for AI agents", "", f"Snapshot {data.release}, facts version {data.manifest.get('facts_version')}. MCP server: {MCP_URL}", ""]
    for lang in ("en", "nl"):
        c = data.langs[lang]
        out += [f"## {'English' if lang == 'en' else 'Nederlands'}", "", *_facts_block(c.facts), ""]
        for module in c.modules.values():
            out += ["---", "", document_markdown(module, lang)]
        for page in sorted(c.pages.values(), key=lambda p: p["key"]):
            if page["kind"] in FULL_TEXT_PAGE_KINDS:
                out += ["---", "", document_markdown(page, lang)]
        for article in c.articles.values():
            out += ["---", "", document_markdown(article, lang)]
    return "\n".join(out)


def find_document(data: Loaded, lang: str, kind: str, key: str) -> dict | None:
    if lang not in data.langs:
        return None
    c = data.langs[lang]
    if kind == "module":
        return c.modules.get(c.module_aliases.get(key, key))
    store = {"page": c.pages, "article": c.articles}.get(kind)
    return store.get(key) if store is not None else None
