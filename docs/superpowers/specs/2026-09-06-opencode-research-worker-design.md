# Poo-IA: diseño del cerebro de consulta con OpenCode

**Fecha:** 2026-09-06
**Estado:** aprobado conceptualmente; pendiente de revisión del documento

## Objetivo

Ampliar Poo-IA con un worker capaz de investigar la documentación técnica de Capnet y coordinar búsquedas acotadas entre repositorios. Poo-IA continuará siendo la única puerta de entrada de Discord, `qwen2.5-coder:3b` atenderá conversación sencilla mediante Ollama y OpenCode resolverá las consultas que requieran documentación.

OpenCode utilizará inicialmente la autenticación OAuth de la cuenta ChatGPT/Codex del propietario, sin una clave de OpenAI API. En una fase posterior podrá cambiarse al proveedor OpenCode Go sin modificar el bot ni su protocolo con el worker.

Esta primera entrega es estrictamente de consulta. El worker no podrá editar archivos, ejecutar comandos generales, crear ramas, hacer commits, desplegar ni publicar cambios.

## Decisión de arquitectura

OpenCode se ejecutará como servicio HTTP separado en el host Ubuntu de la Beelink. Poo-IA seguirá dentro de Docker y lo alcanzará por loopback gracias al `network_mode: host` existente.

```text
Mensaje en el canal autorizado
             │
             ▼
       Poo-IA en Docker
             │
             ├── charla inequívoca ─────► Qwen local en Ollama
             │
             └── cualquier otra petición ► OpenCode en el host
                                                   │
                                                   ├── agente capnet-research
                                                   ├── brain-capnet/ai
                                                   ├── documentación fuente
                                                   └── hasta 2 subagentes de lectura
```

El servicio se iniciará desde `/home/poo/capnet-workspace` para que los 20 repositorios estén dentro de su workspace. La configuración versionada residirá en Poo-IA y se cargará mediante `OPENCODE_CONFIG` y `OPENCODE_CONFIG_DIR`; las credenciales permanecerán en el almacén privado del usuario `poo`.

### Alternativas consideradas

1. **OpenCode como worker separado — elegida.** Soporta autenticación ChatGPT Plus/Pro, servidor programático, agentes, subagentes y permisos granulares. Más adelante acepta OpenCode Go como proveedor sin cambiar la integración.
2. **Hermes como orquestador.** Tiene mejores funciones de asistente personal, memoria y múltiples plataformas de mensajería, pero duplica el gateway, las reglas y la personalidad que Poo-IA ya posee.
3. **Invocar Codex CLI directamente desde Poo-IA.** Es el camino oficial más corto hacia la suscripción, pero acopla Discord a una herramienta de implementación y dificulta cambiar después a OpenCode Go.

## Límites entre componentes

### Poo-IA

- Conserva la conexión de Discord y todos los filtros actuales.
- Decide entre conversación local e investigación documental.
- Gestiona timeout, concurrencia, errores y fragmentación de respuestas.
- Nunca entrega el token de Discord a OpenCode.
- No interpreta ni ejecuta instrucciones de cambio devueltas por el worker.

### Qwen local

- Responde únicamente saludos, agradecimientos y conversación breve inequívoca.
- Usa las reglas y la personalidad existentes.
- No responde preguntas técnicas ni sustituye al worker cuando OpenCode falla.

### OpenCode

- Recibe una consulta independiente y crea una sesión efímera.
- Usa siempre el agente `capnet-research`.
- Busca primero en la capa curada de `brain-capnet/ai/`.
- Verifica detalles contra documentación o código fuente cuando sea necesario.
- Puede delegar búsquedas independientes a dos subagentes de lectura permitidos.
- Devuelve una respuesta en español con rutas de los archivos que respaldan los hechos.

### Codex CLI

Codex CLI queda separado del bot. Podrá utilizarse manualmente para implementar cambios de complejidad media o alta después de una autorización explícita. Poo-IA y OpenCode no lo invocarán automáticamente durante esta fase.

## Estructura versionada

La implementación añadirá unidades pequeñas y separadas:

```text
poo-ia/
├── app/
│   ├── config.py
│   ├── discord_bot.py
│   ├── ollama_client.py
│   ├── opencode_client.py
│   └── router.py
├── opencode/
│   ├── opencode.jsonc
│   ├── instructions.md
│   └── agents/
│       ├── capnet-research.md
│       ├── capnet-docs.md
│       └── capnet-code.md
├── ops/
│   └── opencode-capnet.service
└── tests/
    ├── test_opencode_client.py
    └── test_router.py
```

`opencode_client.py` solo conocerá el protocolo HTTP. `router.py` será una función determinista sin dependencias de Discord ni de modelos. Los agentes y sus instrucciones serán contenido versionable, igual que `rules/` y `personality/`.

