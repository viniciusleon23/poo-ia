"""Utilities for fitting model output into Discord messages."""

from __future__ import annotations


DISCORD_SAFE_MESSAGE_LIMIT = 1900


def split_for_discord(text: str, *, limit: int = DISCORD_SAFE_MESSAGE_LIMIT) -> list[str]:
    """Split text into non-empty chunks no longer than ``limit`` characters.

    Newlines and spaces are preferred as boundaries. A single word longer than the
    limit is split exactly at the limit so every emitted Discord message is valid.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    remaining = text.strip()
    chunks: list[str] = []

    while len(remaining) > limit:
        window = remaining[:limit]
        boundary = max(window.rfind("\n"), window.rfind(" "))

        if boundary <= 0:
            chunks.append(window)
            remaining = remaining[limit:]
            continue

        chunk = remaining[:boundary].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[boundary:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks
