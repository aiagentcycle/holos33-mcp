import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _facts(lang: str) -> dict:
    return json.loads((ROOT / "facts" / f"facts.{lang}.json").read_text(encoding="utf-8"))


def _item(key: str, lang: str, title: str, body: str, **extra) -> dict:
    url = f"https://holos33.com/{lang}/{key}/"
    return {
        "key": key,
        "lang": lang,
        "title": title,
        "url": url,
        "alternate_url": url.replace(f"/{lang}/", "/nl/" if lang == "en" else "/en/"),
        "last_modified": "2026-10-01T10:00:00Z",
        "markdown": body,
        "words": len(body.split()),
        **extra,
    }


def write_release(root: Path, name: str, *, broken: bool = False) -> Path:
    release = root / "releases" / name
    release.mkdir(parents=True)
    for lang in ("nl", "en"):
        pages = [
            _item("pricing", lang, "Pricing", "Early Access is free.", kind="access", description="Free early access"),
            _item("for-businesses", lang, "For businesses", "Pilot of 6 weeks. " * 5, kind="partner", description="B2B"),
            _item("privacy-policy", lang, "Privacy", ("Data stays in the EU. " * 2000), kind="legal", description="Privacy"),
        ]
        modules = [
            _item("hypnosis-mindset", lang, "Hypnosis & Mindset", "Guided hypnosis sessions of 20 to 40 minutes with relaxation.",
                  slug="module-hypnose" if lang == "nl" else "hypnosis-mindset", summary="Hypnosis module"),
            _item("sleep-recovery", lang, "Sleep & Recovery", "Sleep routines and recovery.",
                  slug="module-slaap" if lang == "nl" else "sleep-recovery", summary="Sleep module"),
        ]
        articles = [
            _item("coherent-breathing", lang, "Coherent breathing", "Breathing at six breaths per minute and HRV.",
                  summary="Breathing article", categories=["Breathing"], date_published="2026-09-01T10:00:00Z"),
            _item("hypnosis-science", lang, "What science says about hypnosis", "Hypnosis research overview.",
                  summary="Hypnosis article", categories=["Science"], date_published="2026-08-01T10:00:00Z"),
        ]
        faq = [
            {"id": "faq-1", "question": "Which wearables are supported?", "answer": "Oura, Fitbit, Apple Health, Garmin.", "source_url": "https://holos33.com/en/faq/"},
            {"id": "faq-2", "question": "Is Holos33 a medical app?", "answer": "No.", "source_url": "https://holos33.com/en/faq/"},
        ]
        for kind, data in (("pages", pages), ("modules", modules), ("articles", articles), ("faq", faq)):
            (release / f"{kind}.{lang}.json").write_text(json.dumps([] if broken else data), encoding="utf-8")
        (release / f"facts.{lang}.json").write_text(json.dumps(_facts(lang)), encoding="utf-8")
    manifest = {"release": name, "facts_version": _facts("en")["facts_version"], "generated_at": "2026-10-31T00:00:00Z",
                "warnings": [{"where": "faq.en", "reason": "test"}]}
    (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return release


def point_current(root: Path, name: str) -> None:
    link = root / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path("releases") / name)


@pytest.fixture
def content(tmp_path: Path, monkeypatch) -> Path:
    write_release(tmp_path, "r1")
    point_current(tmp_path, "r1")
    monkeypatch.setenv("HOLOS33_MCP_CONTENT", str(tmp_path))
    monkeypatch.setenv("HOLOS33_MCP_LOG", str(tmp_path / "toolcalls.jsonl"))
    return tmp_path