## Enrutamiento

Durante esta fase, casi todo el trabajo esperado deriva de la documentación. Por eso el enrutador será deliberadamente conservador:

1. Un conjunto pequeño y explícito de saludos, despedidas y agradecimientos se enviará a Ollama.
2. Todo mensaje restante se enviará a OpenCode.
3. No se usará Qwen como clasificador semántico inicial; un modelo de 3B podría enviar preguntas técnicas al camino incorrecto.
4. Más adelante se podrá añadir clasificación estructurada usando métricas reales de las consultas recibidas.

Este enfoque reduce al mínimo las respuestas técnicas inventadas y cumple el flujo acordado sin requerir un prefijo en Discord.

## Protocolo con OpenCode

OpenCode escuchará únicamente en `127.0.0.1:4096` y exigirá autenticación HTTP Basic. Para cada pregunta técnica Poo-IA realizará el siguiente flujo:

1. Crear una sesión con `POST /session`.
2. Enviar el mensaje con `POST /session/:id/message`, seleccionando `capnet-research` y una parte de texto.
3. Extraer únicamente las partes de texto de la respuesta final.
4. Consultar los hijos de la sesión cuando haya delegación y abortar si se excede el presupuesto acordado.
5. Eliminar la sesión en un bloque de limpieza, tanto en éxito como en error.

Cada mensaje de Discord será una sesión nueva. La primera fase no conservará memoria conversacional ni sesiones abandonadas. El cliente reutilizará la sesión HTTP de `aiohttp`, nunca registrará el encabezado de autenticación y validará estados y formatos antes de devolver texto al bot.

## Acceso documental

El agente seguirá esta jerarquía:

1. `brain-capnet/ai/rutas-de-consulta.md` para elegir la ruta de investigación;
2. `brain-capnet/ai/catalogo-servicios.yaml`, `mapa-arquitectura.md` y `glosario.md` para localizar servicios y conceptos;
3. documentación fuente señalada por la capa `ai/`;
4. los demás repositorios de `capnet-workspace` solo cuando haga falta comprobar la implementación actual.

La respuesta distinguirá hechos, inferencias y ausencia de evidencia. Las fuentes se citarán como rutas relativas a `capnet-workspace`, sin copiar documentos completos en Discord.

## Agentes y permisos

Se definirán tres perfiles:

- `capnet-research`: agente principal; relaciona hallazgos y redacta la respuesta.
- `capnet-docs`: subagente para localizar y contrastar documentación.
- `capnet-code`: subagente para comprobar comportamiento en el código existente.

La política global negará todo por defecto y permitirá solamente lectura, listado y búsqueda dentro de `/home/poo/capnet-workspace`. También permitirá al agente principal invocar únicamente los dos subagentes anteriores. Los tres perfiles negarán edición, escritura, shell general, acceso a archivos `.env`, publicación web y operaciones Git mutables.

El agente principal tendrá un presupuesto corto de pasos e instrucciones para utilizar como máximo dos subagentes por consulta. Poo-IA solo procesará una investigación a la vez. Antes y después de las pruebas se comprobará el estado Git de todos los repositorios.

Si la versión instalada de OpenCode no permite hacer cumplir algún límite de subagentes, la delegación se mantendrá deshabilitada hasta añadir una restricción verificable. La lectura con un solo agente seguirá siendo una entrega válida y segura.

## Proveedor actual y migración futura

La instalación inicial usará la opción `ChatGPT Plus/Pro` del proveedor OpenAI de OpenCode. El inicio de sesión será interactivo y las credenciales no se copiarán al repositorio ni al contenedor.

Esta ruta no usa una clave de OpenAI API, pero sí consume la capacidad asociada a la cuenta de ChatGPT/Codex y queda sujeta a sus límites. No se habilitarán compras de créditos ni recargas automáticas durante el despliegue.

Cuando el propietario decida probar OpenCode Go:

1. contratará voluntariamente la suscripción de Go;
2. conectará su clave de Go en el host;
3. cambiará el proveedor/modelo del agente de investigación;
4. repetirá las pruebas de consulta y permisos.

Poo-IA seguirá utilizando el mismo servidor HTTP y no necesitará cambios. Codex CLI continuará disponible por separado para implementación.

## Configuración de Poo-IA

Las nuevas variables vivirán en el `.env` privado de la Beelink:

| Variable | Obligatoria | Valor inicial | Propósito |
| --- | --- | --- | --- |
| `OPENCODE_ENABLED` | No | `false` | Activa el worker solo después de validarlo. |
| `OPENCODE_BASE_URL` | Si está activo | `http://127.0.0.1:4096` | Servidor local de OpenCode. |
| `OPENCODE_SERVER_USERNAME` | No | `opencode` | Usuario de autenticación HTTP Basic. |
| `OPENCODE_SERVER_PASSWORD` | Si está activo | — | Contraseña distinta de otros secretos. |
| `OPENCODE_AGENT` | No | `capnet-research` | Agente obligatorio para consultas. |
| `OPENCODE_TIMEOUT_SECONDS` | No | `300` | Límite total de cinco minutos. |
| `OPENCODE_MAX_CONCURRENT` | No | `1` | Máximo de investigaciones simultáneas. |

