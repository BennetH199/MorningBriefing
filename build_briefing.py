#!/usr/bin/env python3
"""Morgenbriefing.

Ablauf:
1. RSS/Atom-Feeds aus sources.yml abrufen (nur Standardbibliothek + PyYAML).
2. Pro Bereich (Deutschland, Welt, Tech) Claude per API aufrufen, inklusive Websuche
   für Paywall-Lücken und Gegenprüfung. Claude liefert strukturiertes JSON.
3. Antwort validieren (Links nur, wenn sie aus Feeds/Suchergebnissen stammen).
4. JSON-Dateien nach docs/data/ schreiben, die die PWA anzeigt.

Aufrufe:
  python build_briefing.py                 # normaler Lauf
  python build_briefing.py --check-feeds   # nur Feeds testen, kein API-Aufruf, keine Kosten
  python build_briefing.py --force         # auch erstellen, wenn heute schon ein Briefing existiert
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
SOURCES_FILE = ROOT / "sources.yml"
TZ = ZoneInfo("Europe/Berlin")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
USER_AGENT = "Mozilla/5.0 (compatible; Morgenbriefing/1.0; persoenlicher Gebrauch)"

MODEL = os.environ.get("BRIEFING_MODEL", "claude-sonnet-5-5")
USE_WEB_SEARCH = os.environ.get("BRIEFING_WEB_SEARCH", "1") != "0"
WEB_SEARCH_TOOL = os.environ.get("BRIEFING_WEB_SEARCH_TOOL", "web_search_20250305")
MAX_SEARCHES = int(os.environ.get("BRIEFING_MAX_SEARCHES", "5"))
MAX_TOKENS = int(os.environ.get("BRIEFING_MAX_TOKENS", "10000"))
KEEP_DAYS = int(os.environ.get("BRIEFING_KEEP_DAYS", "30"))

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

LEAN_CAMP = {
    "left_edge": "left", "left": "left", "center_left": "left",
    "center": "center",
    "center_right": "right", "right": "right", "right_edge": "right",
}
LEAN_LABEL = {
    "left_edge": "linker Rand", "left": "links", "center_left": "Mitte-links",
    "center": "Mitte", "center_right": "Mitte-rechts", "right": "rechts",
    "right_edge": "rechter Rand", "none": "ohne Einordnung", "unknown": "unbekannt",
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean_text(s: str | None, limit: int | None = None) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    d = None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def fetch_url(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(5_000_000)


def _parse_xml(raw: bytes) -> ET.Element:
    raw = raw.lstrip()
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        return ET.fromstring(text)


def parse_feed(raw: bytes) -> list[dict]:
    """Liest RSS 2.0, RSS 1.0 (RDF) und Atom. Gibt title/link/summary/published zurück."""
    root = _parse_xml(raw)
    items: list[dict] = []
    for el in root.iter():
        name = local(el.tag)
        if name == "item":
            f: dict[str, str] = {}
            for c in el:
                f.setdefault(local(c.tag), "".join(c.itertext()).strip())
            link = f.get("link", "")
            if not link and f.get("guid", "").startswith("http"):
                link = f["guid"]
            items.append({
                "title": clean_text(f.get("title"), 220),
                "link": link.strip(),
                "summary": clean_text(f.get("description") or f.get("encoded") or f.get("summary"), 320),
                "published": parse_date(f.get("pubDate") or f.get("date") or f.get("published") or f.get("updated")),
            })
        elif name == "entry":
            f = {}
            link = ""
            for c in el:
                ln = local(c.tag)
                if ln == "link":
                    href = c.attrib.get("href", "")
                    if href and (c.attrib.get("rel", "alternate") == "alternate" or not link):
                        link = href
                else:
                    f.setdefault(ln, "".join(c.itertext()).strip())
            items.append({
                "title": clean_text(f.get("title"), 220),
                "link": link.strip(),
                "summary": clean_text(f.get("summary") or f.get("content"), 320),
                "published": parse_date(f.get("published") or f.get("updated")),
            })
    return [i for i in items if i["title"] and i["link"].startswith(("http://", "https://"))]


def collect_section(key: str, scfg: dict, settings: dict, now: dt.datetime):
    max_age = dt.timedelta(hours=float(settings.get("max_age_hours", 30)))
    per_source = int(settings.get("max_items_per_source", 8))
    items: list[dict] = []
    status: list[dict] = []
    seen: set[str] = set()
    for src in scfg["sources"]:
        if src.get("enabled", True) is False:
            continue
        st = {"name": src["name"], "ok": False, "count": 0, "error": None}
        try:
            parsed = parse_feed(fetch_url(src["feed"]))
            fresh = [i for i in parsed if not i["published"] or now - i["published"] <= max_age]
            fresh.sort(key=lambda i: i["published"] or now, reverse=True)
            taken = 0
            for it in fresh:
                if taken >= per_source:
                    break
                n = norm_url(it["link"])
                if n in seen:
                    continue
                seen.add(n)
                items.append({
                    "quelle": src["name"],
                    "lean": src.get("lean", "none"),
                    "role": src.get("role", "fact"),
                    "access": src.get("access", "free"),
                    "titel": it["title"],
                    "teaser": it["summary"],
                    "link": it["link"],
                    "zeit": it["published"].astimezone(TZ).strftime("%Y-%m-%d %H:%M") if it["published"] else None,
                })
                taken += 1
            st["ok"], st["count"] = True, taken
        except Exception as e:  # ein defekter Feed darf den Lauf nie stoppen
            st["error"] = f"{type(e).__name__}: {e}"[:160]
        status.append(st)
    for n, it in enumerate(items, 1):
        it["id"] = f"{key[:2].upper()}{n}"
    return items, status


# ---------------------------------------------------------------------------
# URL-Prüfung und Quellenzuordnung
# ---------------------------------------------------------------------------

def norm_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u.strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith("utm_")]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                                        urllib.parse.urlencode(q), ""))
    except ValueError:
        return u.strip()


def domain_of(u: str) -> str:
    try:
        d = urllib.parse.urlsplit(u).netloc.lower()
    except ValueError:
        return ""
    return d[4:] if d.startswith("www.") else d


class Lookup:
    """Ordnet Quellennamen und Domains der Konfiguration zu (Lager kommt NIE vom Modell)."""

    def __init__(self, scfg: dict, items: list[dict]):
        self.by_name = {s["name"].casefold(): s for s in scfg["sources"]}
        self.domains: dict[str, dict] = {}
        for it in items:
            s = self.by_name.get(it["quelle"].casefold())
            if s:
                self.domains[domain_of(it["link"])] = s
        self.allowed = {norm_url(i["link"]) for i in items}

    def add_search_urls(self, urls) -> None:
        self.allowed |= {norm_url(u) for u in urls}

    def source_for(self, name: str, url: str | None) -> dict | None:
        s = self.by_name.get(name.casefold())
        if s:
            return s
        if url:
            d = domain_of(url)
            for dom, src in self.domains.items():
                if d == dom or d.endswith("." + dom) or dom.endswith("." + d):
                    return src
        return None


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body

    def short(self) -> str:
        try:
            msg = json.loads(self.body).get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            msg = ""
        return f"API-Fehler {self.status}: {msg[:140]}".strip()


def api_request(payload: dict, api_key: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    for attempt in range(5):
        req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 529) and attempt < 4:
                wait = min(60, 5 * 2 ** attempt)
                log(f"  API {e.code}, neuer Versuch in {wait}s")
                time.sleep(wait)
                continue
            raise ApiError(e.code, body)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                time.sleep(5 * 2 ** attempt)
                continue
            raise RuntimeError(f"Netzwerkfehler: {e}")
    raise RuntimeError("API nicht erreichbar")


def ask_claude(system: str, user: str, api_key: str, search: bool):
    tools = None
    if search:
        tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES}]
    messages = [{"role": "user", "content": user}]
    urls: set[str] = set()
    searches = tokens_in = tokens_out = 0
    for _ in range(6):
        payload = {"model": MODEL, "max_tokens": MAX_TOKENS, "system": system, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            resp = api_request(payload, api_key)
        except ApiError as e:
            if tools and e.status == 400 and "web_search" in e.body.lower():
                log("  Websuche nicht verfügbar (in der Console aktivieren?) – weiter ohne Websuche")
                tools = None
                continue
            raise
        content = resp.get("content", [])
        usage = resp.get("usage", {})
        tokens_in += usage.get("input_tokens", 0)
        tokens_out += usage.get("output_tokens", 0)
        for b in content:
            t = b.get("type")
            if t == "server_tool_use" and b.get("name") == "web_search":
                searches += 1
            elif t == "web_search_tool_result" and isinstance(b.get("content"), list):
                for r in b["content"]:
                    if isinstance(r, dict) and r.get("url"):
                        urls.add(r["url"])
        if resp.get("stop_reason") == "pause_turn":
            messages.append({"role": "assistant", "content": content})
            continue
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        return text, urls, searches, tokens_in, tokens_out, resp.get("stop_reason")
    raise RuntimeError("Zu viele Fortsetzungen bei der Websuche")


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Antwort enthält kein JSON")
    return json.loads(t[start:end + 1])


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Du bist die Redaktion eines persönlichen Morgenbriefings für einen IT-Berater in Deutschland. Ziel: Er soll auf dem aktuellen Stand sein, ohne von einer politischen Seite eingefärbt zu werden.

ARBEITSWEISE
1. Grundlage sind die gelieferten Feed-Einträge (id, Quelle, Titel, Teaser, Link). Jede Tatsachenbehauptung muss durch diese Einträge oder durch Suchergebnisse gedeckt sein. Erfinde nichts.
2. Fasse Einträge zum selben Ereignis zu einer Story zusammen und wähle die wichtigsten Stories nach Relevanz für die Leserschaft. Ordne nach Wichtigkeit (importance 5 = sehr wichtig).
3. Trenne strikt:
   - facts: überprüfbare Tatsachen, die durch Agentur-/öffentlich-rechtliche Quellen oder durch mindestens zwei voneinander unabhängige Quellen gedeckt sind. Keine Wertungen, keine Adjektive mit Bewertung.
   - left und right: wie Medien des linken bzw. rechten Spektrums das Thema darstellen (Gewichtung, Deutung, Betonung, Auslassungen). Immer mit Quellenname, zum Beispiel "Die taz betont ...". Unterscheide innerhalb eines Lagers, wenn es relevant ist. Nur befüllen, wenn Quellen aus diesem Lager zum Thema vorliegen, sonst null. Unterstelle nichts.
   - disputed: was strittig, unbelegt oder noch unklar ist.
4. Gleichbehandlung: Beide Lager gleich ausführlich, gleich sachlich im Ton, ohne wertende Wörter. Keine eigene politische Meinung, kein Urteil bei umstrittenen Wertfragen. Ausgewogenheit heißt nicht, Falsches zu relativieren: Ist eine Tatsachenbehauptung nachweislich falsch, sage das sachlich und mit Beleg.
5. Quellen mit rolle=perspective dienen nur zur Rahmung, nie als alleinige Grundlage für Fakten.
6. Paywall: Bei zugang=paywall oder partial liegt oft nur ein Teaser vor. Stütze Fakten dann auf frei zugängliche Quellen. Gibt es keine freie Entsprechung in den Feeds, suche per Websuche nach einer freien Quelle (Agenturen, öffentlich-rechtliche Sender, BBC, Guardian und ähnliche). Wird nichts gefunden, setze paywall_note und beschreibe nur, was der Teaser hergibt. Umgehe keine Paywalls.
7. Websuche (nur falls verfügbar, höchstens @@MAX_SEARCHES@@ Suchen): (a) Paywall-Lücken schließen, (b) die wichtigsten 2 bis 3 Stories bei Agenturen gegenprüfen (z. B. Reuters, AP, dpa), (c) fehlende Gegenperspektive bei einem wichtigen Thema nachsuchen. Suchergebnisse nennst du als Quelle mit Name und URL.
8. Blindspots: Themen, die in Quellen des einen Lagers prominent vorkommen, im anderen fehlen. Beschreibe das sachlich und ohne zu unterstellen, warum.
9. Urheberrecht: Formuliere in eigenen Worten. Wörtliche Zitate nur kurz (unter 15 Wörtern) und nur, wenn der Wortlaut entscheidend ist. Baue keine Artikel nach.
10. Links: Nur URLs aus den Feed-Einträgen oder aus Suchergebnissen, exakt unverändert übernehmen. Nie Links erfinden oder ändern. Quellennamen exakt wie in der Quellentabelle schreiben.
11. Sprache: Deutsch, sachlich, knapp.
12. Keine Rechts-, Finanz- oder Regulierungsberatung: Bei solchen Themen nur den Sachstand wiedergeben.
13. Ausgabe: ausschließlich ein einzelnes JSON-Objekt nach dem vorgegebenen Schema. Kein Text davor oder danach, kein Markdown."""

