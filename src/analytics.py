"""
Analytics – deterministische Controlling- und Process-Mining-Berechnungen.

Dieses Modul rechnet; das Sprachmodell in ``agent.py`` formuliert nur. Alle
Kennzahlen entstehen hier reproduzierbar mit pandas und werden als kompakte,
JSON-fähige Faktenbasis an den Agenten übergeben. Dessen Faktenprüfung
gleicht jede Zahl im generierten Text mit genau diesen Fakten ab.

Inhalt:
    Datenaufbereitung   bereinige_sap_export, erkenne_ausreisser
    Controlling         plan_ist_abweichung, umsatz_plan_ist,
                        deckungsbeitragsrechnung, yoy_entwicklung,
                        kennzahlen_bericht
    Process Mining      lade_event_log, analysiere_prozess (→ ProzessAnalyse)

Autor: Sherwan Shweish
Lizenz: MIT
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

__all__ = [
    "BereinigungsReport",
    "ProzessAnalyse",
    "analysiere_prozess",
    "bereinige_sap_export",
    "deckungsbeitragsrechnung",
    "erkenne_ausreisser",
    "kennzahlen_bericht",
    "lade_event_log",
    "lade_tabelle",
    "plan_ist_abweichung",
    "umsatz_plan_ist",
    "yoy_entwicklung",
]

logger = logging.getLogger("controllai.analytics")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUARTALE = ("Q1", "Q2", "Q3", "Q4")
PERIODE = ["geschaeftsjahr", "quartal"]

ERLAUBTE_TABELLEN = frozenset(
    {
        "kostenstellen",
        "produktkategorien",
        "materialien",
        "kunden",
        "umsatz_transaktionen",
        "umsatzplanung",
        "plan_ist_finanzen",
        "v_plan_ist_umsatz",
    }
)


# =============================================================================
# Datenzugriff
# =============================================================================


def lade_tabelle(db_path: str | Path, tabelle: str) -> pd.DataFrame:
    """Liest eine Tabelle schreibgeschützt aus der Controlling-Datenbank.

    Nur Tabellen aus ``ERLAUBTE_TABELLEN`` sind zulässig – der Tabellenname
    wird nie ungeprüft in SQL eingesetzt.
    """
    if tabelle not in ERLAUBTE_TABELLEN:
        raise ValueError(f"Unbekannte Tabelle: {tabelle}")
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Datenbank nicht gefunden: {path} – zuerst 'python -m src.seed_data' ausführen.")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return pd.read_sql_query(f'SELECT * FROM "{tabelle}"', conn)


# =============================================================================
# Datenaufbereitung: SAP-Export bereinigen
# =============================================================================

# Feldnamen angelehnt an die SAP-Fakturatabellen VBRK (Kopf) und VBRP (Position)
SAP_FELDER: dict[str, str] = {
    "VBELN": "faktura_nr",
    "POSNR": "position",
    "FKDAT": "buchungsdatum",
    "KUNAG": "kunden_nr",
    "MATNR": "material_nr",
    "FKIMG": "menge",
    "NETWR": "umsatz_eur",
    "WAVWR": "variable_kosten_eur",
    "WAERK": "waehrung",
}


class BereinigungsReport(BaseModel):
    """Protokoll der Datenbereinigung – macht jeden Eingriff nachvollziehbar."""

    zeilen_eingang: int
    duplikate_entfernt: int = 0
    unvollstaendig_entfernt: int = 0
    fremdwaehrung_entfernt: int = 0
    unbekannte_materialien: int = 0
    stornos: int = 0
    zeilen_ausgang: int = 0
    hinweise: list[str] = Field(default_factory=list)


def _parse_sap_betrag(wert: Any) -> float:
    """Wandelt SAP-Listbeträge in float: '1.234,56' → 1234.56, '1.234,56-' → -1234.56."""
    if wert is None:
        return math.nan
    if isinstance(wert, (int, float, np.integer, np.floating)):
        return float(wert)
    text = str(wert).strip().replace(" ", "").replace(" ", "")
    if not text or text.lower() == "nan":
        return math.nan
    negativ = text.endswith("-") or text.startswith("-")
    text = text.strip("-").replace(".", "").replace(",", ".")
    try:
        zahl = float(text)
    except ValueError:
        return math.nan
    return -zahl if negativ else zahl


def _alpha_out(serie: pd.Series) -> pd.Series:
    """SAP-ALPHA-Konvertierung rückwärts: führende Nullen entfernen ('0000010001' → '10001')."""
    text = serie.astype("string").str.strip()
    leer = text.isna() | (text == "")
    bereinigt = text.str.lstrip("0")
    bereinigt = bereinigt.mask((bereinigt == "") & ~leer, "0")
    return bereinigt.mask(leer, pd.NA)


def bereinige_sap_export(
    roh: pd.DataFrame,
    materialien: pd.DataFrame | None = None,
    kunden: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, BereinigungsReport]:
    """Bereinigt einen Faktura-Export im SAP-Listformat.

    Schritte: Feldnamen übersetzen, ALPHA-Konvertierung, deutsches Datums- und
    Zahlenformat inkl. nachgestelltem Minus, unvollständige Zeilen und
    Dubletten entfernen, Fremdwährung aussondern, Stammdaten anreichern,
    Periodenfelder und Deckungsbeitrag ableiten. Stornos bleiben erhalten.

    Args:
        roh: Export mit SAP-Feldnamen (alle Spalten als Text eingelesen).
        materialien: Materialstamm mit ``material_nr`` und ``produktkategorie``.
        kunden: Kundenstamm mit ``kunden_nr`` und ``region``.

    Returns:
        Bereinigte Transaktionen und ein Bereinigungsprotokoll.
    """
    fehlend = set(SAP_FELDER) - set(roh.columns)
    if fehlend:
        raise ValueError(f"Pflichtfelder fehlen im Export: {', '.join(sorted(fehlend))}")

    report = BereinigungsReport(zeilen_eingang=len(roh))
    df = roh.rename(columns=SAP_FELDER)[list(SAP_FELDER.values())].copy()

    df["faktura_nr"] = _alpha_out(df["faktura_nr"])
    df["position"] = pd.to_numeric(df["position"], errors="coerce").astype("Int64")
    df["kunden_nr"] = _alpha_out(df["kunden_nr"])
    df["material_nr"] = _alpha_out(df["material_nr"])
    df["waehrung"] = df["waehrung"].astype("string").str.strip().str.upper()
    df["buchungsdatum"] = pd.to_datetime(df["buchungsdatum"].astype("string").str.strip(), format="%d.%m.%Y",
                                         errors="coerce")
    for spalte in ("menge", "umsatz_eur", "variable_kosten_eur"):
        df[spalte] = df[spalte].map(_parse_sap_betrag).astype(float)

    pflicht = ["faktura_nr", "position", "buchungsdatum", "material_nr", "umsatz_eur"]
    unvollstaendig = df[pflicht].isna().any(axis=1)
    report.unvollstaendig_entfernt = int(unvollstaendig.sum())
    df = df[~unvollstaendig]

    vor = len(df)
    df = df.drop_duplicates(subset=["faktura_nr", "position"], keep="first")
    report.duplikate_entfernt = vor - len(df)

    fremd = df["waehrung"] != "EUR"
    report.fremdwaehrung_entfernt = int(fremd.sum())
    if report.fremdwaehrung_entfernt:
        report.hinweise.append("Zeilen in Fremdwährung ausgesondert – Umrechnung erforderlich.")
    df = df[~fremd]

    df["variable_kosten_eur"] = df["variable_kosten_eur"].fillna(0.0)
    df["deckungsbeitrag_eur"] = (df["umsatz_eur"] - df["variable_kosten_eur"]).round(2)
    df["geschaeftsjahr"] = df["buchungsdatum"].dt.year.astype(int)
    df["quartal"] = "Q" + df["buchungsdatum"].dt.quarter.astype(str)
    df["ist_storno"] = df["umsatz_eur"] < 0
    report.stornos = int(df["ist_storno"].sum())

    if materialien is not None:
        df = df.merge(materialien[["material_nr", "produktkategorie"]], on="material_nr", how="left")
        report.unbekannte_materialien = int(df["produktkategorie"].isna().sum())
        df["produktkategorie"] = df["produktkategorie"].fillna("Nicht zugeordnet")
    if kunden is not None:
        df = df.merge(kunden[["kunden_nr", "region"]], on="kunden_nr", how="left")
        df["region"] = df["region"].fillna("Unbekannt")
    if report.unbekannte_materialien:
        report.hinweise.append(f"{report.unbekannte_materialien} Zeilen mit unbekanntem Material.")

    df["buchungsdatum"] = df["buchungsdatum"].dt.strftime("%Y-%m-%d")
    df = df.sort_values(["buchungsdatum", "faktura_nr", "position"]).reset_index(drop=True)
    report.zeilen_ausgang = len(df)
    logger.info("Bereinigung: %s", report.model_dump())
    return df, report


def erkenne_ausreisser(
    df: pd.DataFrame,
    wert_spalte: str,
    *,
    gruppen: Sequence[str] | None = None,
    methode: Literal["mad", "iqr"] = "mad",
    schwelle: float | None = None,
    log_transformation: bool = True,
) -> pd.DataFrame:
    """Markiert Ausreißer, ohne Zeilen zu löschen.

    Methoden:
        ``mad``: robuster z-Wert nach Iglewicz & Hoaglin,
                 z = 0,6745 · (x − Median) / MAD, Standardschwelle 3,5
        ``iqr``: Tukey-Zäune Q1 − k·IQR und Q3 + k·IQR, Standard k = 1,5

    Umsätze sind rechtsschief verteilt; deshalb wird standardmäßig auf dem
    Logarithmus gerechnet. Nicht-positive Werte (z. B. Stornos) werden nicht
    bewertet – sie sind Geschäftsvorfälle, keine Messfehler.

    Returns:
        Kopie mit den Spalten ``ist_ausreisser``, ``ausreisser_score`` und
        ``ausreisser_grund``.
    """
    if wert_spalte not in df.columns:
        raise KeyError(wert_spalte)
    out = df.copy()
    werte = out[wert_spalte].astype(float)
    bewertbar = werte > 0 if log_transformation else werte.notna()
    basis = pd.Series(np.nan, index=out.index)
    basis[bewertbar] = np.log(werte[bewertbar]) if log_transformation else werte[bewertbar]

    schluessel = (
        out[list(gruppen)].astype(str).agg(" | ".join, axis=1) if gruppen else pd.Series("alle", index=out.index)
    )
    score = pd.Series(np.nan, index=out.index)
    grund = pd.Series(pd.NA, index=out.index, dtype="string")

    for _, idx in basis.groupby(schluessel).groups.items():
        x = basis.loc[idx].dropna()
        if len(x) < 8:  # zu wenige Werte für eine belastbare Aussage
            continue
        if methode == "mad":
            median = x.median()
            mad = (x - median).abs().median()
            if mad == 0:
                continue
            z = 0.6745 * (x - median) / mad
            score.loc[z.index] = z
            grenze = schwelle or 3.5
            hoch, niedrig = z[z > grenze].index, z[z < -grenze].index
        else:
            q1, q3 = x.quantile(0.25), x.quantile(0.75)
            iqr = q3 - q1
            k = schwelle or 1.5
            score.loc[x.index] = (x - x.median()) / (iqr or 1)
            hoch, niedrig = x[x > q3 + k * iqr].index, x[x < q1 - k * iqr].index
        grund.loc[hoch] = "ungewöhnlich hoher Betrag"
        grund.loc[niedrig] = "ungewöhnlich niedriger Betrag"

    out["ausreisser_score"] = score.round(2)
    out["ausreisser_grund"] = grund
    out["ist_ausreisser"] = grund.notna()
    logger.info("Ausreißer (%s): %d von %d Zeilen markiert", methode, int(out["ist_ausreisser"].sum()), len(out))
    return out


# =============================================================================
# Controlling-Kennzahlen
# =============================================================================


def plan_ist_abweichung(
    df: pd.DataFrame,
    *,
    plan_spalte: str = "plan_eur",
    ist_spalte: str = "ist_eur",
    art: Literal["umsatz", "kosten"] = "kosten",
    toleranz_pct: float = 5.0,
    warnschwelle_pct: float = 10.0,
) -> pd.DataFrame:
    """Berechnet Plan-Ist-Abweichungen mit Bewertung und Ampel.

    Abweichung = Ist − Plan; relative Abweichung = (Ist − Plan) / |Plan| · 100.
    Bei Umsätzen ist eine positive Abweichung günstig, bei Kosten ungünstig.

    Ampellogik:
        rot   – ungünstig und über der Warnschwelle
        gelb  – ungünstig über der Toleranz oder günstig über der Warnschwelle
                (dann ist die Planungsqualität zu hinterfragen)
        grün  – sonst
    """
    out = df.copy()
    plan = out[plan_spalte].astype(float)
    ist = out[ist_spalte].astype(float)
    out["abweichung_eur"] = (ist - plan).round(2)
    out["abweichung_pct"] = ((ist - plan) / plan.abs().replace(0, np.nan) * 100).round(2)

    guenstig = out["abweichung_eur"] >= 0 if art == "umsatz" else out["abweichung_eur"] <= 0
    betrag = out["abweichung_pct"].abs()
    out["bewertung"] = np.where(guenstig, "günstig", "ungünstig")
    out["ampel"] = np.select(
        [
            ~guenstig & (betrag > warnschwelle_pct),
            ~guenstig & (betrag > toleranz_pct),
            guenstig & (betrag > warnschwelle_pct),
        ],
        ["rot", "gelb", "gelb"],
        default="grün",
    )
    return out


def umsatz_plan_ist(
    transaktionen: pd.DataFrame,
    planung: pd.DataFrame,
    gruppen: Sequence[str] = ("geschaeftsjahr", "quartal", "produktkategorie"),
) -> pd.DataFrame:
    """Aggregiert Ist-Umsätze und stellt sie der Umsatzplanung gegenüber."""
    gruppen = list(gruppen)
    ist = transaktionen.groupby(gruppen, as_index=False)["umsatz_eur"].sum().rename(
        columns={"umsatz_eur": "ist_umsatz_eur"}
    )
    plan = planung.groupby(gruppen, as_index=False)["plan_umsatz_eur"].sum()
    merged = plan.merge(ist, on=gruppen, how="outer").fillna({"plan_umsatz_eur": 0.0, "ist_umsatz_eur": 0.0})
    return plan_ist_abweichung(merged, plan_spalte="plan_umsatz_eur", ist_spalte="ist_umsatz_eur", art="umsatz")


def deckungsbeitragsrechnung(
    transaktionen: pd.DataFrame,
    produktkategorien: pd.DataFrame | None = None,
    gruppen: Sequence[str] = ("geschaeftsjahr", "quartal", "produktkategorie"),
) -> pd.DataFrame:
    """Mehrstufige Deckungsbeitragsrechnung.

    DB I  = Umsatz − variable Kosten
    DB II = DB I − Produktfixkosten (aus ``produktkategorien.fixkosten_quartal_eur``)

    Gerechnet wird zunächst je Geschäftsjahr, Quartal und Produktkategorie, damit
    die quartalsbezogenen Fixkosten korrekt zugeordnet werden; danach wird auf
    die gewünschte Gruppierung verdichtet.
    """
    basis_keys = ["geschaeftsjahr", "quartal", "produktkategorie"]
    gruppen = list(gruppen)
    if not set(gruppen) <= set(basis_keys):
        raise ValueError(f"Gruppierung nur über {basis_keys} möglich.")

    basis = transaktionen.groupby(basis_keys, as_index=False)[["umsatz_eur", "variable_kosten_eur"]].sum()
    basis["db_i_eur"] = basis["umsatz_eur"] - basis["variable_kosten_eur"]
    if produktkategorien is not None:
        basis = basis.merge(produktkategorien[["produktkategorie", "fixkosten_quartal_eur"]],
                            on="produktkategorie", how="left")
        basis["fixkosten_eur"] = basis["fixkosten_quartal_eur"].fillna(0.0)
        basis = basis.drop(columns="fixkosten_quartal_eur")
    else:
        basis["fixkosten_eur"] = 0.0
    basis["db_ii_eur"] = basis["db_i_eur"] - basis["fixkosten_eur"]

    werte = ["umsatz_eur", "variable_kosten_eur", "db_i_eur", "fixkosten_eur", "db_ii_eur"]
    out = basis.groupby(gruppen, as_index=False)[werte].sum() if gruppen else basis[werte].sum().to_frame().T
    umsatz = out["umsatz_eur"].replace(0, np.nan)
    out["db_i_marge_pct"] = (out["db_i_eur"] / umsatz * 100).round(2)
    out["db_ii_marge_pct"] = (out["db_ii_eur"] / umsatz * 100).round(2)
    out[werte] = out[werte].round(2)
    return out


def yoy_entwicklung(
    df: pd.DataFrame,
    wert_spalte: str,
    *,
    schluessel: Sequence[str] = ("produktkategorie",),
    mit_quartal: bool = True,
) -> pd.DataFrame:
    """Vergleicht jeden Wert mit derselben Periode des Vorjahres (Year over Year).

    Returns:
        Kopie mit ``vorjahr_wert``, ``yoy_abweichung`` und ``yoy_pct``
        (NaN, wenn kein Vorjahreswert existiert).
    """
    keys = list(schluessel) + (["quartal"] if mit_quartal else [])
    out = df.copy()
    vorjahr = df[keys + ["geschaeftsjahr", wert_spalte]].copy()
    vorjahr["geschaeftsjahr"] = vorjahr["geschaeftsjahr"] + 1
    vorjahr = vorjahr.rename(columns={wert_spalte: "vorjahr_wert"})
    out = out.merge(vorjahr, on=keys + ["geschaeftsjahr"], how="left")
    out["yoy_abweichung"] = (out[wert_spalte] - out["vorjahr_wert"]).round(2)
    out["yoy_pct"] = (out["yoy_abweichung"] / out["vorjahr_wert"].abs().replace(0, np.nan) * 100).round(2)
    return out


def _r(wert: float, stellen: int = 1) -> float | None:
    """Rundet und liefert JSON-taugliche Python-Floats (NaN → None)."""
    if wert is None or (isinstance(wert, float) and math.isnan(wert)):
        return None
    return round(float(wert), stellen)


def kennzahlen_bericht(
    db_path: str | Path,
    geschaeftsjahr: int = 2025,
    top_n: int = 3,
    *,
    ohne_ausreisser: bool = True,
) -> dict[str, Any]:
    """Verdichtet die Controlling-Kennzahlen eines Geschäftsjahres zur Faktenbasis.

    Das Ergebnis ist die Eingabe für ``ControllAIAgent.generate_executive_summary``.
    Prozentwerte werden als Prozentzahl (z. B. 8.4), Beträge in Euro geliefert.

    Zur Prüfung markierte Ausreißer (Verdacht auf Fehlbuchung) werden standardmäßig
    bis zur Klärung aus der Steuerungssicht herausgenommen, aber separat
    ausgewiesen – so verzerrt eine einzelne Fehlbuchung keine Quartalsabweichung.
    """
    tx_alle = lade_tabelle(db_path, "umsatz_transaktionen")
    tx = tx_alle[tx_alle["ist_ausreisser"] == 0] if ohne_ausreisser else tx_alle
    plan = lade_tabelle(db_path, "umsatzplanung")
    kategorien = lade_tabelle(db_path, "produktkategorien")
    kosten = lade_tabelle(db_path, "plan_ist_finanzen")

    if geschaeftsjahr not in set(tx_alle["geschaeftsjahr"]):
        raise ValueError(f"Keine Daten für das Geschäftsjahr {geschaeftsjahr}.")

    db_jahr = deckungsbeitragsrechnung(tx, kategorien, gruppen=["geschaeftsjahr"]).set_index("geschaeftsjahr")
    akt = db_jahr.loc[geschaeftsjahr]
    vj = db_jahr.loc[geschaeftsjahr - 1] if geschaeftsjahr - 1 in db_jahr.index else None

    kosten_jahr = kosten[kosten["geschaeftsjahr"] == geschaeftsjahr]
    gemeinkosten_ist = float(kosten_jahr["ist_eur"].sum())
    gemeinkosten_plan = float(kosten_jahr["plan_eur"].sum())
    betriebsergebnis = float(akt["db_ii_eur"]) - gemeinkosten_ist
    plan_umsatz = float(plan.loc[plan["geschaeftsjahr"] == geschaeftsjahr, "plan_umsatz_eur"].sum())

    up = umsatz_plan_ist(tx, plan)
    up_jahr = up[up["geschaeftsjahr"] == geschaeftsjahr]
    top_umsatz = up_jahr.reindex(up_jahr["abweichung_pct"].abs().sort_values(ascending=False).index).head(top_n)

    kost = plan_ist_abweichung(
        kosten_jahr.groupby(["quartal", "kostenstelle", "kostenart"], as_index=False)[["plan_eur", "ist_eur"]].sum()
    )
    top_kosten = kost[kost["bewertung"] == "ungünstig"].sort_values("abweichung_eur", ascending=False).head(top_n)

    ausreisser = tx_alle[(tx_alle["geschaeftsjahr"] == geschaeftsjahr) & (tx_alle["ist_ausreisser"] == 1)]

    fakten: dict[str, Any] = {
        "unternehmen": "ControllAI Demo GmbH (synthetische Daten)",
        "geschaeftsjahr": geschaeftsjahr,
        "umsatz_eur": _r(akt["umsatz_eur"], 0),
        "plan_umsatz_eur": _r(plan_umsatz, 0),
        "abweichung_umsatz_zum_plan_pct": _r((akt["umsatz_eur"] - plan_umsatz) / plan_umsatz * 100),
        "deckungsbeitrag_i_eur": _r(akt["db_i_eur"], 0),
        "db_i_marge_pct": _r(akt["db_i_marge_pct"]),
        "deckungsbeitrag_ii_eur": _r(akt["db_ii_eur"], 0),
        "db_ii_marge_pct": _r(akt["db_ii_marge_pct"]),
        "gemeinkosten_ist_eur": _r(gemeinkosten_ist, 0),
        "gemeinkosten_plan_eur": _r(gemeinkosten_plan, 0),
        "gemeinkosten_abweichung_pct": _r((gemeinkosten_ist - gemeinkosten_plan) / gemeinkosten_plan * 100),
        "betriebsergebnis_eur": _r(betriebsergebnis, 0),
        "umsatzrentabilitaet_pct": _r(betriebsergebnis / float(akt["umsatz_eur"]) * 100),
        "groesste_umsatzabweichungen": [
            {
                "periode": f"{row.quartal} {row.geschaeftsjahr}",
                "produktkategorie": row.produktkategorie,
                "plan_eur": _r(row.plan_umsatz_eur, 0),
                "ist_eur": _r(row.ist_umsatz_eur, 0),
                "abweichung_pct": _r(row.abweichung_pct),
                "ampel": row.ampel,
            }
            for row in top_umsatz.itertuples()
        ],
        "groesste_kostenueberschreitungen": [
            {
                "periode": f"{row.quartal} {geschaeftsjahr}",
                "kostenstelle": row.kostenstelle,
                "kostenart": row.kostenart,
                "plan_eur": _r(row.plan_eur, 0),
                "ist_eur": _r(row.ist_eur, 0),
                "abweichung_eur": _r(row.abweichung_eur, 0),
                "abweichung_pct": _r(row.abweichung_pct),
            }
            for row in top_kosten.itertuples()
        ],
        "markierte_ausreisser": {
            "anzahl": len(ausreisser),
            "summe_eur": _r(ausreisser["umsatz_eur"].sum(), 0),
            "in_kennzahlen_enthalten": not ohne_ausreisser,
        },
    }
    if vj is not None:
        fakten["umsatz_vorjahr_eur"] = _r(vj["umsatz_eur"], 0)
        fakten["umsatz_yoy_pct"] = _r((akt["umsatz_eur"] - vj["umsatz_eur"]) / vj["umsatz_eur"] * 100)
        fakten["db_ii_vorjahr_eur"] = _r(vj["db_ii_eur"], 0)
    return fakten


# =============================================================================
# Process Mining
# =============================================================================

EVENTLOG_PFLICHT = ("case_id", "aktivitaet", "zeitstempel")


def lade_event_log(quelle: str | Path | pd.DataFrame) -> pd.DataFrame:
    """Liest und validiert ein Event-Log (eine Zeile je abgeschlossener Aktivität).

    Pflichtspalten: ``case_id``, ``aktivitaet``, ``zeitstempel``; optional
    ``ressource``, ``abteilung`` und beliebige Fallattribute.
    """
    log = quelle.copy() if isinstance(quelle, pd.DataFrame) else pd.read_csv(quelle, sep=";", dtype=str)
    fehlend = set(EVENTLOG_PFLICHT) - set(log.columns)
    if fehlend:
        raise ValueError(f"Pflichtspalten fehlen im Event-Log: {', '.join(sorted(fehlend))}")
    log["zeitstempel"] = pd.to_datetime(log["zeitstempel"], errors="coerce")
    ungueltig = log["zeitstempel"].isna() | log["case_id"].isna() | log["aktivitaet"].isna()
    if ungueltig.any():
        logger.warning("%d Ereignisse ohne gültigen Zeitstempel/Fall/Aktivität verworfen.", int(ungueltig.sum()))
        log = log[~ungueltig]
    log["aktivitaet"] = log["aktivitaet"].astype(str).str.strip()
    log = log.drop_duplicates(subset=["case_id", "aktivitaet", "zeitstempel"])
    return log.sort_values(["case_id", "zeitstempel"], kind="stable").reset_index(drop=True)


@dataclass
class ProzessAnalyse:
    """Ergebnis der Process-Mining-Analyse.

    Attributes:
        prozessname: Bezeichnung des Prozesses.
        faelle: eine Zeile je Fall (Durchlaufzeit, Variante, Nacharbeit, Übergaben).
        varianten: Häufigkeit und Durchlaufzeit je Prozessvariante.
        uebergaenge: Kennzahlen je Übergang Aktivität A → Aktivität B.
        aktivitaeten: Häufigkeit je Aktivität und Wiederholungen.
    """

    prozessname: str
    faelle: pd.DataFrame
    varianten: pd.DataFrame
    uebergaenge: pd.DataFrame
    aktivitaeten: pd.DataFrame

    def engpaesse(self, top_n: int = 3) -> pd.DataFrame:
        """Übergänge mit dem größten Anteil an der gesamten Durchlaufzeit."""
        return self.uebergaenge.head(top_n)

    def to_facts(self, top_n: int = 5) -> dict[str, Any]:
        """Kompakte, JSON-fähige Faktenbasis für ``ControllAIAgent.analyze_process``."""
        dlz = self.faelle["durchlaufzeit_tage"]
        nacharbeit = self.faelle["nacharbeit"]
        wiederholt = self.aktivitaeten[self.aktivitaeten["wiederholungen"] > 0]
        return {
            "prozess": self.prozessname,
            "zeitraum": {
                "von": str(self.faelle["start"].min().date()),
                "bis": str(self.faelle["ende"].max().date()),
            },
            "anzahl_faelle": len(self.faelle),
            "anzahl_ereignisse": int(self.faelle["anzahl_ereignisse"].sum()),
            "anzahl_varianten": len(self.varianten),
            "durchlaufzeit_tage": {
                "mittelwert": _r(dlz.mean()),
                "median": _r(dlz.median()),
                "p90": _r(dlz.quantile(0.9)),
                "maximum": _r(dlz.max()),
            },
            "haeufigste_varianten": [
                {
                    "variante": row.variante,
                    "faelle": int(row.faelle),
                    "anteil_pct": _r(row.anteil_pct),
                    "median_durchlaufzeit_tage": _r(row.median_durchlaufzeit_tage),
                }
                for row in self.varianten.head(top_n).itertuples()
            ],
            "engpaesse": [
                {
                    "uebergang": f"{row.von} → {row.nach}",
                    "anzahl": int(row.anzahl),
                    "median_stunden": _r(row.median_h),
                    "p90_stunden": _r(row.p90_h),
                    "anteil_an_gesamtzeit_pct": _r(row.anteil_gesamtzeit_pct),
                    "abteilungswechsel_pct": _r(row.abteilungswechsel_pct),
                }
                for row in self.engpaesse(top_n).itertuples()
            ],
            "nacharbeit": {
                "quote_pct": _r(nacharbeit.mean() * 100),
                "faelle": int(nacharbeit.sum()),
                "wiederholte_aktivitaeten": {
                    row.aktivitaet: int(row.wiederholungen) for row in wiederholt.itertuples()
                },
            },
            "abteilungswechsel_je_fall": _r(self.faelle["abteilungswechsel"].mean()),
            "methodik": (
                "Übergangszeit = Zeit zwischen dem Abschluss zweier aufeinanderfolgender Aktivitäten "
                "(Wartezeit plus Bearbeitungszeit der Folgeaktivität); Engpass-Rangfolge nach Anteil "
                "an der gesamten Durchlaufzeit."
            ),
        }


def analysiere_prozess(
    log: pd.DataFrame,
    *,
    prozessname: str = "Geschäftsprozess",
    abteilung_spalte: str = "abteilung",
) -> ProzessAnalyse:
    """Berechnet Durchlaufzeiten, Varianten, Übergangszeiten, Engpässe und Nacharbeit.

    Args:
        log: Event-Log aus ``lade_event_log``.
        prozessname: Bezeichnung für Bericht und Agent.
        abteilung_spalte: Spalte für Übergaben zwischen Organisationseinheiten
            (BPMN: Wechsel der Lane). Fehlt sie, werden keine Übergaben gezählt.
    """
    if log.empty:
        raise ValueError("Das Event-Log ist leer.")
    log = log.sort_values(["case_id", "zeitstempel"], kind="stable").reset_index(drop=True)
    hat_abteilung = abteilung_spalte in log.columns

    # --- Übergänge A → B je Fall
    g = log.groupby("case_id", sort=False)
    log["nach"] = g["aktivitaet"].shift(-1)
    log["dauer_h"] = (g["zeitstempel"].shift(-1) - log["zeitstempel"]).dt.total_seconds() / 3600
    if hat_abteilung:
        log["wechsel"] = log[abteilung_spalte] != g[abteilung_spalte].shift(-1)
    else:
        log["wechsel"] = False
    tr = log.dropna(subset=["nach"]).rename(columns={"aktivitaet": "von"})

    uebergaenge = tr.groupby(["von", "nach"], as_index=False).agg(
        anzahl=("dauer_h", "size"),
        median_h=("dauer_h", "median"),
        mittel_h=("dauer_h", "mean"),
        p90_h=("dauer_h", lambda s: s.quantile(0.9)),
        summe_h=("dauer_h", "sum"),
        abteilungswechsel_pct=("wechsel", lambda s: s.mean() * 100),
    )
    gesamt_h = uebergaenge["summe_h"].sum()
    uebergaenge["anteil_gesamtzeit_pct"] = uebergaenge["summe_h"] / gesamt_h * 100 if gesamt_h else 0.0
    uebergaenge = uebergaenge.sort_values("anteil_gesamtzeit_pct", ascending=False).reset_index(drop=True)
    num = ["median_h", "mittel_h", "p90_h", "summe_h", "abteilungswechsel_pct", "anteil_gesamtzeit_pct"]
    uebergaenge[num] = uebergaenge[num].round(2)

    # --- Fälle
    faelle = g.agg(
        start=("zeitstempel", "min"),
        ende=("zeitstempel", "max"),
        anzahl_ereignisse=("aktivitaet", "size"),
        variante=("aktivitaet", lambda s: " → ".join(s)),
        nacharbeit=("aktivitaet", lambda s: bool(s.duplicated().any())),
    ).reset_index()
    faelle["durchlaufzeit_tage"] = ((faelle["ende"] - faelle["start"]).dt.total_seconds() / 86_400).round(2)
    faelle = faelle.merge(
        tr.groupby("case_id")["wechsel"].sum().rename("abteilungswechsel").reset_index(), on="case_id", how="left"
    )
    faelle["abteilungswechsel"] = faelle["abteilungswechsel"].fillna(0).astype(int)

    # --- Varianten
    varianten = faelle.groupby("variante", as_index=False).agg(
        faelle=("case_id", "size"),
        median_durchlaufzeit_tage=("durchlaufzeit_tage", "median"),
    )
    varianten["anteil_pct"] = (varianten["faelle"] / len(faelle) * 100).round(2)
    varianten = varianten.sort_values(["faelle", "variante"], ascending=[False, True]).reset_index(drop=True)

    # --- Aktivitäten und Wiederholungen (Nacharbeit)
    je_fall = log.groupby(["case_id", "aktivitaet"]).size().rename("n").reset_index()
    aktivitaeten = je_fall.groupby("aktivitaet", as_index=False).agg(
        ereignisse=("n", "sum"),
        faelle=("case_id", "nunique"),
    )
    aktivitaeten["wiederholungen"] = aktivitaeten["ereignisse"] - aktivitaeten["faelle"]
    aktivitaeten = aktivitaeten.sort_values("ereignisse", ascending=False).reset_index(drop=True)

    logger.info(
        "Process Mining '%s': %d Fälle, %d Varianten, Top-Engpass: %s → %s",
        prozessname, len(faelle), len(varianten), uebergaenge.at[0, "von"], uebergaenge.at[0, "nach"],
    )
    return ProzessAnalyse(prozessname, faelle, varianten, uebergaenge, aktivitaeten)


# =============================================================================
# Kommandozeile: Kennzahlen ohne LLM anzeigen
# =============================================================================


def main() -> None:
    """``python -m src.analytics`` – zeigt die deterministische Faktenbasis (kein API-Schlüssel nötig)."""
    parser = argparse.ArgumentParser(description="Controlling- und Process-Mining-Kennzahlen")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "controlling_erp.db"))
    parser.add_argument("--eventlog", default=str(PROJECT_ROOT / "data" / "sample" / "event_log_p2p.csv"))
    parser.add_argument("--jahr", type=int, default=2025)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    print("=== Controlling-Kennzahlen ===")
    print(json.dumps(kennzahlen_bericht(args.db, args.jahr), ensure_ascii=False, indent=2))
    print("\n=== Process Mining: Purchase-to-Pay ===")
    analyse = analysiere_prozess(lade_event_log(args.eventlog), prozessname="Purchase-to-Pay")
    print(json.dumps(analyse.to_facts(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
