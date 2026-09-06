# Poo-IA: diseño del cerebro de consulta con Hermes

**Fecha:** 2026-09-06  
**Estado:** pendiente de revisión antes de implementar

## Objetivo

Ampliar Poo-IA con un cerebro de consulta capaz de investigar la documentación técnica de Capnet sin sustituir el bot de Discord que ya funciona. La arquitectura conservará `qwen2.5-coder:3b` como puerta de entrada local y utilizará Hermes para las preguntas que requieran buscar, relacionar o resumir información de `brain-capnet`.

La primera fase será exclusivamente de consulta. Hermes podrá leer documentación y coordinar como máximo dos subagentes de investigación, pero no podrá modificar repositorios, ejecutar implementaciones, hacer `push` ni invocar Codex automáticamente.

## Decisión de arquitectura

Se instalará Hermes como servicio separado en el host Ubuntu de la Beelink. Poo-IA continuará dentro de Docker y accederá al servidor HTTP compatible con OpenAI de Hermes mediante `127.0.0.1:8642`, aprovechando el `network_mode: host` existente.

```text
Mensaje en el canal autorizado
             │
             ▼
       Poo-IA en Docker
             │
             ├── conversación sencilla ──► Qwen local en Ollama
             │
             └── consulta técnica ───────► Hermes en el host
                                                │
                                                ├── brain-capnet/ai
                                                ├── documentación fuente
                                                └── hasta 2 subagentes de lectura
```

Esta separación conserva una única conexión de Discord, evita duplicar respuestas y mantiene las credenciales de proveedores de Hermes fuera del contenedor del bot.

### Alternativas consideradas

1. **Hermes como servicio de investigación separado — elegida.** Mantiene estable el bot actual, permite desplegar o detener el cerebro sin afectar Discord y deja clara la frontera entre conversación, investigación e implementación.
2. **Reemplazar Poo-IA por el gateway de Discord de Hermes.** Hermes soporta canales de respuesta libre, pero migrar ahora implicaría cambiar al mismo tiempo el gateway, los filtros, los secretos y el modelo. Se reserva como posible simplificación futura.
3. **Usar OpenCode como worker de consulta.** Es viable, pero su orientación principal es el trabajo de código y se solapa con Codex CLI. Hermes encaja mejor como investigador con subagentes y herramientas restringidas.

## Responsabilidades

### Poo-IA y Qwen

- Mantener el filtro actual de canal, usuario opcional y mensajes de bots.
- Responder saludos, conversación breve y solicitudes simples que no dependan de la documentación.
- Determinar si una petición necesita investigación mediante reglas conservadoras y señales técnicas.
- Enviar las consultas técnicas a Hermes sin incluir secretos ni datos ajenos al mensaje.
- Dividir tanto las respuestas locales como las de Hermes en fragmentos seguros para Discord.

Qwen no inventará una respuesta técnica cuando Hermes no esté disponible. Ante duda, la petición se clasificará como investigación; es preferible una consulta adicional a contestar con información no verificada.

### Hermes

- Recibir una consulta independiente mediante `POST /v1/chat/completions`.
- Buscar primero en la capa curada `brain-capnet/ai/` y seguir sus rutas hacia la documentación fuente cuando haga falta.
- Relacionar información entre servicios y delegar búsquedas acotadas a un máximo de dos subagentes.
- Responder en español claro, separar hechos de inferencias y citar las rutas de los archivos utilizados.
- Informar que no encontró evidencia cuando la documentación no sea suficiente.

### Codex CLI

Codex queda fuera del flujo automático de esta fase. Más adelante podrá recibir una tarea de implementación derivada de una investigación, pero solo después de una autorización explícita del propietario. La suscripción interactiva no se tratará como una API general ni se expondrá al bot de Discord.

## Enrutamiento inicial

El enrutador combinará comprobaciones deterministas con una clasificación local pequeña:

1. Los saludos y mensajes conversacionales inequívocos se envían a Ollama.
2. Las menciones de servicios, endpoints, repositorios, arquitectura, tablas, lambdas, despliegues, errores o cambios se envían a Hermes.
3. Las preguntas largas o ambiguas se consideran técnicas por seguridad y se envían a Hermes.
4. La clasificación de Qwen solo podrá elevar una consulta a investigación; no podrá rebajar una señal técnica determinista a conversación simple.

