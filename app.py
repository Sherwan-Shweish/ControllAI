"""
ControllAI – interaktives Management-Dashboard (Streamlit).

Start:  streamlit run app.py

Ohne API-Schlüssel startet die App automatisch im Demo-Modus: Die Formulierung
der KI-Antworten stammt dann aus Muster-Antworten, alle übrigen Bausteine
(Datenbank, SQL-Guard, Kennzahlen, Faktenprüfung) laufen unverändert.

Autor: Sherwan Shweish
Lizenz: MIT
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from src.agent import AgentAnswer, AgentConfig, AgentError, Intent, LLMError, SQLGuardError
from src.analytics import (
    ProzessAnalyse,
    analysiere_prozess,
    bereinige_sap_export,
    deckungsbeitragsrechnung,
    kennzahlen_bericht,
    lade_event_log,
    lade_tabelle,
    plan_ist_abweichung,
    umsatz_plan_ist,
)
from src.demo import DEMO_FRAGEN, de, demo_erzwungen, erstelle_agent, vz
from src.exporter import bsc_tabelle, erstelle_excel_report, erstelle_powerbi_paket, sammle_controlling_tabellen
from src.seed_data import PROJEKTTEXTE, erzeuge_alles
from src.textextraktion import abdeckung, extrahiere_pdf, fundstellen_text, text_zu_seiten, vorfiltern

st.set_page_config(page_title="ControllAI – Entscheidungsplattform", page_icon="📊", layout="wide")

PERSPEKTIVEN = ["Finanzen", "Kunden", "Interne Prozesse", "Lernen & Entwicklung"]
AMPELFARBEN = {"rot": "#C0392B", "gelb": "#E0A800", "grün": "#2E7D32"}
PRIMAER = "#1F3A5F"
NEUTRAL = "#9AA5B1"

CONFIG = AgentConfig.from_env()
DB_PATH = CONFIG.db_path
SAMPLE_DIR = DB_PATH.parent / "sample"
EVENTLOG_PATH = SAMPLE_DIR / "event_log_p2p.csv"
ROHEXPORT_PATH = SAMPLE_DIR / "sap_export_faktura.csv"


# =============================================================================
# Daten und Agent (gecacht)
# =============================================================================


@st.cache_resource(show_spinner="Demo-Daten werden erzeugt …")
def stelle_daten_sicher(db_path: str) -> str:
    """Erzeugt die Demo-Daten beim ersten Start, falls sie fehlen."""
    db = Path(db_path)
    if not db.exists() or not EVENTLOG_PATH.exists() or not ROHEXPORT_PATH.exists():
        if db.name != "controlling_erp.db":
            raise FileNotFoundError(f"Datenbank {db} nicht gefunden.")
        erzeuge_alles(db.parent)
    return db_path


@st.cache_data(show_spinner=False)
def tabelle(db: str, name: str) -> pd.DataFrame:
    return lade_tabelle(db, name)


@st.cache_data(show_spinner=False)
def fakten(db: str, gj: int) -> dict[str, Any]:
    return kennzahlen_bericht(db, gj)


@st.cache_data(show_spinner=False)
def bereinigung(db: str, rohexport: str) -> dict[str, Any] | None:
    if not Path(rohexport).exists():
        return None
    roh = pd.read_csv(rohexport, sep=";", dtype=str, keep_default_na=False)
    _, report = bereinige_sap_export(roh, tabelle(db, "materialien"), tabelle(db, "kunden"))
    return report.model_dump()


@st.cache_data(show_spinner=False)
def prozess(eventlog: str) -> ProzessAnalyse:
    return analysiere_prozess(lade_event_log(eventlog), prozessname="Purchase-to-Pay")


@st.cache_resource(show_spinner=False)
def agent_holen(demo: bool):
    return erstelle_agent(CONFIG, demo=demo)


@st.cache_data(show_spinner=False)
def excel_export(db: str, gj: int, bsc: pd.DataFrame | None, bereinigung_json: str | None) -> bytes:
    from src.analytics import BereinigungsReport

    report = BereinigungsReport(**json.loads(bereinigung_json)) if bereinigung_json else None
    tabellen = sammle_controlling_tabellen(db, geschaeftsjahr=gj, bsc=bsc, bereinigung=report)
    return erstelle_excel_report(tabellen, metadaten={"Geschäftsjahr": gj, "Quelle": "controlling_erp.db"})


@st.cache_data(show_spinner=False)
def powerbi_export(db: str, bsc: pd.DataFrame | None) -> bytes:
    return erstelle_powerbi_paket(db, bsc=bsc)


# =============================================================================
# Darstellungshilfen
# =============================================================================


def badge(ampel: str, text: str | None = None) -> str:
    farbe = {"rot": "red", "gelb": "orange", "grün": "green"}.get(ampel, "gray")
    return f":{farbe}-background[● {text or ampel}]"


@dataclass(frozen=True)
class Modul:
    """Ein Dashboard-Tab mit seiner betriebswirtschaftlichen Leitfrage."""

    tab: str
    ebene: str
    quelle: str
    frage: str
    beschreibung: str
    zielgruppe: str


MODULE = {
    "cockpit": Modul(
        "📈 Management-Cockpit", "Zahlen", "ERP-/SAP-Export (hier synthetisch)",
        "Wo weichen wir vom Plan ab – und warum?",
        "Plan-Ist-Vergleich, Deckungsbeiträge und Kostenstellen aus den ERP-Daten, mit Ampellogik und einem "
        "Management-Bericht, dessen Zahlen automatisch geprüft werden.",
        "Controlling und Geschäftsführung."),
    "prozess": Modul(
        "🔄 Process Mining", "Abläufe", "Event-Logs des Einkaufsprozesses",
        "Wo verlieren wir im Prozess Zeit?",
        "Wertet die Event-Logs des Einkaufsprozesses (Purchase-to-Pay) aus: Durchlaufzeiten, Engpässe und "
        "Nacharbeit. Daraus leitet der Agent BPMN-basierte Verbesserungsvorschläge ab.",
        "Prozessmanagement, Einkauf und Revision."),
    "bsc": Modul(
        "🧭 BSC-Textanalyse", "Texte", "Geschäftsberichte, Projektbeschreibungen",
        "Welche Digitalisierungsprojekte zahlen auf welche Unternehmensziele ein?",
        "Liest Berichtstexte, erkennt Digitalisierungsprojekte und ordnet sie den vier Perspektiven der "
        "Balanced Scorecard zu, mit Belegzitat und Konfidenz. Die KI schlägt vor, der Mensch entscheidet.",
        "Strategie und digitale Transformation."),
    "sql": Modul(
        "💬 KI-Analyst (Text-to-SQL)", "Zahlen", "Controlling-Datenbank",
        "Wie bekommen Entscheider verlässliche Antworten ohne BI-Wartezeit?",
        "Beantwortet Fragen auf Deutsch direkt aus der Controlling-Datenbank: Die KI schreibt die SQL-Abfrage, "
        "vier Sicherheitsschichten erlauben nur Lesezugriffe, und jede Zahl der Antwort wird geprüft.",
        "Fachbereiche ohne SQL-Kenntnisse."),
}


def aufbau_tabelle() -> str:
    """Markdown-Übersicht: welcher Tab welche Leitfrage beantwortet."""
    zeilen = ["| Informationsebene | Quelle im Unternehmen | Tab | Leitfrage |", "|---|---|---|---|"]
    zeilen += [f"| **{m.ebene}** | {m.quelle} | {m.tab} | „{m.frage}“ |" for m in MODULE.values()]
    return "\n".join(zeilen)


def leitfrage(modul: str) -> None:
    """Kopf jedes Tabs: die betriebswirtschaftliche Frage, was das Modul tut und für wen."""
    m = MODULE[modul]
    st.markdown(f"##### 🎯 {m.frage}")
    st.caption(f"{m.beschreibung} **Für:** {m.zielgruppe}")


def pruefstatus(antwort: AgentAnswer) -> None:
    if antwort.ist_faktentreu:
        st.success("Faktenprüfung bestanden: Jede Zahl der Antwort ist in den Quelldaten belegt.", icon="✅")
    else:
        st.warning(
            "Faktenprüfung: Diese Zahlen sind nicht in den Quelldaten belegt und dürfen so nicht übernommen "
            f"werden: **{', '.join(antwort.unverifizierte_zahlen)}**",
            icon="⚠️",
        )


def plotly_layout(fig, hoehe: int = 360):
    fig.update_layout(height=hoehe, margin=dict(l=10, r=10, t=40, b=10), legend_title_text="",
                      font=dict(family="sans-serif", size=13))
    return fig


def ki_fehler(exc: Exception) -> None:
    st.error(f"Die KI-Anfrage ist fehlgeschlagen: {exc}")
    st.caption("Tipp: In der Seitenleiste den Demo-Modus aktivieren, um die App ohne API-Zugang zu testen.")


# =============================================================================
# Kopf und Seitenleiste
# =============================================================================

db = stelle_daten_sicher(str(DB_PATH))

with st.sidebar:
    st.title("ControllAI")
    st.caption("KI-gestützter Data- und Controlling-Agent")
    live_moeglich = not demo_erzwungen() and not agent_holen(False)[1].demo
    demo_gewaehlt = st.toggle("Demo-Modus verwenden", value=False, key="demo_toggle") if live_moeglich else True
    agent, modus = agent_holen(demo_gewaehlt)
    if modus.demo:
        hinweis = ("Manuell aktiviert." if demo_erzwungen() or demo_gewaehlt is True and live_moeglich
                   else "Kein API-Schlüssel gefunden – alle Funktionen sind mit Muster-Antworten nutzbar.")
        st.info(f"**{modus.bezeichnung}**\n\n{hinweis}", icon="🧪")
        if modus.grund and not demo_erzwungen() and not live_moeglich:
            st.caption(f"Technischer Grund: {modus.grund}")
    else:
        st.success(f"**{modus.bezeichnung}**", icon="🔌")
    jahre = sorted(tabelle(db, "umsatzplanung")["geschaeftsjahr"].unique().tolist(), reverse=True)
    gj = st.selectbox("Geschäftsjahr", jahre, index=0, key="geschaeftsjahr")
    st.divider()
    export_bereich = st.container()
    st.divider()
    st.caption("Synthetische Daten der fiktiven ControllAI Demo GmbH · Methodik nach der Bachelorarbeit "
               "„Digitalisierungsprojekte der DAX-40-Unternehmen“ (Balanced Scorecard)")

st.title("ControllAI – Entscheidungsplattform")
st.caption(f"ControllAI Demo GmbH · Geschäftsjahr {gj} · Alle Kennzahlen deterministisch berechnet, "
           "KI-Texte automatisch gegen die Quelldaten geprüft")
st.markdown("**Drei Informationsebenen, eine Entscheidungsgrundlage:** Zahlen aus dem ERP · Abläufe aus "
            "Event-Logs · Texte aus Berichten")
with st.expander("So ist die App aufgebaut: Welcher Tab beantwortet welche Frage?", expanded=True):
    st.markdown(aufbau_tabelle())
if modus.demo:
    st.info(
        "**Demo-Modus:** Die KI-Texte stammen aus regelbasierten Muster-Antworten. Datenbankabfragen, SQL-Guard, "
        "Kennzahlen, Pydantic-Validierung sowie Zahlen- und Zitatprüfung laufen vollständig echt.",
        icon="🧪",
    )

tab_cockpit, tab_prozess, tab_bsc, tab_sql = st.tabs([m.tab for m in MODULE.values()])


# =============================================================================
# Tab 1: Management-Cockpit
# =============================================================================

with tab_cockpit:
    leitfrage("cockpit")
    f = fakten(db, gj)
    tx = tabelle(db, "umsatz_transaktionen")
    plan = tabelle(db, "umsatzplanung")
    steuerung = tx[tx["ist_ausreisser"] == 0]

    plan_ampel = plan_ist_abweichung(
        pd.DataFrame({"plan_eur": [f["plan_umsatz_eur"]], "ist_eur": [f["umsatz_eur"]]}), art="umsatz"
    ).iloc[0]["ampel"]
    gk_ampel = plan_ist_abweichung(
        pd.DataFrame({"plan_eur": [f["gemeinkosten_plan_eur"]], "ist_eur": [f["gemeinkosten_ist_eur"]]}), art="kosten"
    ).iloc[0]["ampel"]
    ur = f["umsatzrentabilitaet_pct"]
    ur_ampel = "grün" if ur >= 8 else "gelb" if ur >= 5 else "rot"
    marge_vj = (f["db_ii_vorjahr_eur"] / f["umsatz_vorjahr_eur"] * 100) if f.get("umsatz_vorjahr_eur") else None

    k = st.columns(5)
    with k[0]:
        st.metric("Umsatz Ist (Mio. €)", de(f["umsatz_eur"] / 1e6, 1),
                  delta=f"{vz(f['umsatz_yoy_pct'])} % zum Vorjahr" if f.get("umsatz_yoy_pct") is not None else None)
    with k[1]:
        st.metric("Abweichung zum Plan", f"{vz(f['abweichung_umsatz_zum_plan_pct'])} %",
                  delta=f"Plan {de(f['plan_umsatz_eur'] / 1e6, 1)} Mio. €", delta_color="off")
        st.markdown(badge(plan_ampel))
    with k[2]:
        st.metric("DB-II-Marge", f"{de(f['db_ii_marge_pct'], 1)} %",
                  delta=f"{vz(f['db_ii_marge_pct'] - marge_vj)} Pp. zum Vorjahr" if marge_vj else None)
    with k[3]:
        st.metric("Umsatzrentabilität", f"{de(ur, 1)} %",
                  delta=f"Ergebnis {de(f['betriebsergebnis_eur'] / 1e6, 2)} Mio. €", delta_color="off")
        st.markdown(badge(ur_ampel, f"{ur_ampel} · Ziel ≥ 8 %"))
    with k[4]:
        st.metric("Gemeinkosten vs. Budget", f"{vz(f['gemeinkosten_abweichung_pct'])} %",
                  delta=f"Ist {de(f['gemeinkosten_ist_eur'] / 1e6, 2)} Mio. €", delta_color="off")
        st.markdown(badge(gk_ampel))

    st.subheader("Plan-Ist-Abweichung Umsatz")
    up = umsatz_plan_ist(steuerung, plan)
    links, rechts = st.columns([1.15, 1])
    with links:
        matrix = up[up["geschaeftsjahr"] == gj].pivot(index="produktkategorie", columns="quartal",
                                                       values="abweichung_pct")
        fig = px.imshow(matrix, text_auto=".1f", color_continuous_scale="RdYlGn", zmin=-20, zmax=20, aspect="auto",
                        labels=dict(color="Abw. %", x="", y=""))
        fig.update_layout(title=f"Abweichung zum Plan in % · {gj}")
        st.plotly_chart(plotly_layout(fig), width="stretch")
    with rechts:
        kategorien = sorted(up["produktkategorie"].unique())
        kat = st.selectbox("Produktkategorie", kategorien, index=kategorien.index("Sensorik"), key="kat_plan_ist")
        verlauf = up[up["produktkategorie"] == kat].assign(periode=lambda d: d["geschaeftsjahr"].astype(str) + "-"
                                                            + d["quartal"])
        lang = verlauf.melt(id_vars=["periode", "ampel"], value_vars=["plan_umsatz_eur", "ist_umsatz_eur"],
                            var_name="Wert", value_name="Euro")
        lang["Wert"] = lang["Wert"].map({"plan_umsatz_eur": "Plan", "ist_umsatz_eur": "Ist"})
        fig = px.bar(lang, x="periode", y="Euro", color="Wert", barmode="group",
                     color_discrete_map={"Plan": NEUTRAL, "Ist": PRIMAER}, title=f"{kat}: Plan und Ist je Quartal")
        fig.update_layout(xaxis_title="", yaxis_title="Umsatz in €")
        st.plotly_chart(plotly_layout(fig), width="stretch")
        schlecht = verlauf.sort_values("abweichung_pct").iloc[0]
        st.markdown(f"Größte Abweichung: **{schlecht['periode']}** mit **{de(schlecht['abweichung_pct'], 1)} %** "
                    + badge(schlecht["ampel"]))

    st.subheader("Kosten und Deckungsbeiträge")
    links, rechts = st.columns(2)
    with links:
        kosten = tabelle(db, "plan_ist_finanzen").merge(tabelle(db, "kostenstellen")[["kostenstelle", "bezeichnung"]],
                                                        on="kostenstelle")
        kj = plan_ist_abweichung(kosten[kosten["geschaeftsjahr"] == gj], art="kosten")
        kj["position"] = kj["bezeichnung"] + " · " + kj["kostenart"] + " · " + kj["quartal"]
        top = kj.reindex(kj["abweichung_pct"].abs().sort_values(ascending=False).index).head(10)
        fig = px.bar(top.sort_values("abweichung_pct"), x="abweichung_pct", y="position", orientation="h",
                     color="ampel", color_discrete_map=AMPELFARBEN, title=f"Größte Kostenabweichungen {gj} (in %)",
                     hover_data={"plan_eur": ":,.0f", "ist_eur": ":,.0f"})
        fig.update_layout(xaxis_title="Abweichung Ist zu Plan in %", yaxis_title="")
        st.plotly_chart(plotly_layout(fig, 400), width="stretch")
    with rechts:
        dbr = deckungsbeitragsrechnung(steuerung, tabelle(db, "produktkategorien"),
                                       gruppen=["geschaeftsjahr", "produktkategorie"])
        dbj = dbr[dbr["geschaeftsjahr"] == gj].sort_values("db_ii_eur", ascending=False)
        fig = px.bar(dbj, x="produktkategorie", y=["db_i_eur", "db_ii_eur"], barmode="group",
                     color_discrete_sequence=[NEUTRAL, PRIMAER], title=f"Deckungsbeitrag I und II {gj}")
        fig.for_each_trace(lambda t: t.update(name={"db_i_eur": "DB I", "db_ii_eur": "DB II"}[t.name]))
        fig.update_layout(xaxis_title="", yaxis_title="Euro")
        st.plotly_chart(plotly_layout(fig, 400), width="stretch")

    st.dataframe(
        dbj[["produktkategorie", "umsatz_eur", "db_i_eur", "db_i_marge_pct", "db_ii_eur", "db_ii_marge_pct"]],
        hide_index=True, width="stretch",
        column_config={
            "produktkategorie": "Produktkategorie",
            "umsatz_eur": st.column_config.NumberColumn("Umsatz (€)", format="localized"),
            "db_i_eur": st.column_config.NumberColumn("DB I (€)", format="localized"),
            "db_i_marge_pct": st.column_config.NumberColumn("DB-I-Marge", format="%.1f %%"),
            "db_ii_eur": st.column_config.NumberColumn("DB II (€)", format="localized"),
            "db_ii_marge_pct": st.column_config.NumberColumn("DB-II-Marge", format="%.1f %%"),
        },
    )

    st.subheader("Datenqualität")
    links, rechts = st.columns([1, 1.6])
    with links:
        st.markdown("**Bereinigung des SAP-Rohexports**")
        r = bereinigung(db, str(ROHEXPORT_PATH))
        if r:
            a, b = st.columns(2)
            a.metric("Zeilen im Rohexport", de(r["zeilen_eingang"]))
            b.metric("Nach Bereinigung", de(r["zeilen_ausgang"]))
            st.dataframe(
                pd.DataFrame(
                    [("Dubletten entfernt", r["duplikate_entfernt"]),
                     ("Summenzeilen/unvollständig entfernt", r["unvollstaendig_entfernt"]),
                     ("Fremdwährung ausgesondert", r["fremdwaehrung_entfernt"]),
                     ("Unbekannte Materialien", r["unbekannte_materialien"]),
                     ("Stornos (erhalten)", r["stornos"])],
                    columns=["Prüfschritt", "Zeilen"],
                ),
                hide_index=True, width="stretch",
            )
        else:
            st.caption("Kein Rohexport gefunden.")
    with rechts:
        aus = tx[(tx["ist_ausreisser"] == 1) & (tx["geschaeftsjahr"] == gj)]
        st.markdown(f"**Zur Prüfung markierte Ausreißer {gj}** – nicht gelöscht, aber aus den Kennzahlen ausgenommen")
        st.dataframe(
            aus[["faktura_nr", "buchungsdatum", "produktkategorie", "umsatz_eur", "ausreisser_score",
                 "ausreisser_grund"]],
            hide_index=True, width="stretch",
            column_config={
                "faktura_nr": "Faktura", "buchungsdatum": "Datum", "produktkategorie": "Kategorie",
                "umsatz_eur": st.column_config.NumberColumn("Betrag (€)", format="localized"),
                "ausreisser_score": st.column_config.NumberColumn("z-Wert", format="%.1f"),
                "ausreisser_grund": "Befund",
            },
        )
        st.caption("Methode: MAD-basierter z-Wert auf log-skalierten Beträgen je Produktkategorie, Schwelle 3,5.")

    st.subheader("Management-Bericht")
    st.caption("Der Agent formuliert aus der Faktenbasis eine Executive Summary; jede Zahl wird anschließend geprüft.")
    schluessel = f"bericht_{gj}_{modus.demo}"
    if st.button("Executive Summary erzeugen", type="primary", key="btn_bericht"):
        with st.spinner("Bericht wird erstellt …"):
            try:
                st.session_state[schluessel] = agent.generate_executive_summary(f, focus=f"Geschäftsjahr {gj}")
            except (AgentError, LLMError) as exc:
                ki_fehler(exc)
    if schluessel in st.session_state:
        bericht: AgentAnswer = st.session_state[schluessel]
        pruefstatus(bericht)
        with st.container(border=True):
            st.markdown(bericht.antwort)
    with st.expander("Faktenbasis anzeigen (Eingabe für den Agenten)"):
        st.json(f)


# =============================================================================
# Tab 2: Process Mining
# =============================================================================

with tab_prozess:
    leitfrage("prozess")
    pa = prozess(str(EVENTLOG_PATH))
    pf = pa.to_facts()
    eng = pf["engpaesse"][0]

    k = st.columns(5)
    k[0].metric("Fälle (Bestellungen)", pf["anzahl_faelle"])
    k[1].metric("Prozessvarianten", pf["anzahl_varianten"])
    k[2].metric("Durchlaufzeit (Median)", f"{de(pf['durchlaufzeit_tage']['median'], 1)} Tage")
    k[3].metric("Durchlaufzeit (P90)", f"{de(pf['durchlaufzeit_tage']['p90'], 1)} Tage")
    k[4].metric("Nacharbeitsquote", f"{de(pf['nacharbeit']['quote_pct'], 1)} %")

    st.error(
        f"**Engpass: {eng['uebergang']}** – {de(eng['anteil_an_gesamtzeit_pct'], 1)} % der gesamten "
        f"Durchlaufzeit, Median {de(eng['median_stunden'], 1)} Stunden, bei 10 % der Fälle über "
        f"{de(eng['p90_stunden'], 0)} Stunden. Jeder Übergang ist eine Übergabe an eine andere Abteilung.",
        icon="⏱️",
    )

    links, rechts = st.columns(2)
    with links:
        ue = pa.uebergaenge.head(8).copy()
        ue["Übergang"] = ue["von"] + " → " + ue["nach"]
        ue["Rolle"] = ["Engpass" if i == 0 else "weitere Übergänge" for i in range(len(ue))]
        fig = px.bar(ue.iloc[::-1], x="anteil_gesamtzeit_pct", y="Übergang", orientation="h", color="Rolle",
                     color_discrete_map={"Engpass": AMPELFARBEN["rot"], "weitere Übergänge": NEUTRAL},
                     title="Anteil an der gesamten Durchlaufzeit (%)",
                     hover_data={"median_h": ":.1f", "p90_h": ":.1f", "anzahl": True})
        fig.update_layout(xaxis_title="% der Durchlaufzeit", yaxis_title="", showlegend=False)
        fig.update_yaxes(tickfont=dict(size=11))
        st.plotly_chart(plotly_layout(fig, 420), width="stretch")
    with rechts:
        fig = px.histogram(pa.faelle, x="durchlaufzeit_tage", nbins=30, color_discrete_sequence=[PRIMAER],
                           title="Verteilung der Durchlaufzeiten")
        fig.add_vline(x=pf["durchlaufzeit_tage"]["median"], line_dash="dash", line_color=AMPELFARBEN["rot"],
                      annotation_text="Median")
        fig.update_layout(xaxis_title="Tage je Bestellung", yaxis_title="Fälle")
        st.plotly_chart(plotly_layout(fig, 420), width="stretch")

    st.subheader("Prozesslandkarte")
    links, rechts = st.columns([1, 1])
    with links:
        st.caption("Kanten: Anzahl Übergänge und Median der Übergangszeit; rot = Engpass.")
        max_anteil = pa.uebergaenge["anteil_gesamtzeit_pct"].max()
        kanten = []
        for i, row in pa.uebergaenge.iterrows():
            farbe = AMPELFARBEN["rot"] if i == 0 else "#5B6B7F"
            breite = 1 + 4 * row["anteil_gesamtzeit_pct"] / max_anteil
            kanten.append(f'"{row["von"]}" -> "{row["nach"]}" [label="{row["anzahl"]}× · {row["median_h"]:.0f} h", '
                          f'color="{farbe}", fontcolor="{farbe}", penwidth={breite:.1f}];')
        st.graphviz_chart(
            "digraph P2P { rankdir=TB; nodesep=0.4; ranksep=0.3; bgcolor=transparent; "
            'node [shape=box, style="rounded,filled", fillcolor="#EEF2F7", color="#1F3A5F", fontname="Helvetica", '
            'fontsize=11]; edge [fontname="Helvetica", fontsize=9]; ' + " ".join(kanten) + " }",
            width="content",
        )
    with rechts:
        st.caption("Übergänge nach Anteil an der gesamten Durchlaufzeit")
        top_ue = pa.uebergaenge.head(6).assign(uebergang=lambda d: d["von"] + " → " + d["nach"])
        st.dataframe(
            top_ue[["uebergang", "anzahl", "median_h", "p90_h", "anteil_gesamtzeit_pct"]],
            hide_index=True, width="stretch",
            column_config={
                "uebergang": st.column_config.TextColumn("Übergang", width="medium"), "anzahl": "Anzahl",
                "median_h": st.column_config.NumberColumn("Median (h)", format="%.1f"),
                "p90_h": st.column_config.NumberColumn("P90 (h)", format="%.1f"),
                "anteil_gesamtzeit_pct": st.column_config.ProgressColumn("Anteil", format="%.1f %%", min_value=0,
                                                                        max_value=100),
            },
        )
        st.markdown(
            "**Lesart in BPMN:** Die Freigabe liegt in einer anderen *Lane* (Bereichsleitung) als die "
            "Rechnungsprüfung. Jede Übergabe zwischen Lanes erzeugt Wartezeit – deshalb setzen die Vorschläge "
            "unten an Gateway, Service Task und Timer-Ereignis an."
        )

    st.subheader("Prozessvarianten")
    st.dataframe(
        pa.varianten.head(8), hide_index=True, width="stretch",
        column_config={
            "variante": st.column_config.TextColumn("Variante", width="large"),
            "faelle": "Fälle",
            "median_durchlaufzeit_tage": st.column_config.NumberColumn("Median DLZ (Tage)", format="%.1f"),
            "anteil_pct": st.column_config.ProgressColumn("Anteil", format="%.1f %%", min_value=0, max_value=100),
        },
    )

    st.subheader("Optimierungsvorschläge (BPMN)")
    if st.button("BPMN-Vorschläge erzeugen", type="primary", key="btn_bpmn"):
        with st.spinner("Prozess wird analysiert …"):
            try:
                st.session_state[f"bpmn_{modus.demo}"] = agent.analyze_process(pf, process_name="Purchase-to-Pay")
            except (AgentError, LLMError) as exc:
                ki_fehler(exc)
    if f"bpmn_{modus.demo}" in st.session_state:
        vorschlag: AgentAnswer = st.session_state[f"bpmn_{modus.demo}"]
        pruefstatus(vorschlag)
        with st.container(border=True):
            st.markdown(vorschlag.antwort)


# =============================================================================
# Tab 3: BSC-Textanalyse & Human-in-the-Loop
# =============================================================================

with tab_bsc:
    leitfrage("bsc")
    st.markdown(
        "Digitalisierungsprojekte werden den vier Perspektiven der **Balanced Scorecard** zugeordnet – nach der "
        "*Zielsetzung*, nicht nach der Technologie. Das **Drei-Schichten-Prinzip** hält Originalzitat, "
        "KI-Vorschlag und menschliche Entscheidung getrennt; der KI-Vorschlag wird nie überschrieben."
    )
    quelle = st.radio("Textquelle", ["Beispieltexte (12 fiktive Berichtsauszüge)", "Eigene Datei (PDF oder TXT)"],
                      horizontal=True, key="bsc_quelle")
    dokumente: list[dict[str, Any]] = []

    if quelle.startswith("Beispiel"):
        optionen = {f"{t['id']} · {t['unternehmen']} ({t['berichtsjahr']})": t for t in PROJEKTTEXTE}
        auswahl = st.multiselect("Berichtsauszüge", list(optionen), default=list(optionen), key="bsc_auswahl")
        dokumente = [
            {"id": optionen[a]["id"], "text": optionen[a]["text"],
             "quelle": f"{optionen[a]['unternehmen']} {optionen[a]['berichtsjahr']}, S. {optionen[a]['seite']}",
             "referenz": optionen[a]["erwartung"]["perspektive"]}
            for a in auswahl
        ]
        with st.expander("Ausgewählte Texte lesen"):
            for d in dokumente:
                st.markdown(f"**{d['id']} · {d['quelle']}**")
                st.write(d["text"])
    else:
        datei = st.file_uploader("Geschäftsbericht oder Projektbeschreibung", type=["pdf", "txt"], key="bsc_upload")
        if datei is not None:
            try:
                seiten = extrahiere_pdf(datei.getvalue()) if datei.name.lower().endswith(".pdf") else \
                    text_zu_seiten(datei.getvalue().decode("utf-8", errors="replace"))
                funde = vorfiltern(seiten)
                ab = abdeckung(seiten, funde)
                a, b, c = st.columns(3)
                a.metric("Seiten", ab["seiten"])
                b.metric("Seiten mit Treffern", ab["seiten_mit_treffern"])
                c.metric("Fundstellen für die KI", ab["fundstellen"])
                with st.expander("Fundstellen der Vorfilterung (Suchdiktionär)"):
                    for fs in funde[:50]:
                        st.markdown(f"**S. {fs.seite}** · Treffer: {', '.join(fs.treffer)}")
                        st.caption(fs.text[:600])
                if funde:
                    dokumente = [{"id": "Upload", "text": fundstellen_text(funde), "quelle": datei.name,
                                  "referenz": None}]
                if modus.demo:
                    st.info("Im Demo-Modus sind Muster-Antworten nur für die Beispieltexte hinterlegt. Die "
                            "Vorfilterung oben läuft echt; für die Klassifikation eigener Dateien einen "
                            "API-Schlüssel eintragen.")
            except Exception as exc:  # Datei unlesbar, PDF verschlüsselt …
                st.error(f"Die Datei konnte nicht gelesen werden: {exc}")

    if st.button("KI-Analyse starten", type="primary", key="btn_bsc", disabled=not dokumente):
        zeilen = []
        with st.spinner("Texte werden klassifiziert …"):
            try:
                for d in dokumente:
                    antwort = agent.classify_bsc(d["text"], source=d["quelle"])
                    tabelle_bsc = bsc_tabelle(antwort.bsc_initiativen)
                    if not tabelle_bsc.empty:
                        tabelle_bsc.insert(0, "text_id", d["id"])
                        tabelle_bsc["referenz"] = d["referenz"]
                        zeilen.append(tabelle_bsc)
                st.session_state["bsc_basis"] = pd.concat(zeilen, ignore_index=True) if zeilen else pd.DataFrame()
                st.session_state["bsc_lauf"] = st.session_state.get("bsc_lauf", 0) + 1
            except (AgentError, LLMError) as exc:
                ki_fehler(exc)

    basis: pd.DataFrame | None = st.session_state.get("bsc_basis")
    if basis is not None and basis.empty:
        st.info("In den ausgewählten Texten wurden keine Digitalisierungsprojekte identifiziert.")
    elif basis is not None:
        st.subheader("Prüfen und entscheiden")
        st.caption("Spalte **Finale Perspektive** ändern, um den KI-Vorschlag zu überstimmen; **Geprüft** "
                   "anhaken, um die Entscheidung freizugeben. Markierte Fälle zuerst prüfen.")
        bearbeitet = st.data_editor(
            basis[["text_id", "titel", "ki_perspektive", "ki_technologiefeld", "ki_konfidenz", "zitat_verifiziert",
                   "pruefung_erforderlich", "finale_perspektive", "geprueft"]],
            key=f"bsc_editor_{st.session_state.get('bsc_lauf', 0)}",
            hide_index=True, width="stretch", height=36 * (len(basis) + 1) + 4,
            disabled=["text_id", "titel", "ki_perspektive", "ki_technologiefeld", "ki_konfidenz", "zitat_verifiziert",
                      "pruefung_erforderlich"],
            column_config={
                "text_id": "Text",
                "titel": st.column_config.TextColumn("Projekt", width="medium"),
                "ki_perspektive": "KI-Vorschlag",
                "ki_technologiefeld": "Technologiefeld",
                "ki_konfidenz": st.column_config.ProgressColumn("Konfidenz", format="%.2f", min_value=0,
                                                                max_value=1),
                "zitat_verifiziert": st.column_config.CheckboxColumn("Zitat belegt"),
                "pruefung_erforderlich": st.column_config.CheckboxColumn("Prüfen!"),
                "finale_perspektive": st.column_config.SelectboxColumn("Finale Perspektive", options=PERSPEKTIVEN,
                                                                       required=True),
                "geprueft": st.column_config.CheckboxColumn("Geprüft"),
            },
        )
        ergebnis = basis.copy()
        ergebnis["finale_perspektive"] = bearbeitet["finale_perspektive"].to_numpy()
        ergebnis["geprueft"] = bearbeitet["geprueft"].to_numpy()
        ergebnis["status"] = [
            ("überstimmt" if fin != ki else "bestätigt") if ok else "offen"
            for fin, ki, ok in zip(ergebnis["finale_perspektive"], ergebnis["ki_perspektive"], ergebnis["geprueft"],
                                   strict=True)
        ]
        st.session_state["bsc_ergebnis"] = ergebnis

        k = st.columns(5)
        k[0].metric("Projekte", len(ergebnis))
        k[1].metric("Zitate belegt", f"{int(ergebnis['zitat_verifiziert'].sum())} / {len(ergebnis)}")
        k[2].metric("Zur Prüfung markiert", int(ergebnis["pruefung_erforderlich"].sum()))
        k[3].metric("Bestätigt · überstimmt", f"{(ergebnis['status'] == 'bestätigt').sum()} · "
                                              f"{(ergebnis['status'] == 'überstimmt').sum()}")
        k[4].metric("Offen", int((ergebnis["status"] == "offen").sum()))

        links, rechts = st.columns([1, 1.3])
        with links:
            verteilung = ergebnis["finale_perspektive"].value_counts().reindex(PERSPEKTIVEN, fill_value=0)
            fig = px.bar(x=verteilung.index, y=verteilung.values, color_discrete_sequence=[PRIMAER],
                         title="Projekte je BSC-Perspektive (finale Zuordnung)")
            fig.update_layout(xaxis_title="", yaxis_title="Projekte")
            st.plotly_chart(plotly_layout(fig, 330), width="stretch")
            mit_ref = ergebnis[ergebnis["referenz"].notna()]
            if not mit_ref.empty:
                ki_treffer = (mit_ref["ki_perspektive"] == mit_ref["referenz"]).mean() * 100
                final_treffer = (mit_ref["finale_perspektive"] == mit_ref["referenz"]).mean() * 100
                st.markdown(
                    f"**Abgleich mit den Referenzlabels:** KI-Vorschlag {de(ki_treffer, 0)} % · nach "
                    f"menschlicher Prüfung {de(final_treffer, 0)} % ({len(mit_ref)} Projekte)"
                )
        with rechts:
            st.markdown("**Prüfansicht: Originalzitat → KI-Vorschlag → Entscheidung**")
            projekt = st.selectbox(
                "Projekt", ergebnis.index,
                format_func=lambda i: ("⚠️ " if ergebnis.loc[i, "pruefung_erforderlich"] else "") +
                f"{ergebnis.loc[i, 'text_id']} · {ergebnis.loc[i, 'titel']}",
                key="bsc_projekt",
            )
            p = ergebnis.loc[projekt]
            originaltext = next((d["text"] for d in dokumente if d["id"] == p["text_id"]), "")
            with st.container(border=True):
                st.markdown("**① Originalzitat**")
                if originaltext and p["belegzitat"] in originaltext:
                    st.markdown(originaltext.replace(p["belegzitat"], f":orange-background[{p['belegzitat']}]"))
                else:
                    st.markdown(f"> {p['belegzitat']}")
                if p["zitat_verifiziert"]:
                    st.success("Zitat wörtlich im Originaltext gefunden.", icon="✅")
                else:
                    st.error("Zitat nicht wörtlich im Originaltext – möglicherweise umformuliert. Bitte prüfen.",
                             icon="❌")
                st.markdown("**② KI-Vorschlag (unverändert gespeichert)**")
                st.markdown(f"{p['ki_perspektive']} · {p['ki_technologiefeld']} · Konfidenz "
                            f"{de(p['ki_konfidenz'] * 100, 0)} %  \n*{p['ki_begruendung']}*")
                st.markdown("**③ Menschliche Entscheidung**")
                st.markdown(f"{p['finale_perspektive']} · Status: **{p['status']}**")


# =============================================================================
# Tab 4: Text-to-SQL & KI-Analyst
# =============================================================================


def frage_setzen(text: str) -> None:
    st.session_state["frage"] = text
    st.session_state["frage_ausfuehren"] = True


with tab_sql:
    leitfrage("sql")
    st.markdown("**Beispielfragen**")
    kurztitel = {"abweichung": "Plan-Ist-Abweichung Q3 2025", "kosten": "Budgetüberschreitungen 2025",
                 "yoy": "Umsatz zum Vorjahr je Quartal", "marge": "Höchste DB-Marge 2025",
                 "ausreisser": "Markierte Ausreißer"}
    beispiele = [d for d in DEMO_FRAGEN if d.id != "angriff"]
    angriff = next(d for d in DEMO_FRAGEN if d.id == "angriff")
    spalten = st.columns(3)
    for i, demo in enumerate(beispiele):
        spalten[i % 3].button(kurztitel.get(demo.id, demo.frage), key=f"bsp_{demo.id}", on_click=frage_setzen,
                              args=(demo.frage,), help=demo.frage, width="stretch")
    spalten[len(beispiele) % 3].button("🛡️ Sicherheitstest: Daten löschen", key="bsp_angriff",
                                       on_click=frage_setzen, args=(angriff.frage,), help=angriff.frage,
                                       width="stretch")

    with st.form("frageformular", border=False):
        st.text_input("Ihre Frage", key="frage", placeholder="z. B. Welche Kostenstellen haben 2025 ihr Budget "
                                                             "am stärksten überschritten?")
        gesendet = st.form_submit_button("Frage stellen", type="primary")
    if gesendet:
        st.session_state["frage_ausfuehren"] = True

    if st.session_state.pop("frage_ausfuehren", False) and st.session_state.get("frage", "").strip():
        frage = st.session_state["frage"].strip()
        with st.spinner("Abfrage wird erzeugt, geprüft und ausgeführt …"):
            try:
                st.session_state["sql_ergebnis"] = ("ok", frage, agent.ask(frage, intent=Intent.TEXT_TO_SQL))
            except SQLGuardError as exc:
                st.session_state["sql_ergebnis"] = ("blockiert", frage, exc)
            except (AgentError, LLMError) as exc:
                st.session_state["sql_ergebnis"] = ("fehler", frage, exc)

    if "sql_ergebnis" in st.session_state:
        art, frage, inhalt = st.session_state["sql_ergebnis"]
        st.markdown(f"#### {frage}")
        if art == "blockiert":
            st.error(f"**Vom SQL-Guard blockiert:** {inhalt}", icon="🛡️")
            if getattr(inhalt, "sql", None):
                st.code(inhalt.sql, language="sql")
            st.caption("Sicherheitsverstöße werden protokolliert und bewusst nicht zur „Korrektur“ an das "
                       "Sprachmodell zurückgegeben. Die Datenbank ist unverändert.")
        elif art == "fehler":
            ki_fehler(inhalt)
        else:
            antwort: AgentAnswer = inhalt
            with st.container(border=True):
                st.markdown(antwort.antwort)
            if antwort.sql_result is not None:
                pruefstatus(antwort)
                links, rechts = st.columns([1, 1.2])
                with links:
                    st.markdown("**Ausgeführte Read-Only-Abfrage**")
                    st.code(antwort.sql_result.sql, language="sql")
                    st.caption(f"{antwort.sql_result.row_count} Zeilen · {de(antwort.sql_result.duration_ms, 1)} ms"
                               + (" · Ergebnis gekürzt" if antwort.sql_result.truncated else ""))
                with rechts:
                    st.markdown("**Ergebnis**")
                    st.dataframe(pd.DataFrame(antwort.sql_result.rows, columns=antwort.sql_result.columns),
                                 hide_index=True, width="stretch")
            for warnung in antwort.warnungen:
                st.caption(f"Hinweis: {warnung}")
            with st.expander("Arbeitsschritte des Agenten"):
                for schritt in antwort.trace:
                    st.markdown(f"- {schritt}")

    with st.expander("Warum ist das sicher? – vier Schutzschichten"):
        st.markdown(
            "1. **Statische Prüfung:** genau eine Anweisung, nur `SELECT`/`WITH`, keine Kommentare, keine "
            "schreibenden Schlüsselwörter\n"
            "2. **Read-Only-Verbindung:** Datenbank im Modus `mode=ro`, zusätzlich `PRAGMA query_only`\n"
            "3. **SQLite-Authorizer:** Allowlist – nur Lesen, Auswählen und unkritische Funktionen\n"
            "4. **Ressourcenlimits:** Zeitlimit je Abfrage und begrenzte Zeilenzahl\n\n"
            "Danach prüft die Faktenprüfung jede Zahl der formulierten Antwort gegen das Abfrageergebnis."
        )


# =============================================================================
# Export (Seitenleiste, am Ende, damit die BSC-Entscheidungen enthalten sind)
# =============================================================================

with export_bereich:
    st.subheader("Export")
    bsc_ergebnis: pd.DataFrame | None = st.session_state.get("bsc_ergebnis")
    r = bereinigung(db, str(ROHEXPORT_PATH))
    st.download_button(
        "Excel-Report (.xlsx)",
        data=excel_export(db, gj, bsc_ergebnis, json.dumps(r) if r else None),
        file_name=f"ControllAI_Report_{gj}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch", key="dl_excel",
    )
    st.download_button(
        "Power-BI-Paket (CSV, .zip)",
        data=powerbi_export(db, bsc_ergebnis),
        file_name="ControllAI_PowerBI.zip",
        mime="application/zip",
        width="stretch", key="dl_powerbi",
    )
    st.caption("Excel: Plan-Ist, Deckungsbeiträge, Ausreißer, Transaktionen"
               + (", BSC-Entscheidungen" if bsc_ergebnis is not None else "")
               + ". Power BI: Sternschema mit Anleitung.")
