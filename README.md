# Poo-IA

Poo-IA es un asistente personal que usa Discord como primera interfaz. Escucha mensajes normales, sin prefijo `!`, exclusivamente en un canal y para un propietario configurados. Mantiene memoria entre reinicios, consulta la documentación de Capnet y puede preparar cambios pequeños mediante Codex CLI en worktrees aislados.

La integración con AWS y DynamoDB está deliberadamente desactivada en esta fase. El sistema principal no instala AWS CLI, no necesita un perfil AWS y no lee, copia ni modifica credenciales AWS.

## Arquitectura

```text
Discord
   │
   ▼
Poo-IA Core en Docker ─────► Ollama 127.0.0.1:11434
   │                           conversación breve
   ├── SQLite /app/data
   │   memoria, trabajos y entregas pendientes
   │
   ├────────────────────────► OpenCode 127.0.0.1:4096
   │                           investigación de solo lectura
   │
   └────────────────────────► Worker 127.0.0.1:4097
                               Codex, Git, medición y GitHub CLI
                               ejecutados como el usuario poo
```

El contenedor usa `network_mode: host`, por lo que en Ubuntu puede alcanzar los tres servicios locales mediante `127.0.0.1`. OpenCode y el worker escuchan solo en loopback y exigen autenticación HTTP Basic. La futura interfaz web podrá reutilizar el mismo núcleo, SQLite y worker.

### Responsabilidades

| Componente | Responsabilidad |
| --- | --- |
| Ollama / `qwen2.5-coder:3b` | Conversación sencilla y saludos inequívocos. |
| OpenCode / `openai/gpt-5.4-mini` | Buscar evidencia en el snapshot sanitizado `capnet-research-view`, comenzando por `brain-capnet`; no accede a los checkouts operativos, no edita ni ejecuta shell. |
| Núcleo Poo-IA | Filtrar Discord, recordar contexto, resolver intención, serializar trabajos y entregar respuestas. |
| Worker host | Inventariar repos limpios, crear worktrees, ejecutar Codex, medir el diff y publicar un PR autorizado. |
| SQLite | Persistir conversaciones, trabajos, eventos e información pendiente de enviar. |

El repositorio está organizado así:

```text
app/          Núcleo, Discord, memoria, SQLite, router y clientes HTTP
worker/       Worker local de Codex, Git, validación y GitHub
rules/        Reglas editables de memoria, investigación, cambios y AWS futuro
personality/  Voz y personalidad de Poo-IA
opencode/     Configuración del investigador documental de solo lectura
ops/          Servicios systemd y ejemplo privado del worker
docs/         Diseño, plan y manual operativo
tests/        Pruebas sin depender de Discord, Ollama, Codex o GitHub reales
```

Los repositorios que puede modificar el worker no viven dentro de Poo-IA. Son hijos directos de `/home/poo/capnet-workspace`; `brain-capnet` es uno de ellos. Los cambios se crean fuera de sus checkouts base, bajo `/home/poo/capnet-worktrees`. OpenCode no recibe esos checkouts: antes de arrancar se genera `/home/poo/capnet-research-view` únicamente con archivos de texto permitidos de cada `HEAD` confirmado en Git, sin metadatos `.git`, archivos no rastreados ni material detectado como secreto.

## Comportamiento en Discord

- Ignora bots, mensajes vacíos, canales distintos y usuarios distintos del propietario.
- Divide respuestas largas en fragmentos seguros de hasta 1.900 caracteres.
- Recuerda por defecto los diez últimos intercambios completados de los últimos siete días, con un máximo de 12.000 caracteres.
- Solo incorpora a memoria una respuesta que Discord haya recibido completa.
- Reanuda mensajes pendientes y trabajos persistidos después de reiniciar el contenedor.
- Conserva 30 días las solicitudes y trabajos terminados ya notificados; nunca poda trabajo activo o preparado.
- Ejecuta como máximo un trabajo pesado a la vez entre investigación, Codex y publicación.
- Usa investigación como destino conservador de cualquier solicitud técnica que no autorice claramente una modificación.
- Nunca permite que un modelo convierta una consulta ambigua en un cambio.

Ejemplos:

```text
hola
¿Dónde se define base_response en el servicio de tareas?
Ahora revisa qué consumidores dependen de ese campo
agrega task_available con valor predeterminado true en capnet-next-lambda-tasks
cómo va
cancela ese trabajo
arma el PR del trabajo 1a2b3c4d
olvida la conversación
```