Los términos y alias de servicios se obtendrán de `brain-capnet/ai/catalogo-servicios.yaml`, de modo que el enrutamiento evolucione con la documentación y no dependa de una lista duplicada dentro del código.

## Acceso a la documentación

Hermes se ejecutará en el host y tendrá como raíz documental:

```text
/home/poo/capnet-workspace/brain-capnet
```

La búsqueda seguirá este orden:

1. `ai/rutas-de-consulta.md` para identificar la ruta apropiada;
2. `ai/catalogo-servicios.yaml`, `ai/mapa-arquitectura.md` y `ai/glosario.md` para localizar conceptos y servicios;
3. archivos fuente señalados por la capa `ai/` para verificar detalles;
4. otros repositorios de `capnet-workspace` únicamente cuando la pregunta requiera comprobar código y la política de acceso lo permita.

Las respuestas deberán citar rutas relativas a `capnet-workspace`, por ejemplo `brain-capnet/Arquitectura/dependencias.json`, sin volcar archivos completos en Discord.

## Seguridad y límites de herramientas

- Hermes comenzará en modo restringido y sin memoria persistente ni autoaprendizaje.
- Solo se habilitarán lectura y búsqueda dentro de `/home/poo/capnet-workspace`.
- No se habilitarán escritura, edición, shell general, operaciones Git mutables, despliegues, cron ni publicación externa.
- Se permitirá como máximo una investigación activa y dos subagentes por investigación.
- El servidor de Hermes escuchará únicamente en `127.0.0.1:8642`.
- Toda solicitud requerirá una clave Bearer distinta del token de Discord.
- `HERMES_API_KEY`, las credenciales del proveedor y las sesiones de Codex permanecerán fuera de Git.
- Los registros no incluirán tokens, claves Bearer ni el contenido completo de documentos consultados.

Aunque el servidor esté ligado a loopback, su API expone las herramientas del agente; por eso la autenticación y la restricción de herramientas son requisitos, no opciones.

## Proveedor y modelos

El modelo local actual `qwen2.5-coder:3b` tiene una ventana de contexto de 32K y seguirá atendido por Ollama. No será el modelo principal de Hermes porque Hermes exige un contexto mínimo de 64K para su agente.

Hermes se configurará inicialmente con un proveedor compatible que cumpla ese requisito. Puede autenticarse interactivamente con OpenAI Codex usando la suscripción del propietario, si esa modalidad se confirma operativamente, o con otro proveedor configurado para Hermes. La credencial elegida residirá en la cuenta del usuario `poo` del host, no en la imagen de Poo-IA.

El diseño no depende de un proveedor específico: Poo-IA solo conoce la API local de Hermes. Cambiar el modelo de investigación no requerirá modificar el gateway de Discord.

## Configuración propuesta

Las nuevas variables pertenecerán al `.env` privado de Poo-IA:

| Variable | Obligatoria | Valor inicial | Propósito |
| --- | --- | --- | --- |
| `HERMES_ENABLED` | No | `false` | Permite desplegar primero sin alterar el flujo actual. |
| `HERMES_BASE_URL` | Si está activo | `http://127.0.0.1:8642` | API local del worker de investigación. |
| `HERMES_API_KEY` | Si está activo | — | Autenticación Bearer entre Poo-IA y Hermes. |
| `HERMES_TIMEOUT_SECONDS` | No | `300` | Máximo de cinco minutos por investigación. |
| `HERMES_MAX_CONCURRENT` | No | `1` | Evita saturar la Beelink con investigaciones simultáneas. |
| `BRAIN_ROOT` | No | `/home/poo/capnet-workspace/brain-capnet` | Raíz que Hermes puede consultar. |

`HERMES_ENABLED=false` será el valor seguro durante la instalación. Solo se activará después de validar la API y sus permisos localmente.

## Tiempo de espera y concurrencia

Una consulta documental puede durar más que una generación de Ollama, especialmente si Hermes utiliza subagentes. El límite inicial será de 300 segundos. Poo-IA mantendrá el indicador de escritura de Discord y procesará una sola consulta de Hermes a la vez.

