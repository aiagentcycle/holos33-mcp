import json
from pathlib import Path

import pytest

from sync import holos33_sync as s

ROOT = Path(__file__).resolve().parents[1]

PAGE = """<html><head><title>x</title></head><body>
<nav class="nav"><a href="/en/">Home</a> Menu</nav>
<section class="hero"><nav class="breadcrumb">Home / Modules</nav><h1>Hypnosis &amp; Mindset</h1>
<p>Guided sessions<br>of 20 to 40 minutes. <a href="/en/pricing/?utm_source=chatgpt.com&amp;x=1">Start</a></p></section>
<section><h2>Features</h2><ul><li>Intake</li><li>Routine<ol><li>Step one</li></ol></li></ul>
<table><tr><th>Feature</th><th>Limit</th></tr><tr><td>AI coach</td><td>150 | month</td></tr></table>
<p hidden>verborgen</p><div aria-hidden="true">ook verborgen</div><script>alert(1)</script>
<p>Zero​width</p></section>
<div id="cmplz-cookiebanner-container"><p>Cookies!</p></div>
<footer><p>Footer text</p></footer></body></html>"""


def test_markdown_extraction():
    md, h1 = s.html_to_markdown(PAGE, "https://holos33.com/en/modules/hypnosis-mindset/")
    assert h1 == "Hypnosis & Mindset"
    assert md.startswith("# Hypnosis & Mindset")
    assert "[Start](https://holos33.com/en/pricing/?x=1)" in md
    assert "- Intake" in md and "  1. Step one" in md
    assert "| Feature | Limit |" in md and "| AI coach | 150 / month |" in md
    for gone in ("Menu", "Home / Modules", "verborgen", "alert", "Cookies!", "Footer text"):
        assert gone not in md
    assert "Zerowidth" in md


def test_noise_blocks_are_dropped():
    md, _ = s.html_to_markdown("<body><p>🌬️</p><p>5/5</p><p>→</p><p>HRV</p><p>Real text here</p></body>", "https://holos33.com/en/")
    assert md == "Real text here"


def test_rules_remove_blocks_with_notice_and_respect_scope():
    facts = json.loads((ROOT / "facts" / "facts.en.json").read_text(encoding="utf-8"))
    md = "Free Early Access.\n\nTry it free for 7 days, then a monthly or annual subscription.\n\nPay with iDEAL.\n\nSleep 7 days a week."
    warnings: list = []
    out = s.apply_rules(md, s.compile_rules(facts, "pages"), "pages.en.faq", warnings, "[removed]")
    assert out == "Free Early Access.\n\n[removed]\n\nSleep 7 days a week."
    assert len(warnings) == 2
    assert s.compile_rules(facts, "articles") == []


def test_real_faq_contradictions_are_blocked():
    facts = json.loads((ROOT / "facts" / "facts.en.json").read_text(encoding="utf-8"))
    rules = s.compile_rules(facts, "faq")
    assert s.blocked("You can try Holos33 completely free for 7 days — without a credit card.", rules)
    assert s.blocked("We accept iDEAL, credit card and PayPal.", rules)
    assert s.blocked("Holos33 B.V. is registered in the Netherlands.", rules)
    assert s.blocked("Is Holos33 a medical app? No.", rules) is None
    nl = json.loads((ROOT / "facts" / "facts.nl.json").read_text(encoding="utf-8"))
    assert s.blocked("Je kunt Holos33 7 dagen gratis proberen.", s.compile_rules(nl, "faq"))


def test_faq_from_json_ld_strips_zero_width():
    html = '<script type="application/ld+json">{"@graph":[{"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"Wat​ is het?","acceptedAnswer":{"text":"Een <b>platform</b>."}}]}]}</script>'
    assert s.faq_from_html(html) == [{"question": "Wat is het?", "answer": "Een platform ."}]


def test_hreflang_and_slug():
    html = '<link rel="alternate" hreflang="nl" href="https://holos33.com/nl/modules/module-hypnose/" />'
    assert s.hreflang_map(html) == {"nl": "https://holos33.com/nl/modules/module-hypnose/"}
    assert s.slug_of("https://holos33.com/en/") == "home"
    assert s.slug_of("https://holos33.com/en/modules/hypnosis-mindset/") == "hypnosis-mindset"


