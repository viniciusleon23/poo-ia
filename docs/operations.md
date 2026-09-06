# Manual operativo de Poo-IA

Este manual cubre el despliegue actual de una sola persona en la Beelink Ubuntu. La interfaz es Discord; la base persistente vive en Docker y OpenCode/Codex/GitHub viven en el host.

AWS no forma parte de este procedimiento. `AWS_ENABLED` debe permanecer en `false` y ninguna comprobación de este manual requiere credenciales AWS.

## Rutas y servicios

| Recurso | Ubicación |
| --- | --- |
| Repositorio Poo-IA | `/home/poo/poo-ia` |
| Repositorios Capnet | `/home/poo/capnet-workspace/<repositorio>` |
| Vista documental sanitizada | `/home/poo/capnet-research-view` |
| Worktrees aislados | `/home/poo/capnet-worktrees/<job_id>` |
| SQLite | volumen Docker, `/app/data/poo-ia.sqlite3` dentro del contenedor |
| Manifiesto del worker | `/home/poo/.local/share/poo-ia-worker/jobs/<job_id>.json` |
| Artefactos del trabajo | `/home/poo/.local/share/poo-ia-worker/jobs/<job_id>/` |
| Configuración privada del bot | `/home/poo/poo-ia/.env` |
| Configuración privada de OpenCode | `/home/poo/.config/poo-ia/opencode-server.env` |
| Hogar aislado de OpenCode | `/home/poo/.local/share/poo-ia-opencode-home` |
| Credencial aislada de OpenCode | `/home/poo/.local/share/poo-ia-opencode-home/.local/share/opencode/auth.json` |
| Configuración privada del worker | `/home/poo/.config/poo-ia/worker.env` |
| Servicio OpenCode | `opencode-capnet.service` del usuario `poo` |
| Servicio Codex/GitHub | `poo-ia-worker.service` del usuario `poo` |
| Bot | contenedor `poo-ia-discord` |

OpenCode escucha en `127.0.0.1:4096`; el worker en `127.0.0.1:4097`; Ollama en `127.0.0.1:11434`. No publiques estos puertos en el router ni cambies los dos workers a `0.0.0.0`.

El investigador documental usa de forma explícita `openai/gpt-5.4-mini`; comprueba que `opencode models openai` lo liste después de autenticar. Así no depende del último modelo usado ni selecciona por accidente el Ollama local. Codex CLI mantiene una configuración separada para los cambios.

El servidor de OpenCode y el modelo nunca trabajan sobre `/home/poo/capnet-workspace`. Su unidad crea antes de arrancar un snapshot atómico con el contenido de texto permitido de cada commit `HEAD`: no exporta `.git`, archivos no rastreados, binarios ni contenido detectado como secreto. El worker de cambios sí usa los checkouts base, pero en un proceso separado.

## Preparación inicial de OpenCode

Después de autenticar OpenCode como `poo`, copia únicamente su archivo de autenticación al `HOME` aislado, sin mostrar el contenido y con permisos `600`:

```bash
install -d -m 700 /home/poo/.local/share/poo-ia-opencode-home/.local/share/opencode
install -m 600 \
  /home/poo/.local/share/opencode/auth.json \
  /home/poo/.local/share/poo-ia-opencode-home/.local/share/opencode/auth.json
HOME=/home/poo/.local/share/poo-ia-opencode-home \
  /home/poo/.opencode/bin/opencode auth list
```

Crea el snapshot una vez para validar el exportador antes de iniciar el servicio:

```bash
install -d -m 700 /home/poo/capnet-research-view
python3 /home/poo/poo-ia/worker/research_view.py \
  --source /home/poo/capnet-workspace \
  --target /home/poo/capnet-research-view
sed -n '1,80p' /home/poo/capnet-research-view/VIEW-MANIFEST.md
```

El manifiesto debe listar `brain-capnet` y los repositorios esperados, con el SHA de `HEAD` y los conteos de archivos incluidos y omitidos. La unidad vuelve a ejecutar este exportador antes de cada arranque.

## Orden de arranque

1. Ollama.
2. OpenCode documental.
3. Worker Codex/GitHub.
4. Contenedor de Discord.

Los servicios están diseñados para recuperarse si el orden cambia, pero este orden produce diagnósticos más claros.

```bash
systemctl is-active ollama
systemctl --user start opencode-capnet
systemctl --user start poo-ia-worker
cd /home/poo/poo-ia
docker compose up -d
```

## Comprobación de salud

### Vista rápida

