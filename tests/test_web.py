"""Schutz der lokalen Schnittstelle, Auslieferung der Oberfläche und Protokollauszüge."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dienstkern.web import oberflaeche_ausliefern, protokoll_ende, schutz_einrichten


def _app(adresse: str) -> TestClient:
    app = FastAPI()
    schutz_einrichten(app, lambda: adresse, "X-Probe")

    @app.get("/lesen")
    async def lesen():
        return {"ok": True}

    @app.post("/schreiben")
    async def schreiben():
        return {"ok": True}

    return TestClient(app, base_url="http://127.0.0.1")


def test_lokal_nur_bekannte_namen_und_schreiben_mit_kopf():
    c = _app("127.0.0.1")
    assert c.get("/lesen").status_code == 200
    assert c.get("/lesen", headers={"host": "boese.example:8540"}).json()["grund"] == "herkunft_nicht_erlaubt"
    assert c.post("/schreiben").json()["grund"] == "kopf_fehlt"
    assert c.post("/schreiben", headers={"X-Probe": "1"}).status_code == 200


def test_im_netz_jeder_name_aber_schreiben_mit_kopf():
    c = _app("0.0.0.0")
    assert c.get("/lesen", headers={"host": "rechner.lan"}).status_code == 200
    assert c.post("/schreiben", headers={"host": "rechner.lan"}).status_code == 403


SYMBOLE = {
    "favicon.ico": {"image/x-icon", "image/vnd.microsoft.icon"},
    "favicon.svg": {"image/svg+xml"},
    "apple-touch-icon.png": {"image/png"},
    "icon-192.png": {"image/png"},
    "site.webmanifest": {"application/manifest+json"},
}


@pytest.mark.parametrize("methode", ["GET", "HEAD"])
def test_oberflaeche_liefert_symbole_und_manifest_mit_typ(tmp_path: Path, methode: str):
    (tmp_path / "index.html").write_text("<!doctype html>")
    for name in SYMBOLE:
        (tmp_path / name).write_bytes(b"x")
    app = FastAPI()
    oberflaeche_ausliefern(app, tmp_path)
    c = TestClient(app)
    for name, typen in SYMBOLE.items():
        antwort = c.request(methode, f"/{name}")
        assert antwort.status_code == 200, name
        assert antwort.headers["content-type"].split(";")[0] in typen, name
    seite = c.request(methode, "/dienste/unbekannt")
    assert seite.status_code == 200 and seite.headers["content-type"].startswith("text/html")


def test_protokoll_ende(tmp_path: Path):
    datei = tmp_path / "x.log"
    assert protokoll_ende(datei, 10) == []
    datei.write_text("\n".join(f"zeile {i}" for i in range(50)))
    assert protokoll_ende(datei, 3) == ["zeile 47", "zeile 48", "zeile 49"]
