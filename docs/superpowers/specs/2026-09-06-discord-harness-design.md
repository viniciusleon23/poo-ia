# Poo-IA: diseño del harness modular con Discord como primera interfaz

**Fecha:** 2026-09-06
**Estado:** aprobado para especificación e implementación
**Decisión:** opción 2, núcleo modular en Docker y workers locales en la Beelink

## Propósito

Convertir el bot actual de Discord en la primera interfaz de un asistente personal persistente. Poo-IA debe conversar sin prefijos, recordar el contexto durante varios días, consultar la documentación de Capnet y ejecutar cambios pequeños en repositorios mediante Codex CLI. La arquitectura reserva una integración posterior con DynamoDB usando la identidad AWS del propietario, pero esta fase no instala ni configura credenciales AWS. Una aplicación web podrá reutilizar después el mismo núcleo sin reescribir estas capacidades.

Esta entrega se considera terminada únicamente cuando el sistema esté versionado, desplegado en la Beelink y comprobado de extremo a extremo. No basta con que las unidades funcionen localmente.

## Decisiones del alcance

- Discord sigue siendo la única interfaz en esta entrega. La interfaz web queda preparada por las fronteras del núcleo, pero no se construye todavía.
- El bot responde a cualquier mensaje humano aceptado, sin `!` ni otro prefijo.
- Solo procesa el canal configurado y solo los mensajes del identificador Discord del propietario.
- `qwen2.5-coder:3b` mediante Ollama atiende conversación breve e inequívoca.
- OpenCode continúa dedicado a investigación documental y permanece sin permisos de edición.
- Un worker local separado ejecuta Codex, Git y GitHub como el usuario Linux `poo`, y reserva la integración AWS posterior.
- AWS permanece desactivado en esta fase. Cuando el propietario decida activarlo, las credenciales configuradas para `poo` conservarán su misma identidad y permisos; no se crea ni se propone un rol distinto para Poo-IA.
- Codex usa el inicio de sesión de la suscripción ChatGPT mediante Codex CLI. No se configura `OPENAI_API_KEY`.
- Una solicitud explícita de cambio autoriza preparar el cambio local. Una solicitud explícita de crear o armar el PR autoriza además publicar la rama y crear el PR cuando las pruebas y los límites de la tarea se cumplan.
- No se fusionan PR ni se despliegan cambios como parte de esta entrega.
- Solo se ejecuta un trabajo pesado a la vez en la Beelink.

Este documento reemplaza las exclusiones de memoria y base de datos del diseño inicial `2026-09-06-poo-ia-design.md`, y reemplaza la exclusión de invocación automática de Codex del diseño `2026-09-06-opencode-research-worker-design.md`. Conserva todos los filtros de Discord y conserva OpenCode como worker de solo lectura.

## Arquitectura aprobada

```text
Discord Gateway (contenedor, network_mode: host)
    │
    ▼
Poo-IA Core (mismo contenedor)
    ├── filtros de canal y propietario
    ├── orquestador y router
    ├── reglas y personalidad
    ├── memoria SQLite persistente
    ├── cola, estados y entregas pendientes
    │
    ├──► Ollama 127.0.0.1:11434
    ├──► OpenCode 127.0.0.1:4096
    └──► Poo-IA Worker 127.0.0.1:4097
             ├── Codex CLI
             ├── Git / worktrees
             └── GitHub CLI / PR

    [fase posterior] ──► AWS SDK / DynamoDB
```

El núcleo permanece en Docker, como se aprobó. El worker vive en el host y se ejecuta como servicio de usuario `poo`; de ese modo usa directamente sus sesiones de AWS, Codex, GitHub y SSH sin montar credenciales ni el workspace de escritura dentro del contenedor. Ambos se comunican por HTTP autenticado exclusivamente en loopback.