`OPENCODE_ENABLED=false` será el valor predeterminado y permitirá desplegar el código nuevo sin cambiar inmediatamente el comportamiento del bot existente.

## Timeout y concurrencia

El límite de una investigación será de 300 segundos, incluyendo la espera por el semáforo. Si expira, Poo-IA abortará la sesión remota cuando sea posible, ejecutará la limpieza y responderá que la consulta tardó demasiado.

El timeout de Ollama permanecerá independiente en 120 segundos. Una falla de OpenCode no detendrá Discord ni Ollama. Mientras la consulta esté activa, Discord mostrará el indicador de escritura.

## Manejo de errores

- Si OpenCode está desactivado, una consulta técnica indicará que el cerebro documental aún no está disponible.
- Los errores de conexión, autenticación, proveedor, formato o timeout producirán un mensaje breve y seguro.
- Las consultas técnicas fallidas no caerán a Qwen.
- Una respuesta vacía se tratará como error, no como éxito.
- La sesión efímera se eliminará siempre que el servidor sea alcanzable.
- Los secretos y el cuerpo documental no aparecerán en los registros.
- Las respuestas válidas se dividirán mediante el fragmentador existente de 1,900 caracteres.

## Despliegue gradual

1. Añadir cliente, router, configuración, agentes y pruebas a Poo-IA con el worker desactivado.
2. Instalar OpenCode para el usuario `poo` y autenticar la cuenta ChatGPT/Codex interactivamente.
3. Instalar el servicio del host con configuración versionada, loopback y Basic Auth.
4. Validar una sesión HTTP directa de solo lectura sobre `brain-capnet`.
5. Construir la imagen nueva de Poo-IA sin reemplazar el contenedor activo hasta completar las pruebas locales.
6. Activar el worker y ejecutar pruebas desde Discord.
7. Comparar el estado Git de los 20 repositorios antes y después.
8. Ante cualquier regresión, desactivar `OPENCODE_ENABLED` y conservar el servicio básico de Ollama.

## Pruebas

### Unitarias

- validación de variables obligatorias, URLs y timeouts;
- enrutamiento de charla inequívoca frente a cualquier otra petición;
- creación, consulta, aborto y eliminación de sesiones simuladas;
- extracción de texto y rechazo de respuestas vacías o mal formadas;
- autenticación sin exposición de la contraseña;
- timeout y concurrencia máxima de una investigación;
- fragmentación de respuestas largas.

### Integración en la Beelink

- el servidor escucha solo en `127.0.0.1:4096`;
- rechaza solicitudes sin autenticación;
- la cuenta ChatGPT/Codex está conectada sin una API key;
- el agente encuentra un dato verificable y cita rutas fuente;
- reconoce una pregunta sin evidencia suficiente;
- no puede leer `.env`, editar archivos ni ejecutar comandos no permitidos;
- no crea más de dos sesiones hijas;
- el estado Git permanece limpio en los 20 repositorios.

### Extremo a extremo en Discord

1. `hola` obtiene respuesta local de Qwen.
2. Una pregunta sobre dos servicios obtiene respuesta de OpenCode con fuentes.
3. Una petición de cambio recibe análisis, pero no modifica archivos.
4. Otro canal y los mensajes de bots siguen ignorados.
5. Una respuesta extensa llega dividida correctamente.
6. Con OpenCode detenido, la consulta técnica muestra un error seguro y un saludo continúa funcionando.

## Criterios de aceptación

- Poo-IA conserva el canal único, la ausencia de prefijo y la protección contra bucles.
- Qwen sigue siendo el camino local para conversación sencilla.
- Las consultas técnicas usan OpenCode y se fundamentan en `capnet-workspace`.
- El worker no puede modificar repositorios ni leer secretos.
- Como máximo se ejecuta una investigación y dos subagentes de lectura.
- No se añade facturación de API ni una nueva suscripción durante esta fase.
- La integración puede desactivarse sin perder el bot actual.
- El proveedor puede migrar después a OpenCode Go sin cambiar el protocolo de Poo-IA.

## Fuera de alcance

- memoria conversacional o aprendizaje automático de preferencias;
- edición autónoma de repositorios;
- ejecución automática de Codex CLI;
- commits, `push`, despliegues o pull requests desde Discord;
- contratación automática de OpenCode Go o compra de créditos;
- reemplazo del gateway de Discord existente.
