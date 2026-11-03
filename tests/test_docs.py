from starlette.testclient import TestClient

from holos33_mcp.server import create_app


def client(content):
    return TestClient(create_app())


def test_llms_index_lists_mcp_and_documents(content):
    with client(content) as http:
        r = http.get("/llms.txt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["access-control-allow-origin"] == "*"
        assert "https://mcp.holos33.com/mcp" in r.text
        assert "/md/en/module/hypnosis-mindset.md" in r.text
        assert "Good Nature Vibe B.V." in r.text
        again = http.get("/llms.txt", headers={"if-none-match": r.headers["etag"]})
        assert again.status_code == 304


def test_llms_full_contains_text_and_skips_legal(content):
    with client(content) as http:
        text = http.get("/llms-full.txt").text
        assert "Guided hypnosis sessions" in text
        assert "Early Access is free." in text
        assert "Data stays in the EU." not in text  # juridische pagina's alleen als link, niet volledig


def test_markdown_documents(content):
    with client(content) as http:
        r = http.get("/md/nl/module/module-hypnose.md")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
        assert r.text.startswith("# Hypnosis & Mindset") and "Source: https://holos33.com/nl/hypnosis-mindset/" in r.text
        assert http.get("/md/en/article/coherent-breathing.md").status_code == 200
        for bad in ("/md/de/page/pricing.md", "/md/en/secret/x.md", "/md/en/page/pricing", "/md/en/page/..%2F..%2Fetc%2Fpasswd.md"):
            assert http.get(bad).status_code == 404, bad


def test_server_card_and_catalog(content):
    from holos33_mcp import docs

    assert len(docs.DESCRIPTION) <= 100
    with client(content) as http:
        r = http.get("/mcp/server-card", headers={"origin": "https://evil.example"})
        assert r.status_code == 200, "Server Card is publiek; de Origin-check geldt alleen voor /mcp"
        assert r.headers["content-type"].startswith("application/mcp-server-card+json")
        assert r.headers["access-control-allow-origin"] == "*"
        assert "ETag" in r.headers["access-control-expose-headers"]
        card = r.json()
        assert card["name"] == "com.holos33/holos33" and card["remotes"][0]["url"] == "https://mcp.holos33.com/mcp"
        assert "tools" not in card
        assert http.get("/mcp/server-card", headers={"if-none-match": r.headers["etag"]}).status_code == 304
        assert http.options("/mcp/server-card").status_code == 204
        catalog = http.get("/.well-known/ai-catalog.json")
        assert catalog.headers["content-type"].startswith("application/ai-catalog+json")
        assert catalog.json()["entries"][0]["url"] == "https://mcp.holos33.com/mcp/server-card"
        # Het MCP-endpoint zelf blijft de Origin-check houden.
        assert http.post("/mcp", headers={"origin": "https://evil.example"}, json={}).status_code == 403


def test_registry_server_json_is_valid_shape():
    from holos33_mcp import docs

    data = docs.registry_server_json()
    assert data["$schema"].endswith("/2025-12-11/server.schema.json")
    assert set(["name", "description", "version"]) <= set(data)
    assert 3 <= len(data["name"]) <= 200 and 1 <= len(data["description"]) <= 100
    import re

    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", data["name"])
    assert data["remotes"] == [{"type": "streamable-http", "url": "https://mcp.holos33.com/mcp"}]
    assert data["icons"][0] == {"src": "https://mcp.holos33.com/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]}


def test_icons_served_and_in_card(content):
    from holos33_mcp import docs

    with client(content) as http:
        assert http.get("/mcp/server-card").json()["icons"] == docs.ICONS
        for name in ("icon-512.png", "icon-192.png"):
            r = http.get(f"/{name}")
            assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"


def test_docs_503_without_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLOS33_MCP_CONTENT", str(tmp_path))
    with TestClient(create_app()) as http:
        assert http.get("/llms.txt").status_code == 503
