"""Dienstverwaltung gegen echte kleine Prozesse."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dienstkern.messen import rechenzeit
from dienstkern.verwalter import Neustartregeln
from dienstkern.vorlage import Vorlage, dateien_lesen
from dienstkern.zustand import Soll, Zustand

from hilfen import freier_port, offen, probe, verwalter


async def test_starten_messen_anhalten(tmp_path: Path):
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    assert v.berichte()[0].zustand == Zustand.AUS
    e = await v.starten("probe")
    assert e.erfolg, e.meldung
    assert offen(port)
    await v.runde()
    b = v.berichte()[0]
    assert b.zustand == Zustand.LAEUFT and b.gb_jetzt is not None and 0 < b.gb_jetzt < 2 and not b.fremd
    assert b.bedarf_gb >= max(b.gb_jetzt, 0.5)                    # mindestens die Schätzung der Vorlage
    assert (tmp_path / "protokolle" / "probe.log").read_text().count("gestartet vom Test") == 1
    assert v.stand.dienste["probe"].soll == Soll.LAEUFT and b.soll == Soll.LAEUFT
    e = await v.anhalten("probe")
    assert e.erfolg and e.meldung == "beendet"
    assert not offen(port)
    await v.runde()
    b = v.berichte()[0]
    assert b.zustand == Zustand.AUS and b.gb_jetzt is None and b.cpu_prozent is None and not b.aktiv
    assert b.bedarf_gb >= 0.5                                       # Bedarf bleibt nach dem Anhalten bekannt


async def test_unbekannter_dienst(tmp_path: Path):
    v = verwalter(tmp_path)
    (tmp_path / "dienste").mkdir()
    await v.runde()
    e = await v.starten("gibtsnicht")
    assert not e.erfolg and "unbekannt" in e.meldung


async def test_absturz_fuehrt_zu_neustart(tmp_path: Path):
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    assert (await v.starten("probe")).erfolg
    os.killpg(v.stand.dienste["probe"].gruppe, signal.SIGKILL)
    await asyncio.sleep(0.3)
    await v.runde()
    d = v.dienste["probe"]
    assert d.zustand == Zustand.GESTOERT and "Neustart" in d.meldung
    await asyncio.sleep(0.3)
    await v.runde()                       # fälliger Neustart wird angestoßen
    for _ in range(40):
        await asyncio.sleep(0.25)
        if d.zustand == Zustand.LAEUFT:
            break
    assert d.zustand == Zustand.LAEUFT and offen(port)
    await v.anhalten("probe")


async def test_zu_viele_abstuerze_dienst_bleibt_aus(tmp_path: Path):
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path, Neustartregeln(im_fenster=0))
    await v.runde()
    assert (await v.starten("probe")).erfolg
    os.killpg(v.stand.dienste["probe"].gruppe, signal.SIGKILL)
    await asyncio.sleep(0.3)
    await v.runde()
    d = v.dienste["probe"]
    assert d.zustand == Zustand.GESTOERT and d.neustart_um is None and v.stand.dienste["probe"].soll == Soll.AUS


async def test_fremd_gestarteter_dienst_wird_uebernommen_und_angehalten(tmp_path: Path):
    port = freier_port()
    fremd = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            if offen(port):
                break
            time.sleep(0.1)
        probe(tmp_path, port)
        v = verwalter(tmp_path)
        await v.runde()
        b = v.berichte()[0]
        assert b.zustand == Zustand.LAEUFT and b.fremd
        e = await v.starten("probe")
        assert e.erfolg and "bereits" in e.meldung
        assert (await v.anhalten("probe")).erfolg
        assert fremd.wait(timeout=5) is not None
    finally:
        if fremd.poll() is None:
            fremd.kill()


async def test_sollzustand_uebersteht_neustart_des_programms(tmp_path: Path):
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    assert (await v.starten("probe")).erfolg
    os.killpg(v.stand.dienste["probe"].gruppe, signal.SIGKILL)       # Dienst weg, Programm neu gestartet (neuer Verwalter)
    await asyncio.sleep(0.3)
    neu = verwalter(tmp_path)
    assert neu.stand.dienste["probe"].soll == Soll.LAEUFT
    await neu.hochfahren()
    for _ in range(40):
        await asyncio.sleep(0.25)
        if offen(port) and neu.dienste["probe"].zustand == Zustand.LAEUFT:
            break
    assert neu.dienste["probe"].zustand == Zustand.LAEUFT
    await neu.anhalten("probe")


async def test_laufende_eigene_gruppe_wird_wiedererkannt(tmp_path: Path):
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    assert (await v.starten("probe")).erfolg
    neu = verwalter(tmp_path)
    await neu.runde()
    b = neu.berichte()[0]
    assert b.zustand == Zustand.LAEUFT and not b.fremd
    assert (await neu.anhalten("probe")).erfolg
    assert not offen(port)
    v.dienste["probe"].prozess.wait(timeout=5)


async def test_startzeit_ueberschritten_beendet_den_versuch(tmp_path: Path):
    port = freier_port()
    datei = probe(tmp_path, port)
    datei.write_text(datei.read_text().replace(f'"-m", "http.server", "{port}", "--bind", "127.0.0.1"', '"-c", "import time; time.sleep(60)"')
                     .replace("startzeit_s = 15", "startzeit_s = 1"))
    v = verwalter(tmp_path)
    await v.runde()
    e = await v.starten("probe")
    assert not e.erfolg and "nicht erreichbar" in e.meldung
    assert v.stand.dienste["probe"].gruppe is None


async def test_umgebung_ist_abgegrenzt(tmp_path: Path):
    datei = probe(tmp_path, freier_port())
    (tmp_path / "geheim.env").write_text("# Kommentar\nAUS_DATEI='ja'\n")
    datei.write_text(datei.read_text() + f'umgebung_datei = "{tmp_path}/geheim.env"\n[umgebung]\nEIGENES = "1"\n')
    os.environ["NICHT_WEITERGEBEN"] = "x"
    v = verwalter(tmp_path)
    await v.runde()
    env = v.dienste["probe"]._umgebung()
    assert env["AUS_DATEI"] == "ja" and env["EIGENES"] == "1" and "NICHT_WEITERGEBEN" not in env


def test_variablen_kommen_aus_der_dienstumgebung(tmp_path: Path):
    probe(tmp_path, freier_port())
    vorlage = dateien_lesen(tmp_path / "dienste", Vorlage)[0][0].model_copy(update={"befehl": ["$PROG", "--pfad", "${HOME}/x", "$UNBEKANNT"]})
    assert vorlage.programm({"PROG": "/opt/py", "HOME": "/h"}) == ["/opt/py", "--pfad", "/h/x", "$UNBEKANNT"]
    assert vorlage.model_copy(update={"befehl": ["bin/start"]}).programm({})[0] == str(tmp_path / "bin/start")


def test_fehlerhafte_und_unbekannte_felder_werden_gemeldet(tmp_path: Path):
    probe(tmp_path, freier_port())
    probe(tmp_path, freier_port(), kennung="tippfehler", startzeitt_s="3")
    (tmp_path / "dienste" / "Kaputt.toml").write_text('name = "x"\n')
    vorlagen, fehler = dateien_lesen(tmp_path / "dienste", Vorlage)
    assert [v.kennung for v in vorlagen] == ["probe"]
    meldungen = {f.datei: f.meldung for f in fehler}
    assert set(meldungen) == {"Kaputt.toml", "tippfehler.toml"}
    assert meldungen["tippfehler.toml"] == "startzeitt_s: unbekanntes Feld"


BESCHAEFTIGT = ("import socket, threading, time\n"
                "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', {port})); s.listen()\n"
                "threading.Thread(target=lambda: [None for _ in iter(int, 1)], daemon=True).start()\n"
                "time.sleep(120)\n")


async def test_arbeit_wird_an_der_prozessorlast_erkannt(tmp_path: Path):
    port = freier_port()
    datei = probe(tmp_path, port)
    (tmp_path / "last.py").write_text(BESCHAEFTIGT.format(port=port))
    datei.write_text(datei.read_text().replace(f'"-m", "http.server", "{port}", "--bind", "127.0.0.1"', f'"{tmp_path}/last.py"'))
    v = verwalter(tmp_path)
    await v.runde()
    assert (await v.starten("probe")).erfolg
    await v.runde()
    await asyncio.sleep(1.0)
    await v.runde()
    d = v.dienste["probe"]
    assert d.cpu_prozent is not None and d.cpu_prozent > 50 and d.bericht().aktiv
    await v.anhalten("probe")
    assert not d.bericht().aktiv


def test_rechenzeit_aus_ps():
    assert rechenzeit("0:01.50") == 1.5
    assert rechenzeit("12:03.00") == 723.0
    assert rechenzeit("1:00:00") == 3600.0
    assert rechenzeit("2-01:00:00") == 2 * 86400 + 3600.0


def test_bedarf_aus_arbeitsspitzen_ein_ausreisser_faellt_heraus(tmp_path: Path):
    """Je Arbeitsphase zählt ihre Speicherspitze; der Bedarf ist der 80-%-Wert der jüngsten 30, mindestens die Schätzung.
    Ein einziger Ausreißer (13,7 GB bei sonst 4,7) treibt ihn nicht mehr hoch; bleibt er die Regel, steigt er."""
    from dienstkern.dienst import ARBEITSSPITZEN, Dienst, anteilswert
    from dienstkern.vorlage import Vorlage
    from dienstkern.zustand import DienstStand

    assert anteilswert([], 0.8) == 0.0 and anteilswert([3.0], 0.8) == 3.0 and anteilswert([1, 2, 3, 4, 5], 0.8) == 4
    d = Dienst(Vorlage(kennung="klon", name="Klon", ordner=tmp_path, befehl=["true"], speicher_gb=1.0), tmp_path, DienstStand())

    def phase(*werte):
        for w in werte:
            d.gb, d.cpu_prozent = w, 80.0
            d.arbeit_messen()
        d.gb, d.cpu_prozent = 4.0, 0.0
        d.arbeit_messen()
    phase(13.7)
    for _ in range(9):
        phase(4.0, 4.7)
    assert d.stand.arbeitsspitzen.count(13.7) == 1 and d.bedarf_gb == 4.7
    for _ in range(ARBEITSSPITZEN):
        phase(9.5)
    assert len(d.stand.arbeitsspitzen) == ARBEITSSPITZEN and d.bedarf_gb == 9.5 and d.bericht().spitze_gb == 9.5
    d.gb, d.cpu_prozent = 11.0, 80.0                                 # eine laufende Phase zählt schon mit
    d.arbeit_messen()
    assert d.bedarf_gb == 11.0


async def test_start_von_aussen_loescht_keine_abstuerze(tmp_path: Path):
    """Bleibt ein Dienst nach zu vielen Abstürzen aus, startet ihn ein Befehl von außen (Leitstelle) nicht; von Hand schon."""
    port = freier_port()
    probe(tmp_path, port)
    v = verwalter(tmp_path)
    await v.runde()
    d = v.dienste["probe"]
    d.abstuerze.extend([0.0] * (v.regeln.im_fenster + 1))
    e = await v.starten("probe", von_hand=False)
    assert not e.erfolg and "bleibt nach" in e.meldung and not offen(port)
    assert (await v.starten("probe")).erfolg and not d.abstuerze
    await v.anhalten("probe")
