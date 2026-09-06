# Poo-IA: diseño del bot de Discord con Ollama

**Fecha:** 2026-09-06  
**Estado:** aprobado para revisión de especificación

## Objetivo

Crear un repositorio Git limpio para Poo-IA, un bot de Discord ejecutado en Docker sobre una Beelink con Ubuntu. El bot responderá automáticamente a mensajes humanos enviados en un único canal autorizado, mediante Ollama local y el modelo `qwen2.5-coder:3b`.

El proyecto debe poder reemplazar de forma ordenada el bot actual ubicado en `~/discord-bot`, sin publicar el token de Discord ni otros secretos.

## Alcance acordado

- No habrá prefijos ni comandos como `!ia`: un mensaje humano del canal autorizado es un prompt.
- El canal se define con la variable obligatoria `DISCORD_CHANNEL_ID`.
- El bot ignorará mensajes de cualquier bot, incluido él mismo, para impedir respuestas en bucle.
- Las respuestas que superen el límite de Discord se fragmentarán en mensajes de tamaño seguro.
- El modelo y la URL de Ollama podrán cambiarse mediante variables de entorno, con los valores iniciales `qwen2.5-coder:3b` y `http://127.0.0.1:11434`.
- El comportamiento de la IA se separará del código en directorios editables `rules/` y `personality/`.
- La primera versión no mantiene historial conversacional. Cada mensaje se procesa de manera independiente con las reglas y la personalidad cargadas. Esto mantiene el uso de memoria y la complejidad bajos en la Beelink.

Fuera de alcance para esta primera entrega: comandos de administración en Discord, memoria por usuario, soporte multi-canal, base de datos, panel web y streaming de tokens.

## Alternativa elegida

Se elige un bot con un único canal obligatorio y configurable. Frente a una configuración que responde en todo el servidor, evita respuestas accidentales en canales no deseados. Frente a controles administrativos dentro de Discord, conserva el flujo directo de “escribir y recibir respuesta” sin añadir comandos ni estado adicional.

`ALLOWED_USER_ID` será una variable opcional. Si se define, solo ese usuario podrá activar respuestas dentro del canal configurado; si se omite, cualquier humano del canal podrá usar el bot.

## Estructura del repositorio

```text
poo-ia/
├── app/
│   ├── __init__.py
│   ├── config.py            # Validación de variables de entorno
│   ├── discord_bot.py       # Eventos de Discord y filtro del canal
│   ├── ollama_client.py     # Llamada HTTP asíncrona a Ollama
│   ├── prompt_loader.py     # Carga ordenada de rules/ y personality/
│   └── text.py              # División segura para mensajes de Discord
├── personality/
│   └── default.md           # Identidad y tono inicial, editable
├── rules/
│   └── base.md              # Reglas iniciales, editables
├── tests/
│   ├── test_config.py
│   ├── test_prompt_loader.py
│   └── test_text.py
├── .dockerignore
├── .env.example
├── .gitignore
├── compose.yml
├── Dockerfile
├── README.md
└── requirements.txt
```

Los directorios `rules/` y `personality/` son contenido de primer nivel para que puedan modificarse con facilidad. El cargador leerá archivos de texto compatibles en orden alfabético y los unirá en un prompt de sistema estable. Agregar un archivo permitirá extender la conducta sin cambiar Python. El contenedor recibirá ambos directorios mediante volúmenes de solo lectura, de modo que modificar los textos y reiniciar el servicio aplicará los cambios sin reconstruir la imagen.

## Configuración

El archivo real `.env` pertenece exclusivamente a la Beelink y estará ignorado por Git. `.env.example` documentará cada variable sin valores sensibles.

| Variable | Obligatoria | Valor inicial | Propósito |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Sí | — | Token secreto del bot de Discord. |
| `DISCORD_CHANNEL_ID` | Sí | — | Identificador numérico del único canal permitido. |
| `OLLAMA_MODEL` | No | `qwen2.5-coder:3b` | Modelo local que genera la respuesta. |
| `OLLAMA_BASE_URL` | No | `http://127.0.0.1:11434` | Servicio de Ollama del host. |
| `ALLOWED_USER_ID` | No | — | Identificador numérico del único usuario permitido, si se quiere activar el filtro. |
| `OLLAMA_TIMEOUT_SECONDS` | No | `120` | Límite de espera de una generación. |

El arranque fallará de forma clara si faltan `DISCORD_TOKEN` o `DISCORD_CHANNEL_ID`, o si un ID no es numérico. Así no habrá un modo implícito que responda en todo el servidor.

