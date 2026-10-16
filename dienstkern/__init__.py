"""Dienstkern: starten, anhalten und beobachten von Diensten eines Rechners, mit Abhängigkeiten, Sollzustand,
begrenzten Neustarts, Protokoll und Prozessmessung. Gemeinsame Grundlage von Maschinenraum und Stellwerk.

Die Fassung steht in `version.json` im Wurzelordner; eine Installation legt sie als `VERSION` ins Paket.
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path


def fassung(paketordner: Path) -> str:
    """Fassung eines Pakets: `VERSION` im Paket (vom Installationsskript), `version.json` daneben (Quelle) oder die
    beim Bau eingetragene Fassung (installiert aus einem Archiv)."""
    datei = paketordner / "VERSION"
    if datei.is_file():
        return datei.read_text(encoding="utf-8").strip()
    quelle = paketordner.parent / "version.json"
    if quelle.is_file():
        return json.loads(quelle.read_text(encoding="utf-8"))["version"]
    try:
        return metadata.version(paketordner.name)
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = fassung(Path(__file__).resolve().parent)