@pytest.mark.parametrize("url", ["http://holos33.com/x", "https://evil.example/", "https://holos33.com.evil.example/", "file:///etc/passwd"])
def test_only_holos33_https_is_fetched(url):
    with pytest.raises(s.SyncError):
        s.assert_allowed(url)


def _data(n_pages=20, n_modules=6, n_articles=10, n_faq=10):
    def lang_data():
        return {
            "pages": [{"key": f"p{i}", "kind": "core", "words": 300} for i in range(n_pages)],
            "modules": [{"key": f"m{i}", "words": 300} for i in range(n_modules)],
            "articles": [{"key": f"a{i}"} for i in range(n_articles)],
            "faq": [{"id": f"f{i}", "question": "Which wearables are supported?", "answer": "Oura, Fitbit, Apple Health and Garmin."}
                    for i in range(n_faq)],
        }

    return {"nl": lang_data(), "en": lang_data()}


def _facts():
    return {lang: json.loads((ROOT / "facts" / f"facts.{lang}.json").read_text(encoding="utf-8")) for lang in ("nl", "en")}


def test_validate_rejects_shrunk_or_thin_content():
    s.validate(_data(), _facts(), None)
    with pytest.raises(s.SyncError):
        s.validate(_data(n_pages=3), _facts(), None)
    previous = {"counts": {"en": {"articles": 40}, "nl": {}}}
    with pytest.raises(s.SyncError):
        s.validate(_data(n_articles=10), _facts(), previous)
    thin = _data()
    thin["en"]["modules"][0]["words"] = 10
    with pytest.raises(s.SyncError):
        s.validate(thin, _facts(), None)


