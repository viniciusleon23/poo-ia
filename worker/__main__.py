"""Run the Poo-IA worker API."""

from aiohttp import web

from .api import create_app
from .config import WorkerSettings


def main() -> None:
    settings = WorkerSettings.from_environment()
    web.run_app(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