SQLite tiene un único escritor: el núcleo del contenedor. Su archivo vive en un volumen persistente de Docker. El worker no abre la base de datos; expone operaciones idempotentes y conserva únicamente manifiestos operativos de los procesos que necesiten sobrevivir al reinicio del bot.

La futura web hablará con una API del mismo núcleo. La lógica de conversación, memoria, trabajos y backends no podrá depender de objetos de Discord; Discord será un adaptador de entrada y salida.

## Componentes y fronteras

### Adaptador Discord

Responsabilidades:

- ignorar mensajes de bots, vacíos, de otros canales o de otros usuarios;
- entregar al orquestador un objeto neutral con `message_id`, `channel_id`, `user_id` y texto;
- enviar acuses, resultados y archivos generados;
- dividir texto en fragmentos de hasta 1.900 caracteres;
- registrar cada fragmento entregado para reanudar desde la primera parte aún no confirmada;
- mostrar estados comprensibles mientras un trabajo está en cola o ejecutándose.

No decide qué modelo usar, no abre SQLite directamente fuera de las interfaces del núcleo y no ejecuta terminal.

### Orquestador

El orquestador es la única entrada a la lógica de Poo-IA. Por cada conversación:

1. deduplica el mensaje usando el ID externo de Discord;
2. toma un bloqueo asíncrono por `(channel_id, user_id)`;
3. carga contexto completado, repositorio activo y trabajo activo;
4. resuelve controles naturales como olvidar, estado, cancelar o aprobar;
5. clasifica la intención;
6. responde inmediatamente o crea un trabajo persistente;
7. prepara la salida fragmentada;
8. guarda el intercambio solo cuando la salida completa fue confirmada por Discord.

Mensajes de conversaciones diferentes pueden entrar a la vez. Toda llamada a OpenCode, Codex o GitHub entra como trabajo en el mismo planificador persistente del núcleo; su semáforo global solo permite un trabajo pesado activo. Qwen, los controles de memoria y las consultas de estado no ocupan ese semáforo.

### Router de intenciones

Las intenciones iniciales son:

| Intención | Ejemplo | Destino |
| --- | --- | --- |
| `chat` | “hola”, “gracias” | Ollama/Qwen |
| `research` | pregunta sobre servicios o código | OpenCode |
| `aws_report` | consulta o informe de DynamoDB | aviso de integración pospuesta; Worker AWS cuando se active |
| `code_change` | “agrega este campo en ese repo” | Worker Codex |
| `pull_request` | “haz el cambio y arma el PR” | Codex y GitHub |
| `job_status` | “cómo va”, “estado del trabajo” | almacenamiento de trabajos |
| `cancel` | “cancela ese trabajo” | cola/worker |
| `forget` | “olvida la conversación” | memoria |
| `clarify` | falta el repositorio o un dato imprescindible | pregunta breve al propietario |

Los controles y las intenciones mutables se reconocen primero mediante reglas deterministas. Los saludos exactos van a Qwen. La investigación es el destino conservador de cualquier consulta técnica que no sea claramente AWS o un cambio de código. Qwen no tiene autoridad para convertir una petición ambigua en una mutación.

Referencias como “hazlo”, “ese repo” o “el campo anterior” se resuelven con la memoria, el repositorio activo y el trabajo activo. Si dos repositorios siguen siendo candidatos, el sistema pregunta cuál usar en lugar de adivinar.

### Reglas y personalidad

`rules/` y `personality/` permanecen fuera del código. El cargador crea un contexto estable y versionable que se aplica a las respuestas de Qwen, OpenCode y Codex, y después se reutilizará para AWS.

Las reglas se separan al menos en:

- conducta general y formato de respuesta;
- memoria y resolución de referencias;
- investigación documental y citas de rutas;
- cambios de código, pruebas y publicación;
- consultas e informes AWS.

La personalidad define tono e identidad, pero nunca concede capacidades o permisos. Las operaciones habilitadas proceden del router, la configuración y el worker.

