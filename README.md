# Poo-IA

Poo-IA es un bot de Discord que responde automáticamente a los mensajes humanos de **un solo canal configurado**. Usa Ollama local con `qwen2.5-coder:3b` para conversación sencilla y puede delegar consultas técnicas a un worker OpenCode de solo lectura.

No usa prefijos ni comandos: escribe en el canal autorizado y el bot responde. Los demás canales, los mensajes vacíos y los mensajes de bots se ignoran.

## Cómo está organizado

```text
app/          Lógica de Discord, Ollama, configuración y división de mensajes
rules/        Reglas editables que guían las respuestas
personality/  Personalidad y tono editables
opencode/      Política y agentes de consulta de Capnet
ops/           Servicio del host para el worker OpenCode
tests/        Pruebas sin conexión a Discord ni Ollama
```

Los archivos `.md` y `.txt` dentro de `rules/` y `personality/` se cargan en orden alfabético antes de cada respuesta. Puedes añadir archivos o editar los existentes; reinicia el servicio para aplicar cambios. No hace falta cambiar el código para ajustar el comportamiento del bot.

## Requisitos en la Beelink Ubuntu

- Docker Engine y el complemento Docker Compose (`docker compose`).
- Ollama activo en la misma Beelink.
- El modelo local `qwen2.5-coder:3b`.
- Un bot de Discord con **Message Content Intent** activado. Ya está activado para el bot existente.
- Acceso al repositorio y al archivo `.env` actual de `~/discord-bot`.
- OpenCode solo si se habilitará la consulta documental. El bot básico funciona sin él.

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

# Worker documental. Déjalo apagado durante la instalación inicial.
OPENCODE_ENABLED=false
OPENCODE_BASE_URL=http://127.0.0.1:4096
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=una_contraseña_local_larga
OPENCODE_AGENT=capnet-research
OPENCODE_TIMEOUT_SECONDS=300
OPENCODE_MAX_CONCURRENT=1
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
| `OPENCODE_ENABLED` | No | Activa el cerebro documental; por defecto `false`. |
| `OPENCODE_BASE_URL` | No | Servidor del host; por defecto `http://127.0.0.1:4096`. |
| `OPENCODE_SERVER_USERNAME` | No | Usuario de autenticación local; por defecto `opencode`. |
| `OPENCODE_SERVER_PASSWORD` | Si OpenCode está activo | Contraseña HTTP compartida con el servicio del host. |
| `OPENCODE_AGENT` | No | Agente de consulta; por defecto `capnet-research`. |
| `OPENCODE_TIMEOUT_SECONDS` | No | Espera total de una investigación; por defecto `300`. |
| `OPENCODE_MAX_CONCURRENT` | No | Investigaciones simultáneas; por defecto `1`. |

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

## Cerebro documental con OpenCode

El flujo inicial es conservador: saludos y agradecimientos inequívocos se responden con Ollama; cualquier otro mensaje se considera una posible consulta técnica y se envía a OpenCode. Si el worker falla, Poo-IA no permite que Qwen improvise una respuesta técnica.

OpenCode se ejecuta directamente en la Beelink desde `/home/poo/capnet-workspace`. Sus perfiles pueden leer y buscar en los repositorios, pero niegan edición, shell, archivos `.env`, acceso fuera del workspace y publicación externa.

Instálalo como el usuario `poo` con el método oficial:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Después entra de forma interactiva a OpenCode desde `/home/poo/capnet-workspace`, usa `/connect`, selecciona OpenAI y elige **ChatGPT Plus/Pro**. Autoriza tu cuenta en el navegador. No selecciones la opción de API key.

La configuración del worker se conserva en este repositorio. Para comprobar cómo la interpreta la versión instalada:

```bash
cd /home/poo/capnet-workspace
OPENCODE_CONFIG=/home/poo/poo-ia/opencode/opencode.jsonc \
OPENCODE_CONFIG_DIR=/home/poo/poo-ia/opencode \
opencode debug config
```

El servicio necesita una contraseña local separada. Guárdala en un archivo privado:

```bash
mkdir -p /home/poo/.config/poo-ia
chmod 700 /home/poo/.config/poo-ia
```

El archivo `/home/poo/.config/poo-ia/opencode-server.env` debe contener `OPENCODE_SERVER_PASSWORD=` seguido por una contraseña aleatoria larga y debe tener permisos `600`. Copia el mismo valor en el `.env` privado de Poo-IA. Nunca lo añadas a Git.

Para instalar la unidad incluida se requieren permisos administrativos:

```bash
sudo cp /home/poo/poo-ia/ops/opencode-capnet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-capnet
sudo systemctl status opencode-capnet
```

Comprueba que solo escucha localmente y que exige autenticación antes de cambiar `OPENCODE_ENABLED=true`. Luego reconstruye Poo-IA:

```bash
cd /home/poo/poo-ia
docker compose up -d --build
docker compose logs -f discord-bot
```

Para volver inmediatamente al comportamiento sin worker, cambia `OPENCODE_ENABLED=false` y reinicia el contenedor.

### Uso de la suscripción y OpenCode Go

La instalación inicial utiliza el inicio de sesión ChatGPT/Codex y no configura una clave de OpenAI API. Las consultas consumen la capacidad disponible de esa cuenta. Este proyecto no activa compras de créditos ni recarga automática.

OpenCode Go es una opción futura y voluntaria. Al contratarlo, solo será necesario conectar su proveedor y cambiar el modelo del agente; el protocolo entre Discord y OpenCode seguirá igual. No guardes su clave en el repositorio.

## Pruebas locales

Las pruebas verifican la configuración, el filtro de canal/usuario, el enrutamiento, la carga de reglas y personalidad, la división de respuestas largas y las solicitudes simuladas a Ollama y OpenCode.

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
- **El cerebro documental no está habilitado:** confirma que OpenCode funciona localmente antes de cambiar `OPENCODE_ENABLED=true`.
- **OpenCode devuelve un error:** revisa `systemctl status opencode-capnet`, su autenticación y la sesión de ChatGPT/Codex; no añadas una API key como solución rápida.
- **La consulta tarda demasiado:** el límite predeterminado es de cinco minutos y solo se procesa una investigación simultánea.
- **El contenedor no inicia por nombre duplicado:** detén primero la instalación antigua desde `~/discord-bot` con `docker compose down`.

## Seguridad

`.env`, entornos virtuales y archivos generados están excluidos de Git y de la imagen de Docker. Antes de publicar cambios, comprueba siempre:

```bash
git status
git diff --cached
```
