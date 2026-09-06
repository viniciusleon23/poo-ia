"""Conservative routing between local conversation and documentation research."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum


class Backend(str, Enum):
    """Backends available for an accepted Discord message."""

    OLLAMA = "ollama"
    OPENCODE = "opencode"


SMALL_TALK = frozenset(
    {
        "adios",
        "buen dia",
        "buenas",
        "buenas noches",
        "buenas tardes",
        "buenos dias",
        "como estas",
        "gracias",
        "hasta luego",
        "hey",
        "hola",
        "hola mundo",
        "muchas gracias",
        "que tal",
    }
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def choose_backend(message: str) -> Backend:
    """Route only an exact, unambiguous small-talk phrase to local Ollama."""
    return Backend.OLLAMA if _normalize(message) in SMALL_TALK else Backend.OPENCODE