### OpenCode documental

Se conserva el servicio existente en `127.0.0.1:4096` con autenticación y configuración versionada. Cada investigación recibe:

- la pregunta actual;
- el contexto conversacional relevante;
- el repositorio activo, si existe;
- las reglas de respuesta;
- la instrucción de respaldar hechos con rutas dentro de `capnet-workspace`.

OpenCode crea una sesión efímera, responde y la elimina. Continúa sin `edit`, `bash` ni `task`; la versión instalada no permite garantizar el límite de subagentes, por lo que la delegación permanece deshabilitada. Un timeout debe solicitar abortado antes de limpiar la sesión.

### Worker local

El nuevo servicio `poo-ia-worker` escucha solo en `127.0.0.1:4097`, exige un secreto compartido y se ejecuta bajo `poo`. Su API mínima en esta fase es:

- `GET /healthz`;
- `POST /v1/jobs/codex`;
- `GET /v1/jobs/{job_id}`;
- `POST /v1/jobs/{job_id}/cancel`;
- `POST /v1/jobs/{job_id}/publish`.

Cada `job_id` es una clave de idempotencia. Repetir la misma llamada devuelve el mismo trabajo y nunca vuelve a aplicar un cambio o crear un segundo PR. Los manifiestos se guardan bajo `/home/poo/.local/share/poo-ia-worker/jobs/` sin secretos. Al arrancar, el worker reconcilia procesos, ramas y PR existentes antes de declarar un trabajo interrumpido.

El núcleo deriva un `request_id` estable de la fuente y el ID del mensaje Discord, crea `inbound_request` y `job` en una sola transacción y confirma esa transacción antes de llamar al worker. El `job_id` es determinista para ese `request_id`. El worker guarda el hash del payload con el primer uso: una repetición idéntica devuelve el trabajo existente y una repetición del mismo ID con otro payload devuelve conflicto. Así, un timeout HTTP no puede crear otra ejecución.

### AWS y DynamoDB — integración posterior

Esta fase deja definida la frontera y la configuración, pero mantiene `AWS_ENABLED=false`. El endpoint de ejecución AWS se omite; el router devuelve un resultado determinista `AWS_DISABLED` que explica que la integración está pospuesta. No se añade `boto3`, no se instala AWS CLI, no se copia un perfil, no se inicia sesión y no se manipulan credenciales. Cuando el propietario abra la fase AWS, el worker usará `boto3` y la cadena de credenciales estándar del usuario `poo`, incluyendo `AWS_PROFILE` y `AWS_REGION` cuando estén configurados. La identidad se verificará con STS antes de habilitar el backend. No se copiarán claves a Git, SQLite, prompts, Discord ni logs.

La primera superficie funcional de la fase AWS cubrirá:

- obtener la identidad activa;
- listar y describir tablas accesibles;
- `GetItem`, `Query`, `Scan` paginado y `BatchGetItem`;
- seleccionar atributos y límites;
- producir resúmenes y archivos JSON o CSV para informes.

Estas capacidades usan exactamente los permisos que AWS conceda a la identidad configurada; la aplicación no altera esa identidad. Otros servicios u operaciones podrán añadirse después sin cambiar la autenticación.

Cuando esa fase se active y una petición natural necesite traducirse a una operación DynamoDB, OpenCode preparará un plan JSON tipado. El núcleo validará su esquema y el worker ejecutará únicamente esa operación estructurada; nunca se ejecutará texto del modelo como shell. El resultado identificará tabla, región, paginación y cantidad de elementos sin mostrar valores secretos.

### Codex, Git y GitHub

Codex CLI se ejecuta de forma no interactiva con la sesión ChatGPT del usuario `poo`, sin fijar un modelo y sin clave API. Para cada trabajo:

