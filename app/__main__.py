"""Executable module for the Poo-IA Discord bot."""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from .config import ConfigurationError, Settings
from .discord_bot import PooIAClient


def main() -> int:
    """Load configuration and run the Discord client."""
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        logging.error("Configuration error: %s", error)
        return 2

    PooIAClient(settings).run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
