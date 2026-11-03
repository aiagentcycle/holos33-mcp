"""Live rooktest tegen een draaiende server. Gebruik: python deploy/smoke_test.py [url].

Test over echte HTTP: nieuwste protocol via de FastMCP-client, oudere protocolversies via
ruwe JSON-RPC, transportregels (405, 403) en de grootte en snelheid van alle tools.
Exitcode 0 als alles klopt.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

try:
    import httpx
except ImportError:  # de gepinde MCP-stack levert httpx2
    import httpx2 as httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8324/mcp"
FAILS: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("OK   " if ok else "FAIL ") + label)
    if not ok:
        FAILS.append(label)


CALLS = [
    ("get_overview", {"lang": "nl"}),
    ("list_modules", {"lang": "en"}),
    ("get_module", {"module": "module-hypnose", "lang": "nl"}),
    ("search_content", {"query": "slaap HRV", "lang": "nl", "limit": 10}),
    ("search_content", {"query": "where is my data stored", "lang": "en", "types": ["page", "faq"]}),
    ("get_article", {"article": "coffee-before-sleep", "lang": "en"}),
    ("get_page", {"page": "privacy-policy", "lang": "en"}),
    ("get_page", {"page": "early-access-terms", "lang": "nl"}),
    ("get_faq", {"lang": "en"}),
    ("get_access_and_pricing", {"lang": "en"}),
    ("get_privacy_and_data", {"lang": "nl"}),
    ("get_partner_info", {"audience": "professionals", "lang": "nl"}),
    ("get_contact_and_links", {"lang": "en"}),
]


async def latest_protocol() -> None:
    from fastmcp import Client

    async with Client(URL) as client:
        tools = await client.list_tools()
        check(len(tools) == 11, f"nieuwste protocol: {len(tools)} tools")
        sizes, times = [], []
        for name, args in CALLS:
            start = time.perf_counter()
            result = await client.call_tool(name, args, raise_on_error=False)
            times.append((time.perf_counter() - start) * 1000)
            text = result.content[0].text if result.content else ""
            sizes.append(len(text))
            check(not result.is_error and result.structured_content is not None, f"{name} {json.dumps(args, ensure_ascii=False)} -> {len(text)} tekens")
        check(max(sizes) < 40_000, f"grootste antwoord {max(sizes)} tekens (< 40k)")
        check(statistics.median(times) < 300, f"mediane duur {statistics.median(times):.0f} ms")
        bad = await client.call_tool("get_module", {"module": "yoga-nidra", "lang": "en"}, raise_on_error=False)
        check(bad.is_error, "onbekende module geeft isError")
        templates = await client.list_resource_templates()
        check(len(templates) == 4, f"{len(templates)} resource-sjablonen")
        facts = await client.read_resource("holos33://facts/nl")
        check('"Good Nature Vibe B.V."' in facts[0].text, "resource holos33://facts/nl leesbaar")


def legacy_protocol(version: str) -> None:
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with httpx.Client(timeout=10) as http:
        init = http.post(URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}},
        })
        body = init.json() if init.headers.get("content-type", "").startswith("application/json") else {}
        negotiated = body.get("result", {}).get("protocolVersion")
        check(init.status_code == 200 and negotiated is not None, f"initialize {version}: HTTP {init.status_code}, onderhandeld {negotiated}")
        h2 = dict(headers, **{"mcp-protocol-version": negotiated or version})
        if init.headers.get("mcp-session-id"):
            h2["mcp-session-id"] = init.headers["mcp-session-id"]
        http.post(URL, headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = http.post(URL, headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = listed.json().get("result", {}).get("tools", []) if listed.status_code == 200 else []
        check(len(tools) == 11, f"tools/list na initialize {version}: {len(tools)} tools")
        if tools:
            ann = tools[0].get("annotations", {})
            check(ann.get("readOnlyHint") is True and "outputSchema" in tools[0], "draadformaat gebruikt readOnlyHint en outputSchema (camelCase)")


def transport_rules() -> None:
    with httpx.Client(timeout=10) as http:
        get = http.get(URL, headers={"accept": "text/event-stream"})
        check(get.status_code in (405, 400), f"GET /mcp -> {get.status_code}")
        evil = http.post(URL, headers={"origin": "https://evil.example", "content-type": "application/json",
                                       "accept": "application/json, text/event-stream"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check(evil.status_code == 403, f"vreemde Origin -> {evil.status_code}")
        # Alleen het pad vervangen; de hostnaam mcp.holos33.com bevat zelf ook "/mcp" na "//".
        health = http.get(URL.rsplit("/mcp", 1)[0] + "/healthz")
        check(health.status_code == 200 and health.json().get("status") == "ok", f"/healthz -> {health.status_code} {health.text[:80]}")


def main() -> int:
    transport_rules()
    for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
        legacy_protocol(version)
    asyncio.run(latest_protocol())
    print(f"\n{len(FAILS)} fout(en)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