Si llega otra consulta técnica mientras una está activa, esperará en una cola limitada. Si la espera o la investigación excede el límite, el bot responderá con un mensaje breve indicando que la consulta tardó demasiado; el proceso de Discord continuará activo.

El timeout de 120 segundos de Ollama seguirá siendo independiente. Una interrupción de SSH hacia la Beelink no implica por sí misma que Discord, Docker u Ollama estén caídos.

## Manejo de errores

- Si Hermes no está configurado, las consultas técnicas indicarán que el cerebro documental aún no está disponible.
- Si la API devuelve error, respuesta vacía, autenticación inválida o timeout, Poo-IA registrará un detalle seguro y enviará un aviso comprensible al canal.
- Las preguntas técnicas fallidas no caerán automáticamente a Qwen, para evitar respuestas plausibles pero no fundamentadas.
- Una falla de Hermes no detendrá el cliente de Discord ni las respuestas simples de Ollama.
- Las respuestas que excedan el límite de Discord usarán el fragmentador existente de 1,900 caracteres.

## Despliegue gradual

1. Instalar Hermes para el usuario `poo` y completar su autenticación interactiva.
2. Aplicar el perfil restringido de lectura y verificar que no pueda modificar `capnet-workspace`.
3. Iniciar la API de Hermes solo en loopback y probar una consulta con autenticación.
4. Añadir el cliente, el enrutador y las variables de Hermes a Poo-IA manteniendo `HERMES_ENABLED=false`.
5. Construir y probar la imagen del bot sin reemplazar el contenedor funcional hasta superar las pruebas.
6. Activar Hermes, reiniciar Poo-IA y ejecutar una consulta documental desde el canal autorizado.
7. Comparar el estado Git de todos los repositorios antes y después para demostrar que la investigación fue de solo lectura.
8. Hacer rollback desactivando `HERMES_ENABLED` si la integración falla; Ollama continuará disponible.

## Pruebas

### Unitarias

- configuración válida e inválida de todas las variables de Hermes;
- clasificación de saludos, términos técnicos, alias del catálogo y mensajes ambiguos;
- cliente HTTP con autenticación, respuesta válida, error, respuesta vacía y timeout;
- cola con concurrencia máxima de una investigación;
- ausencia de claves y tokens en registros;
- fragmentación de respuestas documentales largas.

### Integración en la Beelink

- Hermes responde en `127.0.0.1:8642` y rechaza peticiones sin Bearer;
- una consulta encuentra un dato verificable de `brain-capnet` y devuelve rutas fuente;
- una consulta inexistente reconoce la falta de evidencia;
- dos consultas simultáneas no crean más de una investigación activa;
- un timeout no reinicia ni desconecta el bot;
- `git status --short` permanece limpio en los 20 repositorios del workspace.

### Extremo a extremo en Discord

1. `hola` obtiene una respuesta de Qwen.
2. Una pregunta sobre la relación entre dos servicios obtiene una respuesta de Hermes con fuentes.
3. Un mensaje fuera del canal configurado no genera respuesta.
4. Un mensaje de otro bot no genera respuesta.
5. Una respuesta extensa llega dividida sin superar el límite de Discord.
6. Con Hermes detenido, una consulta técnica muestra el error seguro y un saludo sigue funcionando con Ollama.

## Criterios de aceptación

- El bot existente conserva su comportamiento sin prefijo y su restricción al canal autorizado.
- Qwen sigue funcionando como puerta de entrada local.
- Las consultas técnicas se fundamentan en `brain-capnet` y presentan rutas de las fuentes.
- Hermes no puede modificar ninguno de los repositorios de `capnet-workspace`.
- No hay credenciales ni contenido sensible en Git o en los registros.
- El límite de cinco minutos y la concurrencia de una consulta evitan que la Beelink se sature.
- La integración puede desactivarse con una variable sin perder el servicio básico de Ollama.

## Fases posteriores

Una segunda fase podrá transformar una respuesta de investigación en una propuesta de cambio y preparar un paquete de contexto para Codex CLI. La ejecución seguirá necesitando una aprobación explícita y tendrá controles separados para rama, pruebas, commit y `push`. No se implementará ejecución autónoma de cambios como parte de este diseño.
