# Morgenbriefing

Tägliches, ausgewogenes Nachrichtenbriefing als App auf dem iPhone: **Deutschland, Welt, Tech**.
Jede Story zeigt gesicherte Fakten, die Rahmung linker und rechter Medien nebeneinander,
Strittiges und eine Spektrum-Leiste, die zeigt, aus welchen Lagern die Quellen stammen.

Läuft automatisch über GitHub Actions. Kein eigener Server nötig.

## Einrichtung (ca. 15 Minuten)

1. **Repository anlegen:** Auf github.com ein neues Repository erstellen (z. B. `morgenbriefing`)
   und den gesamten Inhalt dieses Ordners hochladen (*Add file → Upload files*).
   Wichtig: Der versteckte Ordner `.github/workflows/` muss dabei sein. Falls nicht:
   *Add file → Create new file*, als Name `.github/workflows/briefing.yml` eintippen und den Inhalt
   der mitgelieferten Datei `briefing.yml` einfügen.
2. **API-Key als Secret:** In der Anthropic Console einen API-Key erzeugen (dort auch ein Ausgabenlimit setzen
   und **Websuche aktivieren**, falls die Organisation das verlangt). In GitHub:
   *Settings → Secrets and variables → Actions → New repository secret*, Name `ANTHROPIC_API_KEY`.
   Den Key nie in Dateien, Chats oder Issues einfügen.
3. **Pages aktivieren:** *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
4. **Feeds testen (kostenlos):** *Actions → Morgenbriefing → Run workflow*, Haken bei
   **Nur Feeds testen** setzen. Im Log siehst du pro Quelle `[ok]` oder `[FEHL]`.
   Defekte Feeds in `sources.yml` korrigieren oder mit `enabled: false` abschalten.
5. **Erstes Briefing erzeugen:** *Run workflow* ohne Haken. Nach ein paar Minuten ist die Seite unter
   `https://<dein-github-name>.github.io/<repo-name>/` erreichbar (die URL steht im Deploy-Schritt).
6. **iPhone:** URL in **Safari** öffnen → *Teilen → Zum Home-Bildschirm*.

Ab dann läuft alles von selbst.

## Zeitplan

GitHub rechnet Cron in UTC. Der Workflow startet täglich um 03:47, 04:47 und 05:47 UTC
(Berlin: im Sommer 05:47, 06:47, 07:47 Uhr; im Winter 04:47, 05:47, 06:47 Uhr).
Das Skript überspringt spätere Läufe, wenn das Briefing von heute schon vollständig vorliegt.
Schlägt ein Bereich fehl, versucht der nächste Lauf es erneut.
GitHub kann geplante Läufe verzögern. Plane einen Puffer ein.

Hinweis: GitHub deaktiviert geplante Workflows nach 60 Tagen ohne Repository-Aktivität.
Da täglich neue Daten committet werden, sollte das nicht passieren. Schau trotzdem gelegentlich
in den Actions-Tab.

## Quellen anpassen

Alles steht in `sources.yml`: Quelle, Feed-URL, Lager (`lean`), Rolle (`fact` oder `perspective`) und
Paywall-Status. Die Einordnung ist grob und eine bewusste Konfigurationsentscheidung. Passe sie an,
wenn du eine Quelle anders siehst. Quellen mit `perspective` dienen nur der Einordnung, nie als
alleinige Faktenbasis.

Reuters, AP und dpa bieten keine freien RSS-Feeds. Sie werden über die Websuche zur Gegenprüfung genutzt.

## Wie die Ausgewogenheit entsteht

- Faktenbasis aus Agentur-/öffentlich-rechtlichen Quellen oder mindestens zwei unabhängigen Quellen.
- Linke und rechte Rahmung stehen gleich ausführlich nebeneinander, mit Quellennamen.
- Das Lager einer Quelle kommt aus `sources.yml`, nie vom Modell.
- Links werden geprüft: Nur URLs aus den Feeds oder aus Suchergebnissen werden angezeigt.
- Bei Paywalls sucht Claude eine frei zugängliche Quelle zur selben Meldung. Paywalls werden nicht umgangen.
- Abschnitt „Nur eine Seite berichtet" zeigt Blindspots.

Grenzen: Das ist KI-Zusammenfassung. Sie kann Fehler oder unbewusste Färbungen enthalten.
Prüfe wichtige Details in den verlinkten Originalquellen.

## Kosten

Pro Tag drei API-Aufrufe (ein Aufruf je Bereich) inklusive Websuche, in der Regel wenige Cent bis
unter einen Euro, abhängig von Modell und Anzahl der Suchen. Stellschrauben als Umgebungsvariablen im
Workflow: `BRIEFING_MODEL`, `BRIEFING_MAX_SEARCHES` (Standard 5 je Bereich), `BRIEFING_WEB_SEARCH=0`
(Websuche aus). Setze in der Anthropic Console ein Ausgabenlimit.

## Dateien

| Datei | Zweck |
|---|---|
| `build_briefing.py` | Feeds holen, Claude aufrufen, Ergebnis prüfen, JSON schreiben |
| `sources.yml` | Quellen und Einordnung |
| `docs/` | Die App (HTML, Manifest, Service Worker, Icons, Daten) |
| `.github/workflows/briefing.yml` | Zeitplan und Veröffentlichung |
| `tests/` | Offline-Tests (laufen bei jedem Lauf vorab) |

Lokal testen: `pip install pyyaml`, dann `python build_briefing.py --check-feeds`
bzw. `python -m unittest discover tests`.

## Datenschutz und Hinweise

- Nur für privaten Gebrauch gedacht. Keine Kundendaten oder internen Informationen verwenden.
- Eine GitHub-Pages-Seite ist öffentlich erreichbar (Link kennt nur, wer ihn hat, ist aber nicht geschützt).
  Das Briefing enthält nur Zusammenfassungen und Links. Private Repositories mit Pages können einen
  kostenpflichtigen GitHub-Plan erfordern. Prüfe das in den GitHub-Einstellungen.
- Zusammenfassungen verlinken auf die Originalartikel und kopieren keine Texte.
