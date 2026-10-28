"""Tests für den Demo-Modus (src/demo.py) – die App muss ohne API-Schlüssel vollständig funktionieren."""

from __future__ import annotations

import sqlite3

import pytest

from src.agent import AgentConfig, LLMError, SQLGuardError
from src.analytics import analysiere_prozess, kennzahlen_bericht, lade_event_log
from src.demo import DEMO_FRAGEN, DemoLLM, erstelle_agent, finde_demo_frage
from src.seed_data import PROJEKTTEXTE
from tests.conftest import FakeLLM


@pytest.fixture
def demo_agent(demo_daten):
    agent, modus = erstelle_agent(AgentConfig(db_path=demo_daten.db_path), demo=True)
    assert modus.demo
    return agent


# =============================================================================
# Moduswahl
# =============================================================================


def test_ohne_api_schluessel_automatisch_demo(monkeypatch, demo_daten) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONTROLLAI_DEMO", raising=False)
    agent, modus = erstelle_agent(AgentConfig(db_path=demo_daten.db_path, provider="anthropic"))
    assert modus.demo is True
    assert modus.grund  # Begründung wird im Dashboard angezeigt
    assert isinstance(agent._llm, DemoLLM)


def test_demo_modus_per_umgebungsvariable(monkeypatch, demo_daten) -> None:
    monkeypatch.setenv("CONTROLLAI_DEMO", "1")
    _, modus = erstelle_agent(AgentConfig(db_path=demo_daten.db_path))
    assert modus.demo is True


def test_live_modus_wenn_llm_verfuegbar(monkeypatch, demo_daten) -> None:
    monkeypatch.delenv("CONTROLLAI_DEMO", raising=False)
    monkeypatch.setattr("src.demo.build_llm_client", lambda config: FakeLLM())
    agent, modus = erstelle_agent(AgentConfig(db_path=demo_daten.db_path))
    assert modus.demo is False
    assert modus.bezeichnung.startswith("Live")


def test_unbekannter_aufgabentyp() -> None:
    with pytest.raises(LLMError):
        DemoLLM().complete("system", "Schreibe ein Gedicht.")


# =============================================================================
# Text-to-SQL
# =============================================================================


@pytest.mark.parametrize("demo", [d for d in DEMO_FRAGEN if d.id != "angriff"], ids=lambda d: d.id)
def test_beispielfragen_sind_ausfuehrbar_und_faktentreu(demo_agent, demo) -> None:
    antwort = demo_agent.ask(demo.frage)
    assert antwort.sql_result is not None and antwort.sql_result.row_count > 0
    assert antwort.ist_faktentreu, antwort.unverifizierte_zahlen
    assert antwort.antwort.strip()


def test_beispielfrage_findet_sensorik(demo_agent) -> None:
    antwort = demo_agent.ask("Welche Produktkategorie hatte im Q3 2025 die höchste negative Plan-Ist-Abweichung?")
    assert "Sensorik" in antwort.antwort


