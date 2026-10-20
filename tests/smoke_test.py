"""Smoke tests that need no LLM key: schema, templates, routes, search/fetch helpers."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import app, load_library
from app.agent import _DDGParser, _TextExtractor, _is_public_url, _sanitize_site

REQUIRED = {"slug", "app_name", "developer_console", "difficulty", "setup_time",
            "approval", "summary", "steps", "requirements", "nuances",
            "agent_can_do", "human_must_do", "evidence"}

def test_guides_schema():
    for g in load_library():
        missing = REQUIRED - g.keys()
        assert not missing, f"{g.get('slug')}: missing {missing}"
        assert g["difficulty"] in ("easy", "medium", "hard")
        assert g["evidence"], f"{g['slug']}: no evidence"
        for ev in g["evidence"]:
            assert ev["source"].startswith("http"), ev
            assert ev["status"] in ("docs-supported", "tested", "inferred")
    print("guides schema OK")

def test_parsers():
    lp = _DDGParser()
    lp.feed('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">x</a>')
    assert lp.results == ["https://example.com/docs"], lp.results
    te = _TextExtractor()
    te.feed("<html><script>bad()</script><body><p>Hello <b>world</b></p></body></html>")
    assert "Hello world" in te.text() and "bad()" not in te.text()
    print("parsers OK")

def test_routes():
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200 and "Guide library" in r.text
    r = c.get("/guides/github")
    assert r.status_code == 200 and "OAuth apps" in r.text
    r = c.get("/guides/does-not-exist")
    assert r.status_code == 404
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"]
    r = c.post("/api/generate", json={"query": ""})
    assert r.status_code == 400
    print("routes OK")

def test_ssrf_guard():
    assert not _is_public_url("http://localhost:8000/")
    assert not _is_public_url("http://127.0.0.1/")
    assert not _is_public_url("http://169.254.169.254/latest/meta-data")
    assert not _is_public_url("http://10.0.0.5/internal")
    assert not _is_public_url("http://192.168.1.1/")
    assert not _is_public_url("ftp://example.com/")
    assert not _is_public_url("file:///etc/passwd")
    assert _is_public_url("https://docs.github.com/")
    print("ssrf guard OK")

def test_url_sanitizer():
    assert _sanitize_site("https://figma.com/apps?token=secret123#frag") == "https://figma.com/apps"
    assert _sanitize_site("figma.com?api_key=xyz") == "https://figma.com"
    assert _sanitize_site("https://example.com") == "https://example.com"
    for bad in ("https://user:pass@example.com/", "ftp://example.com"):
        try:
            _sanitize_site(bad)
            raise AssertionError(f"should have rejected {bad}")
        except ValueError:
            pass
    print("url sanitizer OK")

if __name__ == "__main__":
    test_ssrf_guard()
    test_url_sanitizer()
    test_guides_schema()
    test_parsers()
    test_routes()
    print("ALL SMOKE TESTS PASSED")
