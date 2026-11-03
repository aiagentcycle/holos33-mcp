import asyncio
import json
from pathlib import Path

import jsonschema
import pytest

from holos33_mcp.content import Snapshot, split_parts
from holos33_mcp.origin import origin_allowed
from tests.conftest import point_current, write_release

EXPECTED_TOOLS = [
    "get_overview",
    "list_modules",
    "get_module",
    "search_content",
    "get_article",
    "get_page",
    "get_faq",
    "get_access_and_pricing",
    "get_privacy_and_data",
    "get_partner_info",
    "get_contact_and_links",
]


def run(coro):
    return asyncio.run(coro)


def client_for(root: Path):
    from fastmcp import Client

    from holos33_mcp.server import build_mcp

    return Client(build_mcp(Snapshot(root)))


async def _tools(root):
    async with client_for(root) as client:
        return await client.list_tools()


async def _call(root, name, args=None):
    async with client_for(root) as client:
        return await client.call_tool(name, args or {}, raise_on_error=False)


def test_tool_contract(content):
    tools = run(_tools(content))
    assert [t.name for t in tools] == EXPECTED_TOOLS
    assert [t.name for t in run(_tools(content))] == EXPECTED_TOOLS, "volgorde moet deterministisch zijn"
    for tool in tools:
        assert tool.title, tool.name
        assert len(tool.name) <= 64
        ann = tool.annotations
        assert ann.read_only_hint is True and ann.destructive_hint is False
        assert ann.idempotent_hint is True and ann.open_world_hint is False
        assert tool.output_schema, f"{tool.name} mist outputSchema"
        assert tool.description and "ignore" not in tool.description.lower()


ARGS = {
    "get_overview": {"lang": "nl"},
    "list_modules": {"lang": "en"},
    "get_module": {"module": "module-hypnose", "lang": "nl"},
    "search_content": {"query": "hypnose hypnosis", "lang": "en", "limit": 3},
    "get_article": {"article": "coherent-breathing", "lang": "en"},
    "get_page": {"page": "pricing", "lang": "en"},
    "get_faq": {"lang": "en", "topic": "wearables"},
    "get_access_and_pricing": {"lang": "en"},
    "get_privacy_and_data": {"lang": "nl"},
    "get_partner_info": {"audience": "businesses", "lang": "en"},
    "get_contact_and_links": {"lang": "en"},
}


@pytest.mark.parametrize("name", EXPECTED_TOOLS)
def test_every_tool_matches_its_output_schema(content, name):
    tools = {t.name: t for t in run(_tools(content))}
    result = run(_call(content, name, ARGS[name]))
    assert not result.is_error, result.content
    structured = result.structured_content
    jsonschema.validate(structured, tools[name].output_schema)
    assert structured["meta"]["disclaimer"]
    assert structured["meta"]["snapshot"] == "r1"
    # tekstversie moet dezelfde JSON zijn
    assert json.loads(result.content[0].text) == structured


def test_answers_carry_facts_not_page_contradictions(content):
    data = run(_call(content, "get_access_and_pricing", {"lang": "en"})).structured_content
    assert data["early_access_price_eur"] == 0
    assert data["credit_card_required"] is False
    assert data["wearables_supported"] == ["Oura", "Fitbit", "Apple Health", "Garmin"]
    overview = run(_call(content, "get_overview", {"lang": "en"})).structured_content
    assert overview["legal_name"] == "Good Nature Vibe B.V."
    assert "Oosterwolde" in overview["address"]
    faq = run(_call(content, "get_faq", {"lang": "en"})).structured_content
    assert faq["note"] and "left out" in faq["note"]


def test_search_ranks_and_logs_zero_hits(content):
    data = run(_call(content, "search_content", {"query": "hypnosis", "lang": "en", "types": ["article"]})).structured_content
    assert data["results"] and data["results"][0]["key"] == "hypnosis-science"
    assert all(r["type"] == "article" for r in data["results"])
    empty = run(_call(content, "search_content", {"query": "zzqx", "lang": "en"})).structured_content
    assert empty["results"] == []
    rows = [json.loads(line) for line in (content / "toolcalls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["zero_hit_query"] == "zzqx"
    assert "ip" not in rows[-1] and rows[-1]["status"] == "ok"


def test_search_matches_word_forms_and_keys(content):
    data = run(_call(content, "search_content", {"query": "price", "lang": "en"})).structured_content
    assert data["results"][0]["key"] == "pricing"
    module = run(_call(content, "get_module", {"module": "hypnosis-mindset", "lang": "en"})).structured_content
    assert [r["key"] for r in module["related"]][:1] == ["hypnosis-science"]


def test_long_documents_come_in_parts(content):
    first = run(_call(content, "get_page", {"page": "privacy-policy", "lang": "en"})).structured_content
    assert first["parts_total"] >= 3 and len(first["markdown"]) <= 12_100
    last = run(_call(content, "get_page", {"page": "privacy-policy", "lang": "en", "part": first["parts_total"]})).structured_content
    assert last["part"] == first["parts_total"]
    too_far = run(_call(content, "get_page", {"page": "privacy-policy", "lang": "en", "part": 20}))
    assert too_far.is_error


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_module", {"module": "yoga-nidra", "lang": "en"}),
        ("get_article", {"article": "bestaat-niet", "lang": "en"}),
        ("get_page", {"page": "../../etc/passwd", "lang": "en"}),
        ("get_overview", {"lang": "de"}),
        ("search_content", {"query": "", "lang": "en"}),
        ("search_content", {"query": "x" * 201, "lang": "en"}),
        ("search_content", {"query": "slaap", "lang": "en", "limit": 50}),
        ("get_partner_info", {"audience": "hackers", "lang": "en"}),
    ],
)
def test_bad_input_is_an_error_not_a_silent_answer(content, name, args):
    result = run(_call(content, name, args))
    assert result.is_error


