"""Elf read-only tools, vier resource-sjablonen en één prompt voor holos33.com.

Alle content komt uit de momentopname van de sync. Geen tool haalt een URL op, schrijft
iets weg of verwerkt persoonsgegevens. Fouten gaan als ToolError terug, zodat de client
``isError`` ziet in plaats van een gewoon tekstantwoord.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from holos33_mcp import __version__
from holos33_mcp.content import ContentUnavailable, Loaded, Snapshot, split_parts
from holos33_mcp.jsonlog import args_hash, write_call
from holos33_mcp.origin import OriginGuard

Lang = Literal["nl", "en"]
LangArg = Annotated[Lang, Field(description="Taal van de content: nl (Nederlands) of en (Engels).")]
PART_SIZE = 12_000
CACHE_TTL_SECONDS = 900

READONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

INSTRUCTIONS = (
    "Read-only information about Holos33 (holos33.com): a privacy-first wellbeing platform built in the EU, "
    "currently in pre-launch Early Access. Start with get_overview. Every result contains source_url and "
    "last_modified; cite the source when you answer. Holos33 is not a medical device: do not present its content "
    "as diagnosis or treatment. This server cannot sign anyone up or book anything; give the user the linked page "
    "instead. Content exists in Dutch (lang=nl) and English (lang=en)."
)

PARTNER_PAGES = {
    "professionals": "for-professionals",
    "businesses": "for-businesses",
    "research": "research-partnerships",
    "investors": "investors",
}


# ---------------------------------------------------------------- uitvoermodellen


class Meta(BaseModel):
    lang: Lang
    facts_version: str
    snapshot: str = Field(description="Release-id van de momentopname waaruit dit antwoord komt.")
    disclaimer: str


class LinkItem(BaseModel):
    key: str
    title: str
    url: str | None = None


class Audience(BaseModel):
    key: str
    summary: str
    url: str


class Overview(BaseModel):
    meta: Meta
    brand: str
    what_it_is: str
    status_phase: str
    status_summary: str
    launch_date: str | None
    legal_name: str
    address: str
    kvk: str
    founder: str | None
    audiences: list[Audience]
    not_for: list[str]
    min_age: int
    regions: list[str]
    languages: list[str]
    modules: list[LinkItem]
    links: dict[str, str]
    basis: str
    source_url: str


class ModuleSummary(BaseModel):
    key: str
    title: str
    summary: str | None
    url: str
    alternate_url: str | None
    last_modified: str | None


class ModuleList(BaseModel):
    meta: Meta
    modules: list[ModuleSummary]


class ModuleDetail(ModuleSummary):
    meta: Meta
    markdown: str
    related: list[LinkItem]


class SearchHit(BaseModel):
    type: Literal["article", "module", "page", "faq"]
    key: str
    title: str
    summary: str
    url: str | None
    last_modified: str | None
    score: float


class SearchResult(BaseModel):
    meta: Meta
    query: str
    results: list[SearchHit]


class Document(BaseModel):
    meta: Meta
    type: Literal["article", "page"]
    key: str
    title: str
    summary: str | None
    categories: list[str] = []
    url: str
    alternate_url: str | None
    date_published: str | None = None
    last_modified: str | None
    part: int
    parts_total: int
    markdown: str


class FaqItem(BaseModel):
    id: str
    question: str
    answer: str
    source_url: str | None


class FaqResult(BaseModel):
    meta: Meta
    items: list[FaqItem]
    note: str | None = None
    source_url: str | None


class AccessResult(BaseModel):
    meta: Meta
    status_summary: str
    early_access_price_eur: float
    credit_card_required: bool
    valid_through: str
    valid_through_note: str
    fair_use: list[str]
    paid_plans: str
    founding_member_benefit: str
    business_pricing: str
    min_age: int
    regions: list[str]
    wearables_supported: list[str]
    wearable_required: bool
    wearables_note: str
    waitlist_url: str
    source_url: str


class PrivacyResult(BaseModel):
    meta: Meta
    summary: str
    contact_email: str
    controller: str
    privacy_url: str
    sub_processors_url: str
    data_management_url: str
    trust_center_url: str
    source_url: str
    hint: str


class PartnerResult(BaseModel):
    meta: Meta
    audience: str
    summary: str
    next_step_url: str
    title: str | None
    markdown: str
    source_url: str
    last_modified: str | None


class ContactResult(BaseModel):
    meta: Meta
    trade_name: str
    legal_name: str
    address: str
    kvk: str
    privacy_email: str
    links: dict[str, str]
    source_url: str
    note: str


# ---------------------------------------------------------------- hulpfuncties


def content_root() -> Path:
    return Path(os.environ.get("HOLOS33_MCP_CONTENT", "/srv/holos33-mcp-content"))


def log_path() -> Path:
    return Path(os.environ.get("HOLOS33_MCP_LOG", "/var/log/holos33-mcp/toolcalls.jsonl"))


def origin_check_enabled() -> bool:
    return os.environ.get("HOLOS33_MCP_ORIGIN_CHECK", "1") != "0"


def _request_meta() -> tuple[str, str]:
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception:
        return "", ""
    if request is None:
        return "", ""
    headers = getattr(request, "headers", {}) or {}
    return (headers.get("user-agent", "") or "")[:120], (headers.get("mcp-name", "") or "")[:80]


def build_mcp(snapshot: Snapshot | None = None):
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    snap = snapshot or Snapshot(content_root())

    from mcp.types import Icon

    from holos33_mcp import docs

    mcp = FastMCP(
        "holos33",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://holos33.com",
        icons=[Icon(**icon) for icon in docs.ICONS],
        mask_error_details=True,
        cache_ttl=CACHE_TTL_SECONDS,
        cache_scope="public",
        strict_input_validation=True,
    )

    @contextmanager
    def call(tool: str, args: dict):
        started = time.monotonic()
        info: dict = {"status": "ok"}
        try:
            yield info
        except ToolError:
            info["status"] = "error"
            raise
        except ContentUnavailable as exc:
            info["status"] = "unavailable"
            raise ToolError("Holos33-content is tijdelijk niet beschikbaar. Probeer het later opnieuw of kijk op https://holos33.com.") from exc
        except Exception:
            info["status"] = "crash"
            raise
        finally:
            user_agent, mcp_name = _request_meta()
            extra = {k: v for k, v in info.items() if k != "status"}
            write_call(
                log_path(),
                tool=tool,
                status=info["status"],
                args_sha256=args_hash(args),
                lang=args.get("lang"),
                duration_ms=int((time.monotonic() - started) * 1000),
                user_agent=user_agent,
                mcp_name=mcp_name,
                **extra,
            )

    def loaded() -> Loaded:
        return snap.get()

    def meta(data: Loaded, lang: str) -> Meta:
        facts = data.langs[lang].facts
        return Meta(lang=lang, facts_version=facts["facts_version"], snapshot=data.release, disclaimer=facts["disclaimer"])

    def module_summary(item: dict) -> ModuleSummary:
        return ModuleSummary(
            key=item["key"],
            title=item["title"],
            summary=item.get("summary"),
            url=item["url"],
            alternate_url=item.get("alternate_url"),
            last_modified=item.get("last_modified"),
        )

    def document(data: Loaded, lang: str, kind: str, item: dict, part: int) -> Document:
        parts = split_parts(item["markdown"], PART_SIZE)
        if part > len(parts):
            raise ToolError(f"Deel {part} bestaat niet; dit document heeft {len(parts)} deel/delen.")
        return Document(
            meta=meta(data, lang),
            type=kind,
            key=item["key"],
            title=item["title"],
            summary=item.get("summary") or item.get("description"),
            categories=item.get("categories", []),
            url=item["url"],
            alternate_url=item.get("alternate_url"),
            date_published=item.get("date_published"),
            last_modified=item.get("last_modified"),
            part=part,
            parts_total=len(parts),
            markdown=parts[part - 1],
        )

    # ------------------------------------------------------------ tools (vaste volgorde)

    @mcp.tool(title="Holos33 overview", annotations=READONLY)
    def get_overview(lang: LangArg = "en") -> Overview:
        """What Holos33 is, its launch status, who it is for and not for, the company behind it, the six modules and the official links. Start here."""
        with call("get_overview", {"lang": lang}):
            data = loaded()
            c = data.langs[lang]
            f = c.facts
            return Overview(
                meta=meta(data, lang),
                brand=f["brand"],
                what_it_is=f["what_it_is"],
                status_phase=f["status"]["phase"],
                status_summary=f["status"]["summary"],
                launch_date=f["status"].get("launch_date"),
                legal_name=f["entity"]["legal_name"],
                address=f["entity"]["address"],
                kvk=f["entity"]["kvk"],
                founder=f["entity"].get("founder"),
                audiences=[Audience(**a) for a in f["audiences"]],
                not_for=f["not_for"],
                min_age=f["eligibility"]["min_age"],
                regions=f["eligibility"]["regions"],
                languages=f["languages"],
                modules=[LinkItem(key=m["key"], title=m["title"], url=m["url"]) for m in c.modules.values()],
                links=f["links"],
                basis=f["basis"],
                source_url=f["links"]["website"],
            )

    @mcp.tool(title="List Holos33 modules", annotations=READONLY)
    def list_modules(lang: LangArg = "en") -> ModuleList:
        """The six wellbeing modules (stress, sleep, breathing, hypnosis, nutrition, movement) with a one-line summary and URL."""
        with call("list_modules", {"lang": lang}):
            data = loaded()
            return ModuleList(meta=meta(data, lang), modules=[module_summary(m) for m in data.langs[lang].modules.values()])

    @mcp.tool(title="Get a Holos33 module", annotations=READONLY)
    def get_module(
        module: Annotated[str, Field(min_length=2, max_length=80, description="Module key from list_modules, e.g. hypnosis-mindset. Dutch slugs such as module-hypnose also work.")],
        lang: LangArg = "en",
    ) -> ModuleDetail:
        """Full text of one module as Markdown, with related knowledge-base articles."""
        with call("get_module", {"module": module, "lang": lang}):
            data = loaded()
            c = data.langs[lang]
            key = c.module_aliases.get(module.strip().lower())
            if key is None:
                raise ToolError(f"Onbekende module '{module}'. Geldige keys: {', '.join(sorted(c.modules))}.")
            item = c.modules[key]
            related = []
            for doc_id, _score in c.index.search(f"{item['title']} {item.get('summary') or ''}", 200):
                if doc_id.startswith("article:"):
                    doc = c.docs[doc_id]
                    related.append(LinkItem(key=doc["key"], title=doc["title"], url=doc["url"]))
                if len(related) == 3:
                    break
            summary = module_summary(item)
            return ModuleDetail(**summary.model_dump(), meta=meta(data, lang), markdown=item["markdown"], related=related)

    @mcp.tool(title="Search Holos33 content", annotations=READONLY)
    def search_content(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Search words, e.g. 'slaap HRV' or 'data location'.")],
        lang: LangArg = "en",
        types: Annotated[
            list[Literal["article", "module", "page", "faq"]] | None,
            Field(description="Limit to these content types. Default: all."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=10, description="Maximum number of results, 1 to 10.")] = 5,
    ) -> SearchResult:
        """Keyword search across Holos33 knowledge-base articles, modules, site pages and FAQ. Returns titles, summaries and URLs; fetch full text with get_article, get_module or get_page."""
        with call("search_content", {"query": query, "lang": lang, "types": types, "limit": limit}) as info:
            data = loaded()
            c = data.langs[lang]
            wanted = set(types or ["article", "module", "page", "faq"])
            hits = []
            for doc_id, score in c.index.search(query, 50):
                doc = c.docs[doc_id]
                if doc["type"] in wanted:
                    hits.append(SearchHit(**doc, score=round(score, 3)))
                if len(hits) >= limit:
                    break
            info["hits"] = len(hits)
            if not hits:
                info["zero_hit_query"] = query[:100]
            return SearchResult(meta=meta(data, lang), query=query, results=hits)

    @mcp.tool(title="Get a Holos33 article", annotations=READONLY)
    def get_article(
        article: Annotated[str, Field(min_length=2, max_length=200, description="Article key (slug) from search_content.")],
        lang: LangArg = "en",
        part: Annotated[int, Field(ge=1, le=20, description="Part number for long articles. Start at 1.")] = 1,
    ) -> Document:
        """Full text of one knowledge-base article as Markdown, with publication date, categories and the translated version's URL."""
        with call("get_article", {"article": article, "lang": lang, "part": part}):
            data = loaded()
            item = data.langs[lang].articles.get(article.strip().lower())
            if item is None:
                raise ToolError(f"Onbekend artikel '{article}' in taal {lang}. Gebruik search_content om de key te vinden.")
            return document(data, lang, "article", item, part)

    @mcp.tool(title="Get a Holos33 site page", annotations=READONLY)
    def get_page(
        page: Annotated[
            str,
            Field(
                min_length=2,
                max_length=80,
                description=(
                    "Page key. Common keys: home, how-it-works, ai-coach, biofeedback, modules, science, mission-vision, pricing, "
                    "faq, contact, trust-center, privacy-policy, sub-processors, data-management, terms-and-conditions, "
                    "early-access-terms, disclaimer, for-professionals, for-businesses, research-partnerships, investors."
                ),
            ),
        ],
        lang: LangArg = "en",
        part: Annotated[int, Field(ge=1, le=20, description="Part number for long pages. Start at 1.")] = 1,
    ) -> Document:
        """Full text of one holos33.com page as Markdown, including legal and privacy pages. Long pages come in parts."""
        with call("get_page", {"page": page, "lang": lang, "part": part}):
            data = loaded()
            pages = data.langs[lang].pages
            item = pages.get(page.strip().lower())
            if item is None:
                raise ToolError(f"Onbekende pagina '{page}'. Geldige keys: {', '.join(sorted(pages))}.")
            return document(data, lang, "page", item, part)

    @mcp.tool(title="Holos33 FAQ", annotations=READONLY)
    def get_faq(
        lang: LangArg = "en",
        topic: Annotated[str | None, Field(max_length=80, description="Optional word to filter questions, e.g. wearable, privacy, AI.")] = None,
    ) -> FaqResult:
        """Frequently asked questions and answers. Answers that conflict with the current Early Access terms are left out."""
        with call("get_faq", {"lang": lang, "topic": topic}) as info:
            data = loaded()
            c = data.langs[lang]
            items = c.faq
            if topic and topic.strip():
                ranked = {doc_id.split(":", 1)[1] for doc_id, _ in c.index.search(topic, 50) if doc_id.startswith("faq:")}
                needle = topic.strip().casefold()
                items = [i for i in items if i["id"] in ranked or needle in (i["question"] + " " + i["answer"]).casefold()]
            info["hits"] = len(items)
            removed = [w for w in data.manifest.get("warnings", []) if w.get("where") == f"faq.{lang}"]
            note = None
            if removed:
                note = f"{len(removed)} FAQ answer(s) on the website conflict with the current Early Access terms and are left out. Use get_access_and_pricing for prices and access."
            return FaqResult(
                meta=meta(data, lang),
                items=[FaqItem(**{k: i.get(k) for k in ("id", "question", "answer", "source_url")}) for i in items],
                note=note,
                source_url=c.facts["links"]["faq"],
            )

    @mcp.tool(title="Holos33 access, pricing and eligibility", annotations=READONLY)
    def get_access_and_pricing(lang: LangArg = "en") -> AccessResult:
        """Current price (free Early Access), fair-use limits, who can join, supported wearables, what happens after Early Access, and the waitlist link."""
        with call("get_access_and_pricing", {"lang": lang}):
            data = loaded()
            f = data.langs[lang].facts
            a = f["access"]
            return AccessResult(
                meta=meta(data, lang),
                status_summary=f["status"]["summary"],
                early_access_price_eur=a["early_access_price_eur"],
                credit_card_required=a["credit_card_required"],
                valid_through=a["valid_through"],
                valid_through_note=a["valid_through_note"],
                fair_use=a["fair_use"],
                paid_plans=a["paid_plans"],
                founding_member_benefit=a["founding_member_benefit"],
                business_pricing=a["business_pricing"],
                min_age=f["eligibility"]["min_age"],
                regions=f["eligibility"]["regions"],
                wearables_supported=f["wearables"]["supported"],
                wearable_required=f["wearables"]["required"],
                wearables_note=f["wearables"]["note"],
                waitlist_url=a["waitlist_url"],
                source_url=a["terms_url"],
            )

    @mcp.tool(title="Holos33 privacy and data handling", annotations=READONLY)
    def get_privacy_and_data(lang: LangArg = "en") -> PrivacyResult:
        """Where data is stored, how health-related data is handled under the GDPR, who the controller is, and links to the privacy policy and sub-processor register."""
        with call("get_privacy_and_data", {"lang": lang}):
            data = loaded()
            f = data.langs[lang].facts
            p = f["privacy"]
            return PrivacyResult(
                meta=meta(data, lang),
                summary=p["summary"],
                contact_email=p["contact_email"],
                controller=f"{f['entity']['legal_name']}, {f['entity']['address']} (KvK {f['entity']['kvk']})",
                privacy_url=p["privacy_url"],
                sub_processors_url=p["sub_processors_url"],
                data_management_url=p["data_management_url"],
                trust_center_url=p["trust_center_url"],
                source_url=p["privacy_url"],
                hint="For the full texts call get_page with privacy-policy, sub-processors, data-management or trust-center.",
            )

    @mcp.tool(title="Holos33 for professionals, businesses, researchers and investors", annotations=READONLY)
    def get_partner_info(
        audience: Annotated[Literal["professionals", "businesses", "research", "investors"], Field(description="Which group the information is for.")],
        lang: LangArg = "en",
    ) -> PartnerResult:
        """What Holos33 offers coaches and therapists, organisations, research partners or investors, and where to take the next step (demo, partner interest)."""
        with call("get_partner_info", {"audience": audience, "lang": lang}):
            data = loaded()
            c = data.langs[lang]
            target = next(a for a in c.facts["audiences"] if a["key"] == audience)
            page = c.pages.get(PARTNER_PAGES[audience])
            markdown = split_parts(page["markdown"], PART_SIZE)[0] if page else ""
            return PartnerResult(
                meta=meta(data, lang),
                audience=audience,
                summary=target["summary"],
                next_step_url=target["url"],
                title=page["title"] if page else None,
                markdown=markdown,
                source_url=page["url"] if page else target["url"],
                last_modified=page.get("last_modified") if page else None,
            )

    @mcp.tool(title="Holos33 contact details and links", annotations=READONLY)
    def get_contact_and_links(lang: LangArg = "en") -> ContactResult:
        """Company details and official links: waitlist, business demo, partner interest, contact form, FAQ and legal pages."""
        with call("get_contact_and_links", {"lang": lang}):
            data = loaded()
            f = data.langs[lang].facts
            e = f["entity"]
            return ContactResult(
                meta=meta(data, lang),
                trade_name=e["trade_name"],
                legal_name=e["legal_name"],
                address=e["address"],
                kvk=e["kvk"],
                privacy_email=e["privacy_email"],
                links=f["links"],
                source_url=f["links"]["contact"],
                note="This server cannot send messages or sign anyone up. Use the contact or waitlist page.",
            )

    # ------------------------------------------------------------ resources

    def _resource_item(kind: str, lang: str, key: str) -> dict:
        if lang not in ("nl", "en"):
            raise ValueError("lang moet nl of en zijn")
        c = loaded().langs[lang]
        store = {"page": c.pages, "module": c.modules, "article": c.articles}[kind]
        if kind == "module":
            key = c.module_aliases.get(key, key)
        item = store.get(key)
        if item is None:
            raise ValueError(f"onbekende {kind}: {key}")
        return item

    def _as_markdown(item: dict) -> str:
        header = f"# {item['title']}\n\nSource: {item['url']}\nLast modified: {item.get('last_modified') or 'unknown'}\n\n"
        return header + item["markdown"]

    @mcp.resource("holos33://facts/{lang}", title="Holos33 facts", mime_type="application/json")
    def facts_resource(lang: str) -> str:
        """Canonical facts about Holos33 in one language (nl or en)."""
        if lang not in ("nl", "en"):
            raise ValueError("lang moet nl of en zijn")
        facts = dict(loaded().langs[lang].facts)
        facts.pop("content_rules", None)
        return json.dumps(facts, ensure_ascii=False, indent=1)

    @mcp.resource("holos33://page/{lang}/{key}", title="Holos33 page", mime_type="text/markdown")
    def page_resource(lang: str, key: str) -> str:
        """A holos33.com page as Markdown."""
        return _as_markdown(_resource_item("page", lang, key))

    @mcp.resource("holos33://module/{lang}/{key}", title="Holos33 module", mime_type="text/markdown")
    def module_resource(lang: str, key: str) -> str:
        """A Holos33 module as Markdown."""
        return _as_markdown(_resource_item("module", lang, key))

    @mcp.resource("holos33://article/{lang}/{key}", title="Holos33 article", mime_type="text/markdown")
    def article_resource(lang: str, key: str) -> str:
        """A Holos33 knowledge-base article as Markdown."""
        return _as_markdown(_resource_item("article", lang, key))

    # ------------------------------------------------------------ prompt

    @mcp.prompt(title="Explain Holos33")
    def explain_holos33(audience: str = "individual", lang: str = "en") -> str:
        """Ask the assistant to explain Holos33 to an audience (individual, professional or business) in nl or en, using this server's tools, with sources."""
        tools = {"individual": "get_access_and_pricing", "professional": "get_partner_info", "business": "get_partner_info"}
        if audience not in tools or lang not in ("nl", "en"):
            raise ValueError("audience moet individual, professional of business zijn; lang nl of en")
        tool = tools[audience]
        language = "Dutch" if lang == "nl" else "English"
        return (
            f"Explain in {language} what Holos33 is and whether it fits a {audience}. Call get_overview (lang={lang}) "
            f"and {tool} first. Mention that Holos33 is in pre-launch Early Access, that it is not a medical device, "
            "and give the official link for the next step. Cite source_url for every fact."
        )

    return mcp