```bash
systemctl --user is-active opencode-capnet poo-ia-worker
systemctl --user --no-pager --full status opencode-capnet poo-ia-worker
cd /home/poo/poo-ia
docker compose ps
docker inspect --format '{{.State.Status}} {{.RestartCount}}' poo-ia-discord
ss -ltn '( sport = :4096 or sport = :4097 or sport = :11434 )'
```

Los puertos 4096 y 4097 deben aparecer enlazados a loopback. Ollama debe responder:

```bash
curl -fsS http://127.0.0.1:11434/api/tags
```

OpenCode debe rechazar una petición sin credenciales. La petición autenticada solicita la contraseña sin escribirla en el comando:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4096/doc
curl -fsS -u opencode http://127.0.0.1:4096/doc -o /dev/null
```

La primera debe devolver `401`. La segunda termina sin error si la contraseña corresponde a `OPENCODE_SERVER_PASSWORD`.

Comprueba también la procedencia del snapshot:

```bash
test -f /home/poo/capnet-research-view/VIEW-MANIFEST.md
sed -n '1,80p' /home/poo/capnet-research-view/VIEW-MANIFEST.md
if find /home/poo/capnet-research-view -type d -name .git -print -quit | grep -q .; then
  echo 'Error: la vista contiene metadatos Git.' >&2
  exit 1
fi
```

`VIEW-MANIFEST.md` debe listar `brain-capnet` y los demás repositorios exportados, con el SHA de `HEAD` y los conteos de archivos incluidos y omitidos.

El worker se valida igual:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4097/healthz
curl -fsS -u poo-ia http://127.0.0.1:4097/healthz
curl -fsS -u poo-ia http://127.0.0.1:4097/v1/repositories
```

La primera debe devolver `401`. `/healthz` devuelve JSON con `status: ok`; `/v1/repositories` devuelve una lista `repositories` con los repositorios base limpios. Si falta un repositorio, comprueba su estado:

```bash
git -C /home/poo/capnet-workspace/nombre-del-repo status --short
```

Codex y GitHub deben usar las sesiones del usuario `poo`:

```bash
codex login status
gh auth status
ssh -T git@github.com
```

Por último, revisa que Discord haya conectado al canal esperado:

```bash
cd /home/poo/poo-ia
docker compose logs --tail=100 discord-bot
```

## Prueba funcional desde Discord

Ejecuta estas pruebas en orden y espera la respuesta de cada una:

1. `hola` — confirma Ollama.
2. `¿Dónde está documentado el servicio de tareas?` — confirma OpenCode y la cola.
3. `¿Qué repositorio acabamos de consultar?` — confirma contexto conversacional.
4. Reinicia el contenedor y pregunta `¿qué acabamos de revisar?` — confirma SQLite.
5. Solicita una respuesta extensa — confirma la división en mensajes de hasta 1.900 caracteres.
6. `olvida la conversación` y vuelve a preguntar por lo anterior — confirma el olvido.
7. Solicita un cambio pequeño y explícito en un repositorio de prueba — confirma Codex, el worktree y la validación.
8. `cómo va` — confirma consulta de estado.
9. En otro trabajo largo, `cancela ese trabajo` — confirma cancelación.
10. `arma el PR del trabajo <id-corto>` — confirma Git y GitHub solo cuando se quiera publicar.

Antes y después de la prueba de cambio, el checkout base debe conservar la misma rama, commit y estado limpio. El cambio esperado debe existir únicamente en `/home/poo/capnet-worktrees/<job_id>` y en la rama `poo-ia/...`.

## Estados de trabajo

| Estado | Significado |
| --- | --- |
| `queued` | Espera su turno en la cola global. |
| `running` | OpenCode o Codex está trabajando. |
| `prepared` | El cambio y su diagnóstico existen en un worktree, todavía sin PR o bloqueados por una validación. |
| `publishing` | Git/GitHub está creando o recuperando la publicación. |
| `succeeded` | La investigación terminó o el PR quedó publicado. |
| `failed` | Falló con un detalle seguro y el diagnóstico se conserva. |
| `cancelled` | El propietario canceló una tarea en cola o activa. |

La forma preferida de consultar el último trabajo es escribir `cómo va` en Discord. Para uno anterior, incluye el ID corto que mostró el bot. Los mensajes duplicados de Discord y los reintentos HTTP conservan el mismo identificador; no crean otro cambio ni otro PR.

En esta fase los comandos de prueba provenientes de repositorios no se ejecutan automáticamente: la validación queda en `unavailable` hasta que exista una sandbox dedicada para ellos. El detector se conserva para pruebas internas, pero no se confía en `AGENTS.md`, `Makefile` ni scripts del repositorio durante la operación normal.