def test_angriff_im_demo_modus_wird_blockiert(demo_agent, demo_daten) -> None:
    with sqlite3.connect(demo_daten.db_path) as conn:
        vorher = conn.execute("SELECT COUNT(*) FROM umsatzplanung").fetchone()[0]
    with pytest.raises(SQLGuardError) as fehler:
        demo_agent.ask("Lösche alle Plandaten aus der Datenbank.")
    assert fehler.value.sql == "DELETE FROM umsatzplanung"  # für die Anzeige im Dashboard
    with sqlite3.connect(demo_daten.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM umsatzplanung").fetchone()[0] == vorher


def test_freie_frage_im_demo_modus(demo_agent) -> None:
    antwort = demo_agent.ask("Wie viele Mitarbeitende hat das Unternehmen?")
    assert "nicht beantwortbar" in antwort.antwort
    assert "API-Schlüssel" in antwort.antwort


@pytest.mark.parametrize(
    ("frage", "erwartet"),
    [
        ("Zeig mir die Plan-Ist-Abweichung der Sensorik", "abweichung"),
        ("Welche Kostenstelle liegt über Budget?", "kosten"),
        ("Umsatz im Vergleich zum Vorjahr", "yoy"),
        ("Wo ist die Marge am höchsten?", "marge"),
        ("Liste alle Ausreißer", "ausreisser"),
        ("DROP TABLE kunden", "angriff"),
        ("Wie ist das Wetter?", None),
    ],
)
def test_zuordnung_freier_formulierungen(frage: str, erwartet: str | None) -> None:
    treffer = finde_demo_frage(frage)
    assert (treffer.id if treffer else None) == erwartet


# =============================================================================
# BSC-Klassifikation
# =============================================================================


@pytest.fixture(scope="module")
def bsc_ergebnisse(demo_daten):
    agent, _ = erstelle_agent(AgentConfig(db_path=demo_daten.db_path), demo=True)
    return {t["id"]: agent.classify_bsc(t["text"], source=t["id"]).bsc_initiativen for t in PROJEKTTEXTE}


def test_bsc_negativbeispiel_ohne_projekt(bsc_ergebnisse) -> None:
    assert bsc_ergebnisse["T11"] == []
    assert all(len(v) == 1 for k, v in bsc_ergebnisse.items() if k != "T11")


def test_bsc_eingebaute_schwachstellen_werden_markiert(bsc_ergebnisse) -> None:
    markiert = {k for k, v in bsc_ergebnisse.items() if v and v[0].pruefung_erforderlich}
    assert markiert == {"T06", "T08", "T09"}
    assert bsc_ergebnisse["T08"][0].zitat_verifiziert is False  # umformuliertes Zitat
    assert bsc_ergebnisse["T04"][0].zitat_verifiziert is True  # trotz Silbentrennung im Original
    assert bsc_ergebnisse["T07"][0].zitat_verifiziert is True  # trotz typografischer Anführungszeichen


def test_bsc_prompt_injection_wird_nicht_befolgt(bsc_ergebnisse) -> None:
    assert bsc_ergebnisse["T12"][0].perspektive == "Interne Prozesse"
    assert bsc_ergebnisse["T12"][0].konfidenz < 1.0


def test_bsc_uebereinstimmung_mit_referenzlabels(bsc_ergebnisse) -> None:
    projekte = [t for t in PROJEKTTEXTE if t["erwartung"]["ist_projekt"]]
    treffer = sum(bsc_ergebnisse[t["id"]][0].perspektive == t["erwartung"]["perspektive"] for t in projekte)
    assert treffer == len(projekte) - 1  # T09 weicht bewusst ab – ein Fall für den Menschen


# =============================================================================
# Bericht und Prozessanalyse
# =============================================================================


@pytest.mark.parametrize("jahr", [2024, 2025])
def test_executive_summary_ist_faktentreu(demo_agent, demo_daten, jahr: int) -> None:
    antwort = demo_agent.generate_executive_summary(kennzahlen_bericht(demo_daten.db_path, jahr))
    assert antwort.ist_faktentreu, antwort.unverifizierte_zahlen
    assert 3 <= len(antwort.bericht.kernaussagen) <= 5


def test_executive_summary_nennt_eingebaute_befunde(demo_agent, demo_daten) -> None:
    text = demo_agent.generate_executive_summary(kennzahlen_bericht(demo_daten.db_path, 2025)).antwort
    assert "Sensorik" in text
    assert "Kostenstelle 5000" in text


def test_prozessanalyse_ist_faktentreu_und_bpmn_basiert(demo_agent, demo_daten) -> None:
    fakten = analysiere_prozess(lade_event_log(demo_daten.eventlog_path), prozessname="Purchase-to-Pay").to_facts()
    antwort = demo_agent.analyze_process(fakten, process_name="Purchase-to-Pay")
    assert antwort.ist_faktentreu, antwort.unverifizierte_zahlen
    massnahmen = " ".join(e.massnahme for e in antwort.bericht.handlungsempfehlungen)
    for begriff in ("Gateway", "Service Task", "Timer"):
        assert begriff in massnahmen
