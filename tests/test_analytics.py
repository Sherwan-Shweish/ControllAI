"""Tests für src/analytics.py und src/seed_data.py – deterministische Kennzahlen.

Die synthetischen Daten enthalten bewusst eingebaute Befunde (siehe Docstring
von ``src/seed_data.py``). Die Tests prüfen, dass die Analysen genau diese
Befunde wiederfinden – und die Rechenlogik an kleinen Beispielen mit exakt
bekannten Ergebnissen.
"""

from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
import pandas as pd
import pytest

from src.agent import FactChecker
from src.analytics import (
    _alpha_out,
    _parse_sap_betrag,
    analysiere_prozess,
    deckungsbeitragsrechnung,
    erkenne_ausreisser,
    kennzahlen_bericht,
    lade_event_log,
    lade_tabelle,
    plan_ist_abweichung,
    umsatz_plan_ist,
    yoy_entwicklung,
)
from src.seed_data import erzeuge_alles

# =============================================================================
# Datenaufbereitung
# =============================================================================


@pytest.mark.parametrize(
    ("roh", "erwartet"),
    [("1.234,56", 1234.56), ("1.234,56-", -1234.56), ("12,000", 12.0), ("0,00", 0.0), (42, 42.0)],
)
def test_sap_betraege_werden_korrekt_gelesen(roh, erwartet: float) -> None:
    assert _parse_sap_betrag(roh) == pytest.approx(erwartet)


@pytest.mark.parametrize("roh", ["", "   ", "abc", None])
def test_ungueltige_sap_betraege_werden_nan(roh) -> None:
    assert math.isnan(_parse_sap_betrag(roh))


def test_alpha_konvertierung_entfernt_fuehrende_nullen() -> None:
    ergebnis = _alpha_out(pd.Series(["0000010001", "000000000000100101  ", "0000", ""])).tolist()
    assert ergebnis[:3] == ["10001", "100101", "0"]
    assert pd.isna(ergebnis[3])


def test_bereinigung_rekonstruiert_den_sauberen_datenbestand(demo_daten) -> None:
    """Rohexport = saubere Buchungen + Dubletten + Summenzeilen + Fremdwährung."""
    r = demo_daten.bereinigung
    assert r.duplikate_entfernt == 30
    assert r.unvollstaendig_entfernt == 8  # Zwischensummenzeilen ohne Beleg
    assert r.fremdwaehrung_entfernt == 3
    assert r.unbekannte_materialien == 0
    assert r.stornos == 8  # Stornos bleiben als Geschäftsvorfall erhalten
    assert r.zeilen_ausgang == demo_daten.anzahl_transaktionen
    assert r.zeilen_eingang - 30 - 8 - 3 == r.zeilen_ausgang


