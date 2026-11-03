import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative_path: str):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_portable_plugin_manifests_only_connect_to_public_holos33():
    mcp = load(".mcp.json")
    assert mcp == {
        "mcpServers": {
            "holos33": {
                "type": "http",
                "url": "https://mcp.holos33.com/mcp",
            }
        }
    }

    claude = load(".claude-plugin/plugin.json")
    grok = load(".grok-plugin/plugin.json")
    marketplace = load(".claude-plugin/marketplace.json")
    assert claude["name"] == grok["name"] == "holos33-mcp"
    assert claude["version"] == grok["version"] == "1.0.2"
    assert claude["license"] == grok["license"] == "MIT"
    assert marketplace["plugins"] == [
        {
            "name": "holos33-mcp",
            "source": "./",
            "description": claude["description"],
        }
    ]
    assert (ROOT / claude["icon"]).is_file()


def test_plugin_manifests_contain_no_secret_or_private_commerce_configuration():
    text = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            ".mcp.json",
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
            ".grok-plugin/plugin.json",
        )
    ).lower()
    for forbidden in (
        "com.goodnaturevibe/" + "commerce",
        "commerce-mcp." + "goodnaturevibe.com",
        '"headers"',
        '"env"',
        '"command"',
        "api_" + "key",
        "pass" + "word",
        "to" + "ken",
    ):
        assert forbidden not in text
