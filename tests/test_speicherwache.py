"""Speicherwache gegen echte Prozesse, die absichtlich Speicher belegen (tests/wachsend.py, höchstens 1,2 GB).

Gegenprobe an der Fassung davor (0.4.2): derselbe wachsende Dienst läuft dort ungebremst bis an seine eigene Obergrenze
weiter (tools/gegenprobe_speicher.py), hier wird er beendet und gesperrt.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from dienstkern.dienst import Dienst
from dienstkern.messen import Druckstufe, Systemspeicher
from dienstkern.speicherwache import Eingriff, Speicherschutz, Speicherwache, entscheiden, grenze_gb, knapp
from dienstkern.verwalter import Neustartregeln, Verwalter
from dienstkern.vorlage import Vorlage, dateien_lesen
from dienstkern.zustand import DienstStand, Soll, Zustand

from hilfen import freier_port, offen, probe

WACHSEND = Path(__file__).with_name("wachsend.py")
RUHIG = Systemspeicher(60.0, Druckstufe.NORMAL)


class MitWache(Verwalter):
    def __init__(self, tmp: Path, schutz: Speicherschutz) -> None:
        super().__init__(lambda: dateien_lesen(tmp / "dienste", Vorlage), tmp / "protokolle", tmp / "zustand.json",
                         Neustartregeln(pausen_s=[0.2]), takt_s=0.5, herkunft="vom Test")
        self._schutz = schutz

    @property
    def speicherschutz(self) -> Speicherschutz:
        return self._schutz


def wachsend(tmp: Path, port: int, kennung: str, schritt_mb: int, hoechstens_mb: int, speicher_gb: float, pause_s: float = 0.1) -> None:
    (tmp / "dienste").mkdir(parents=True, exist_ok=True)
    (tmp / "dienste" / f"{kennung}.toml").write_text("\n".join([
        f'name = "Wächst {kennung}"', f'ordner = "{tmp}"',
        f'befehl = ["{sys.executable}", "{WACHSEND}", "{port}", "{schritt_mb}", "{pause_s}", "{hoechstens_mb}"]',
        f"port = {port}", f"speicher_gb = {speicher_gb}", "startzeit_s = 15", "anhaltezeit_s = 5"]) + "\n")


async def bis(bedingung, frist_s: float) -> bool:
    ende = time.monotonic() + frist_s
    while time.monotonic() < ende:
        if bedingung():
            return True
        await asyncio.sleep(0.1)
    return bedingung()


def schutz(**mehr) -> Speicherschutz:
    return Speicherschutz(**{"takt_s": 0.2, "faktor": 1.5, "zuschlag_gb": 0.2, "frei_mindestens_prozent": 0, **mehr})


async def test_wachsender_dienst_wird_beendet_und_gesperrt(tmp_path: Path):
    port = freier_port()
    wachsend(tmp_path, port, "hoerer", schritt_mb=100, hoechstens_mb=1200, speicher_gb=0.2)   # Grenze 0,2 * 1,5 + 0,2 = 0,5 GB
    v = MitWache(tmp_path, schutz())
    v.wache = Speicherwache(v, system=lambda: RUHIG)
    gemeldet: list[tuple[Eingriff, bool]] = []

    async def weitersagen(e: Eingriff) -> None:
        gemeldet.append((e, offen(port)))                    # zu diesem Zeitpunkt läuft der Dienst noch

    v.vor_speicherabbruch = weitersagen
    schleife = asyncio.create_task(v.schleife())
    try:
        await v.runde()
        assert (await v.starten("hoerer")).erfolg
        assert await bis(lambda: not offen(port), 15), "der wachsende Dienst läuft noch"
        d = v.dienste["hoerer"]
        assert await bis(lambda: d.zustand == Zustand.GESTOERT, 5)
    finally:
        schleife.cancel()
        await v.beenden()
    assert len(gemeldet) == 1 and gemeldet[0][1], "vor dem Beenden weitergesagt"
    e = gemeldet[0][0]
    assert e.anlass == "grenze" and e.grenze_gb == 0.5 and 0.5 < e.gb < 1.0, e
    sperre = v.stand.dienste["hoerer"].sperre
    assert sperre is not None and sperre.grund.startswith("abgebrochen: Speicher") and sperre.anlass == "grenze"
    assert v.stand.dienste["hoerer"].soll == Soll.AUS
    b = d.bericht()
    assert b.sperre == sperre.grund and b.meldung.startswith("gesperrt: abgebrochen: Speicher")
    assert b.zustand == Zustand.GESTOERT and b.gb_jetzt is None
    assert "Speicherwache" in (tmp_path / "protokolle" / "hoerer.log").read_text()
    assert '"sperre"' in (tmp_path / "zustand.json").read_text()


async def test_nach_dem_abbruch_startet_nichts_von_selbst(tmp_path: Path):
    port = freier_port()
    wachsend(tmp_path, port, "hoerer", schritt_mb=100, hoechstens_mb=1200, speicher_gb=0.2)
    v = MitWache(tmp_path, schutz())
    v.wache = Speicherwache(v, system=lambda: RUHIG)
    await v.runde()
    assert (await v.starten("hoerer")).erfolg
    assert await bis(lambda: False, 1.0) is False                # wachsen lassen
    eingriffe = await v.wache.runde()
    assert [e.kennung for e in eingriffe] == ["hoerer"] and not offen(port)
    # Sollzustand, Absturzregeln: nichts startet ihn, auch nicht nach mehreren Runden
    for _ in range(4):
        await v.runde()
        await asyncio.sleep(0.3)
    assert not offen(port) and v.dienste["hoerer"].neustart_um is None
    # ein Befehl von außen (die Leitstelle nach geduldigem Warten) und einer von Hand: beide abgelehnt
    e = await v.starten("hoerer", von_hand=False)
    assert not e.erfolg and "gesperrt" in e.meldung
    e = await v.starten("hoerer")
    assert not e.erfolg and "Sperre von Hand aufgehoben" in e.meldung
    # ein Dienst, der ihn braucht, startet ihn auch nicht
    probe(tmp_path, freier_port(), kennung="nutzer", braucht='["hoerer"]')
    await v.runde()
    e = await v.starten("nutzer")
    assert not e.erfolg and "gesperrt" in e.meldung and not offen(port)
    await v.anhalten("nutzer")
    # Neustart des Programms (oder des Rechners): die Sperre steht im Stand
    neu = MitWache(tmp_path, schutz())
    await neu.hochfahren()
    await asyncio.sleep(0.5)
    assert not offen(port) and neu.stand.dienste["hoerer"].sperre is not None
    assert neu.dienste["hoerer"].bericht().sperre
    # erst die Hand hebt die Sperre auf; gestartet wird damit nichts
    probe(tmp_path, port, kennung="hoerer")                       # inzwischen behoben: ein ruhiger Dienst
    await neu.runde()
    assert neu.entsperren("hoerer").erfolg and not offen(port)
    assert neu.stand.dienste["hoerer"].sperre is None and neu.dienste["hoerer"].zustand == Zustand.AUS
    assert (await neu.starten("hoerer")).erfolg and offen(port)
    await neu.anhalten("hoerer")
    await neu.beenden()
    await v.beenden()


async def test_knapper_rechner_beendet_den_mit_dem_groessten_ueberschuss(tmp_path: Path):
    dick_port, duenn_port = freier_port(), freier_port()
    wachsend(tmp_path, dick_port, "dick", schritt_mb=400, hoechstens_mb=400, speicher_gb=0.05)   # Grenze 4,1 GB: nie erreicht
    probe(tmp_path, duenn_port, kennung="duenn")                                                 # angemeldet 0,5 GB, braucht weniger
    lage = {"jetzt": RUHIG}
    v = MitWache(tmp_path, schutz(zuschlag_gb=4.0, frei_mindestens_prozent=8, ueberschuss_mindestens_gb=0.2))
    v.wache = Speicherwache(v, system=lambda: lage["jetzt"])
    await v.runde()
    assert (await v.starten("dick")).erfolg and (await v.starten("duenn")).erfolg
    await asyncio.sleep(1.0)                                      # 400 MB sind belegt
    await v.runde()
    assert v.dienste["dick"].gb is not None and v.dienste["dick"].gb > 0.3
    assert await v.wache.runde() == []                            # der Rechner hat genug: niemand wird beendet
    lage["jetzt"] = Systemspeicher(5.0, Druckstufe.NORMAL)        # nachgestellt: nur noch 5 Prozent frei
    eingriffe = await v.wache.runde()
    assert [(e.kennung, e.anlass) for e in eingriffe] == [("dick", "knapp")]
    assert "nur noch 5 Prozent frei" in eingriffe[0].satz
    assert not offen(dick_port) and offen(duenn_port)             # wer im Rahmen bleibt, läuft weiter
    lage["jetzt"] = Systemspeicher(None, Druckstufe.KRITISCH)     # kritischer Druck, aber keiner mehr über seiner Anmeldung
    assert await v.wache.runde() == [] and offen(duenn_port)
    await v.anhalten("duenn")
    await v.beenden()


def test_entscheiden_und_grenze():
    s = Speicherschutz(faktor=2, zuschlag_gb=4, frei_mindestens_prozent=10, ueberschuss_mindestens_gb=1)

    def jetzt(lage: Systemspeicher) -> list[tuple[float, Systemspeicher]]:
        return [(0.0, lage)]

    moss, klein, fest = dienst("moss", 6), dienst("klein", 2), dienst("fest", 6, grenze=8)
    assert grenze_gb(moss.vorlage, s) == 16 and grenze_gb(fest.vorlage, s) == 8
    assert entscheiden([(moss, 15.9), (klein, 1.0)], jetzt(RUHIG), s) == []
    assert [e.kennung for e in entscheiden([(moss, 16.1), (fest, 8.5), (klein, 1.0)], jetzt(RUHIG), s)] == ["moss", "fest"]
    knapp_ = jetzt(Systemspeicher(9.0, Druckstufe.WARNUNG))
    assert [e.kennung for e in entscheiden([(moss, 7.5), (klein, 3.6)], knapp_, s)] == ["klein"]      # 1,6 GB über der Anmeldung
    assert entscheiden([(moss, 6.5), (klein, 2.5)], knapp_, s) == []                                    # keiner deutlich darüber
    assert entscheiden([(moss, 9.0)], jetzt(Systemspeicher(9.0, Druckstufe.NORMAL)), s.model_copy(update={"frei_mindestens_prozent": 0})) == []


def dienst(kennung: str, speicher_gb: float, grenze: float | None = None) -> Dienst:
    return Dienst(Vorlage(kennung=kennung, name=kennung, ordner=Path("/"), befehl=["x"], speicher_gb=speicher_gb, speicher_grenze_gb=grenze),
                  Path("/tmp"), DienstStand())


def lage(swap_gb: float, druck: Druckstufe = Druckstufe.NORMAL) -> Systemspeicher:
    return Systemspeicher(60.0, druck, swap_gb)


def test_auslagerung_kurz_und_wenig_wird_geduldet():
    """Nur nachgestellte Werte: echte Auslagerung erzeugt kein Test."""
    s = Speicherschutz(swap_hoechstens_gb=24, swap_geduldet_gb=4, swap_geduldet_s=300, swap_zuwachs_gb_je_min=4, swap_zuwachs_fenster_s=60)
    # 3 GB, langsam gewachsen, eine Stunde lang: unter dem Geduldeten
    assert knapp([(t, lage(min(3.0, t / 600), Druckstufe.WARNUNG)) for t in range(0, 3600, 10)], s) is None
    # 10 GB für zwei Minuten bei Druck: über dem Geduldeten, aber nicht lange genug, und langsam gewachsen
    verlauf = [(t, lage(2.0)) for t in range(0, 600, 10)] + [(t, lage(2.0 + (t - 600) / 60 * 3, Druckstufe.WARNUNG)) for t in range(600, 760, 10)]
    verlauf = [(t, l) for t, l in verlauf if l.swap_gb <= 10] + [(t, lage(10.0, Druckstufe.WARNUNG)) for t in range(760, 880, 10)]
    assert knapp(verlauf, s) is None
    # alte, ruhende Auslagerung ohne Druck zählt nicht als dauerhaft
    assert knapp([(t, lage(12.0)) for t in range(0, 3600, 10)], s) is None


def test_auslagerung_dauerhaft_schnell_oder_viel_ist_knapp():
    s = Speicherschutz(swap_hoechstens_gb=24, swap_geduldet_gb=4, swap_geduldet_s=300, swap_zuwachs_gb_je_min=4, swap_zuwachs_fenster_s=60)
    dauerhaft = [(t, lage(6.0, Druckstufe.WARNUNG)) for t in range(0, 400, 10)]
    assert "seit über 5 Minuten mehr als 4,0 GB ausgelagert" in knapp(dauerhaft, s)
    schnell = [(t, lage(1.0 + t / 60 * 6)) for t in range(0, 61, 1)]                 # 6 GB je Minute
    assert "wächst um 6,0 GB je Minute" in knapp(schnell, s)
    assert "25,0 GB ausgelagert, mehr als 24,0 GB" in knapp([(0.0, lage(25.0))], s)
    assert knapp([(0.0, lage(25.0))], s.model_copy(update={"swap_hoechstens_gb": 0})) is None        # abgeschaltet
    # entschieden wird dann wie bei knappem Rechner: der am weitesten über seiner Anmeldung
    moss, klein = dienst("moss", 6), dienst("klein", 2)
    e = entscheiden([(moss, 12.0), (klein, 2.1)], schnell, s)
    assert [(x.kennung, x.anlass) for x in e] == [("moss", "knapp")] and "Auslagerung wächst" in e[0].satz


async def test_wache_misst_den_verlauf_der_auslagerung(tmp_path: Path):
    """Die Wache führt den Verlauf selbst (nachgestellte Lage und Uhr) und greift beim schnellen Zuwachs ein."""
    port = freier_port()
    wachsend(tmp_path, port, "dick", schritt_mb=300, hoechstens_mb=300, speicher_gb=0.05)
    uhr = {"t": 0.0}
    werte = {"swap": 0.5}
    v = MitWache(tmp_path, schutz(zuschlag_gb=4.0, ueberschuss_mindestens_gb=0.1, swap_zuwachs_gb_je_min=4, swap_zuwachs_fenster_s=60))
    v.wache = Speicherwache(v, system=lambda: lage(werte["swap"]), uhr=lambda: uhr["t"])
    await v.runde()
    assert (await v.starten("dick")).erfolg
    await asyncio.sleep(1.0)
    for schritt in range(0, 31, 5):                          # 30 s lang 1 GB je Minute: geduldet
        uhr["t"], werte["swap"] = float(schritt), 0.5 + schritt / 60
        assert await v.wache.runde() == []
    for schritt in range(35, 66, 5):                         # dann 12 GB je Minute
        uhr["t"], werte["swap"] = float(schritt), 1.0 + (schritt - 30) / 60 * 12
        eingriffe = await v.wache.runde()
        if eingriffe:
            break
    assert [(e.kennung, e.anlass) for e in eingriffe] == [("dick", "knapp")] and "Auslagerung wächst" in eingriffe[0].satz
    assert not offen(port) and v.stand.dienste["dick"].sperre is not None
    await v.beenden()