def test_datenbank_ist_konsistent(demo_daten) -> None:
    with sqlite3.connect(demo_daten.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        abweichend = conn.execute(
            "SELECT COUNT(*) FROM umsatz_transaktionen "
            "WHERE ABS(deckungsbeitrag_eur - (umsatz_eur - variable_kosten_eur)) > 0.01"
        ).fetchone()[0]
        assert abweichend == 0
        perioden = conn.execute("SELECT DISTINCT geschaeftsjahr || quartal FROM umsatz_transaktionen").fetchall()
        assert len(perioden) == 8


def test_bereinigung_meldet_fehlende_pflichtfelder() -> None:
    from src.analytics import bereinige_sap_export

    with pytest.raises(ValueError, match="NETWR"):
        bereinige_sap_export(pd.DataFrame(columns=["VBELN", "POSNR", "FKDAT", "KUNAG", "MATNR", "FKIMG"]))


# =============================================================================
# Ausreißer
# =============================================================================


def test_eingebaute_fehlbuchungen_werden_gefunden(demo_daten) -> None:
    tx = lade_tabelle(demo_daten.db_path, "umsatz_transaktionen")
    markiert = set(tx.loc[tx["ist_ausreisser"] == 1, "faktura_nr"])
    assert set(demo_daten.injizierte_ausreisser) <= markiert
    assert len(markiert - set(demo_daten.injizierte_ausreisser)) <= 2  # wenige Fehlalarme
    assert tx.loc[tx["ist_storno"] == 1, "ist_ausreisser"].eq(0).all()  # Stornos sind keine Ausreißer


@pytest.mark.parametrize("methode", ["mad", "iqr"])
def test_ausreisser_methoden_an_kleinem_beispiel(methode: str) -> None:
    rng = np.random.default_rng(0)
    werte = list(rng.lognormal(np.log(1_000), 0.2, size=50)) + [50_000, 5]
    df = pd.DataFrame({"betrag": werte})
    ergebnis = erkenne_ausreisser(df, "betrag", methode=methode)
    assert ergebnis["ist_ausreisser"].iloc[-2:].all()
    assert ergebnis.loc[ergebnis.index[-2], "ausreisser_grund"] == "ungewöhnlich hoher Betrag"
    assert ergebnis.loc[ergebnis.index[-1], "ausreisser_grund"] == "ungewöhnlich niedriger Betrag"
    assert len(ergebnis) == len(df)  # markieren, nicht löschen


def test_ausreisser_kleine_gruppen_werden_nicht_bewertet() -> None:
    df = pd.DataFrame({"betrag": [1, 2, 1_000_000], "g": ["a", "a", "a"]})
    assert not erkenne_ausreisser(df, "betrag", gruppen=["g"])["ist_ausreisser"].any()


# =============================================================================
# Controlling-Kennzahlen
# =============================================================================


@pytest.mark.parametrize(
    ("art", "plan", "ist", "pct", "bewertung", "ampel"),
    [
        ("umsatz", 100.0, 85.0, -15.0, "ungünstig", "rot"),
        ("umsatz", 100.0, 102.0, 2.0, "günstig", "grün"),
        ("umsatz", 100.0, 115.0, 15.0, "günstig", "gelb"),  # Planungsqualität prüfen
        ("kosten", 100.0, 107.0, 7.0, "ungünstig", "gelb"),
        ("kosten", 100.0, 125.0, 25.0, "ungünstig", "rot"),
        ("kosten", 100.0, 96.0, -4.0, "günstig", "grün"),
    ],
)
def test_plan_ist_abweichung_und_ampel(art, plan, ist, pct, bewertung, ampel) -> None:
    zeile = plan_ist_abweichung(pd.DataFrame({"plan_eur": [plan], "ist_eur": [ist]}), art=art).iloc[0]
    assert zeile["abweichung_eur"] == pytest.approx(ist - plan)
    assert zeile["abweichung_pct"] == pytest.approx(pct)
    assert (zeile["bewertung"], zeile["ampel"]) == (bewertung, ampel)


def test_plan_ist_bei_planwert_null() -> None:
    zeile = plan_ist_abweichung(pd.DataFrame({"plan_eur": [0.0], "ist_eur": [500.0]})).iloc[0]
    assert math.isnan(zeile["abweichung_pct"])


def test_deckungsbeitragsrechnung_rechnet_exakt() -> None:
    tx = pd.DataFrame(
        {
            "geschaeftsjahr": [2025] * 3,
            "quartal": ["Q1", "Q1", "Q2"],
            "produktkategorie": ["A", "A", "A"],
            "umsatz_eur": [1_000.0, 500.0, 2_000.0],
            "variable_kosten_eur": [600.0, 300.0, 1_000.0],
        }
    )
    kat = pd.DataFrame({"produktkategorie": ["A"], "fixkosten_quartal_eur": [200.0]})
    jahr = deckungsbeitragsrechnung(tx, kat, gruppen=["geschaeftsjahr"]).iloc[0]
    assert jahr["db_i_eur"] == 1_600.0  # 3.500 − 1.900
    assert jahr["fixkosten_eur"] == 400.0  # zwei Quartale à 200
    assert jahr["db_ii_eur"] == 1_200.0
    assert jahr["db_i_marge_pct"] == pytest.approx(45.71, abs=0.01)


def test_yoy_entwicklung() -> None:
    df = pd.DataFrame(
        {"geschaeftsjahr": [2024, 2025, 2025], "quartal": ["Q1", "Q1", "Q2"], "produktkategorie": ["A"] * 3,
         "umsatz": [100.0, 110.0, 50.0]}
    )
    out = yoy_entwicklung(df, "umsatz").set_index(["geschaeftsjahr", "quartal"])
    assert out.loc[(2025, "Q1"), "yoy_pct"] == pytest.approx(10.0)
    assert math.isnan(out.loc[(2025, "Q2"), "yoy_pct"])  # kein Vorjahreswert


def test_eingebaute_umsatzabweichung_wird_gefunden(demo_daten) -> None:
    tx = lade_tabelle(demo_daten.db_path, "umsatz_transaktionen")
    plan = lade_tabelle(demo_daten.db_path, "umsatzplanung")
    up = umsatz_plan_ist(tx[tx["ist_ausreisser"] == 0], plan)
    q3 = up[(up["geschaeftsjahr"] == 2025) & (up["quartal"] == "Q3")].sort_values("abweichung_pct")
    schlechteste = q3.iloc[0]
    assert schlechteste["produktkategorie"] == "Sensorik"
    assert schlechteste["ampel"] == "rot"

    # Python-Berechnung und SQL-View liefern identische Werte
    view = lade_tabelle(demo_daten.db_path, "v_plan_ist_umsatz")
    v = view[(view["geschaeftsjahr"] == 2025) & (view["quartal"] == "Q3") & (view["produktkategorie"] == "Sensorik")]
    assert v["abweichung_pct"].iloc[0] == pytest.approx(schlechteste["abweichung_pct"], abs=0.01)


def test_kennzahlenbericht(demo_daten) -> None:
    fakten = kennzahlen_bericht(demo_daten.db_path, 2025)
    assert json.loads(json.dumps(fakten)) == fakten  # JSON-fähig, keine NaN

    # Eingebaute Befunde
    top_kosten = fakten["groesste_kostenueberschreitungen"][0]
    assert (top_kosten["kostenstelle"], top_kosten["kostenart"]) == ("5000", "IT- und Softwarekosten")
    assert any(a["produktkategorie"] == "Sensorik" and a["ampel"] == "rot"
               for a in fakten["groesste_umsatzabweichungen"])
    assert fakten["markierte_ausreisser"]["anzahl"] >= 4

    # Innere Konsistenz der Kennzahlen
    assert fakten["betriebsergebnis_eur"] == pytest.approx(
        fakten["deckungsbeitrag_ii_eur"] - fakten["gemeinkosten_ist_eur"], abs=1
    )
    assert fakten["umsatzrentabilitaet_pct"] == pytest.approx(
        fakten["betriebsergebnis_eur"] / fakten["umsatz_eur"] * 100, abs=0.05
    )
    assert 0 < fakten["umsatzrentabilitaet_pct"] < 20


def test_kennzahlen_ueberstehen_die_faktenpruefung(demo_daten) -> None:
    """Ein korrekt formulierter Satz aus den Fakten löst keine Warnung aus."""
    f = kennzahlen_bericht(demo_daten.db_path, 2025)

    def de(x: float, stellen: int = 1) -> str:
        return f"{x:,.{stellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    satz = (
        f"Der Umsatz 2025 lag mit {de(f['umsatz_eur'] / 1e6)} Mio. € um {de(f['umsatz_yoy_pct'])} % über dem "
        f"Vorjahr; die Umsatzrentabilität beträgt {de(f['umsatzrentabilitaet_pct'])} %, das Betriebsergebnis "
        f"{de(f['betriebsergebnis_eur'], 0)} €."
    )
    assert FactChecker.unverified_numbers(satz, FactChecker.collect_numbers(f)) == []


