"""Offline-Tests: keine Netzwerkzugriffe, keine API-Kosten.  Ausführen: python -m unittest discover tests"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_briefing as bb  # noqa: E402

NOW = dt.datetime(2026, 11, 3, 5, 0, tzinfo=bb.TZ)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>T</title>
<item><title>Haushalt &amp; Streit</title><link>https://www.tagesschau.de/a1?utm_source=x</link>
<description>&lt;p&gt;Teaser &amp;amp; mehr&lt;/p&gt;</description><pubDate>Tue, 03 Nov 2026 03:30:00 +0100</pubDate></item>
<item><title>Alt</title><link>https://www.tagesschau.de/old</link><pubDate>Mon, 26 Oct 2026 03:30:00 +0100</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom Titel</title>
<link rel="alternate" href="https://www.heise.de/n/1"/><summary>Kurz</summary>
<updated>2026-11-03T02:00:00Z</updated></entry></feed>"""

BAD_ENTITY = b"""<?xml version="1.0"?><rss><channel><item><title>A &nbsp; B</title>
<link>https://example.org/x</link></item></channel></rss>"""

SCFG = {"title": "Deutschland", "max_stories": 6, "focus": "Test", "sources": [
    {"name": "Tagesschau", "feed": "x", "lean": "center", "role": "fact", "access": "free"},
    {"name": "taz", "feed": "x", "lean": "left", "role": "fact", "access": "free"},
    {"name": "Welt", "feed": "x", "lean": "center_right", "role": "fact", "access": "partial"},
]}


class FeedTests(unittest.TestCase):
    def test_rss(self):
        items = bb.parse_feed(RSS.encode())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Haushalt & Streit")
        self.assertEqual(items[0]["summary"], "Teaser & mehr")
        self.assertIsNotNone(items[0]["published"])

    def test_atom(self):
        items = bb.parse_feed(ATOM.encode())
        self.assertEqual(items[0]["link"], "https://www.heise.de/n/1")

    def test_broken_entity_is_tolerated(self):
        self.assertEqual(len(bb.parse_feed(BAD_ENTITY)), 1)

    def test_collect_filters_old_and_reports_errors(self):
        def fake_fetch(url, timeout=20):
            if url == "bad":
                raise OSError("down")
            return RSS.encode()
        cfg = {"sources": [dict(SCFG["sources"][0], feed="ok"), dict(SCFG["sources"][1], feed="bad")]}
        with mock.patch.object(bb, "fetch_url", fake_fetch):
            items, status = bb.collect_section("de", cfg, {"max_age_hours": 30}, NOW)
        self.assertEqual([i["titel"] for i in items], ["Haushalt & Streit"])
        self.assertTrue(status[0]["ok"])
        self.assertFalse(status[1]["ok"])
        self.assertEqual(items[0]["id"], "DE1")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.items = [{"quelle": "Tagesschau", "link": "https://www.tagesschau.de/a1", "lean": "center"},
                      {"quelle": "taz", "link": "https://taz.de/b2", "lean": "left"}]
        self.lookup = bb.Lookup(SCFG, self.items)
        self.lookup.add_search_urls(["https://www.reuters.com/world/x/"])

    def test_extract_json_with_fence(self):
        self.assertEqual(bb.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(bb.extract_json('Hier: {"a": 2} Ende'), {"a": 2})

    def test_invented_links_are_dropped_and_lean_comes_from_config(self):
        raw = {"tageslage": "x", "stories": [{
            "title": "T", "importance": 4, "facts": ["F1", "F2"], "left": "taz betont", "right": None,
            "sources": [
                {"name": "Tagesschau", "url": "https://www.tagesschau.de/a1/"},
                {"name": "taz", "url": "https://taz.de/ERFUNDEN"},
                {"name": "Reuters", "url": "https://www.reuters.com/world/x"},
            ]}]}
        sec = bb.sanitize_section(raw, SCFG, self.lookup)
        src = sec["stories"][0]["sources"]
        self.assertEqual(src[0]["url"], "https://www.tagesschau.de/a1/")
        self.assertIsNone(src[1]["url"])
        self.assertEqual(src[1]["lean"], "left")          # Name bekannt -> Lager aus Konfiguration
        self.assertEqual(src[2]["lean"], "unknown")       # Websuche-Quelle ohne Einordnung
        self.assertIsNotNone(src[2]["url"])               # aber URL stammt aus Suchergebnis

    def test_domain_match_assigns_lean(self):
        src = bb.sanitize_sources([{"name": "taz.de", "url": "https://taz.de/b2"}], self.lookup)
        self.assertEqual(src[0]["lean"], "left")

    def test_story_without_facts_is_skipped_and_empty_raises(self):
        with self.assertRaises(ValueError):
            bb.sanitize_section({"stories": [{"title": "T", "facts": []}]}, SCFG, self.lookup)


class PipelineTests(unittest.TestCase):
    def test_full_run_with_fake_api(self):
        reply = {"tageslage": "Lage", "stories": [{"title": "Haushalt", "importance": 5, "facts": ["Fakt"],
                 "sources": [{"name": "Tagesschau", "url": "https://www.tagesschau.de/a1"}]}], "blindspots": [], "kurz": []}
        fake_resp = {"content": [{"type": "server_tool_use", "name": "web_search"},
                                 {"type": "web_search_tool_result", "content": [{"url": "https://www.reuters.com/z"}]},
                                 {"type": "text", "text": json.dumps(reply)}],
                     "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}
        calls = []

        def fake_api(payload, key):
            calls.append(payload)
            return fake_resp

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bb, "fetch_url", lambda u, timeout=20: RSS.encode()), \
                mock.patch.object(bb, "api_request", fake_api), \
                mock.patch.object(bb, "DATA_DIR", Path(tmp)), \
                mock.patch.object(bb, "SOURCES_FILE", Path(tmp) / "s.yml"), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test", "BRIEFING_FORCE": "1"}), \
                mock.patch.object(sys, "argv", ["x"]):
            (Path(tmp) / "s.yml").write_text(
                "settings: {max_age_hours: 9999}\nsections:\n  de:\n    title: Deutschland\n    sources:\n"
                "      - {name: Tagesschau, feed: a, lean: center, role: fact, access: free}\n")
            self.assertEqual(bb.main(), 0)
            latest = json.loads((Path(tmp) / "latest.json").read_text())
            self.assertEqual(latest["sections"]["de"]["stories"][0]["title"], "Haushalt")
            self.assertEqual(latest["sections"]["de"]["stats"]["web_searches"], 1)
            idx = json.loads((Path(tmp) / "index.json").read_text())
            self.assertEqual(len(idx["dates"]), 1)
        self.assertEqual(calls[0]["tools"][0]["name"], "web_search")
        self.assertIn("Haushalt", calls[0]["messages"][0]["content"])

    def test_check_feeds_makes_no_api_call(self):
        with mock.patch.object(bb, "fetch_url", lambda u, timeout=20: RSS.encode()), \
                mock.patch.object(bb, "api_request", side_effect=AssertionError("API darf nicht aufgerufen werden")), \
                mock.patch.object(sys, "argv", ["x", "--check-feeds"]):
            self.assertEqual(bb.main(), 0)


if __name__ == "__main__":
    unittest.main()