SCHEMA_TEXT = """{
  "tageslage": "2-3 Sätze: die Lage in diesem Bereich",
  "stories": [
    {
      "title": "kurzer, neutraler Titel",
      "importance": 1-5,
      "facts": ["gesicherte Tatsache (max. 4)"],
      "left": "Rahmung durch linke Medien oder null",
      "right": "Rahmung durch rechte Medien oder null",
      "disputed": "Strittiges/Offenes oder null",
      "consulting_angle": "nur Tech: 1-2 Sätze, was das für IT-Projekte/Kunden bedeutet, sonst null",
      "paywall_note": "Hinweis falls Details hinter Paywall liegen, sonst null",
      "sources": [{"name": "Quellenname", "url": "https://..."}]
    }
  ],
  "blindspots": [
    {"title": "Thema", "side": "links oder rechts (Lager, das es prominent bringt)", "note": "sachliche Beschreibung", "sources": [{"name": "...", "url": "..."}]}
  ],
  "kurz": [
    {"title": "Kurzmeldung", "summary": "ein Satz", "source": "Quellenname", "url": "https://..."}
  ]
}"""


def german_date(now: dt.datetime) -> str:
    return f"{WEEKDAYS[now.weekday()]}, {now.day}. {now.month}. {now.year}"


def build_user_prompt(key: str, scfg: dict, items: list[dict], now: dt.datetime, search: bool) -> str:
    table = [
        {"name": s["name"], "lager": LEAN_LABEL.get(s.get("lean", "none"), "ohne Einordnung"),
         "rolle": s.get("role", "fact"), "zugang": s.get("access", "free")}
        for s in scfg["sources"] if s.get("enabled", True) is not False
    ]
    feed_items = [
        {"id": i["id"], "quelle": i["quelle"], "titel": i["titel"], "teaser": i["teaser"],
         "link": i["link"], "zeit": i["zeit"]}
        for i in items
    ]
    max_stories = int(scfg.get("max_stories", 6))
    search_note = (
        f"Websuche ist verfügbar (höchstens {MAX_SEARCHES} Suchen)."
        if search else "Es steht keine Websuche zur Verfügung: arbeite nur mit den Feed-Daten und nenne Lücken offen."
    )
    return (
        f"Heute ist {german_date(now)}. Erstelle den Bereich \"{scfg['title']}\" des Morgenbriefings.\n\n"
        f"SCHWERPUNKT: {' '.join(str(scfg.get('focus', '')).split())}\n\n"
        f"{search_note}\n"
        f"Limits: höchstens {max_stories} Stories, höchstens 3 Blindspots, höchstens 8 Kurzmeldungen. "
        "Pro Story höchstens 4 facts (je unter 200 Zeichen); left/right/disputed je unter 400 Zeichen.\n\n"
        "QUELLENTABELLE (Einordnung ist grob und konfigurierbar):\n"
        f"{json.dumps(table, ensure_ascii=False)}\n\n"
        f"FEED-EINTRÄGE ({len(feed_items)}):\n"
        f"{json.dumps(feed_items, ensure_ascii=False)}\n\n"
        "AUSGABE-SCHEMA (nur dieses JSON liefern):\n"
        f"{SCHEMA_TEXT}"
    )


