from __future__ import annotations

import unittest

from app.router import Backend, choose_backend


class RouterTests(unittest.TestCase):
    def test_routes_unambiguous_small_talk_to_ollama(self) -> None:
        messages = (
            "hola",
            "¡Hola!",
            "hola mundo",
            "Buenos días",
            "¿Cómo estás?",
            "muchas gracias",
            "hasta luego",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(choose_backend(message), Backend.OLLAMA)

    def test_routes_everything_else_to_opencode(self) -> None:
        messages = (
            "hola, ¿cómo funciona API Users?",
            "qué servicios dependen de customer-service",
            "revisa el repositorio",
            "necesito cambiar un endpoint",
            "explícame esto",
            "gracias, ahora busca la lambda",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(choose_backend(message), Backend.OPENCODE)
