"""Was jede Verwaltungsoberfläche auf dem Kern braucht: Schutz der lokalen Schnittstelle, Auslieferung der gebauten
Oberfläche, Protokollauszüge und die eigene Protokolldatei.

Schutz gegen fremde Webseiten im selben Browser: nur bekannte Rechnernamen im Host-Kopf (gegen DNS-Rebinding), solange
die Oberfläche nur auf diesem Rechner lauscht, und schreibende Aufrufe nur mit einem eigenen Kopf (den ein fremder
Ursprung ohne Freigabe nicht setzen darf).
"""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

LOKALE_NAMEN = {"127.0.0.1", "localhost", "[::1]"}
LOKALE_ADRESSEN = {"127.0.0.1", "::1", "localhost"}
PROTOKOLL_LESEN_BYTES = 256 * 1024


class Protokollauszug(BaseModel):
    datei: str
    zeilen: list[str]


def protokoll_ende(datei: Path, zeilen: int) -> list[str]:
    """Die letzten Zeilen einer Protokolldatei (höchstens die letzten 256 KiB gelesen)."""
    zeilen = max(1, min(zeilen, 2000))
    try:
        with open(datei, "rb") as f:
            f.seek(0, 2)
            groesse = f.tell()
            f.seek(max(0, groesse - PROTOKOLL_LESEN_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return text.splitlines()[-zeilen:]


def schutz_einrichten(app: FastAPI, lauschadresse: Callable[[], str], kopf: str) -> None:
    """Host-Prüfung, solange nur auf diesem Rechner gelauscht wird; schreibende Aufrufe nur mit `kopf: 1`."""

    @app.middleware("http")
    async def schutz(request: Request, weiter):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if lauschadresse() in LOKALE_ADRESSEN and host not in LOKALE_NAMEN:
            return JSONResponse({"grund": "herkunft_nicht_erlaubt", "meldung": "Nur auf diesem Rechner erreichbar."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(kopf.lower()) != "1":
            return JSONResponse({"grund": "kopf_fehlt", "meldung": f"Schreibende Aufrufe brauchen den Kopf {kopf}: 1."}, status_code=403)
        return await weiter(request)


def oberflaeche_ausliefern(app: FastAPI, wurzel: Path) -> None:
    """Liefert die gebaute Oberfläche aus; unbekannte Pfade bekommen index.html. Zuletzt einrichten (fängt alles)."""
    if not (wurzel / "index.html").exists():
        @app.get("/", include_in_schema=False)
        async def ohne():
            return JSONResponse({"meldung": "Oberfläche nicht gebaut; Schnittstelle unter /api/stand"})
        return

    @app.get("/{pfad:path}", include_in_schema=False)
    async def seite(pfad: str):
        datei = (wurzel / pfad).resolve()
        if pfad and datei.is_file() and datei.is_relative_to(wurzel):
            kopf = {"Cache-Control": "public, max-age=604800, immutable"} if pfad.startswith("assets/") else {"Cache-Control": "no-cache"}
            return FileResponse(datei, headers=kopf)
        return FileResponse(wurzel / "index.html", headers={"Cache-Control": "no-cache"})


def protokoll_einrichten(datei: Path) -> None:
    """Ausgabe des Programms in eine rotierende Datei und auf die Konsole."""
    datei.parent.mkdir(parents=True, exist_ok=True)
    form = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ablage = logging.handlers.RotatingFileHandler(datei, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    konsole = logging.StreamHandler()
    for h in (ablage, konsole):
        h.setFormatter(form)
    wurzel = logging.getLogger()
    wurzel.setLevel(logging.INFO)
    wurzel.handlers = [ablage, konsole]
