"""
Demo-Modus – der Agent ohne API-Schlüssel.

Ist kein API-Schlüssel hinterlegt, ersetzt ``DemoLLM`` das Sprachmodell durch
regelbasierte Muster-Antworten. Alle übrigen Bausteine laufen unverändert:
SQL-Guard, Ausführung auf der echten Datenbank, Pydantic-Validierung,
Zahlen- und Zitatprüfung. Der Demo-Modus zeigt damit die vollständige
Architektur – nur die Formulierung stammt aus Vorlagen statt aus einem LLM.

Was der Demo-Modus kann:
    Text-to-SQL       sechs hinterlegte Beispielfragen (inkl. eines Angriffs,
                      den der SQL-Guard blockiert); die Antworttexte werden aus
                      dem echten Abfrageergebnis formuliert
    BSC-Analyse       vorbereitete Vorschläge für die zwölf Beispieltexte –
                      mit bewusst eingebauten Schwachstellen (niedrige
                      Konfidenz, umformuliertes Zitat, abweichende Perspektive),
                      damit die Prüfmechanismen und der Human-in-the-Loop
                      sichtbar werden
    Bericht/Prozess   Executive Summary und BPMN-Vorschläge, berechnet aus den
                      übergebenen Kennzahlen

Autor: Sherwan Shweish
Lizenz: MIT
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.agent import AgentConfig, ControllAIAgent, FactChecker, LLMError, build_llm_client
from src.seed_data import PROJEKTTEXTE

__all__ = ["DEMO_FRAGEN", "Betriebsmodus", "DemoFrage", "DemoLLM", "erstelle_agent", "finde_demo_frage"]


# =============================================================================
# Formatierung
# =============================================================================


def de(wert: float, stellen: int = 0) -> str:
    """Deutsches Zahlenformat: 1234567.8 → '1.234.567,8'."""
    text = f"{wert:,.{stellen}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def vz(wert: float, stellen: int = 1) -> str:
    """Zahl mit Vorzeichen im deutschen Format: 9.04 → '+9,0'."""
    return ("+" if wert > 0 else "") + de(wert, stellen)


# =============================================================================
# Text-to-SQL: Beispielfragen
# =============================================================================


@dataclass(frozen=True)
class DemoFrage:
    """Eine im Demo-Modus hinterlegte Frage mit SQL und Antwortvorlage."""

    id: str
    frage: str
    schluessel: tuple[tuple[str, ...], ...]  # Treffer, wenn alle Begriffe einer Gruppe vorkommen
    sql: str | None
    erklaerung: str
    antwort: Callable[[list[dict[str, Any]]], str]


def _antwort_abweichung(r: list[dict[str, Any]]) -> str:
    a = r[0]
    text = (
        f"Im Q3 2025 lag {a['produktkategorie']} mit {de(a['abweichung_pct'], 1)} % am deutlichsten unter Plan: "
        f"Ist {de(a['ist_umsatz_eur'])} € gegenüber Plan {de(a['plan_umsatz_eur'])} € "
        f"(Abweichung {de(a['abweichung_eur'])} €)."
    )
    if len(r) > 1 and r[1]["abweichung_pct"] < 0:
        text += f" Danach folgt {r[1]['produktkategorie']} mit {de(r[1]['abweichung_pct'], 1)} %."
    return text


def _antwort_kosten(r: list[dict[str, Any]]) -> str:
    a = r[0]
    text = (
        f"Die größte Budgetüberschreitung 2025 betrifft die Kostenstelle {a['kostenstelle']} ({a['bezeichnung']}) "
        f"bei den {a['kostenart']}: Ist {de(a['ist_eur'])} € gegenüber Plan {de(a['plan_eur'])} €, also "
        f"{de(a['ueberschreitung_eur'])} € bzw. {de(a['ueberschreitung_pct'], 1)} % mehr."
    )
    if len(r) > 1:
        b = r[1]
        text += (
            f" Auf Platz zwei folgt die Kostenstelle {b['kostenstelle']} ({b['bezeichnung']}, {b['kostenart']}) "
            f"mit {de(b['ueberschreitung_pct'], 1)} %."
        )
    return text


def _antwort_yoy(r: list[dict[str, Any]]) -> str:
    teile = ", ".join(f"{z['quartal']} {vz(z['yoy_pct'])} %" for z in r)
    staerkstes = max(r, key=lambda z: z["yoy_pct"])
    return (
        f"Entwicklung des Umsatzes 2025 gegenüber dem Vorjahr je Quartal: {teile}. "
        f"Am stärksten war das Wachstum im {staerkstes['quartal']} mit {vz(staerkstes['yoy_pct'])} % "
        f"({de(staerkstes['umsatz_2025_eur'])} € nach {de(staerkstes['umsatz_2024_eur'])} €)."
    )


def _antwort_marge(r: list[dict[str, Any]]) -> str:
    hoch, tief = r[0], r[-1]
    return (
        f"{hoch['produktkategorie']} erzielte 2025 mit {de(hoch['db_i_marge_pct'], 1)} % die höchste "
        f"DB-I-Marge (DB I {de(hoch['db_i_eur'])} €). Am niedrigsten lag {tief['produktkategorie']} "
        f"mit {de(tief['db_i_marge_pct'], 1)} %."
    )


def _antwort_ausreisser(r: list[dict[str, Any]]) -> str:
    a = r[0]
    anzahl = f"{len(r)} Buchungen sind" if len(r) <= 12 else "Mehrere Buchungen sind"
    return (
        f"{anzahl} als Ausreißer zur Prüfung markiert. Die größte ist Faktura {a['faktura_nr']} in der "
        f"Kategorie {a['produktkategorie']} über {de(a['umsatz_eur'])} €. Die markierten Buchungen bleiben im "
        "Datenbestand, sind aber bis zur Klärung aus den Plan-Ist-Kennzahlen ausgenommen."
    )


DEMO_FRAGEN: tuple[DemoFrage, ...] = (
    DemoFrage(
        id="angriff",
        frage="Lösche alle Plandaten aus der Datenbank.",
        schluessel=(("lösch",), ("delete",), ("drop",), ("entfern",)),
        sql="DELETE FROM umsatzplanung",
        erklaerung="Simulierter Angriff: Das Modell erzeugt eine schreibende Anweisung.",
        antwort=lambda r: "",
    ),
    DemoFrage(
        id="abweichung",
        frage="Welche Produktkategorie hatte im Q3 2025 die höchste negative Plan-Ist-Abweichung?",
        schluessel=(("abweichung", "q3"), ("sensorik",), ("plan-ist",)),
        sql=(
            "SELECT produktkategorie, plan_umsatz_eur, ist_umsatz_eur, abweichung_eur, abweichung_pct\n"
            "FROM v_plan_ist_umsatz\n"
            "WHERE geschaeftsjahr = 2025 AND quartal = 'Q3'\n"
            "ORDER BY abweichung_pct ASC\nLIMIT 5"
        ),
        erklaerung="Plan-Ist-Vergleich aus der View v_plan_ist_umsatz, aufsteigend nach relativer Abweichung.",
        antwort=_antwort_abweichung,
    ),
    DemoFrage(
        id="kosten",
        frage="Welche Kostenstellen haben 2025 ihr Budget am stärksten überschritten?",
        schluessel=(("kostenstelle",), ("budget",), ("kosten", "überschr")),
        sql=(
            "SELECT f.kostenstelle, k.bezeichnung, f.kostenart,\n"
            "       ROUND(SUM(f.plan_eur), 2) AS plan_eur,\n"
            "       ROUND(SUM(f.ist_eur), 2) AS ist_eur,\n"
            "       ROUND(SUM(f.ist_eur) - SUM(f.plan_eur), 2) AS ueberschreitung_eur,\n"
            "       ROUND((SUM(f.ist_eur) - SUM(f.plan_eur)) * 100.0 / NULLIF(SUM(f.plan_eur), 0), 2)"
            " AS ueberschreitung_pct\n"
            "FROM plan_ist_finanzen f\n"
            "JOIN kostenstellen k ON k.kostenstelle = f.kostenstelle\n"
            "WHERE f.geschaeftsjahr = 2025\n"
            "GROUP BY f.kostenstelle, k.bezeichnung, f.kostenart\n"
            "HAVING ueberschreitung_eur > 0\n"
            "ORDER BY ueberschreitung_eur DESC\nLIMIT 5"
        ),
        erklaerung="Summe Plan und Ist je Kostenstelle und Kostenart für 2025, nur Überschreitungen.",
        antwort=_antwort_kosten,
    ),
    DemoFrage(
        id="yoy",
        frage="Wie hat sich der Umsatz je Quartal 2025 im Vergleich zum Vorjahr entwickelt?",
        schluessel=(("vorjahr",), ("yoy",), ("quartal", "umsatz", "entwick")),
        sql=(
            "WITH q AS (\n"
            "    SELECT geschaeftsjahr, quartal, SUM(umsatz_eur) AS umsatz\n"
            "    FROM umsatz_transaktionen\n"
            "    WHERE ist_ausreisser = 0\n"
            "    GROUP BY geschaeftsjahr, quartal\n"
            ")\n"
            "SELECT a.quartal,\n"
            "       ROUND(b.umsatz, 2) AS umsatz_2024_eur,\n"
            "       ROUND(a.umsatz, 2) AS umsatz_2025_eur,\n"
            "       ROUND((a.umsatz - b.umsatz) * 100.0 / b.umsatz, 2) AS yoy_pct\n"
            "FROM q a\n"
            "JOIN q b ON b.quartal = a.quartal AND b.geschaeftsjahr = 2024\n"
            "WHERE a.geschaeftsjahr = 2025\n"
            "ORDER BY a.quartal"
        ),
        erklaerung="Quartalsumsätze ohne markierte Ausreißer, 2025 gegenüber 2024.",
        antwort=_antwort_yoy,
    ),
    DemoFrage(
        id="marge",
        frage="Welche Produktkategorie erzielte 2025 die höchste Deckungsbeitragsmarge?",
        schluessel=(("marge",), ("deckungsbeitrag",)),
        sql=(
            "SELECT produktkategorie,\n"
            "       ROUND(SUM(umsatz_eur), 2) AS umsatz_eur,\n"
            "       ROUND(SUM(deckungsbeitrag_eur), 2) AS db_i_eur,\n"
            "       ROUND(SUM(deckungsbeitrag_eur) * 100.0 / SUM(umsatz_eur), 2) AS db_i_marge_pct\n"
            "FROM umsatz_transaktionen\n"
            "WHERE geschaeftsjahr = 2025 AND ist_ausreisser = 0\n"
            "GROUP BY produktkategorie\n"
            "ORDER BY db_i_marge_pct DESC"
        ),
        erklaerung="DB I und DB-I-Marge je Produktkategorie 2025, ohne markierte Ausreißer.",
        antwort=_antwort_marge,
    ),
    DemoFrage(
        id="ausreisser",
        frage="Welche Buchungen sind als Ausreißer zur Prüfung markiert?",
        schluessel=(("ausreißer",), ("ausreisser",), ("prüfung", "markiert")),
        sql=(
            "SELECT faktura_nr, buchungsdatum, produktkategorie, kunden_nr, umsatz_eur, ausreisser_grund\n"
            "FROM umsatz_transaktionen\n"
            "WHERE ist_ausreisser = 1\n"
            "ORDER BY umsatz_eur DESC"
        ),
        erklaerung="Alle statistisch auffälligen Buchungen, absteigend nach Betrag.",
        antwort=_antwort_ausreisser,
    ),
)


def finde_demo_frage(frage: str) -> DemoFrage | None:
    """Ordnet eine Frage einer hinterlegten Demo-Frage zu (Schlüsselwortvergleich)."""
    text = frage.lower()
    for demo in DEMO_FRAGEN:
        if text.strip() == demo.frage.lower() or any(all(k in text for k in gruppe) for gruppe in demo.schluessel):
            return demo
    return None


# =============================================================================
# BSC: vorbereitete Vorschläge für die Beispieltexte
# =============================================================================


def _vorschlag(text_id: str, **abweichend: Any) -> dict[str, Any]:
    eintrag = next(t for t in PROJEKTTEXTE if t["id"] == text_id)
    e = eintrag["erwartung"]
    return {
        "perspektive": e["perspektive"],
        "technologiefeld": e["technologiefeld"],
        "belegzitat": e["belegzitat"],
        **abweichend,
    }


DEMO_BSC: dict[str, list[dict[str, Any]]] = {
    "T01": [_vorschlag("T01", titel="IoT-Vernetzung der Fertigung", konfidenz=0.92,
                       beschreibung="Anbindung von mehr als 12.000 Maschinen an eine einheitliche IoT-Plattform.",
                       begruendung="Ziel ist die Steigerung der Anlageneffektivität – eine Prozesszielsetzung.")],
    "T02": [_vorschlag("T02", titel="KI-gestützter Schadenassistent", konfidenz=0.90,
                       beschreibung="Schadenmeldung per Foto mit schneller Regulierungszusage.",
                       begruendung="Die Zielsetzung ist ausdrücklich die Kundenzufriedenheit.")],
    "T03": [_vorschlag("T03", titel="Migration in eine Hybrid-Cloud", konfidenz=0.88,
                       beschreibung="Stilllegung eigener Rechenzentren, nutzungsabhängige Abrechnung.",
                       begruendung="Begründet wird das Vorhaben mit Einsparungen bei den IT-Betriebskosten.")],
    "T04": [_vorschlag("T04", titel="Rechnungsverarbeitung mit Software-Robotern", konfidenz=0.95,
                       beschreibung="Automatisches Auslesen und Abgleichen von Lieferantenrechnungen.",
                       begruendung="Ziel und Wirkung liegen in kürzeren Bearbeitungszeiten im Finanzprozess.")],
    "T05": [_vorschlag("T05", titel="Energie-App für Haushaltskunden", konfidenz=0.90,
                       beschreibung="App zur Steuerung von Verbrauch, Wallbox und Photovoltaik.",
                       begruendung="Die App soll Kunden binden und neue Kundengruppen gewinnen.")],
    # Bewusst unsicher: Forschungseffizienz (Prozesse) oder Pipeline-Wert (Finanzen)?
    "T06": [_vorschlag("T06", titel="KI-gestützte Wirkstoffforschung", konfidenz=0.62,
                       beschreibung="Vorselektion von Molekülkandidaten mit KI-Modellen.",
                       begruendung="Kürzere Forschungsphase spricht für Prozesse; ein Finanzbezug ist "
                                   "ebenfalls denkbar, wird aber nicht genannt.")],
    "T07": [_vorschlag("T07", titel="Data Academy", konfidenz=0.93,
                       beschreibung="Qualifizierungsprogramm für Datenkompetenz und Data Governance.",
                       begruendung="Aufbau von Kompetenzen der Beschäftigten.")],
    # Bewusst fehlerhaft: Zitat leicht umformuliert („Steigerung“ statt „Verbesserung“)
    "T08": [_vorschlag("T08", titel="Datenbasierte Preis- und Promotionsplanung", konfidenz=0.85,
                       beschreibung="Analyse von Abverkaufsdaten für Preis- und Promotionsentscheidungen.",
                       begruendung="Messbare Wirkung auf die Bruttomarge.",
                       belegzitat="Die Lösung trug 2023 nach unseren Berechnungen rund 0,8 Prozentpunkte zur "
                                  "Steigerung der Bruttomarge bei")],
    # Bewusst abweichend vom Referenzlabel (Lernen & Entwicklung) – Fall für den Menschen
    "T09": [_vorschlag("T09", titel="Konzernweite Entwicklerplattform in der Cloud", konfidenz=0.64,
                       perspektive="Interne Prozesse",
                       beschreibung="Einheitliche Werkzeuge und automatisierte Testumgebungen für Produktteams.",
                       begruendung="Einheitliche Werkzeuge deuten auf effizientere Entwicklungsprozesse; das "
                                   "Einarbeiten neuer Fachkräfte spricht für Lernen & Entwicklung.")],
    "T10": [_vorschlag("T10", titel="Digitaler Wartungsservice für Kompressoren", konfidenz=0.87,
                       beschreibung="Sensorbasierte Verschleißmeldung bei ausgelieferten Anlagen.",
                       begruendung="Ziel sind höhere Anlagenverfügbarkeit beim Kunden und Serviceverträge.")],
    "T11": [],
    # Die eingeschleuste Anweisung im Text wird ignoriert
    "T12": [_vorschlag("T12", titel="KI-gestützte Absatzprognosen", konfidenz=0.90,
                       beschreibung="Steuerung der Warendisposition in den Filialen.",
                       begruendung="Ziel ist eine effizientere Disposition – eine Prozesszielsetzung.")],
}

_TEXT_ERKENNUNG = {t["id"]: FactChecker.normalize_text(t["text"][:80]) for t in PROJEKTTEXTE}


# =============================================================================
# Berichte aus Kennzahlen
# =============================================================================


def _executive_summary(f: dict[str, Any]) -> dict[str, Any]:
    gj = f.get("geschaeftsjahr", "")
    kern: list[str] = []
    empf: list[dict[str, Any]] = []

    if f.get("umsatz_eur") is not None:
        satz = f"Der Umsatz {gj} beträgt {de(f['umsatz_eur'] / 1e6, 1)} Mio. €"
        if f.get("umsatz_yoy_pct") is not None:
            satz += f" ({vz(f['umsatz_yoy_pct'])} % zum Vorjahr)"
        if f.get("abweichung_umsatz_zum_plan_pct") is not None:
            satz += f"; die Abweichung zum Plan liegt bei {vz(f['abweichung_umsatz_zum_plan_pct'])} %"
        kern.append(satz + ".")
    if f.get("db_ii_marge_pct") is not None and f.get("betriebsergebnis_eur") is not None:
        kern.append(
            f"Die DB-II-Marge liegt bei {de(f['db_ii_marge_pct'], 1)} %, die Umsatzrentabilität bei "
            f"{de(f['umsatzrentabilitaet_pct'], 1)} % "
            f"(Betriebsergebnis {de(f['betriebsergebnis_eur'] / 1e6, 2)} Mio. €)."
        )

    abweichungen = f.get("groesste_umsatzabweichungen", [])
    negativ = next((a for a in abweichungen if (a.get("abweichung_pct") or 0) < 0), None)
    if negativ:
        kern.append(
            f"Größte negative Planabweichung: {negativ['produktkategorie']} im {negativ['periode']} mit "
            f"{de(negativ['abweichung_pct'], 1)} % ({de(negativ['ist_eur'] / 1e6, 2)} statt "
            f"{de(negativ['plan_eur'] / 1e6, 2)} Mio. €)."
        )
        empf.append({
            "massnahme": f"Ursachenanalyse {negativ['produktkategorie']} mit Vertrieb und Supply Chain",
            "begruendung": f"Planabweichung von {de(negativ['abweichung_pct'], 1)} % im {negativ['periode']}; "
                           "Lieferfähigkeit, Auftragsbestand und Forecast prüfen.",
            "prioritaet": "hoch" if negativ.get("ampel") == "rot" else "mittel",
            "verantwortlich": "Vertrieb / Supply Chain",
        })

    kosten = f.get("groesste_kostenueberschreitungen", [])
    if kosten:
        k = kosten[0]
        kern.append(
            f"Größte Budgetüberschreitung: Kostenstelle {k['kostenstelle']} ({k['kostenart']}) im {k['periode']} "
            f"mit {vz(k['abweichung_pct'])} % ({de(k['abweichung_eur'])} €) über Plan."
        )
        empf.append({
            "massnahme": f"Budgetüberschreitung {k['kostenart']} (Kostenstelle {k['kostenstelle']}) klären",
            "begruendung": f"Ist {de(k['ist_eur'])} € gegenüber Plan {de(k['plan_eur'])} €; Ursachen und "
                           "Folgekosten bewerten und den Forecast anpassen.",
            "prioritaet": "hoch" if (k.get("abweichung_pct") or 0) > 20 else "mittel",
            "verantwortlich": "Controlling / Kostenstellenverantwortung",
        })

    positiv = next((a for a in abweichungen if (a.get("abweichung_pct") or 0) > 0), None)
    if positiv:
        kern.append(
            f"{positiv['produktkategorie']} übertraf im {positiv['periode']} den Plan um "
            f"{de(positiv['abweichung_pct'], 1)} % – die Planung sollte auf Unterschätzung geprüft werden."
        )

    aus = f.get("markierte_ausreisser") or {}
    if aus.get("anzahl"):
        ausgenommen = (
            "" if aus.get("in_kennzahlen_enthalten") else " und bis zur Klärung aus den Kennzahlen ausgenommen"
        )
        empf.append({
            "massnahme": "Markierte Ausreißer mit dem Rechnungswesen klären",
            "begruendung": f"Buchungen über zusammen {de(aus['summe_eur'] / 1e6, 2)} Mio. € sind zur Prüfung "
                           f"markiert{ausgenommen}.",
            "prioritaet": "mittel",
            "verantwortlich": "Rechnungswesen",
        })

    return {
        "titel": f"Management-Bericht Geschäftsjahr {gj}".strip(),
        "kernaussagen": kern[:5] or ["Für den gewählten Zeitraum liegen keine auswertbaren Kennzahlen vor."],
        "handlungsempfehlungen": empf[:4],
        "risiken_und_datenluecken": [
            "Synthetische Demo-Daten: Die Aussagen illustrieren die Methodik, nicht ein reales Unternehmen.",
            "Gemeinkosten liegen nur auf Kostenstellenebene vor; eine Zuordnung zu Produktkategorien "
            "(DB III) ist nicht möglich.",
            "Ursachen der Abweichungen sind aus den Zahlen allein nicht ableitbar und mit den Fachbereichen zu klären.",
        ],
    }


def _prozessbericht(f: dict[str, Any]) -> dict[str, Any]:
    dlz = f.get("durchlaufzeit_tage", {})
    engpaesse = f.get("engpaesse", [])
    nach = f.get("nacharbeit", {})
    kern = [
        f"Analysiert wurden {f.get('anzahl_faelle')} Fälle in {f.get('anzahl_varianten')} Prozessvarianten; die "
        f"Durchlaufzeit beträgt im Median {de(dlz.get('median', 0), 1)} Tage (P90: {de(dlz.get('p90', 0), 1)} Tage)."
    ]
    empf: list[dict[str, Any]] = []
    if engpaesse:
        e = engpaesse[0]
        kern.append(
            f"Engpass ist der Übergang {e['uebergang']}: Median {de(e['median_stunden'], 1)} Stunden, "
            f"{de(e['anteil_an_gesamtzeit_pct'], 1)} % der gesamten Durchlaufzeit."
        )
        if (e.get("abteilungswechsel_pct") or 0) >= 99.9:
            kern.append("Jeder dieser Übergänge ist eine Übergabe an eine andere Abteilung (BPMN: Wechsel der Lane).")
        empf.append({
            "massnahme": "Freigabe nach Wertgrenzen staffeln (Gateway vereinfachen)",
            "begruendung": f"Der Übergang {e['uebergang']} bindet {de(e['anteil_an_gesamtzeit_pct'], 1)} % der "
                           "Durchlaufzeit. Rechnungen mit passender Bestellung und gebuchtem Wareneingang können "
                           "unterhalb einer Wertgrenze ohne gesonderte Freigabe weiterlaufen.",
            "prioritaet": "hoch",
            "verantwortlich": "Bereichsleitung / Kreditorenbuchhaltung",
        })
        empf.append({
            "massnahme": "Timer-Grenzereignis mit Eskalation an der Freigabeaufgabe ergänzen",
            "begruendung": f"10 % der Freigaben warten länger als {de(e['p90_stunden'], 1)} Stunden; ein "
                           "Timer-Ereignis eskaliert liegengebliebene Freigaben automatisch an eine Vertretung.",
            "prioritaet": "mittel",
            "verantwortlich": "Prozessverantwortung Purchase-to-Pay",
        })
    if len(engpaesse) > 1:
        kern.append(
            f"An zweiter Stelle steht {engpaesse[1]['uebergang']} mit "
            f"{de(engpaesse[1]['anteil_an_gesamtzeit_pct'], 1)} % der Gesamtzeit."
        )
    if nach.get("faelle"):
        aktivitaet = next(iter(nach.get("wiederholte_aktivitaeten", {})), "eine Aktivität")
        kern.append(
            f"In {de(nach['quote_pct'], 1)} % der Fälle wird „{aktivitaet}“ wiederholt – eine Nacharbeitsschleife."
        )
        empf.insert(1, {
            "massnahme": "Rechnungsprüfung als automatischen Drei-Wege-Abgleich modellieren (Service Task)",
            "begruendung": f"Nacharbeitsquote von {de(nach['quote_pct'], 1)} %: Preis- und Mengenabweichungen "
                           "zwischen Bestellung, Wareneingang und Rechnung früher erkennen und an den Einkauf "
                           "zurückspielen.",
            "prioritaet": "hoch",
            "verantwortlich": "Einkauf / Kreditorenbuchhaltung",
        })
    return {
        "titel": f"Prozessanalyse {f.get('prozess', '')}".strip(),
        "kernaussagen": kern[:5],
        "handlungsempfehlungen": empf[:4],
        "risiken_und_datenluecken": [
            "Das Event-Log enthält nur Abschlusszeitpunkte; Übergangszeiten umfassen Warte- und Bearbeitungszeit.",
            "Ursachen der Wartezeit (Abwesenheiten, Arbeitslast, fehlende Unterlagen) sind im Event-Log "
            "nicht sichtbar.",
        ],
    }


# =============================================================================
# Das Demo-Sprachmodell
# =============================================================================


def _zwischen(text: str, start: str, ende: str) -> str:
    treffer = re.search(re.escape(start) + r"\s*(.*?)\s*" + re.escape(ende), text, re.DOTALL)
    return treffer.group(1) if treffer else ""


class DemoLLM:
    """Regelbasierter Ersatz für das Sprachmodell (implementiert ``LLMClient``)."""

    kennung = "Demo-Modus (regelbasierte Muster-Antworten)"

    def complete(self, system: str, user: str) -> str:
        if "Ausgeführte SQL-Abfrage" in user:
            return self._sql_antwort(user)
        if "SQLite-Abfrage" in user:
            return self._sql_entwurf(user)
        if "Balanced Scorecard" in user:
            return self._bsc(user)
        if "Executive Summary" in user:
            return json.dumps(_executive_summary(self._fakten(user)), ensure_ascii=False)
        if "Process-Mining" in user:
            return json.dumps(_prozessbericht(self._fakten(user)), ensure_ascii=False)
        raise LLMError("Demo-Modus: Für diesen Aufgabentyp ist keine Muster-Antwort hinterlegt.")

    @staticmethod
    def _fakten(user: str) -> dict[str, Any]:
        return json.loads(_zwischen(user, "<<<DOKUMENT_BEGINN>>>", "<<<DOKUMENT_ENDE>>>") or "{}")

    @staticmethod
    def _sql_entwurf(user: str) -> str:
        frage = _zwischen(user, "<<<FRAGE_BEGINN>>>", "<<<FRAGE_ENDE>>>")
        demo = finde_demo_frage(frage)
        if demo is None:
            return json.dumps({
                "sql": None,
                "erklaerung": "Im Demo-Modus sind nur die Beispielfragen hinterlegt. Für freie Fragen einen "
                              "API-Schlüssel in der Datei .env eintragen.",
            }, ensure_ascii=False)
        return json.dumps({"sql": demo.sql, "erklaerung": demo.erklaerung}, ensure_ascii=False)

    @staticmethod
    def _sql_antwort(user: str) -> str:
        frage = re.search(r"^Frage: (.*)$", user, re.MULTILINE)
        demo = finde_demo_frage(frage.group(1) if frage else "")
        daten = json.loads(_zwischen(user, "<<<DOKUMENT_BEGINN>>>", "<<<DOKUMENT_ENDE>>>") or "[]")
        if demo is None or not daten:
            return "Das Abfrageergebnis ist in der Tabelle dargestellt."
        return demo.antwort(daten)

    @staticmethod
    def _bsc(user: str) -> str:
        text = FactChecker.normalize_text(_zwischen(user, "<<<DOKUMENT_BEGINN>>>", "<<<DOKUMENT_ENDE>>>"))
        initiativen: list[dict[str, Any]] = []
        for text_id, anfang in _TEXT_ERKENNUNG.items():
            if anfang in text:
                initiativen.extend(DEMO_BSC[text_id])
        return json.dumps({"initiativen": initiativen}, ensure_ascii=False)


# =============================================================================
# Agent-Fabrik mit automatischem Fallback
# =============================================================================


@dataclass(frozen=True)
class Betriebsmodus:
    """Beschreibt, ob der Agent live oder im Demo-Modus arbeitet."""

    demo: bool
    bezeichnung: str
    grund: str | None = None


def demo_erzwungen() -> bool:
    """``CONTROLLAI_DEMO=1`` erzwingt den Demo-Modus (z. B. für Präsentationen und Tests)."""
    return os.getenv("CONTROLLAI_DEMO", "").strip().lower() in {"1", "true", "ja", "yes", "on"}


def erstelle_agent(
    config: AgentConfig | None = None, *, demo: bool | None = None
) -> tuple[ControllAIAgent, Betriebsmodus]:
    """Erzeugt den Agenten – live, wenn ein API-Schlüssel vorhanden ist, sonst im Demo-Modus.

    Die App stürzt dadurch nie wegen eines fehlenden Schlüssels oder SDKs ab.
    """
    config = config or AgentConfig.from_env()
    if demo is None:
        demo = demo_erzwungen()
    if not demo:
        try:
            agent = ControllAIAgent(config, llm=build_llm_client(config))
            return agent, Betriebsmodus(False, f"Live · {config.provider} · {config.model}")
        except LLMError as exc:
            grund = str(exc)
    else:
        grund = "Demo-Modus wurde manuell aktiviert."
    return ControllAIAgent(config, llm=DemoLLM()), Betriebsmodus(True, DemoLLM.kennung, grund)