La cancelación aplica a trabajos `queued` o `running`. Un trabajo `prepared` ya no está ejecutándose: conserva el worktree para revisión, publicación posterior o limpieza manual.

## Logs y diagnóstico

### Discord y núcleo

```bash
cd /home/poo/poo-ia
docker compose logs --tail=200 discord-bot
docker compose logs -f discord-bot
```

### OpenCode

```bash
journalctl --user -u opencode-capnet -n 200 --no-pager
journalctl --user -u opencode-capnet -f
```

Un timeout o cancelación debe pedir a OpenCode que aborte la sesión antes de eliminarla. Si OpenCode perdió su sesión con el proveedor, vuelve a autenticar el `HOME` aislado y reinicia solo ese servicio:

```bash
HOME=/home/poo/.local/share/poo-ia-opencode-home \
  /home/poo/.opencode/bin/opencode auth login
systemctl --user restart opencode-capnet
```

### Worker

```bash
journalctl --user -u poo-ia-worker -n 200 --no-pager
journalctl --user -u poo-ia-worker -f
ls -la /home/poo/.local/share/poo-ia-worker/jobs
```

Cada trabajo tiene un manifiesto hermano `<job_id>.json`; su directorio `<job_id>/` puede incluir salida final, `change.diff`, `worker.log` y `validation.log`. Estos archivos pueden contener la solicitud o detalles internos; consúltalos solo localmente y no los pegues completos en Discord.

### SQLite

Comprueba que el volumen exista:

```bash
cd /home/poo/poo-ia
docker volume ls --filter label=com.docker.compose.project=poo-ia
docker compose exec -T discord-bot python -c 'import os,sqlite3; p=os.environ["POOIA_DB_PATH"]; c=sqlite3.connect(p); print(c.execute("PRAGMA integrity_check").fetchone()[0])'
```

El resultado esperado es `ok`. No abras la base con una herramienta que escriba mientras el bot está activo.

## Recuperación tras reinicios

### Solo reinició el contenedor

SQLite conserva la memoria, la cola y las partes de respuesta pendientes. Inicia el contenedor y observa que vuelva a conectar:

```bash
cd /home/poo/poo-ia
docker compose up -d
docker compose logs --tail=100 discord-bot
```

No uses `docker compose down -v`: elimina el volumen de SQLite.

### Reinició OpenCode

```bash
systemctl --user restart opencode-capnet
systemctl --user --no-pager status opencode-capnet
sed -n '1,80p' /home/poo/capnet-research-view/VIEW-MANIFEST.md
```

`ExecStartPre` reconstruye de forma atómica `/home/poo/capnet-research-view` en cada inicio o reinicio, antes de que OpenCode escuche. El manifiesto debe reflejar los `HEAD` actuales. Una investigación en curso puede reintentarse con el mismo trabajo persistido; comprueba su estado desde Discord.

### Reinició el servicio worker

Los procesos Codex se lanzan separados del proceso HTTP y los manifiestos viven fuera del servicio. Tras reiniciar, el worker reconcilia el proceso y el estado existentes:

```bash
systemctl --user restart poo-ia-worker
systemctl --user --no-pager status poo-ia-worker
curl -fsS -u poo-ia http://127.0.0.1:4097/healthz
```

### Reinició toda la Beelink

Con `linger` habilitado, systemd restaura los servicios de usuario. El núcleo vuelve a tomar trabajos persistidos; el worker recupera los que están en cola y puede reintentar una ejecución interrumpida una vez sobre el mismo `job_id` y worktree.

```bash
systemctl is-active ollama
systemctl --user is-active opencode-capnet poo-ia-worker
cd /home/poo/poo-ia
docker compose ps
```

Si un trabajo no puede continuar, pasa a `failed` sin borrar el diff o los logs existentes.

## Copia de seguridad

Detén nuevas solicitudes de Discord durante la copia. La API de backup de SQLite permite obtener una base consistente sin copiar directamente archivos WAL:

```bash
install -d -m 700 /home/poo/backups/poo-ia
install -m 600 /dev/null /home/poo/backups/poo-ia/poo-ia-backup.sqlite3
cd /home/poo/poo-ia
docker compose run --rm --no-deps --user 0:0 --cap-add DAC_OVERRIDE \
  -v /home/poo/backups/poo-ia:/backup \
  discord-bot python -c 'import os,sqlite3; s=sqlite3.connect(os.environ["POOIA_DB_PATH"]); d=sqlite3.connect("/backup/poo-ia-backup.sqlite3"); s.backup(d); d.close(); s.close()'
```