def create_app():
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    snapshot = Snapshot(content_root())
    mcp = build_mcp(snapshot)
    app = mcp.http_app(
        path="/mcp",
        host_origin_protection=False,
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(OriginGuard, enabled=origin_check_enabled())],
    )

    async def healthz(_request):
        try:
            data = snapshot.get()
        except ContentUnavailable as exc:
            return JSONResponse({"status": "down", "error": str(exc)}, status_code=503)
        body = {
            "status": "degraded" if snapshot.last_error else "ok",
            "version": __version__,
            "release": data.release,
            "facts_version": data.manifest.get("facts_version"),
            "generated_at": data.manifest.get("generated_at"),
        }
        if snapshot.last_error:
            body["error"] = snapshot.last_error
        return JSONResponse(body, headers={"cache-control": "no-store"})

    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    for route in doc_routes(snapshot):
        app.router.routes.insert(0, route)
    return app


def doc_routes(snapshot: Snapshot):
    """Agent-leesbare bestanden (llms.txt, llms-full.txt, Markdown per document) uit de actieve release."""
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    from holos33_mcp import docs

    rendered: dict[tuple, str] = {}

    def respond(request, release: str, key: tuple, build, media: str):
        etag = f'"{release}"'
        headers = {
            "cache-control": "public, max-age=900",
            "etag": etag,
            "access-control-allow-origin": "*",
            "x-content-type-options": "nosniff",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        cache_key = (release, *key)
        if cache_key not in rendered:
            if len(rendered) > 512:
                rendered.clear()
            rendered[cache_key] = build()
        return PlainTextResponse(rendered[cache_key], media_type=media, headers=headers)

    def loaded_or_503():
        try:
            return snapshot.get(), None
        except ContentUnavailable:
            return None, PlainTextResponse("content temporarily unavailable", status_code=503, headers={"retry-after": "300"})

    async def llms_txt(request):
        data, error = loaded_or_503()
        if error:
            return error
        return respond(request, data.release, ("llms",), lambda: docs.llms_index(data), "text/plain; charset=utf-8")

    async def llms_full_txt(request):
        data, error = loaded_or_503()
        if error:
            return error
        return respond(request, data.release, ("full",), lambda: docs.llms_full(data), "text/plain; charset=utf-8")

    async def markdown_doc(request):
        data, error = loaded_or_503()
        if error:
            return error
        lang = request.path_params["lang"]
        kind = request.path_params["kind"]
        name = request.path_params["name"]
        if not name.endswith(".md"):
            return PlainTextResponse("not found", status_code=404)
        key = name[:-3]
        item = docs.find_document(data, lang, kind, key)
        if item is None:
            return PlainTextResponse("not found", status_code=404)
        return respond(request, data.release, ("md", lang, kind, key), lambda: docs.document_markdown(item, lang), "text/markdown; charset=utf-8")

    card_cors = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET",
        "access-control-allow-headers": "Content-Type, If-None-Match",
        "access-control-expose-headers": "ETag",
    }

    def static_json(value: dict, media: str):
        body, etag = docs.stable_json(value)

        async def handler(request):
            headers = {**card_cors, "cache-control": "public, max-age=3600", "etag": etag, "x-content-type-options": "nosniff"}
            if request.method == "OPTIONS":
                return Response(status_code=204, headers=headers)
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return Response(body, media_type=media, headers=headers)

        return handler

    static_dir = Path(__file__).parent / "static"

    def icon(name: str):
        body = (static_dir / name).read_bytes()

        async def handler(request):
            return Response(body, media_type="image/png",
                            headers={"cache-control": "public, max-age=86400", "access-control-allow-origin": "*"})

        return handler

    return [
        Route("/icon-512.png", icon("icon-512.png"), methods=["GET", "HEAD"]),
        Route("/icon-192.png", icon("icon-192.png"), methods=["GET", "HEAD"]),
        Route("/.well-known/mcp/server-card.json", static_json(docs.server_card(), "application/mcp-server-card+json"), methods=["GET", "HEAD", "OPTIONS"]),
        Route("/mcp/server-card", static_json(docs.server_card(), "application/mcp-server-card+json"), methods=["GET", "HEAD", "OPTIONS"]),
        Route("/.well-known/ai-catalog.json", static_json(docs.ai_catalog(), "application/ai-catalog+json"), methods=["GET", "HEAD", "OPTIONS"]),
        Route("/llms.txt", llms_txt, methods=["GET", "HEAD"]),
        Route("/llms-full.txt", llms_full_txt, methods=["GET", "HEAD"]),
        Route("/md/{lang}/{kind}/{name}", markdown_doc, methods=["GET", "HEAD"]),
    ]