def test_unbekanntes_geschaeftsjahr(demo_daten) -> None:
    with pytest.raises(ValueError, match="2019"):
        kennzahlen_bericht(demo_daten.db_path, 2019)


def test_tabellenzugriff_nur_ueber_allowlist(demo_daten) -> None:
    with pytest.raises(ValueError):
        lade_tabelle(demo_daten.db_path, "sqlite_master")


# =============================================================================
# Process Mining
# =============================================================================


@pytest.fixture(scope="module")
def p2p(demo_daten):
    return analysiere_prozess(lade_event_log(demo_daten.eventlog_path), prozessname="Purchase-to-Pay")


def test_engpass_rechnungsfreigabe_wird_erkannt(p2p) -> None:
    top = p2p.engpaesse(1).iloc[0]
    assert (top["von"], top["nach"]) == ("Rechnungsprüfung", "Rechnung freigeben")
    assert top["anteil_gesamtzeit_pct"] > 25
    assert top["abteilungswechsel_pct"] == 100  # Übergabe an die Bereichsleitung (Lane-Wechsel)


def test_nacharbeit_und_varianten(p2p) -> None:
    assert 0.12 < p2p.faelle["nacharbeit"].mean() < 0.28  # eingebaut: ca. 20 %
    assert p2p.varianten["faelle"].sum() == len(p2p.faelle) == 300
    assert p2p.varianten["anteil_pct"].sum() == pytest.approx(100, abs=0.1)
    wiederholt = p2p.aktivitaeten.set_index("aktivitaet")["wiederholungen"]
    assert wiederholt["Rechnungsprüfung"] > 0
    assert wiederholt["Rechnung freigeben"] == 0


