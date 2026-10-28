"""Oberflächentests des Dashboards mit Streamlits AppTest – im Demo-Modus, ohne Browser."""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SUBMIT = "FormSubmitter:frageformular-Frage stellen"


@pytest.fixture
def app(monkeypatch, demo_daten) -> AppTest:
    monkeypatch.setenv("CONTROLLAI_DEMO", "1")
    monkeypatch.setenv("DB_PATH", str(demo_daten.db_path))
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_app_startet_im_demo_modus(app: AppTest) -> None:
    assert len(app.tabs) == 4
    assert any("Demo-Modus" in i.value for i in app.info)
    labels = [m.label for m in app.metric]
    for erwartet in ("Umsatz Ist (Mio. €)", "Abweichung zum Plan", "DB-II-Marge", "Durchlaufzeit (Median)"):
        assert erwartet in labels


def test_jeder_tab_nennt_seine_leitfrage(app: AppTest) -> None:
    texte = " ".join(m.value for m in app.markdown)
    for frage in (
        "Wo weichen wir vom Plan ab – und warum?",
        "Wo verlieren wir im Prozess Zeit?",
        "Welche Digitalisierungsprojekte zahlen auf welche Unternehmensziele ein?",
        "Wie bekommen Entscheider verlässliche Antworten ohne BI-Wartezeit?",
    ):
        assert f"🎯 {frage}" in texte  # Kopf des Tabs
        assert f"„{frage}“" in texte  # Übersichtstabelle oben
    assert len(app.expander) >= 1 and "Welcher Tab beantwortet welche Frage" in app.expander[0].label


def test_engpass_wird_hervorgehoben(app: AppTest) -> None:
    assert any("Engpass: Rechnungsprüfung → Rechnung freigeben" in e.value for e in app.error)


def test_executive_summary_und_bpmn(app: AppTest) -> None:
    app.button(key="btn_bericht").click().run()
    app.button(key="btn_bpmn").click().run()
    assert not app.exception
    assert len([s for s in app.success if "Faktenprüfung bestanden" in s.value]) == 2
    assert any("Management-Bericht Geschäftsjahr 2025" in m.value for m in app.markdown)


def test_bsc_human_in_the_loop(app: AppTest) -> None:
    app.button(key="btn_bsc").click().run()
    assert not app.exception
    werte = {m.label: m.value for m in app.metric}
    assert werte["Projekte"] == "11"
    assert werte["Zitate belegt"] == "10 / 11"
    assert werte["Zur Prüfung markiert"] == "3"
    assert werte["Offen"] == "11"  # ohne menschliche Freigabe ist nichts entschieden


def test_text_to_sql_beispielfrage(app: AppTest) -> None:
    app.button(key="bsp_abweichung").click().run()
    assert not app.exception
    assert any("Sensorik" in m.value and "unter Plan" in m.value for m in app.markdown)
    assert any("Faktenprüfung bestanden" in s.value for s in app.success)
    assert any("v_plan_ist_umsatz" in c.value for c in app.code)


def test_sicherheitstest_wird_blockiert(app: AppTest) -> None:
    app.button(key="bsp_angriff").click().run()
    assert not app.exception
    assert any("Vom SQL-Guard blockiert" in e.value for e in app.error)
    assert any("DELETE FROM umsatzplanung" in c.value for c in app.code)


def test_freie_frage_im_demo_modus(app: AppTest) -> None:
    app.text_input(key="frage").input("Wie viele Mitarbeitende hat das Unternehmen?")
    app.button(key=SUBMIT).click().run()
    assert not app.exception
    assert any("nicht beantwortbar" in m.value for m in app.markdown)


def test_geschaeftsjahr_wechsel(app: AppTest) -> None:
    app.selectbox(key="geschaeftsjahr").select(2024).run()
    assert not app.exception
    assert any("Geschäftsjahr 2024" in c.value for c in app.caption)