1. se resuelve un único repositorio real dentro de `/home/poo/capnet-workspace`;
2. OpenCode ejecuta un preflight documental de solo lectura y devuelve rutas y restricciones relevantes; este paso solo se omite si la petición es totalmente mecánica, contiene una ruta de archivo inequívoca y el propietario dice expresamente “sin investigar”;
3. se comprueba el estado del repositorio base;
4. se crea un worktree en `/home/poo/capnet-worktrees/<job_id>/` y una rama `poo-ia/<job_id>-<slug>`;
5. se ejecuta `codex exec` con `--sandbox workspace-write`, la petición, la memoria necesaria y el preflight, usando instrucciones acotadas y salida estructurada;
6. se ejecutan las pruebas pertinentes indicadas por el repositorio o identificadas por Codex;
7. se calcula y guarda el diff, el resumen y el resultado de pruebas;
8. se devuelve el resultado a Discord.

Un “cambio pequeño” inicial puede modificar como máximo un repositorio, cinco archivos versionados y 400 líneas añadidas o eliminadas. El presupuesto se mide después de la ejecución con `git diff --numstat`: un renombre cuenta como un archivo; archivos nuevos y eliminados cuentan; las líneas añadidas y eliminadas se suman; cualquier binario se considera fuera del presupuesto. Excederlo no descarta el trabajo: conserva el worktree en estado `prepared`, muestra la medida real y bloquea la publicación automática hasta una orden explícita que mencione el trabajo. Los límites son configurables.

“Haz el cambio” prepara la rama y deja el trabajo en `prepared`, listo para inspección o publicación posterior. “Haz el cambio y arma el PR” contiene autorización de publicación: si la validación lo permite, el worker hace `push` y usa `gh pr create`; después devuelve una URL verificable. Una orden posterior como “arma el PR del trabajo ABC123” mueve un trabajo `prepared` a `publishing`. Repetir cualquier orden comprueba primero si ya existe la rama o el PR. El proceso no modifica archivos del working tree base ni mueve o crea commits sobre su rama base o `main`; los refs y metadatos Git compartidos sí pueden reflejar el worktree y la rama nueva.

La validación tiene estados explícitos: `passed`, `unchanged_failure`, `failed`, `unavailable` y `timed_out`. El worker obtiene el comando de `AGENTS.md`, documentación del repositorio o manifiestos estándar; si no existe un comando local reproducible, usa `unavailable`. Cuando una prueba posterior falla y es razonable ejecutarla en el commit base, compara ambos resultados: el mismo fallo preexistente produce `unchanged_failure`. Una petición que ya incluye “arma el PR” puede publicarse automáticamente con `passed`, `unchanged_failure` o `unavailable`, indicando claramente los dos últimos. `failed` y `timed_out` requieren una orden posterior que identifique el trabajo y autorice publicar pese al resultado.

## Persistencia

El volumen `poo-ia-data` contiene `/app/data/poo-ia.sqlite3`. SQLite usa claves foráneas, WAL, `busy_timeout` y migraciones versionadas.

### Tablas

`conversations`

- identidad por `source`, `channel_id` y `user_id`;
- repositorio activo y último trabajo;
- fechas de creación y actualización.

`inbound_requests`

- ID del mensaje Discord con restricción única;
- contenido, intención, backend, estado y trabajo asociado;
- permite recibir de nuevo un evento sin duplicar ejecución;
- se conserva durante 30 días para idempotencia y nunca se incorpora al prompt directamente.

`exchanges`

- texto de usuario, texto de asistente, backend y fecha;
- se crea solo después de confirmar todos los fragmentos salientes;
- un intercambio fallido o parcialmente enviado no entra al contexto futuro.

`jobs`

- UUID, petición, tipo, repositorio, estado, rama y referencia externa;
- payload y checkpoint JSON sin secretos;
- resumen, error seguro, contador de intentos y marcas de tiempo;
- los trabajos terminales y su texto operativo se conservan 30 días.

`job_events`

- historial append-only de transiciones y progreso seguro;
- sigue la retención de 30 días del trabajo asociado.