## Flujo de una respuesta

```text
Mensaje de Discord
        │
        ├── ¿Es de un bot? ──────────────── sí → ignorar
        ├── ¿Es del canal configurado? ──── no → ignorar
        ├── ¿El usuario está permitido? ─── no → ignorar
        │
        ▼
Cargar rules/ + personality/ + mensaje del usuario
        │
        ▼
POST /api/generate a Ollama en 127.0.0.1:11434
        │
        ▼
Fragmentar la respuesta en mensajes de hasta 1,900 caracteres
        │
        ▼
Enviar la respuesta al mismo canal
```

El cliente usará `aiohttp` y una sesión reutilizable. Enviará `stream: false` a `POST /api/generate`. Mientras espera, Discord mostrará el indicador de escritura.

## Errores y límites

- Si Ollama no responde, devuelve un estado no exitoso o entrega una respuesta vacía, el bot registrará el detalle y enviará un mensaje breve y seguro al canal autorizado; el proceso seguirá ejecutándose.
- Los tiempos de espera HTTP se configurarán con `OLLAMA_TIMEOUT_SECONDS`.
- La fragmentación priorizará saltos de línea y espacios cuando sea posible, con límite de 1,900 caracteres para respetar el máximo de Discord de 2,000 y dejar margen.
- Los archivos de reglas o personalidad vacíos no detendrán el bot; se cargarán los demás archivos disponibles. La ausencia total de contenido también será válida para facilitar el inicio.
- Los errores de configuración se detendrán al inicio con mensajes explícitos, porque no es seguro iniciar sin el canal permitido o sin token.

## Contenedor y despliegue

`Dockerfile` construirá una imagen Python pequeña y reproducible con dependencias bloqueadas por versión. `compose.yml` definirá el servicio `discord-bot`, conservará el nombre de contenedor `poo-ia-discord`, cargará `.env`, usará `restart: unless-stopped` y `network_mode: host`.

La red del host es necesaria para que `http://127.0.0.1:11434` apunte al Ollama que ya se ejecuta en la Beelink. No se publicarán puertos de Docker. `rules/` y `personality/` se montarán como lectura sola. El token nunca se copiará a la imagen ni se añadirá al repositorio.

El README incluirá estos pasos operativos:

1. Clonar el repositorio en la Beelink.
2. Trasladar el token existente de `~/discord-bot/.env` a la nueva copia y completar `DISCORD_CHANNEL_ID`.
3. Comprobar que `ollama list` muestra `qwen2.5-coder:3b`.
4. Construir e iniciar con Docker Compose.
5. Consultar registros y enviar un mensaje de prueba al canal autorizado.
6. Actualizar el servicio de forma segura tras cambios de código, reglas o personalidad.

## Pruebas y criterios de aceptación

Antes de desplegar se ejecutarán pruebas unitarias para:

- validar configuración obligatoria y opcional;
- cargar varios archivos de reglas y personalidad de forma determinista;
- dividir respuestas largas sin exceder el límite de Discord;
- comprobar que el filtro de canal y usuario se aplica antes de llamar a Ollama mediante objetos simulados.

En la Beelink se verificará, cuando haya autenticación SSH disponible:

1. `ollama list` contiene `qwen2.5-coder:3b`;
2. una solicitud local a Ollama genera texto;
3. la imagen de Docker se construye correctamente;
4. `poo-ia-discord` queda en ejecución;
5. un mensaje humano en el canal autorizado recibe una respuesta;
6. un mensaje en otro canal y un mensaje de bot no generan respuesta;
7. una respuesta extensa se entrega en varios mensajes válidos.

La Beelink respondió a la conexión de red, pero la sesión actual no dispone de una credencial SSH aceptada. El despliegue y las pruebas de los puntos 1–7 se ejecutarán directamente allí cuando se habilite autenticación por clave pública o una sesión SSH interactiva autorizada. Mientras tanto, el repositorio podrá construirse, probarse y versionarse localmente sin incluir secretos.

## Seguridad y mantenimiento

- `.env`, entornos virtuales, cachés, archivos de cobertura y secretos se excluirán mediante `.gitignore` y `.dockerignore`.
- El README indicará habilitar el Message Content Intent, que ya está activo para este bot.
- El identificador del canal se conserva fuera del código, por lo que mover el bot a otro canal solo requiere cambiar `.env` y reiniciar.
- Cada cambio de reglas o personalidad seguirá siendo texto versionable y revisable, sin necesidad de tocar la lógica del bot.
