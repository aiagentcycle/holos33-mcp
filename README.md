# Holos33 MCP server

[![mcptry](https://mcptry.com/badge/com.holos33/holos33)](https://mcptry.com/servers/com.holos33/holos33)

Read-only [Model Context Protocol](https://modelcontextprotocol.io) server for [holos33.com](https://holos33.com), an EU, privacy-first wellbeing platform that combines six modules (stress regulation, sleep and recovery, breathing and relaxation, movement and vitality, nutrition and rhythm, hypnosis and mindset) with HRV biofeedback and an AI coach.

AI agents can use it to answer questions about Holos33 with sourced, current facts: what it is, how it works, the modules, Early Access and pricing, privacy and data handling, information for professionals, businesses, researchers and investors, the FAQ and contact details. Content is a validated snapshot of the website, refreshed every three hours, in English and Dutch.

- **Endpoint:** `https://mcp.holos33.com/mcp` (Streamable HTTP, no login, read-only)
- **MCP Registry:** `com.holos33/holos33`
- **Server Card:** <https://mcp.holos33.com/mcp/server-card>
- **For crawlers and LLMs:** [`llms.txt`](https://mcp.holos33.com/llms.txt), [`llms-full.txt`](https://mcp.holos33.com/llms-full.txt), Markdown per document under `/md/`

## Connect

Add it as a remote MCP server in any client that supports Streamable HTTP, for example in Claude Code:

```bash
claude mcp add --transport http holos33 https://mcp.holos33.com/mcp
```

## Tools

| Tool | What it returns |
| --- | --- |
| `get_overview` | What Holos33 is, its status, entity and the key facts |
| `list_modules` / `get_module` | The six modules, with the full module text |
| `search_content` | Ranked search over pages, modules, articles and FAQ |
| `get_page` / `get_article` | Full text of one site page or blog article, in parts for long documents |
| `get_faq` | The FAQ, filtered against the facts file |
| `get_access_and_pricing` | Early Access, eligibility and pricing as currently published |
| `get_privacy_and_data` | Privacy, data handling, sub-processors |
| `get_partner_info` | Information for professionals, businesses, research partners and investors |
| `get_contact_and_links` | Contact details and official links |

Resources (`holos33://facts/{lang}`, `holos33://page/{lang}/{key}`, `holos33://module/{lang}/{key}`, `holos33://article/{lang}/{key}`) and one prompt (`Explain Holos33`) expose the same content. All tools are annotated `readOnlyHint: true`, return structured JSON with an output schema, and log only a hash of their arguments.

## How it stays correct

- `facts/facts.{nl,en}.json` is the source of truth for entity, status, pricing, wearables, links and disclaimer. It also holds `blocked_patterns`: passages on the website that contradict the facts are removed from the snapshot and reported as warnings.
- `sync/holos33_sync.py` (standard library only) pulls the WordPress REST API and the rendered HTML of holos33.com, converts it to Markdown, applies the rules, validates counts and minimum text per page, and writes a new release. The sync is fail-closed: a partial, shrunken or broken site never replaces the previous release.
- The server loads a new release automatically within five seconds and keeps the old one if the new one does not load.
- A daily check covers reachability, freshness, version consistency with the Server Card and the Registry, discovery files, transport rules, TLS and the release contents.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/python sync/holos33_sync.py --content ./content --facts ./facts --log ""
HOLOS33_MCP_CONTENT=./content python -m holos33_mcp --host 127.0.0.1 --port 8324
.venv/bin/python -m pytest -q
```

The server only binds to localhost. In production it runs behind nginx with rate limiting and an Origin check on `/mcp`. See `deploy/` for the systemd units, the timer and the nginx config.

Built with [FastMCP](https://github.com/jlowin/fastmcp). Maintained by Good Nature Vibe B.V. (Oosterwolde, the Netherlands), the company behind Holos33.

## License

MIT, see `LICENSE`. The facts files describe Holos33's own public information and are included as an example of the content format.
