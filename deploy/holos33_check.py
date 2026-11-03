"""Dagelijkse controle van de harde regel (docs/HARDE-REGEL.md). Leest alleen, herstelt niets.

Exitcode 1 bij één rode check: dan staat holos33-check.service op failed en toont de MOTD het.
Uitkomst: /var/log/holos33-mcp/checks.jsonl (geschiedenis) en check-status.txt (laatste run, leesbaar).
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from holos33_mcp import __version__  # noqa: E402
from holos33_mcp.docs import MCP_URL, SERVER_NAME  # noqa: E402
from sync.holos33_sync import MIN_PAGE_WORDS, MIN_PAGE_WORDS_KINDS  # noqa: E402

HOST = "mcp.holos33.com"
BASE = f"https://{HOST}"
REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers/com.holos33%2Fholos33/versions/latest"
EXPECTED_TXT = (APP / "deploy" / "registry-txt.txt").read_text().strip()
NEIGHBOURS = ["https://mcp.aiagentcycle.com/mcp", "https://mcp.goodnaturevibe.com/mcp"]
LOG_DIR = Path("/var/log/holos33-mcp")
UA = "holos33-check/1.0"
MAX_RELEASE_AGE_H = 12
MIN_CERT_DAYS = 14

results: list[dict] = []


def check(name: str):
    def wrap(fn):
        try:
            detail = fn()
            results.append({"check": name, "ok": True, "detail": detail})
        except Exception as error:  # elke fout is een rode check, met reden
            results.append({"check": name, "ok": False, "detail": f"{error.__class__.__name__}: {error}"[:300]})
        return fn

    return wrap


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None, retry: bool = True) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            # Een 429 van onze eigen rate-limit (bv. een run vlak na de vorige) is geen storing: even wachten.
            if error.code == 429 and retry and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return error.code, dict(error.headers), error.read()
    raise RuntimeError("onbereikbaar")


def rpc(url: str, method: str, params: dict | None = None, version: str = "2025-11-25") -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": version}
    status, _, raw = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers)
    if status != 200:
        raise RuntimeError(f"{method} gaf HTTP {status}")
    text = raw.decode()
    if text.startswith("event:"):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


@check("1 bereikbaar: initialize + 11 tools")
def _reachable():
    init = rpc(MCP_URL, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "holos33-check", "version": "1"}})
    tools = rpc(MCP_URL, "tools/list", version=init["result"]["protocolVersion"])["result"]["tools"]
    if len(tools) != 11:
        raise RuntimeError(f"{len(tools)} tools, verwacht 11")
    return f"protocol {init['result']['protocolVersion']}, {len(tools)} tools"


@check("1 healthz ok")
def _health():
    status, _, raw = http("GET", f"{BASE}/healthz")
    data = json.loads(raw)
    if status != 200 or data.get("status") != "ok":
        raise RuntimeError(f"HTTP {status} {data}")
    return f"release {data['release']}"


@check("2 actueel: release < 12 uur, laatste sync ok")
def _fresh():
    status, _, raw = http("GET", f"{BASE}/healthz")
    data = json.loads(raw)
    made = datetime.strptime(data["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - made).total_seconds() / 3600
    lines = (LOG_DIR / "sync.jsonl").read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    if age_h > MAX_RELEASE_AGE_H:
        raise RuntimeError(f"release is {age_h:.1f} uur oud; laatste sync: {last.get('status')} {last.get('error', '')}")
    if last.get("status") != "ok":
        raise RuntimeError(f"laatste sync faalde: {last.get('error')}")
    return f"release {age_h:.1f} uur oud, sync {last['ts']} ok"


@check("3 één versie: code = Server Card = Registry (active, juiste URL)")
def _version():
    status, _, raw = http("GET", f"{MCP_URL}/server-card")
    card = json.loads(raw)
    if status != 200 or card.get("version") != __version__ or card.get("name") != SERVER_NAME:
        raise RuntimeError(f"Server Card {status}: {card.get('name')} {card.get('version')} (code {__version__})")
    # Directe opvraging; de zoekfunctie van het Registry is niet betrouwbaar (soms 0 resultaten).
    status, _, raw = http("GET", REGISTRY, headers={"Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"niet gevonden in het Registry (HTTP {status})")
    entry = json.loads(raw)
    server, meta = entry["server"], entry["_meta"]["io.modelcontextprotocol.registry/official"]
    urls = [r["url"] for r in server.get("remotes", [])]
    if server["version"] != __version__:
        raise RuntimeError(f"Registry heeft {server['version']}, code heeft {__version__}: publiceer opnieuw")
    if meta.get("status") != "active" or urls != [MCP_URL]:
        raise RuntimeError(f"Registry-status {meta.get('status')}, remotes {urls}")
    return f"{SERVER_NAME} {__version__} active"


@check("4 TXT-record voor het Registry staat")
def _txt():
    for resolver in ("1.1.1.1", "8.8.8.8"):
        out = subprocess.run(["dig", "+short", "TXT", "holos33.com", f"@{resolver}"], capture_output=True, text=True, timeout=20).stdout
        if not any(EXPECTED_TXT == line.replace('" "', "").strip('"') for line in out.splitlines()):
            raise RuntimeError(f"TXT ontbreekt of wijkt af bij {resolver}")
    return "zichtbaar bij 1.1.1.1 en 8.8.8.8"


@check("5 vindbaar: Server Card, ai-catalog, llms.txt, llms-full.txt")
def _discovery():
    out = []
    for path, media in (("/mcp/server-card", "application/mcp-server-card+json"), ("/.well-known/ai-catalog.json", "application/ai-catalog+json"),
                        ("/llms.txt", "text/plain"), ("/llms-full.txt", "text/plain")):
        status, headers, raw = http("GET", BASE + path)
        content_type = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if status != 200 or not content_type.startswith(media) or len(raw) < 100:
            raise RuntimeError(f"{path}: HTTP {status}, {content_type}, {len(raw)} B")
        out.append(f"{path} {len(raw)}B")
    return ", ".join(out)


@check("6 TLS-certificaat > 14 dagen")
def _cert():
    context = ssl.create_default_context()
    with socket.create_connection((HOST, 443), timeout=15) as raw_sock, context.wrap_socket(raw_sock, server_hostname=HOST) as tls:
        not_after = tls.getpeercert()["notAfter"]
    days = (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400
    if days < MIN_CERT_DAYS:
        raise RuntimeError(f"certificaat verloopt over {days:.0f} dagen ({not_after}); kijk naar certbot.timer")
    return f"nog {days:.0f} dagen"


@check("6 transportregels: GET 405, vreemde Origin 403, rate-limit 429")
def _transport():
    status, _, _ = http("GET", MCP_URL, headers={"Accept": "text/event-stream"})
    if status != 405:
        raise RuntimeError(f"GET /mcp gaf {status}")
    status, _, _ = http("POST", MCP_URL, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        {"Origin": "https://evil.example", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    if status != 403:
        raise RuntimeError(f"vreemde Origin gaf {status}")
    with ThreadPoolExecutor(max_workers=30) as pool:
        codes = list(pool.map(lambda _: http("GET", f"{BASE}/llms.txt", retry=False)[0], range(40)))
    if 429 not in codes:
        raise RuntimeError(f"geen 429 bij 40 snelle verzoeken: {sorted(set(codes))}")
    return f"405/403 ok, {codes.count(429)} van 40 begrensd"


@check("7 buren: AAC en GNV antwoorden")
def _neighbours():
    out = []
    time.sleep(2)
    for url in NEIGHBOURS:
        status, _, raw = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        text = raw.decode(errors="replace")
        if status != 200 or '"tools"' not in text:
            raise RuntimeError(f"{url}: HTTP {status}")
        out.append(f"{url.split('//')[1].split('/')[0]} 200")
    return ", ".join(out)


@check("8 release-inhoud: kernpagina's hebben tekst, geen onbekende pagina's")
def _release_content():
    # De regels uit facts.json geven bewust warnings (verwijderde claims); die zijn geen fout. Rood is alleen een
    # bijna lege core-, access- of privacy-pagina in de live release, of een pagina die de sync niet kent.
    current = Path(os.environ.get("HOLOS33_MCP_CONTENT", "/srv/holos33-mcp-content")) / "current"
    manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
    warnings = manifest.get("warnings") or []
    thin = []
    for lang in ("nl", "en"):
        for page in json.loads((current / f"pages.{lang}.json").read_text(encoding="utf-8")):
            if page.get("kind") in MIN_PAGE_WORDS_KINDS and page.get("words", 0) < MIN_PAGE_WORDS:
                thin.append(f"{lang}/{page.get('key')} ({page.get('words', 0)} woorden)")
    unknown = [str(w.get("where")) for w in warnings if "onbekende pagina" in str(w.get("reason", ""))]
    if thin or unknown:
        raise RuntimeError(f"bijna leeg: {', '.join(thin[:5]) or '-'}; onbekend: {', '.join(unknown[:5]) or '-'}; zie manifest.json")
    return f"{len(warnings)} sync-warnings (regels toegepast), kernpagina's >= {MIN_PAGE_WORDS} woorden"


def main() -> int:
    ok = all(r["ok"] for r in results)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"ts": stamp, "ok": ok, "version": __version__, "failed": [r["check"] for r in results if not r["ok"]], "results": results}
    lines = [f"holos33-check {stamp}: {'GROEN' if ok else 'ROOD'} ({sum(r['ok'] for r in results)}/{len(results)})"]
    lines += [f"  {'OK  ' if r['ok'] else 'FOUT'} {r['check']}: {r['detail']}" for r in results]
    if not ok:
        lines.append("  Harde regel: /opt/holos33-mcp/app/docs/HARDE-REGEL.md")
    text = "\n".join(lines) + "\n"
    print(text, end="")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "checks.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        (LOG_DIR / "check-status.txt").write_text(text, encoding="utf-8")
    except OSError as error:
        print(f"kon status niet schrijven: {error}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