# ---------------------------------------------------------------------------
# Validierung
# ---------------------------------------------------------------------------

def s(x, limit: int) -> str | None:
    if not isinstance(x, str):
        return None
    x = " ".join(x.split())
    if not x or x.lower() in ("null", "none"):
        return None
    return x if len(x) <= limit else x[:limit].rsplit(" ", 1)[0] + "…"


def sanitize_sources(raw, lookup: Lookup, limit: int = 8) -> list[dict]:
    out, seen = [], set()
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        name = s(r.get("name"), 80)
        if not name:
            continue
        url = r.get("url") if isinstance(r.get("url"), str) else None
        verified = bool(url and url.startswith(("http://", "https://")) and norm_url(url) in lookup.allowed)
        src = lookup.source_for(name, url if verified else None)
        lean = src.get("lean", "none") if src else "unknown"
        entry_key = (name.casefold(), norm_url(url) if verified else "")
        if entry_key in seen:
            continue
        seen.add(entry_key)
        out.append({
            "name": src["name"] if src else name,
            "url": url if verified else None,
            "lean": lean,
            "camp": LEAN_CAMP.get(lean),
            "role": src.get("role") if src else None,
        })
        if len(out) >= limit:
            break
    return out


def sanitize_section(raw: dict, scfg: dict, lookup: Lookup) -> dict:
    stories = []
    for st in raw.get("stories", []) if isinstance(raw.get("stories"), list) else []:
        if not isinstance(st, dict):
            continue
        title = s(st.get("title"), 160)
        raw_facts = st.get("facts") if isinstance(st.get("facts"), list) else []
        facts = [f for f in (s(x, 280) for x in raw_facts) if f][:5]
        if not title or not facts:
            continue
        imp = st.get("importance")
        imp = imp if isinstance(imp, int) and 1 <= imp <= 5 else 3
        sources = sanitize_sources(st.get("sources"), lookup)
        stories.append({
            "title": title,
            "importance": imp,
            "facts": facts,
            "left": s(st.get("left"), 520),
            "right": s(st.get("right"), 520),
            "disputed": s(st.get("disputed"), 520),
            "consulting_angle": s(st.get("consulting_angle"), 420),
            "paywall_note": s(st.get("paywall_note"), 220),
            "sources": sources,
            "unverified": not any(x["url"] for x in sources),
        })
    stories.sort(key=lambda x: -x["importance"])
    stories = stories[: int(scfg.get("max_stories", 6))]

    blind = []
    for b in raw.get("blindspots", []) if isinstance(raw.get("blindspots"), list) else []:
        if not isinstance(b, dict):
            continue
        title, note = s(b.get("title"), 140), s(b.get("note"), 420)
        if not title or not note:
            continue
        side = s(b.get("side"), 20)
        side = side.lower() if side and side.lower() in ("links", "rechts") else None
        blind.append({"title": title, "side": side, "note": note,
                      "sources": sanitize_sources(b.get("sources"), lookup, 5)})
    blind = blind[:3]

    kurz = []
    for k in raw.get("kurz", []) if isinstance(raw.get("kurz"), list) else []:
        if not isinstance(k, dict):
            continue
        title = s(k.get("title"), 140)
        if not title:
            continue
        src = sanitize_sources([{"name": k.get("source") or "", "url": k.get("url")}], lookup, 1)
        kurz.append({"title": title, "summary": s(k.get("summary"), 260),
                     "source": src[0] if src else None})
    kurz = kurz[:8]

    if not stories:
        raise ValueError("Antwort enthielt keine verwertbaren Stories")
    return {
        "title": scfg["title"],
        "tageslage": s(raw.get("tageslage"), 700),
        "stories": stories,
        "blindspots": blind,
        "kurz": kurz,
    }


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def run_section(key: str, scfg: dict, items: list[dict], now: dt.datetime, api_key: str):
    system = SYSTEM_PROMPT.replace("@@MAX_SEARCHES@@", str(MAX_SEARCHES if USE_WEB_SEARCH else 0))
    user = build_user_prompt(key, scfg, items, now, USE_WEB_SEARCH)
    text, urls, searches, t_in, t_out, stop = ask_claude(system, user, api_key, USE_WEB_SEARCH)
    if stop == "max_tokens":
        log("  Warnung: Antwort wurde wegen max_tokens abgeschnitten")
    data = extract_json(text)
    lookup = Lookup(scfg, items)
    lookup.add_search_urls(urls)
    section = sanitize_section(data, scfg, lookup)
    section["stats"] = {"feed_items": len(items), "web_searches": searches}
    return section, t_in, t_out