Cambia el nombre del archivo entre copias para no sobrescribir la anterior. Conserva juntos los manifiestos y worktrees cuando haya cambios preparados. Primero confirma que no haya trabajos `queued`, `running` ni `publishing`; después detén el servicio y verifica que no sobreviva un proceso desacoplado:

```bash
systemctl --user stop poo-ia-worker
if pgrep -u poo -f 'python.*worker\.job_process' >/dev/null; then
  echo 'Todavía hay un trabajo activo; no hagas la copia.' >&2
  exit 1
fi
cp -a /home/poo/.local/share/poo-ia-worker /home/poo/backups/poo-ia/worker-state
cp -a /home/poo/capnet-worktrees /home/poo/backups/poo-ia/capnet-worktrees
systemctl --user start poo-ia-worker
```

Los backups contienen conversación y código; protégelos como datos privados.

### Restaurar SQLite

La restauración reemplaza la memoria y cola actuales. Haz primero una copia del estado presente y después detén el bot:

```bash
cd /home/poo/poo-ia
docker compose stop discord-bot
docker compose run --rm --no-deps --user 0:0 --cap-add DAC_OVERRIDE \
  -v /home/poo/backups/poo-ia:/backup:ro \
  discord-bot python -c 'import os,sqlite3; s=sqlite3.connect("file:/backup/poo-ia-backup.sqlite3?mode=ro",uri=True); d=sqlite3.connect(os.environ["POOIA_DB_PATH"]); s.backup(d); d.close(); s.close()'
docker compose run --rm --no-deps --user 0:0 --cap-add CHOWN discord-bot \
  chown -R 10001:10001 /app/data
docker compose up -d discord-bot
```

Restaura manifiestos y worktrees únicamente como un conjunto consistente. Detén `poo-ia-worker` y verifica con el mismo `pgrep` que no quede `worker.job_process`: `KillMode=process` permite que un trabajo ya lanzado sobreviva al servicio HTTP. No combines un manifiesto de un trabajo con otro worktree.

## Actualización

Primero detén el ingreso de Discord. Deja que el worker termine cualquier estado `queued`, `running` o `publishing` y confirma también que no sobreviva una publicación automática en el breve handoff `prepared`. Después detén los dos servicios del host. Esto evita mezclar un proceso hijo con código antiguo, un worker nuevo y otro esquema de manifiesto.

El bloque siguiente crea la copia **antes** de descargar código o ejecutar una migración:

```bash
cd /home/poo/poo-ia
pooia_backup_dir=/home/poo/backups/poo-ia/pre-update-$(date +%Y%m%d-%H%M%S)
install -d -m 700 "$pooia_backup_dir"
install -m 600 /dev/null "$pooia_backup_dir/poo-ia.sqlite3"
docker compose stop discord-bot
docker compose run --rm --no-deps --user 0:0 --cap-add DAC_OVERRIDE \
  -v "$pooia_backup_dir:/backup" \
  discord-bot python -c 'import os,sqlite3; s=sqlite3.connect(os.environ["POOIA_DB_PATH"]); d=sqlite3.connect("/backup/poo-ia.sqlite3"); s.backup(d); d.close(); s.close()'

curl -fsS -u poo-ia http://127.0.0.1:4097/healthz
```

No continúes mientras el JSON muestre elementos `queued`, `running` o `publishing`, ni mientras exista un proceso del trabajo. Cuando todos estén en cero:

```bash
systemctl --user stop poo-ia-worker opencode-capnet
if pgrep -u poo -f 'python.*worker\.job_process' >/dev/null; then
  echo 'Todavía hay un trabajo activo; no actualices.' >&2
  exit 1
fi

cd /home/poo/poo-ia
git status --short
git pull --ff-only

.venv-worker/bin/python -m pip install -r requirements-worker.txt
cp ops/opencode-capnet.service ops/poo-ia-worker.service /home/poo/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start opencode-capnet poo-ia-worker

docker compose config --quiet
docker compose build
docker compose run --rm --no-deps --user 0:0 --cap-add CHOWN discord-bot \
  chown -R 10001:10001 /app/data
docker compose up -d
docker compose ps
```

Ejecuta después la comprobación de salud y las primeras seis pruebas funcionales. Un cambio de `rules/` o `personality/` se aplica al reiniciar el contenedor.

## Rollback de código

La base de datos puede contener una migración que una versión anterior no entienda. Antes del rollback repite el mismo drenado: detén Discord, espera hasta que no haya estados `queued`, `running` o `publishing`, detén el worker y comprueba con `pgrep` que no quede `worker.job_process`. Conserva SQLite, manifiestos y worktrees. El arranque rechaza migraciones con checksum cambiado y bases con una versión futura: nunca edites un archivo de migración ya aplicado; añade `002_...`, `003_...`, etc.