`outbox` y `outbox_parts`

- salidas prefragmentadas, orden y confirmación de cada parte;
- permiten continuar una respuesta después de reiniciar sin repetir partes confirmadas.

`schema_migrations`

- versiones aplicadas de manera atómica.

### Política de memoria

- clave de conversación: `(channel_id, user_id)`;
- retención: siete días;
- máximo: diez intercambios completados;
- límite: 12.000 caracteres de contexto, eliminando primero los intercambios más antiguos;
- limpieza al iniciar, leer y escribir;
- `olvida la conversación` borra los intercambios y reinicia repositorio/último trabajo de esa conversación;
- el olvido no borra el historial operativo de trabajos ni ramas, necesario para evitar duplicados y poder auditar resultados;
- ningún request, trabajo, evento o manifiesto retenido por operación vuelve a entrar en un prompt después del olvido;
- trabajos no terminales se conservan hasta concluir; worktrees y manifiestos terminales se limpian después de 30 días;
- el archivo y su volumen sobreviven reconstrucciones y reinicios.

## Estados y recuperación

Los estados canónicos son:

```text
queued → running → succeeded
            ├──→ prepared → publishing → succeeded
            ├──→ failed
            └──→ cancelled
queued ────────────────────────────────────────→ cancelled
```

Un trabajo de consulta puede pasar directamente de `running` a `succeeded`. Las transiciones se validan en el almacenamiento y se registran en `job_events`.

La recuperación distingue tres escenarios:

- si reinicia solo el contenedor, los procesos del worker siguen y el núcleo vuelve a consultar el mismo `job_id`;
- si reinicia el servicio worker, los procesos Codex se ejecutan en unidades transitorias de usuario independientes y se reconcilian mediante manifiesto y archivos de salida;
- si reinicia toda la máquina, los trabajos `queued` vuelven a FIFO, las consultas de solo lectura se reintentan y un Codex interrumpido continúa una sola vez sobre el mismo worktree y `job_id`; si no puede continuar, queda `failed` conservando el diff;
- antes de reintentar una publicación GitHub se comprueban rama remota y PR por `job_id` para recuperar la URL existente;
- las salidas pendientes se reanudan desde la primera parte no confirmada;
- nunca se crean dos ramas, dos mutaciones o dos PR para un mismo mensaje.

Al iniciar Discord, un despachador obtiene el canal configurado y vacía las salidas pendientes por conversación y orden de creación antes de procesar una respuesta nueva para esa conversación. El ACK de cada parte es el retorno exitoso de `channel.send`, cuyo ID se registra inmediatamente. Si el proceso muere exactamente entre el envío remoto y ese registro, puede repetirse únicamente ese fragmento de texto; la ejecución del backend y cualquier efecto Git/GitHub continúan siendo idempotentes.

El propietario puede preguntar “cómo va” y recibir el estado, último evento y tiempo transcurrido. Puede cancelar un trabajo en cola o activo; el worker termina el proceso hijo y conserva el diagnóstico.

## Configuración

El `.env` privado del bot incorpora:

| Variable | Uso |
| --- | --- |
| `ALLOWED_USER_ID` | Propietario obligatorio para esta entrega; el arranque falla si falta. |
| `POOIA_DB_PATH=/app/data/poo-ia.sqlite3` | Archivo SQLite persistente. |
| `MEMORY_RETENTION_DAYS=7` | Retención de conversación. |
| `MEMORY_MAX_EXCHANGES=10` | Pares conservados. |
| `MEMORY_MAX_CONTEXT_CHARS=12000` | Presupuesto de contexto. |
| `JOB_MAX_CONCURRENT=1` | Trabajos pesados simultáneos. |
| `WORKER_ENABLED` | Activa el worker local tras validarlo. |
| `WORKER_BASE_URL=http://127.0.0.1:4097` | Endpoint loopback. |
| `WORKER_SERVER_USERNAME` | Usuario HTTP interno. |
| `WORKER_SERVER_PASSWORD` | Secreto HTTP interno. |
| `WORKER_TIMEOUT_SECONDS` | Tiempo máximo de una llamada de control. |

