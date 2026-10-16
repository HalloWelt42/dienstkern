"""Befehlsdienste (Start kehrt zurück, Prüfen und Anhalten per Befehl) und Abhängigkeiten, gegen echte Prozesse."""

from __future__ import annotations

import sys
from pathlib import Path

from dienstkern.vorlage import Vorlage, dateien_lesen
from dienstkern.zustand import Soll, Zustand

from hilfen import freier_port, offen, probe, verwalter

STARTEN = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1'], "
           "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); open('pid', 'w').write(str(p.pid))")
ANHALTEN = "import os, signal; os.kill(int(open('pid').read()), signal.SIGTERM)"


def befehlsdienst(tmp: Path, port: int, mehr: str = "") -> None:
    (tmp / "dienste").mkdir(parents=True, exist_ok=True)
    (tmp / "dienste" / "laufzeit.toml").write_text(
        f'name = "Laufzeit"\nordner = "{tmp}"\nsteuerung = "befehle"\nport = {port}\nspeicher_gb = 0.5\nstartzeit_s = 15\nanhaltezeit_s = 10\n'
        f'prozessmuster = "http.server {port}"\n'
        f'befehl = ["{sys.executable}", "-c", "{STARTEN.format(port=port)}"]\nanhaltebefehl = ["{sys.executable}", "-c", "{ANHALTEN}"]\n{mehr}')


async def test_befehlsdienst_starten_messen_anhalten(tmp_path: Path):
    port = freier_port()
    befehlsdienst(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    assert v.berichte()[0].zustand == Zustand.AUS
    e = await v.starten("laufzeit")
    assert e.erfolg, e.meldung
    assert offen(port)
    await v.runde()
    b = v.berichte()[0]
    assert b.zustand == Zustand.LAEUFT and b.gb_jetzt is not None and b.gb_jetzt > 0 and not b.fremd
    e = await v.anhalten("laufzeit")
    assert e.erfolg and e.meldung == "beendet" and not offen(port)
    await v.runde()
    assert v.berichte()[0].zustand == Zustand.AUS


async def test_abhaengigkeit_wird_zuerst_gestartet(tmp_path: Path):
    port_a, port_b = freier_port(), freier_port()
    befehlsdienst(tmp_path, port_a)
    probe(tmp_path, port_b, kennung="datenbank", braucht='["laufzeit"]')
    v = verwalter(tmp_path)
    await v.runde()
    e = await v.starten("datenbank")
    assert e.erfolg, e.meldung
    assert offen(port_a) and offen(port_b)
    assert v.stand.dienste["laufzeit"].soll == Soll.LAEUFT
    for k in reversed(v.reihenfolge(["datenbank", "laufzeit"])):
        await v.anhalten(k)
    assert not offen(port_a) and not offen(port_b)


async def test_reihenfolge_nach_abhaengigkeiten(tmp_path: Path):
    for k, braucht in (("oberflaeche", '["backend"]'), ("backend", '["datenbank"]'), ("datenbank", "[]"), ("einzeln", "[]")):
        probe(tmp_path, freier_port(), kennung=k, braucht=braucht)
    v = verwalter(tmp_path)
    await v.runde()
    assert v.reihenfolge(["oberflaeche", "einzeln", "backend", "datenbank"]) == ["datenbank", "backend", "oberflaeche", "einzeln"]
    assert v.reihenfolge(["oberflaeche"]) == ["oberflaeche"]          # nur die genannten


async def test_abhaengigkeit_im_kreis(tmp_path: Path):
    port = freier_port()
    befehlsdienst(tmp_path, port, mehr='braucht = ["laufzeit"]\n')
    v = verwalter(tmp_path)
    await v.runde()
    e = await v.starten("laufzeit")
    assert not e.erfolg and "Kreis" in e.meldung and not offen(port)
    assert v.reihenfolge(["laufzeit"]) == ["laufzeit"]


def test_befehlsdienst_braucht_anhaltebefehl_und_pruefung(tmp_path: Path):
    (tmp_path / "dienste").mkdir()
    (tmp_path / "dienste" / "halb.toml").write_text(f'name = "Halb"\nordner = "{tmp_path}"\nsteuerung = "befehle"\nbefehl = ["true"]\nport = 1\n')
    (tmp_path / "dienste" / "blind.toml").write_text(f'name = "Blind"\nordner = "{tmp_path}"\nsteuerung = "befehle"\nbefehl = ["true"]\nanhaltebefehl = ["true"]\n')
    vorlagen, fehler = dateien_lesen(tmp_path / "dienste", Vorlage)
    assert not vorlagen and sorted(f.datei for f in fehler) == ["blind.toml", "halb.toml"]
    meldungen = {f.datei: f.meldung for f in fehler}
    assert "anhaltebefehl" in meldungen["halb.toml"] and "port, pruefadresse oder pruefbefehl" in meldungen["blind.toml"]
