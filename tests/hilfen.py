"""Gemeinsame Hilfen der Tests: freie Ports, ein Ordner mit Vorlagen und ein Verwalter darauf."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

from dienstkern.verwalter import Neustartregeln, Verwalter
from dienstkern.vorlage import Vorlage, dateien_lesen


def freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def offen(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def probe(tmp: Path, port: int, kennung: str = "probe", **mehr: str) -> Path:
    """Ein HTTP-Server aus der Standardbibliothek spielt den Dienst."""
    (tmp / "dienste").mkdir(parents=True, exist_ok=True)
    zeilen = ['name = "Probe"', f'ordner = "{tmp}"', f'befehl = ["{sys.executable}", "-m", "http.server", "{port}", "--bind", "127.0.0.1"]',
              f"port = {port}", "speicher_gb = 0.5", "startzeit_s = 15", "anhaltezeit_s = 5", *(f"{k} = {v}" for k, v in mehr.items())]
    datei = tmp / "dienste" / f"{kennung}.toml"
    datei.write_text("\n".join(zeilen) + "\n")
    return datei


def verwalter(tmp: Path, regeln: Neustartregeln | None = None) -> Verwalter:
    return Verwalter(lambda: dateien_lesen(tmp / "dienste", Vorlage), tmp / "protokolle", tmp / "zustand.json",
                     regeln or Neustartregeln(pausen_s=[0.2]), takt_s=1, herkunft="vom Test")