Para ejecutar temporalmente un commit conocido sin reescribir ramas:

```bash
cd /home/poo/poo-ia
git switch --detach COMMIT_CONOCIDO
.venv-worker/bin/python -m pip install -r requirements-worker.txt
systemctl --user restart poo-ia-worker
docker compose up -d --build
```

Para regresar a la versión actual:

```bash
cd /home/poo/poo-ia
git switch main
git pull --ff-only
systemctl --user restart poo-ia-worker
docker compose up -d --build
```

Si el commit anterior no reconoce la versión de SQLite, restaura la copia `pre-update` correspondiente siguiendo “Restaurar SQLite” antes de iniciarlo. No borres el volumen, manifiestos, ramas ni worktrees durante un rollback. Una vez diagnosticado el problema, el rollback permanente debe hacerse mediante un commit de reversión revisable.

## Problemas frecuentes

| Síntoma | Comprobación |
| --- | --- |
| El bot no responde | Canal y usuario de `.env`, Message Content Intent y logs del contenedor. |
| Responde a `hola` pero no investiga | Estado de OpenCode, contraseña emparejada, manifiesto de la vista y `HOME=/home/poo/.local/share/poo-ia-opencode-home /home/poo/.opencode/bin/opencode auth list`. |
| Investiga pero no prepara cambios | Worker activo, sesión Codex y repositorio presente/limpio. |
| Worker devuelve 401 | `WORKER_SERVER_USERNAME/PASSWORD` del bot deben coincidir con `WORKER_USERNAME/PASSWORD` del host. |
| OpenCode devuelve 401 | `OPENCODE_SERVER_USERNAME/PASSWORD` deben coincidir en ambos archivos. |
| Repo no aparece | Debe ser hijo directo de `capnet-workspace`, un repositorio Git y tener checkout base limpio. |
| Codex pide login | Ejecuta `codex login --device-auth` como `poo` y confirma `codex login status`. |
| No puede hacer push o PR | Revisa `gh auth status`, el remoto Git y `ssh -T git@github.com`. |
| Cambio queda `prepared` | Revisa validación y presupuesto; publica después con una orden que identifique el trabajo. |
| Mensaje AWS no se ejecuta | Es el comportamiento esperado de esta fase; `AWS_ENABLED=true` está rechazado. |
| Memoria desapareció | Revisa el volumen, que no se haya usado `down -v` y que no se haya enviado la frase de olvido. |

## Configuración del worker

| Variable | Valor predeterminado o esperado |
| --- | --- |
| `WORKER_HOST` | `127.0.0.1`; debe ser loopback. |
| `WORKER_PORT` | `4097`. |
| `WORKER_USERNAME` | `poo-ia`. |
| `WORKER_PASSWORD` | Secreto de al menos 16 caracteres. |
| `CAPNET_WORKSPACE` | `/home/poo/capnet-workspace`. |
| `CAPNET_WORKTREES` | `/home/poo/capnet-worktrees`. |
| `WORKER_DATA_ROOT` | `/home/poo/.local/share/poo-ia-worker`. |
| `OPERATIONAL_RETENTION_DAYS` | `30`; aplica a manifiestos y artefactos terminales del worker. |
| `WORKER_RETENTION_SWEEP_SECONDS` | `3600`; intervalo del barrido cuando el worker está libre. |
| `CODEX_EXECUTABLE` | Ruta real de `codex`. |
| `GIT_EXECUTABLE` | Ruta real de `git`. |
| `GH_EXECUTABLE` | Ruta real de `gh`. |
| `CODEX_MAX_CHANGED_FILES` | `5`. |
| `CODEX_MAX_CHANGED_LINES` | `400`. |
| `CODEX_TIMEOUT_SECONDS` | `1800`. |
| `VALIDATION_TIMEOUT_SECONDS` | `600`. |
| `GITHUB_TIMEOUT_SECONDS` | `120`. |
| `WORKER_POLL_SECONDS` | `0.5` en el host. |

El núcleo elimina al arrancar y después cada hora la memoria que supera `MEMORY_RETENTION_DAYS` y las solicitudes o trabajos terminales ya entregados que superan su propio `OPERATIONAL_RETENTION_DAYS` de `.env`. El worker usa la variable homónima de `worker.env` para manifiestos, artefactos, worktrees y ramas locales terminales. Ambos valores son 30 días por defecto, pero se configuran por separado. Los trabajos activos, preparados o con salida de Discord pendiente nunca se eliminan por antigüedad; el worker tampoco elimina ramas remotas.

No añadas variables AWS al archivo del worker en esta fase.