`olvida la conversación` debe enviarse como esa frase exacta, admitiendo diferencias de acentos, mayúsculas, puntuación y espacios. Borra la memoria conversacional y reinicia el repositorio y trabajo activos; conserva únicamente los registros operativos necesarios para idempotencia.

Una orden explícita de cambio prepara una rama y devuelve su repositorio, rama, validación y resumen. Solo una orden que diga crear, armar o publicar el PR autoriza el `push` y `gh pr create`. Poo-IA no fusiona PR ni despliega.

## Requisitos de la Beelink

- Ubuntu con Docker Engine y Docker Compose.
- Ollama activo con `qwen2.5-coder:3b`.
- Python 3.11 o posterior para el worker del host.
- Git, GitHub CLI (`gh`) y Codex CLI.
- OpenCode instalado y autenticado con el proveedor elegido.
- Bot de Discord con Message Content Intent activado.
- `/home/poo/capnet-workspace` con los repositorios Git como hijos directos.
- Los checkouts base que se vayan a usar deben estar limpios.

Comprobaciones iniciales:

```bash
ollama list
docker compose version
python3 --version
git --version
gh --version
codex --version
opencode --version
```

Prueba Ollama:

```bash
curl http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-coder:3b","prompt":"Di hola en una frase.","stream":false}'
```

## Instalación o migración

Si el repositorio aún no existe en la Beelink:

```bash
cd /home/poo
git clone https://github.com/viniciusleon23/poo-ia.git
cd /home/poo/poo-ia
install -m 600 /home/poo/discord-bot/.env .env
```

Si ya existe:

```bash
cd /home/poo/poo-ia
git pull --ff-only
```

El archivo `.env` anterior conserva el token, pero debe completarse con las variables de la siguiente sección. Ejecuta `chmod 600 /home/poo/poo-ia/.env` incluso si ya existía; `.env` nunca se añade a Git.

### 1. Configurar el núcleo

Copia `.env.example` si partes de cero y edita `.env`:

```bash
cd /home/poo/poo-ia
cp .env.example .env
chmod 600 .env
```

Configuración funcional de referencia:

```dotenv
DISCORD_TOKEN=token_privado_existente
DISCORD_CHANNEL_ID=1546178859739906180
ALLOWED_USER_ID=id_numerico_del_propietario

OLLAMA_MODEL=qwen2.5-coder:3b
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_TIMEOUT_SECONDS=120

POOIA_DB_PATH=/app/data/poo-ia.sqlite3
MEMORY_RETENTION_DAYS=7
MEMORY_MAX_EXCHANGES=10
MEMORY_MAX_CONTEXT_CHARS=12000
OPERATIONAL_RETENTION_DAYS=30
JOB_MAX_CONCURRENT=1

OPENCODE_ENABLED=true
OPENCODE_BASE_URL=http://127.0.0.1:4096
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=secreto_interno_de_opencode
OPENCODE_AGENT=capnet-research
OPENCODE_TIMEOUT_SECONDS=300
OPENCODE_MAX_CONCURRENT=1

WORKER_ENABLED=true
WORKER_BASE_URL=http://127.0.0.1:4097
WORKER_SERVER_USERNAME=poo-ia
WORKER_SERVER_PASSWORD=secreto_interno_del_worker
WORKER_TIMEOUT_SECONDS=30
WORKER_POLL_SECONDS=2

AWS_ENABLED=false
```

`ALLOWED_USER_ID` es obligatorio. En Discord, activa el modo desarrollador, haz clic derecho sobre tu usuario y selecciona **Copiar ID de usuario**. El canal se obtiene de la misma forma con **Copiar ID del canal**.

Usa dos secretos internos aleatorios y diferentes, de al menos 16 caracteres: uno para OpenCode y otro para el worker. Los valores emparejados deben coincidir exactamente entre `.env` y sus archivos del host. Los nombres de usuario de autenticación Basic no pueden contener `:` ni saltos de línea.

### 2. Preparar OpenCode documental

OpenCode corre como `poo`, pero trabaja exclusivamente en `/home/poo/capnet-research-view`. Esa ruta es un snapshot sanitizado creado a partir de los commits `HEAD` de `/home/poo/capnet-workspace`; no contiene los checkouts operativos, metadatos `.git`, archivos no rastreados, binarios ni archivos que coincidan con los filtros de credenciales. Su configuración niega por defecto toda herramienta y habilita únicamente las operaciones locales de lectura necesarias.

