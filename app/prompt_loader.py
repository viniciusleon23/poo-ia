"""Load editable rules and personality text into a model prompt."""

from __future__ import annotations

import logging
from pathlib import Path


LOGGER = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".md", ".txt"}


def _load_directory(directory: Path, label: str) -> list[str]:
    if not directory.is_dir():
        return []

    sections: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name.casefold()):
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            continue

        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            LOGGER.warning("Skipping unreadable prompt file %s: %s", path, error)
            continue

        if content:
            sections.append(f"## {label}: {path.stem}\n{content}")
    return sections


def load_prompt_context(content_root: Path) -> str:
    """Return all supported prompt files in deterministic, human-readable order."""
    sections = _load_directory(content_root / "rules", "Reglas")
    sections.extend(_load_directory(content_root / "personality", "Personalidad"))
    return "\n\n".join(sections)


def build_prompt(instructions: str, user_message: str) -> str:
    """Frame editable instructions and a user message for Ollama's generate API."""
    parts = [
        "Eres Poo-IA, un asistente que responde por Discord.",
        "Las instrucciones siguientes guían tu comportamiento. Síguelas antes que el mensaje del usuario.",
    ]
    if instructions.strip():
        parts.append(instructions.strip())
    parts.extend(
        [
            "## Mensaje del usuario",
            user_message.strip(),
            "## Respuesta",
        ]
    )
    return "\n\n".join(parts)