def test_missing_content_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLOS33_MCP_LOG", str(tmp_path / "log.jsonl"))
    result = run(_call(tmp_path, "get_overview", {"lang": "en"}))
    assert result.is_error
    assert "tijdelijk niet beschikbaar" in result.content[0].text


def test_broken_new_release_keeps_old_one(content):
    snap = Snapshot(content)
    assert snap.get().release == "r1"
    write_release(content, "r2", broken=True)
    point_current(content, "r2")
    snap._checked = 0
    assert snap.get().release == "r1"
    assert snap.last_error and "r2" in snap.last_error
    write_release(content, "r3")
    point_current(content, "r3")
    snap._checked = 0
    assert snap.get().release == "r3" and snap.last_error is None


def test_unexpected_load_error_keeps_old_release_and_is_not_retried(content, monkeypatch):
    from holos33_mcp import content as content_module

    snap = Snapshot(content)
    assert snap.get().release == "r1"
    release = write_release(content, "r2")
    (release / "manifest.json").write_text(json.dumps(["geen", "dict"]), encoding="utf-8")  # manifest.get -> AttributeError
    point_current(content, "r2")
    calls = []
    real_load = content_module.load_release

    def counting(path):
        calls.append(path.name)
        return real_load(path)

    monkeypatch.setattr(content_module, "load_release", counting)
    snap._checked = 0
    assert snap.get().release == "r1"
    assert snap.last_error and "r2" in snap.last_error and "AttributeError" in snap.last_error
    snap._checked = 0
    assert snap.get().release == "r1"
    assert calls == ["r2"], "dezelfde kapotte target niet elke 5 s opnieuw laden"
    write_release(content, "r3")
    point_current(content, "r3")
    snap._checked = 0
    assert snap.get().release == "r3" and snap.last_error is None
    assert calls == ["r2", "r3"]


def test_origin_guard_only_guards_exact_mcp_path():
    from holos33_mcp.origin import OriginGuard

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def status_for(path):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        scope = {"type": "http", "path": path, "headers": [(b"origin", b"https://evil.example")]}
        await OriginGuard(app)(scope, receive, send)
        return sent[0]["status"]

    assert run(status_for("/mcp/")) == 200
    assert run(status_for("/mcp/x")) == 200
    assert run(status_for("/mcp")) == 403


def test_resources_and_prompt(content):
    async def go():
        async with client_for(content) as client:
            templates = await client.list_resource_templates()
            facts = await client.read_resource("holos33://facts/en")
            module = await client.read_resource("holos33://module/nl/module-hypnose")
            prompts = await client.list_prompts()
            prompt = await client.get_prompt("explain_holos33", {"audience": "business", "lang": "nl"})
            return templates, facts, module, prompts, prompt

    templates, facts, module, prompts, prompt = run(go())
    assert {t.uri_template for t in templates} >= {"holos33://facts/{lang}", "holos33://article/{lang}/{key}"}
    parsed = json.loads(facts[0].text)
    assert parsed["brand"] == "Holos33" and "content_rules" not in parsed
    assert module[0].text.startswith("# Hypnosis")
    assert [p.name for p in prompts] == ["explain_holos33"]
    assert "Dutch" in prompt.messages[0].content.text


def test_split_parts_respects_size():
    text = "\n\n".join(["a" * 500] * 100)
    parts = split_parts(text, 2000)
    assert all(len(p) <= 2000 for p in parts)
    assert "\n\n".join(parts) == text
    assert split_parts("", 10) == [""]


def test_origin_rules():
    assert origin_allowed(None)
    assert origin_allowed("https://mcp.holos33.com")
    assert not origin_allowed("https://evil.example")
    assert origin_allowed("https://evil.example", enabled=False)
