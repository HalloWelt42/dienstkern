# Dienstkern

Gemeinsame Grundlage für Programme, die Dienste eines Rechners starten, anhalten und beobachten. Der Kern hält fest,
was laufen soll, startet zuerst, was ein Dienst braucht, startet nach einem Absturz begrenzt neu, misst Speicher und
Prozessorlast je Dienst und schreibt je Dienst ein eigenes Protokoll. Woher die Dienste kommen und wer sie sehen darf,
entscheidet das Programm darüber.

## Zusammenspiel

Der Dienstkern ist die Grundlage von [Maschinenraum](https://github.com/HalloWelt42/maschinenraum) und
[Stellwerk](https://github.com/HalloWelt42/stellwerk). Beide nutzen dieselbe Dienststeuerung, haben aber getrennte
Aufgaben:

| Programm | Verwaltet | Anbindung |
|---|---|---|
| Maschinenraum | KI-Dienste, etwa die des [Klangwerks](https://github.com/HalloWelt42/klangwerk) | meldet Fähigkeiten und Speicherbedarf an die [Leitstelle](https://github.com/HalloWelt42/leitstelle) |
| Stellwerk | eigene Webapps, nativ oder mit Docker | keine; die Webapps nutzen KI nur über die Leitstelle |

## Was der Kern bietet

| Modul | Inhalt |
|---|---|
| `vorlage` | `Vorlage` eines Dienstes (Befehl, Port oder Prüfung, Umgebung, Abhängigkeiten), Lesen von TOML-Dateien mit lesbaren Fehlern; unbekannte Felder sind ein Fehler |
| `dienst` | ein Dienst: starten in eigener Prozessgruppe und Umgebung, prüfen, anhalten (Signal oder eigener Befehl), messen, `Bericht` |
| `verwalter` | alle Dienste eines Programms: Runde, Neustartregeln, Sollzustand, Abhängigkeiten, Reihenfolge |
| `zustand` | Zustände, Sollzustand und Prozessgruppen in einer Datei, die Neustarts übersteht |
| `messen` | Prozesstabelle, Speicher eines Prozesses (fest: physischer Fußabdruck auf dem Mac, PSS auf Linux; dazu auf dem Mac aus demselben Aufruf die eingeblendeten Dateien im Arbeitsspeicher, die das System freigeben kann), lesbarer Programmname aus der Befehlszeile, Prozess hinter einem Port |
| `web` | Schutz der lokalen Schnittstelle, Auslieferung der Oberfläche, Protokollauszüge |

Zwei Arten der Steuerung:

- `prozess`: der Befehl ist der Dienst und läuft, bis er angehalten wird.
- `befehle`: der Befehl startet etwas und kehrt zurück, zum Beispiel einen Container; geprüft und angehalten wird
  dann mit eigenen Befehlen, nie per Signal.

## Nutzung

```python
from pathlib import Path

from dienstkern.verwalter import Verwalter
from dienstkern.vorlage import Vorlage, dateien_lesen

ordner = Path("~/Beispiel").expanduser()
verwalter = Verwalter(lambda: dateien_lesen(ordner / "dienste", Vorlage), ordner / "protokolle", ordner / "zustand.json",
                      herkunft="vom Beispiel")
# in einer asyncio-Schleife: await verwalter.schleife(); await verwalter.starten("probe")
```

Ein Programm erweitert `Vorlage` um eigene Felder (Unterklasse) oder baut die Vorlagen aus eigenen Dateien und gibt
sie über die `quelle` an den Verwalter.

## Installieren

Als Abhängigkeit eines Programms:

```bash
pip install "dienstkern @ https://github.com/HalloWelt42/dienstkern/archive/refs/heads/main.tar.gz"
```

## Entwickeln

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
```

```bash
.venv/bin/python -m pytest
```

Die Tests laufen gegen echte kleine Prozesse (ein HTTP-Server aus der Standardbibliothek spielt den Dienst).

---

## Lizenz

**Nicht-kommerzielle Nutzung** - Siehe [LICENSE](LICENSE)

Erlaubt: Private Nutzung, Installation, persönliche Anpassungen, Teilen mit Quellenangabe

Verboten: Kommerzielle Nutzung, Verkauf, Einbindung in kommerzielle Produkte

---

## Unterstützen

Dienstkern ist ein privates Hobby-Projekt. Kein Tracking, keine Werbung, keine Kompromisse.

Wenn dir das Projekt gefällt, kannst du ihm einen Stern geben - oder direkt hier:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/HalloWelt42)

**Crypto:**

| Coin | Adresse |
|------|---------|
| BTC | `bc1qnd599khdkv3v3npmj9ufxzf6h4fzanny2acwqr` |
| DOGE | `DL7tuiYCqm3xQjMDXChdxeQxqUGMACn1ZV` |
| ETH | `0x8A28fc47bFFFA03C8f685fa0836E2dBe1CA14F27` |

Copyright (c) 2026 HalloWelt42
