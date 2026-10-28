"""Gemeinsame Test-Fixtures.

Alle Tests laufen ohne API-Schlüssel und ohne Kosten: Das Sprachmodell wird
durch ``FakeLLM`` ersetzt, das vorab festgelegte Antworten zurückgibt und
jeden Prompt protokolliert.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.agent import AgentConfig, ControllAIAgent
from src.seed_data import SeedErgebnis, erzeuge_alles


class FakeLLM:
    """Ersetzt das Sprachmodell durch vorab festgelegte Antworten."""

    def __init__(self, *antworten: str | dict[str, Any]) -> None:
        self.antworten = list(antworten)
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.system_prompts.append(system)
        self.prompts.append(user)
        if not self.antworten:
            raise AssertionError("FakeLLM: Es wurde ein weiterer, nicht erwarteter LLM-Aufruf ausgelöst.")
        antwort = self.antworten.pop(0)
        return antwort if isinstance(antwort, str) else json.dumps(antwort, ensure_ascii=False)

    @property
    def aufrufe(self) -> int:
        return len(self.prompts)


@pytest.fixture
def mini_db(tmp_path: Path) -> Path:
    """Kleine Datenbank mit bekannten Werten für exakte Prüfungen."""
    db = tmp_path / "mini.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE plan_ist (
                quartal TEXT, jahr INTEGER, produktkategorie TEXT, plan_umsatz REAL, ist_umsatz REAL
            );
            INSERT INTO plan_ist VALUES
                ('Q3', 2025, 'Elektronik', 100000, 87500),
                ('Q3', 2025, 'Haushalt',    50000, 51200),
                ('Q2', 2025, 'Elektronik',  90000, 91000);
            CREATE TABLE geheim (wert INTEGER);
            INSERT INTO geheim VALUES (1);
            """
        )
    return db


@pytest.fixture
def agent_factory(mini_db: Path) -> Callable[..., ControllAIAgent]:
    """Erzeugt einen Agenten mit FakeLLM; ``agent._llm`` gibt Zugriff auf die Prompts."""

    def erzeuge(*antworten: str | dict[str, Any], db: Path | None = None, **konfig: Any) -> ControllAIAgent:
        return ControllAIAgent(AgentConfig(db_path=db or mini_db, **konfig), llm=FakeLLM(*antworten))

    return erzeuge


@pytest.fixture(scope="session")
def demo_daten(tmp_path_factory: pytest.TempPathFactory) -> SeedErgebnis:
    """Vollständige synthetische Demo-Datenbasis (einmal je Testlauf erzeugt)."""
    return erzeuge_alles(tmp_path_factory.mktemp("demo"), seed=42)