El entorno privado del servicio host incorpora:

| Variable | Uso |
| --- | --- |
| `POOIA_WORKER_HOST=127.0.0.1` | Interfaz local obligatoria. |
| `POOIA_WORKER_PORT=4097` | Puerto local. |
| `POOIA_WORKER_SERVER_USERNAME` / `PASSWORD` | Mismas credenciales internas del bot. |
| `CAPNET_WORKSPACE=/home/poo/capnet-workspace` | Raíz permitida de repositorios. |
| `CAPNET_WORKTREES=/home/poo/capnet-worktrees` | Worktrees aislados. |
| `WORKER_STATE_ROOT=/home/poo/.local/share/poo-ia-worker` | Manifiestos y resultados. |
| `AWS_ENABLED=false` | Punto de extensión posterior; `AWS_PROFILE` y `AWS_REGION` no se configuran en esta fase. |
| `CODEX_ENABLED` / `CODEX_BIN=/usr/local/bin/codex` | Activación de Codex CLI. |
| `CODEX_TIMEOUT_SECONDS=1800` | Límite de ejecución Codex. |
| `CODEX_MAX_FILES=5` / `CODEX_MAX_CHANGED_LINES=400` | Presupuesto inicial. |
| `GITHUB_ENABLED` / `GH_BIN=/usr/bin/gh` | Publicación de ramas y PR. |

Se conservan las variables actuales de Discord, Ollama y OpenCode. Los secretos reales se mantienen fuera del repositorio y los logs imprimen solo nombres de configuración, nunca valores.

## Uso de recursos

La Beelink tiene Intel N95 de cuatro núcleos, 7,5 GiB de RAM, 4 GiB de swap y espacio de disco suficiente. En la medición actual, Discord consume aproximadamente 32 MiB y OpenCode entre 400 y 500 MiB; el modelo Qwen ocupa 1,9 GB en disco.

La operación acordada es:

- un solo trabajo pesado global;
- `OPENCODE_MAX_CONCURRENT=1`;
- Ollama con paralelismo uno y liberación del modelo tras un periodo corto;
- un solo worktree en construcción o prueba a la vez;
- informes y diffs grandes se escriben a disco y se adjuntan, no se mantienen completos en memoria;

No se introduce Redis ni PostgreSQL en esta etapa.

## Manejo de errores

- Los errores de una integración no detienen Discord ni corrompen la memoria.
- Los mensajes al usuario son breves y contienen un ID de trabajo; el detalle técnico seguro queda en eventos/logs.
- Autenticación ausente o expirada se informa como acción requerida, sin intentar cambiar de cuenta o proveedor.
- Un timeout de OpenCode aborta y elimina su sesión.
- Un timeout de Codex cancela el proceso, conserva worktree/diff y marca el trabajo fallido.
- Cuando se implemente AWS, sus errores conservarán la categoría y request ID cuando sea seguro, sin incluir credenciales ni valores sensibles.
- Un fallo parcial al enviar Discord reanuda las partes pendientes; no guarda un intercambio incompleto.
- Un fallo de SQLite impide iniciar el núcleo con un diagnóstico claro; no arranca en modo sin memoria.

## Plan de entregas internas

La implementación se divide sin cambiar la arquitectura:

