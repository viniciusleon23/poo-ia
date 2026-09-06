# Poo-IA

Poo-IA es un bot de Discord que responde automáticamente a los mensajes humanos de **un solo canal configurado**. Genera sus respuestas con Ollama local y, por defecto, usa `qwen2.5-coder:3b`.

No usa prefijos ni comandos: escribe en el canal autorizado y el bot responde. Los demás canales, los mensajes vacíos y los mensajes de bots se ignoran.

## Cómo está organizado

```text
app/          Lógica de Discord, Ollama, configuración y división de mensajes
rules/        Reglas editables que guían las respuestas
personality/  Personalidad y tono editables
tests/        Pruebas sin conexión a Discord ni Ollama
```

Los archivos `.md` y `.txt` dentro de `rules/` y `personality/` se cargan en orden alfabético antes de cada respuesta. Puedes añadir archivos o editar los existentes; reinicia el servicio para aplicar cambios. No hace falta cambiar el código para ajustar el comportamiento del bot.

## Requisitos en la Beelink Ubuntu

- Docker Engine y el complemento Docker Compose (`docker compose`).
- Ollama activo en la misma Beelink.
- El modelo local `qwen2.5-coder:3b`.
- Un bot de Discord con **Message Content Intent** activado. Ya está activado para el bot existente.
- Acceso al repositorio y al archivo `.env` actual de `~/discord-bot`.

Comprueba Ollama antes de desplegar:

```bash
ollama list
curl http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-coder:3b","prompt":"Di hola en una frase.","stream":false}'
```

La segunda orden debe devolver JSON con un campo `response`.

## Instalación y migración

En la Beelink, clona el proyecto junto al bot actual:

```bash
cd ~
git clone https://github.com/viniciusleon23/poo-ia.git
cd poo-ia
cp ~/discord-bot/.env .env
```

Abre `.env` y conserva el token existente. Añade el identificador del único canal en el que Poo-IA debe responder:

```dotenv
DISCORD_TOKEN=tu_token_existente
DISCORD_CHANNEL_ID=123456789012345678
OLLAMA_MODEL=qwen2.5-coder:3b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TIMEOUT_SECONDS=120

# Opcional: si se define, solo este usuario puede activar respuestas.
# ALLOWED_USER_ID=123456789012345678
```

Para copiar el ID del canal: en Discord activa el **modo desarrollador**, haz clic derecho sobre el canal y selecciona **Copiar ID del canal**. No publiques el contenido de `.env` ni el token.

El contenedor anterior se llama también `poo-ia-discord`; detenlo desde su carpeta antes de iniciar el nuevo servicio:

```bash
cd ~/discord-bot
docker compose down

cd ~/poo-ia
docker compose up -d --build
docker compose ps
docker compose logs -f discord-bot
```

Cuando aparezca un registro similar a `Connected as ...; listening only to channel ...`, escribe un mensaje normal en el canal configurado. Poo-IA debe contestar sin necesidad de `!ia`.

## Configuración

| Variable | ¿Obligatoria? | Descripción |
| --- | --- | --- |
| `DISCORD_TOKEN` | Sí | Token secreto del bot. Nunca se añade a Git. |
| `DISCORD_CHANNEL_ID` | Sí | ID numérico del único canal autorizado. |
| `OLLAMA_MODEL` | No | Modelo de Ollama; por defecto `qwen2.5-coder:3b`. |
| `OLLAMA_BASE_URL` | No | Dirección local de Ollama; por defecto `http://127.0.0.1:11434`. |
| `OLLAMA_TIMEOUT_SECONDS` | No | Tiempo máximo de espera por respuesta; por defecto `120`. |
| `ALLOWED_USER_ID` | No | Si se define, restringe el uso a una sola cuenta de Discord. |

El bot se niega a iniciar si faltan el token o el ID de canal. No existe un modo que responda de forma accidental en todos los canales.

## Docker y Ollama

`compose.yml` usa `network_mode: host`. En Ubuntu esto permite que el contenedor alcance el Ollama del host en `127.0.0.1:11434`; no se deben añadir puertos publicados al servicio.

Los directorios `rules/` y `personality/` se montan en modo de solo lectura. Para aplicar cambios en ellos:

```bash
cd ~/poo-ia
docker compose restart discord-bot
```

Para actualizar código o dependencias:

```bash
cd ~/poo-ia
git pull --ff-only
docker compose up -d --build
```

## Pruebas locales

Las pruebas verifican la configuración, el filtro de canal/usuario, la carga de reglas y personalidad, la división de respuestas largas y las solicitudes a Ollama simuladas.

```bash
cd ~/poo-ia
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Para validar la configuración de Docker sin iniciar el bot:

```bash
docker compose config
```

## Diagnóstico rápido

- **El bot conecta pero no responde:** confirma que el mensaje está en el `DISCORD_CHANNEL_ID` configurado y que el Message Content Intent sigue activado.
- **`Configuration error`:** revisa que `.env` contenga `DISCORD_TOKEN` y un `DISCORD_CHANNEL_ID` numérico.
- **No se puede obtener respuesta de Ollama:** ejecuta `ollama list`, prueba la solicitud `curl` anterior y consulta `docker compose logs -f discord-bot`.
- **El contenedor no inicia por nombre duplicado:** detén primero la instalación antigua desde `~/discord-bot` con `docker compose down`.

## Seguridad

`.env`, entornos virtuales y archivos generados están excluidos de Git y de la imagen de Docker. Antes de publicar cambios, comprueba siempre:

```bash
git status
git diff --cached
```