Si todavía no está instalado:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Desde una terminal interactiva, abre OpenCode, ejecuta `/connect`, selecciona OpenAI y después ChatGPT Plus/Pro. No elijas API key si quieres utilizar esa suscripción. El flujo está descrito en la [documentación de proveedores de OpenCode](https://opencode.ai/docs/providers/).

Verifica que las credenciales del proveedor estén disponibles:

```bash
opencode auth list
opencode models openai
```

La configuración fija `openai/gpt-5.4-mini` para que una sesión nueva no elija Ollama u otro proveedor por historial. El comando de modelos debe listar ese identificador antes de iniciar el servicio. Codex CLI conserva su propia selección para implementar cambios.

El servicio usa un `HOME` aislado para que solo vea la credencial que necesita. Copia una vez el archivo de autenticación recién creado, sin mostrar su contenido:

```bash
install -d -m 700 /home/poo/.local/share/poo-ia-opencode-home/.local/share/opencode
install -m 600 \
  /home/poo/.local/share/opencode/auth.json \
  /home/poo/.local/share/poo-ia-opencode-home/.local/share/opencode/auth.json
HOME=/home/poo/.local/share/poo-ia-opencode-home \
  /home/poo/.opencode/bin/opencode auth list
```

Si la sesión caduca, reautentica directamente ese hogar aislado; no vuelvas a copiar una caché más amplia:

```bash
HOME=/home/poo/.local/share/poo-ia-opencode-home \
  /home/poo/.opencode/bin/opencode auth login
```

Crea y revisa el snapshot antes de instalar el servicio:

```bash
install -d -m 700 /home/poo/capnet-research-view
python3 /home/poo/poo-ia/worker/research_view.py \
  --source /home/poo/capnet-workspace \
  --target /home/poo/capnet-research-view
sed -n '1,80p' /home/poo/capnet-research-view/VIEW-MANIFEST.md
```

`VIEW-MANIFEST.md` identifica el commit exportado de cada repositorio y cuenta los archivos incluidos y omitidos. Verifica ahí que estén `brain-capnet` y los repositorios esperados. La unidad ejecuta el mismo exportador en `ExecStartPre`, así que cada inicio o reinicio de `opencode-capnet` reconstruye el snapshot de forma atómica antes de atender consultas.

Crea el archivo privado del servicio:

```bash
install -d -m 700 /home/poo/.config/poo-ia
nano /home/poo/.config/poo-ia/opencode-server.env
chmod 600 /home/poo/.config/poo-ia/opencode-server.env
```

Contenido:

```dotenv
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=el_mismo_secreto_opencode_del_env_del_bot
```

Instala la unidad:

```bash
install -d -m 700 /home/poo/.config/systemd/user
cp /home/poo/poo-ia/ops/opencode-capnet.service /home/poo/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now opencode-capnet
systemctl --user status opencode-capnet
```

La autenticación Basic y las variables `OPENCODE_SERVER_PASSWORD` y `OPENCODE_SERVER_USERNAME` corresponden al [servidor oficial de OpenCode](https://opencode.ai/docs/es/server/).

### 3. Autenticar Codex y GitHub

Codex se ejecuta en el host como `poo`; su sesión no se copia al contenedor. En la Beelink sin interfaz gráfica:

```bash
codex login --device-auth
codex login status
```

Abre en tu navegador el enlace mostrado e introduce el código de un solo uso. Este es el flujo oficial para una máquina remota y permite usar el inicio de sesión de ChatGPT; no configura `OPENAI_API_KEY`. Consulta la [documentación oficial de autenticación de Codex](https://learn.chatgpt.com/es-419/docs/auth).

Después autentica GitHub CLI y el protocolo usado por los remotos de Capnet:

```bash
gh auth login
gh auth status
git -C /home/poo/capnet-workspace/brain-capnet remote -v
ssh -T git@github.com
env -i HOME=/home/poo \
  PATH=/home/poo/.local/bin:/usr/local/bin:/usr/bin:/bin \
  GIT_TERMINAL_PROMPT=0 \
  git -C /home/poo/capnet-workspace/brain-capnet ls-remote origin HEAD
git config --global --get user.name
git config --global --get user.email
```

Si nombre o correo no están configurados, establécelos con tus valores reales antes de publicar. La prueba `ls-remote` reproduce un acceso sin agente SSH ni entrada interactiva, como el worker de systemd; debe terminar correctamente. El `push` debe funcionar con el remoto existente y `gh auth status` debe mostrar la cuenta que creará los PR.

### 4. Instalar el worker del host

```bash
cd /home/poo/poo-ia
install -d -m 700 /home/poo/.config/poo-ia
install -d -m 700 /home/poo/.config/systemd/user
python3 -m venv .venv-worker
.venv-worker/bin/python -m pip install --upgrade pip
.venv-worker/bin/python -m pip install -r requirements-worker.txt

cp ops/poo-ia-worker.env.example /home/poo/.config/poo-ia/worker.env
chmod 600 /home/poo/.config/poo-ia/worker.env
nano /home/poo/.config/poo-ia/worker.env
```

En `worker.env`, reemplaza `WORKER_PASSWORD` con el mismo valor usado como `WORKER_SERVER_PASSWORD` en `.env`. Confirma también las rutas reales de `codex`, `git` y `gh` con `command -v`.

Instala e inicia el servicio:

```bash
cp /home/poo/poo-ia/ops/poo-ia-worker.service /home/poo/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now poo-ia-worker
systemctl --user status poo-ia-worker
```

Comprueba que rechaza una solicitud anónima y acepta la autenticada. `curl -u poo-ia` solicitará la contraseña sin incluirla en el historial de la terminal:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4097/healthz
curl -u poo-ia http://127.0.0.1:4097/healthz
curl -u poo-ia http://127.0.0.1:4097/v1/repositories
```

La primera respuesta debe ser `401`. `/healthz` debe devolver `{"status":"ok",...}` y `/v1/repositories`, `{"repositories":[...]}` con los repositorios base limpios.

### 5. Iniciar Discord

Detén el contenedor antiguo si conserva el mismo nombre:

```bash
cd /home/poo/discord-bot
docker compose down
```

Construye e inicia Poo-IA:

```bash
cd /home/poo/poo-ia
docker compose config --quiet
docker compose build
docker compose run --rm --no-deps --user 0:0 \
  --cap-add CHOWN --cap-add DAC_OVERRIDE discord-bot \
  chown -R 10001:10001 /app/data
docker compose up -d
docker compose ps
docker compose logs --tail=100 discord-bot
```

El ajuste de propietario usa únicamente `CAP_CHOWN` y `CAP_DAC_OVERRIDE` en ese contenedor efímero: la segunda capacidad es necesaria para atravesar un directorio `0700` que todavía pertenezca a otro UID y la primera cambia su propietario. La aplicación normal corre como el usuario sin privilegios `10001`, sin capacidades Linux y con `no-new-privileges`. El volumen `poo-ia-data` conserva SQLite aunque se reconstruya el contenedor. No uses `docker compose down -v`, porque `-v` elimina esa memoria persistente.

Para que los servicios de usuario arranquen tras reiniciar la Beelink sin mantener una sesión SSH abierta, puede ser necesario habilitar una vez el *linger* de `poo` desde una cuenta con permisos administrativos:

```bash
sudo loginctl enable-linger poo
```

## Variables del núcleo

| Variable | Requerida | Uso |
| --- | --- | --- |
| `DISCORD_TOKEN` | Sí | Token privado del bot. |
| `DISCORD_CHANNEL_ID` | Sí | Único canal atendido. |
| `ALLOWED_USER_ID` | Sí | Único usuario atendido. |
| `OLLAMA_MODEL` | No | Por defecto `qwen2.5-coder:3b`. |
| `OLLAMA_BASE_URL` | No | Por defecto `http://127.0.0.1:11434`; debe ser loopback. |
| `OLLAMA_TIMEOUT_SECONDS` | No | Límite total de generación; por defecto 120 s. |
| `POOIA_DB_PATH` | No | Por defecto `/app/data/poo-ia.sqlite3`. |
| `MEMORY_RETENTION_DAYS` | No | Por defecto 7. |
| `MEMORY_MAX_EXCHANGES` | No | Por defecto 10. |
| `MEMORY_MAX_CONTEXT_CHARS` | No | Por defecto 12.000. |
| `OPERATIONAL_RETENTION_DAYS` | No | Retención de solicitudes y trabajos terminados; por defecto 30 días. |
| `JOB_MAX_CONCURRENT` | No | Debe permanecer en 1 para esta Beelink. |
| `OPENCODE_ENABLED` | No | Activa investigación; exige su contraseña si es `true`. |
| `OPENCODE_BASE_URL` | No | Por defecto `http://127.0.0.1:4096`; debe ser loopback. |
| `OPENCODE_SERVER_USERNAME` | No | Por defecto `opencode`. |
| `OPENCODE_SERVER_PASSWORD` | Con OpenCode | Secreto de al menos 16 caracteres compartido con `opencode-server.env`. |
| `OPENCODE_AGENT` | No | Por defecto `capnet-research`. |
| `OPENCODE_TIMEOUT_SECONDS` | No | Por defecto 300 s. |
| `OPENCODE_MAX_CONCURRENT` | No | Por defecto 1. |
| `WORKER_ENABLED` | No | Activa cambios y PR; exige su contraseña si es `true`. |
| `WORKER_BASE_URL` | No | Por defecto `http://127.0.0.1:4097`; debe ser loopback. |
| `WORKER_SERVER_USERNAME` | No | Por defecto `poo-ia`. |
| `WORKER_SERVER_PASSWORD` | Con worker | Secreto de al menos 16 caracteres compartido con `WORKER_PASSWORD`. |
| `WORKER_TIMEOUT_SECONDS` | No | Timeout de cada llamada HTTP; por defecto 30 s. |
| `WORKER_POLL_SECONDS` | No | Intervalo de consulta de trabajos; por defecto 2 s. |
| `AWS_ENABLED` | No | Su valor predeterminado es `false`; `true` impide iniciar por diseño. |

Las variables internas del worker, sus límites y rutas están explicados en [docs/operations.md](docs/operations.md).

## Límites iniciales de cambios

- Un repositorio por trabajo.
- Hasta cinco archivos versionados.
- Hasta 400 líneas añadidas o eliminadas en total.
- Un binario se considera fuera del presupuesto.
- El checkout base debe estar limpio y no cambia de rama ni recibe commits.
- Codex trabaja en `/home/poo/capnet-worktrees/<job_id>` sobre una rama `poo-ia/...`.
- En esta primera fase de un solo propietario, Codex usa el acceso del usuario `poo`; el worktree aislado, el presupuesto de diff y la revisión previa al PR son los límites operativos.
- La ejecución automática de comandos de prueba del repositorio permanece deshabilitada hasta contar con una sandbox dedicada; por ahora la validación se informa como `unavailable`.
- Un diff fuera de presupuesto bloquea la publicación hasta una autorización posterior que identifique el trabajo y pida forzarla.

Los límites no borran el resultado: el trabajo queda `prepared` para inspección. Los reintentos con el mismo ID son idempotentes; no crean otra ejecución, rama o PR.

## Pruebas

```bash
cd /home/poo/poo-ia
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-worker.txt
.venv/bin/python -m unittest discover -s tests -t . -v
```

Valida también la imagen:

```bash
docker compose build
docker compose run --rm --no-deps discord-bot python -c 'import app, app.discord_bot, app.orchestrator, app.storage'
```

## Operación y recuperación

El manual de salud, logs, reinicios, actualización, copias, recuperación y rollback está en [docs/operations.md](docs/operations.md).

Resumen diario:

```bash
systemctl --user is-active opencode-capnet poo-ia-worker
cd /home/poo/poo-ia
docker compose ps
docker compose logs --tail=50 discord-bot
codex login status
gh auth status
```

## AWS queda fuera de esta fase

No añadas `AWS_PROFILE`, `AWS_REGION`, claves ni archivos de credenciales para validar esta entrega. Las solicitudes sobre AWS o DynamoDB reciben una respuesta determinista que indica que la integración está pospuesta.

Cuando el propietario abra esa fase, el worker usará la cadena estándar de credenciales del usuario `poo` y exactamente los permisos de su identidad configurada. Ese trabajo posterior añadirá verificación con STS, operaciones DynamoDB paginadas e informes; no requiere cambiar la autenticación actual de Discord, memoria, OpenCode o Codex.

## Secretos y datos privados

- `.env`, `opencode-server.env` y `worker.env` deben tener permisos `600` y no pertenecen al repositorio.
- La caché de Codex pertenece al usuario `poo`; trátala como una contraseña y no la copies a Git, Discord o logs.
- OpenCode recibe únicamente el snapshot sanitizado de commits Git y usa un `HOME` aislado; los filtros del exportador excluyen `.env`, claves, certificados y contenido que parezca una credencial.
- SQLite, manifiestos, resultados y logs pueden contener solicitudes o fragmentos de código; no los publiques.
- Antes de hacer push, revisa `git status` y `git diff --cached`.