1. **Núcleo persistente:** migraciones, memoria, deduplicación, outbox, bloqueo por conversación, volumen Docker y reglas.
2. **Orquestación documental:** contexto para Qwen/OpenCode, router ampliado, abortado real y estados de consulta.
3. **Worker host:** servicio loopback, manifiestos, salud, cola global y comunicación autenticada.
4. **Codex/GitHub:** autenticación por suscripción, worktrees, pruebas, presupuesto, diff, publicación y PR idempotente.
5. **Operación:** recuperación, documentación, despliegue, comprobaciones reales y manual de prueba del propietario.
6. **AWS posterior:** solo después de que el propietario valide el funcionamiento principal, instalar/configurar su identidad, añadir DynamoDB paginado e informes adjuntos.

## Pruebas automatizadas

La suite cubrirá como mínimo:

- configuración antigua y nueva, sin revelar secretos;
- migraciones, reapertura, WAL y persistencia;
- retención de siete días, diez intercambios y 12.000 caracteres con reloj controlado;
- aislamiento por canal/usuario, olvido y resolución de referencias;
- deduplicación por mensaje y serialización por conversación;
- intercambio guardado solo después de confirmar todos los fragmentos;
- reanudación de una salida parcialmente enviada;
- transiciones de trabajos, FIFO, concurrencia uno, cancelación y recuperación;
- controles del router y caída conservadora a investigación;
- contexto correcto en Ollama, OpenCode y Codex;
- AWS desactivado de forma predeterminada y sin dependencia de credenciales para iniciar;
- resolución segura de repositorios dentro del workspace;
- comando Codex, timeout, cancelación, límites, diff y pruebas con procesos simulados;
- publicación GitHub idempotente, estados de validación y errores de autenticación;
- autenticación e idempotencia de la API interna;
- filtros Discord, fragmentación, reintentos y ACK;
- regresión de todos los tests actuales.

Las pruebas se ejecutan en el entorno reproducible del proyecto o dentro de Docker; la ausencia de `aiohttp` en el Python global del equipo de desarrollo no constituye un fallo de la aplicación.

## Aceptación en la Beelink

Antes de declarar terminada la entrega se conservará evidencia de lo siguiente:

1. El repositorio publicado no contiene `.env`, tokens, credenciales ni archivos de sesión.
2. `docker compose config` es válido, la imagen se construye y la suite completa pasa.
3. Ollama responde con `qwen2.5-coder:3b`, OpenCode responde autenticado y ambos son alcanzables desde el contenedor.
4. El worker responde solo en loopback y rechaza una llamada sin autenticación.
5. El usuario propietario activa el bot en el canal acordado; bots, otros canales y otro usuario no activan backends.
6. Una respuesta larga produce únicamente fragmentos de hasta 1.900 caracteres.
7. Tras conversar, reiniciar y decir “agrégalo”, Poo-IA conserva el repositorio y el campo mencionados; “olvida la conversación” elimina ese contexto.
8. Una pregunta documental devuelve hechos acompañados por rutas reales y los 20 repositorios permanecen limpios.
9. AWS permanece desactivado y el funcionamiento principal no requiere AWS CLI, perfil ni credenciales.
10. Una petición pequeña crea un worktree y rama aislados, modifica únicamente el repo elegido, ejecuta pruebas y devuelve el diff sin cambiar los archivos del working tree base ni crear commits sobre su rama base.
11. Una petición explícita de PR publica exactamente una rama y devuelve una URL de PR verificable; repetir el mensaje no crea otro PR.
12. Al reiniciar el contenedor durante un trabajo real, el mismo ID se reconcilia y la salida se entrega sin duplicar la ejecución; la recuperación del worker y de una máquina completa queda cubierta por pruebas automatizadas de manifiestos y checkpoints.
13. En ningún momento hay más de un trabajo pesado en `running`.
14. README y `.env.example` explican instalación, autenticación, operación, recuperación y prueba desde Discord, y dejan AWS claramente pospuesto.

El sistema no se declara terminado hasta comprobar los catorce puntos. Si Codex o GitHub presentan una confirmación interactiva que el agente no pueda completar con la sesión autorizada, se pedirá esa única intervención y la verificación continuará después; “pendiente de login” no equivale a entrega terminada.