def test_write_release_swaps_symlink_and_prunes(tmp_path):
    for i in range(7):
        (tmp_path / "releases" / f"20200101T00000{i}Z").mkdir(parents=True)
    target = s.write_release(tmp_path, _data(), _facts(), [], 1)
    assert (tmp_path / "current").resolve() == target.resolve()
    manifest = json.loads((tmp_path / "current" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["en"]["modules"] == 6
    left = sorted(p.name for p in (tmp_path / "releases").iterdir() if p.is_dir())
    assert len(left) == s.KEEP_RELEASES and left[-1] == target.name
    assert not (tmp_path / ".current.tmp").exists()


def test_validate_rejects_empty_core_page_and_thin_faq():
    empty = _data()
    empty["en"]["pages"][0].update(kind="core", words=0)
    with pytest.raises(s.SyncError, match="te weinig tekst"):
        s.validate(empty, _facts(), None)
    legal = _data()
    legal["en"]["pages"][0].update(kind="legal", words=0)
    s.validate(legal, _facts(), None)  # alleen core, access en privacy moeten 60 woorden hebben
    short_answer = _data()
    short_answer["nl"]["faq"][0]["answer"] = "Nee."
    with pytest.raises(s.SyncError, match="faq"):
        s.validate(short_answer, _facts(), None)
    short_question = _data()
    short_question["nl"]["faq"][0]["question"] = "Prijs?"
    with pytest.raises(s.SyncError, match="faq"):
        s.validate(short_question, _facts(), None)


def test_write_release_removes_leftover_tmp_dirs_and_counts_warnings(tmp_path):
    leftover = tmp_path / "releases" / ".oud.tmp"
    leftover.mkdir(parents=True)
    (leftover / "pages.en.json").write_text("[]", encoding="utf-8")
    target = s.write_release(tmp_path, _data(), _facts(), [{"where": "faq.en", "reason": "x"}], 1)
    assert not leftover.exists()
    assert not [p for p in (tmp_path / "releases").iterdir() if p.name.endswith(".tmp")]
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["warning_count"] == 1 and len(manifest["warnings"]) == 1


class FakeFetcher:
    """WP-REST-lijsten per (taal, endpoint) als batches; HTML per URL."""

    def __init__(self, batches: dict, pages: dict):
        self.batches = batches
        self.pages = pages
        self.json_urls: list[str] = []
        self.count = 0

    def get(self, url):
        if "/wp-json/" not in url:
            return self.pages[url], {}
        self.json_urls.append(url)
        lang = url.split("/")[3]
        endpoint = url.split("/wp/v2/")[1].split("?")[0]
        page = int(url.rsplit("&page=", 1)[1])
        batches = self.batches.get((lang, endpoint), [])
        if not batches:
            return "[]", {"x-wp-totalpages": "0"}  # endpoint zonder items (zoals een site zonder posts)
        if page > len(batches):
            raise s.SyncError(f"ophalen mislukt: {url}: HTTP 400")  # zoals WordPress bij een pagina voorbij het einde
        return json.dumps(batches[page - 1]), {"x-wp-totalpages": str(len(batches))}


def test_fetch_all_follows_pages_until_short_batch():
    batches = {("en", "posts"): [[{"id": i} for i in range(100)], [{"id": i} for i in range(100, 103)]]}
    fake = FakeFetcher(batches, {})
    items = s.fetch_all(fake, "https://holos33.com/en/wp-json/wp/v2/posts?per_page=100")
    assert [i["id"] for i in items] == list(range(103))
    assert [u.rsplit("&page=", 1)[1] for u in fake.json_urls] == ["1", "2"]
    # Precies 100 items: de header zegt 1 pagina, dus geen tweede (onbestaande) pagina opvragen.
    exact = FakeFetcher({("en", "posts"): [[{"id": i} for i in range(100)]]}, {})
    assert len(s.fetch_all(exact, "https://holos33.com/en/wp-json/wp/v2/posts?per_page=100")) == 100
    assert len(exact.json_urls) == 1
    endless = FakeFetcher({("en", "posts"): [[{"id": 0}] * 100] * 20}, {})
    with pytest.raises(s.SyncError, match="meer dan 10"):
        s.fetch_all(endless, "https://holos33.com/en/wp-json/wp/v2/posts?per_page=100")
    assert len(endless.json_urls) == s.MAX_API_PAGES


def test_build_applies_rules_to_titles_and_descriptions():
    page_url = "https://holos33.com/en/ai-coach/"
    module_url = "https://holos33.com/en/modules/sleep-recovery/"
    body = "<p>" + "The coach helps you build calm daily routines with breathing and rest. " * 6 + "</p>"
    html_page = f'<html><head><meta name="description" content="Wij genezen alles"></head><body><main><h1>Genezing gegarandeerd</h1>{body}</main></body></html>'
    html_module = f'<html><head><meta name="description" content="Sleep module"></head><body><main>{body}</main></body></html>'
    batches = {
        ("en", "pages"): [[{"id": 1, "slug": "ai-coach", "link": page_url, "title": {"rendered": "AI Coach"}}]],
        ("en", "modules"): [[{"id": 2, "slug": "sleep-recovery", "link": module_url, "title": {"rendered": "Genezing gegarandeerd"}}]],
    }
    facts = {
        "en": {"content_rules": {"removed_notice": "[removed]",
                                 "blocked_patterns": [{"pattern": "genez(en|ing)", "reason": "geen genezingsclaims"}]}},
        "nl": {},
    }
    data, warnings = s.build(FakeFetcher(batches, {page_url: html_page, module_url: html_module}), facts)
    page = data["en"]["pages"][0]
    assert page["description"] is None
    assert page["title"] == "AI Coach"  # h1 geblokkeerd, dus de WordPress-titel
    module = data["en"]["modules"][0]
    assert module["title"] == "Sleep recovery"  # geen schone kandidaat, dus afgeleid van de key
    assert module["summary"] == "Sleep module"
    where = {w["where"]: w for w in warnings}
    assert where["pages.en.ai-coach.description"]["excerpt"] == "Wij genezen alles"
    assert where["pages.en.ai-coach.title"]["reason"] == "geen genezingsclaims"
    assert where["modules.en.sleep-recovery.title"]["excerpt"] == "Genezing gegarandeerd"
    assert "Genezing" not in page["markdown"]


def test_redirect_is_an_error_not_a_retry(monkeypatch):
    import http.server
    import threading
    import urllib.request

    assert not issubclass(s.SyncError, OSError)  # dus buiten de retry-except van Fetcher.get
    with pytest.raises(s.SyncError, match="redirect 301 https://evil.example/"):
        s._NoRedirect().redirect_request(urllib.request.Request("https://holos33.com/en/"), None, 301, "Moved", {}, "https://evil.example/")

    hits = []

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(301)
            self.send_header("Location", "https://evil.example/")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(s, "assert_allowed", lambda url: None)
    try:
        with pytest.raises(s.SyncError, match="redirect 301"):
            s.Fetcher(delay=0, retries=2).get(f"http://127.0.0.1:{server.server_port}/x")
    finally:
        server.shutdown()
        server.server_close()
    assert hits == ["/x"]