def test_durchlaufzeiten_plausibel(p2p) -> None:
    dlz = p2p.faelle["durchlaufzeit_tage"]
    assert (dlz > 0).all()
    assert dlz.median() < dlz.quantile(0.9) <= dlz.max()


def test_prozessfakten_fuer_den_agenten(p2p) -> None:
    fakten = p2p.to_facts(top_n=3)
    assert json.loads(json.dumps(fakten)) == fakten
    assert fakten["anzahl_faelle"] == 300
    assert len(fakten["engpaesse"]) == 3
    assert fakten["engpaesse"][0]["uebergang"] == "Rechnungsprüfung → Rechnung freigeben"


def test_event_log_validierung() -> None:
    with pytest.raises(ValueError, match="zeitstempel"):
        lade_event_log(pd.DataFrame({"case_id": ["1"], "aktivitaet": ["A"]}))
    with pytest.raises(ValueError, match="leer"):
        analysiere_prozess(pd.DataFrame(columns=["case_id", "aktivitaet", "zeitstempel"]))


def test_minimales_event_log_exakt() -> None:
    log = lade_event_log(
        pd.DataFrame(
            {
                "case_id": ["1", "1", "1", "2", "2"],
                "aktivitaet": ["A", "B", "C", "A", "C"],
                "zeitstempel": ["2025-01-01 08:00", "2025-01-01 10:00", "2025-01-02 10:00",
                                "2025-01-01 08:00", "2025-01-01 12:00"],
            }
        )
    )
    analyse = analysiere_prozess(log)
    ue = analyse.uebergaenge.set_index(["von", "nach"])
    assert ue.loc[("B", "C"), "median_h"] == 24
    assert ue.loc[("A", "B"), "median_h"] == 2
    assert analyse.uebergaenge.iloc[0][["von", "nach"]].tolist() == ["B", "C"]  # größter Zeitanteil
    assert len(analyse.varianten) == 2
    assert analyse.faelle.set_index("case_id").loc["1", "durchlaufzeit_tage"] == pytest.approx(26 / 24, abs=0.01)


# =============================================================================
# Generator
# =============================================================================


def test_generator_ist_reproduzierbar(tmp_path, demo_daten) -> None:
    zweiter_lauf = erzeuge_alles(tmp_path, seed=42)
    for tabelle in ("umsatz_transaktionen", "plan_ist_finanzen", "umsatzplanung"):
        pd.testing.assert_frame_equal(
            lade_tabelle(demo_daten.db_path, tabelle), lade_tabelle(zweiter_lauf.db_path, tabelle)
        )
    assert zweiter_lauf.eventlog_path.read_text(encoding="utf-8") == demo_daten.eventlog_path.read_text(
        encoding="utf-8"
    )


def test_projekttexte_datei(demo_daten) -> None:
    daten = json.loads(demo_daten.projekttexte_path.read_text(encoding="utf-8"))
    assert len(daten["texte"]) == 12
    assert {t["id"] for t in daten["texte"]} == {f"T{i:02d}" for i in range(1, 13)}