def day_complete(path: Path) -> bool:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        secs = d.get("sections", {})
        return bool(secs) and all("error" not in v for v in secs.values())
    except (OSError, ValueError):
        return False


def write_outputs(doc: dict, today: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, indent=1)
    (DATA_DIR / f"{today}.json").write_text(payload, encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(payload, encoding="utf-8")
    dated = sorted((p for p in DATA_DIR.glob("????-??-??.json")), reverse=True)
    for old in dated[KEEP_DAYS:]:
        old.unlink()
    dates = [p.stem for p in dated[:KEEP_DAYS]]
    (DATA_DIR / "index.json").write_text(
        json.dumps({"latest": today, "dates": dates}, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-feeds", action="store_true", help="nur Feeds testen (keine API-Kosten)")
    ap.add_argument("--force", action="store_true", help="auch neu erstellen, wenn heute schon vorhanden")
    ap.add_argument("--sections", help="nur diese Bereiche, z. B. de,tech")
    args = ap.parse_args()

    check_only = args.check_feeds or os.environ.get("CHECK_FEEDS", "").lower() == "true"
    force = (args.force or os.environ.get("BRIEFING_FORCE") == "1"
             or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")

    cfg = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    settings = cfg.get("settings", {})
    now = dt.datetime.now(TZ)
    today = now.date().isoformat()
    out_path = DATA_DIR / f"{today}.json"

    if not check_only and not force and out_path.exists() and day_complete(out_path):
        log(f"Briefing für {today} existiert bereits vollständig – nichts zu tun.")
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not check_only and not api_key:
        log("Fehler: ANTHROPIC_API_KEY ist nicht gesetzt (GitHub: Settings → Secrets and variables → Actions).")
        return 2

    wanted = set(args.sections.split(",")) if args.sections else None
    sections: dict[str, dict] = {}
    feed_status: dict[str, list] = {}
    total_in = total_out = 0

    for key, scfg in cfg["sections"].items():
        if wanted and key not in wanted:
            continue
        log(f"\n=== {scfg['title']} ===")
        items, status = collect_section(key, scfg, settings, now)
        feed_status[key] = status
        ok = sum(1 for x in status if x["ok"])
        log(f"Feeds erreichbar: {ok}/{len(status)}, Einträge: {len(items)}")
        for st in status:
            mark = "ok  " if st["ok"] else "FEHL"
            extra = f"{st['count']} Einträge" if st["ok"] else st["error"]
            log(f"  [{mark}] {st['name']}: {extra}")
        if check_only:
            continue
        if not items:
            sections[key] = {"title": scfg["title"], "error": "Keine Feed-Einträge abrufbar."}
            continue
        try:
            sections[key], t_in, t_out = run_section(key, scfg, items, now, api_key)
            total_in += t_in
            total_out += t_out
            log(f"  Fertig: {len(sections[key]['stories'])} Stories, "
                f"{sections[key]['stats']['web_searches']} Websuchen")
        except ApiError as e:
            sections[key] = {"title": scfg["title"], "error": e.short()}
            log(f"  Fehler: {e.short()}")
        except Exception as e:
            sections[key] = {"title": scfg["title"], "error": f"{type(e).__name__}: {str(e)[:140]}"}
            log(f"  Fehler: {type(e).__name__}: {e}")

    if check_only:
        log("\nFeed-Check beendet (keine Dateien geschrieben).")
        return 0

    if all("error" in v for v in sections.values()):
        log("\nAlle Bereiche fehlgeschlagen – vorhandene Daten bleiben unverändert.")
        return 1

    doc = {
        "meta": {
            "date": today,
            "weekday": WEEKDAYS[now.weekday()],
            "generated_at": now.isoformat(timespec="minutes"),
            "model": MODEL,
            "web_search": USE_WEB_SEARCH,
            "tokens_in": total_in,
            "tokens_out": total_out,
            "feed_status": feed_status,
            "sources": {
                k: [{"name": x["name"], "lean": x.get("lean", "none"), "role": x.get("role", "fact"),
                     "access": x.get("access", "free")} for x in v["sources"] if x.get("enabled", True) is not False]
                for k, v in cfg["sections"].items() if not wanted or k in wanted
            },
        },
        "sections": sections,
    }
    write_outputs(doc, today)
    log(f"\nBriefing {today} geschrieben ({total_in} Tokens ein, {total_out} aus).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
