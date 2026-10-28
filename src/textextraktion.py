"""
Textextraktion – PDF-Geschäftsberichte lesen und relevante Textstellen vorfiltern.

Übernimmt Stufe 1 des Verfahrens aus der Bachelorarbeit: Ein Suchdiktionär
mit deutschen und englischen Begriffen je Technologiefeld wählt die
Textstellen aus, die überhaupt an das Sprachmodell gehen. Das senkt Kosten,
reduziert Rauschen und macht nachvollziehbar, warum eine Stelle geprüft wurde.

Ein zusätzliches Recall-Feld („Allgemeine Digitalisierungsbegriffe“) fängt
Vorhaben auf, die ohne Technologienamen beschrieben werden (z. B. „Tool“,
„digitalisiert“). Es dient nur der Trefferfindung, nicht der Klassifikation.
Absätze, die überwiegend aus Zahlen bestehen (Tabellen, Kennzahlenwerke),
werden ausgeschlossen.

Autor: Sherwan Shweish
Lizenz: MIT
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "RECALL_FELD",
    "SUCHDIKTIONAER",
    "Fundstelle",
    "Seite",
    "abdeckung",
    "extrahiere_pdf",
    "fundstellen_text",
    "text_zu_seiten",
    "vorfiltern",
]

RECALL_FELD = "Allgemeine Digitalisierungsbegriffe"

SUCHDIKTIONAER: dict[str, list[str]] = {
    "Cloud": [r"cloud\w*", r"rechenzentr\w*", r"\bsaas\b", r"hyperscaler\w*"],
    "Data Analytics": [r"\bdaten(?!schutz)\w*", r"analytics?", r"\bdata\b", r"dashboard\w*",
                       r"business intelligence"],
    "Prozessautomatisierung": [r"automatisier\w*", r"software-roboter\w*", r"\brpa\b", r"workflow\w*",
                               r"dunkelverarbeitung"],
    "IoT/Industrie 4.0": [r"\biot\b", r"industrie 4\.0", r"sensor\w*", r"vernetz\w*", r"digitale\w* zwilling\w*",
                          r"smart factory"],
    "KI": [r"\bki\b", r"künstliche\w* intelligenz", r"machine learning", r"maschinelle\w* lernen",
           r"\bgenerative\w*"],
    "Digitale Plattformen und Anwendungen": [r"\bapps?\b", r"plattform\w*", r"portal\w*", r"e-commerce",
                                             r"online-shop\w*"],
    RECALL_FELD: [r"digitalisier\w*", r"\bsoftware\b", r"\btools?\b", r"it-system\w*", r"it-infrastruktur"],
}

_MUSTER = {feld: re.compile("|".join(begriffe), re.IGNORECASE) for feld, begriffe in SUCHDIKTIONAER.items()}


@dataclass(frozen=True)
class Seite:
    """Eine Seite eines Dokuments (bei Textdateien: das ganze Dokument als Seite 1)."""

    nummer: int
    text: str


@dataclass
class Fundstelle:
    """Ein Absatz mit mindestens einem Treffer aus dem Suchdiktionär."""

    seite: int
    text: str
    treffer: dict[str, list[str]] = field(default_factory=dict)

    @property
    def technologiefelder(self) -> list[str]:
        """Treffer ohne das reine Recall-Feld."""
        return [f for f in self.treffer if f != RECALL_FELD]


def extrahiere_pdf(quelle: bytes | str | Path) -> list[Seite]:
    """Liest den Text eines PDFs seitenweise (benötigt ``pdfplumber``)."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Für PDF-Dateien wird 'pdfplumber' benötigt: pip install pdfplumber") from exc
    datei = io.BytesIO(quelle) if isinstance(quelle, bytes) else quelle
    with pdfplumber.open(datei) as pdf:
        return [Seite(i, page.extract_text() or "") for i, page in enumerate(pdf.pages, start=1)]


def text_zu_seiten(text: str) -> list[Seite]:
    """Textdatei als Seitenliste; Seitenvorschübe (\\f) trennen Seiten."""
    return [Seite(i, teil) for i, teil in enumerate(text.split("\f"), start=1)]


def _absaetze(text: str, ziel_laenge: int = 900) -> list[str]:
    """Teilt Seitentext in Absätze; lange Blöcke ohne Leerzeilen werden satzweise gruppiert."""
    absaetze: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= ziel_laenge * 1.5:
            absaetze.append(block)
            continue
        aktuell = ""
        for satz in re.split(r"(?<=[.!?])\s+", block):
            if aktuell and len(aktuell) + len(satz) > ziel_laenge:
                absaetze.append(aktuell)
                aktuell = satz
            else:
                aktuell = f"{aktuell} {satz}".strip()
        if aktuell:
            absaetze.append(aktuell)
    return absaetze


def ist_zahlenwerk(absatz: str, schwelle: float = 0.3) -> bool:
    """True, wenn mehr als ``schwelle`` der Zeichen (ohne Leerraum) Ziffern sind."""
    zeichen = re.sub(r"\s", "", absatz)
    return bool(zeichen) and sum(c.isdigit() for c in zeichen) / len(zeichen) > schwelle


def vorfiltern(seiten: list[Seite], *, min_zeichen: int = 60) -> list[Fundstelle]:
    """Wählt alle Absätze mit Diktionärstreffern aus (Stufe 1 der Analyse)."""
    fundstellen: list[Fundstelle] = []
    for seite in seiten:
        for absatz in _absaetze(seite.text):
            if len(absatz) < min_zeichen or ist_zahlenwerk(absatz):
                continue
            treffer = {
                feld: sorted({m.group(0).lower() for m in muster.finditer(absatz)})
                for feld, muster in _MUSTER.items()
            }
            treffer = {feld: begriffe for feld, begriffe in treffer.items() if begriffe}
            if treffer:
                fundstellen.append(Fundstelle(seite.nummer, absatz, treffer))
    return fundstellen


def fundstellen_text(fundstellen: list[Fundstelle]) -> str:
    """Fügt Fundstellen zu einem Text für die BSC-Klassifikation zusammen."""
    return "\n\n".join(f.text for f in fundstellen)


def abdeckung(seiten: list[Seite], fundstellen: list[Fundstelle]) -> dict[str, int]:
    """Kennzahlen der Vorfilterung für die Anzeige im Dashboard."""
    felder: dict[str, int] = {}
    for f in fundstellen:
        for feld in f.treffer:
            felder[feld] = felder.get(feld, 0) + 1
    return {
        "seiten": len(seiten),
        "seiten_mit_treffern": len({f.seite for f in fundstellen}),
        "fundstellen": len(fundstellen),
        **{f"feld: {k}": v for k, v in sorted(felder.items())},
    }
