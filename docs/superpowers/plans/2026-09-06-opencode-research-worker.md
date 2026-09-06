# Plan de implementación: worker de consulta con OpenCode

**Diseño aprobado:** `docs/superpowers/specs/2026-09-06-opencode-research-worker-design.md`

## Resultado esperado

Poo-IA seguirá respondiendo saludos mediante Ollama y enviará cualquier otra petición a un servidor OpenCode local. El worker consultará `capnet-workspace` con perfiles de solo lectura, devolverá fuentes y podrá desactivarse sin afectar el bot actual.

## Restricciones de ejecución

- Mantener `OPENCODE_ENABLED=false` hasta validar el servicio del host.
- No reemplazar el contenedor funcional antes de que pasen las pruebas.
- No introducir claves de API ni secretos en Git.
- No permitir edición, shell general ni Git mutable al worker.
- No comprar OpenCode Go ni créditos.
- Conservar el filtro actual de canal y usuario.

## Tarea 1: configuración del bot

**Archivos:**

- Modificar `app/config.py`.
- Modificar `tests/test_config.py`.

**Pasos:**

1. Añadir pruebas para valores predeterminados de OpenCode.
2. Añadir pruebas para activación, credenciales, URL, timeout y concurrencia inválidos.
3. Implementar lectura y validación de:
   - `OPENCODE_ENABLED`;
   - `OPENCODE_BASE_URL`;
   - `OPENCODE_SERVER_USERNAME`;
   - `OPENCODE_SERVER_PASSWORD`;
   - `OPENCODE_AGENT`;
   - `OPENCODE_TIMEOUT_SECONDS`;
   - `OPENCODE_MAX_CONCURRENT`.
4. Verificar que la contraseña solo sea obligatoria cuando el worker esté activo.
5. Ejecutar las pruebas de configuración.

## Tarea 2: enrutamiento determinista

**Archivos:**

- Crear `app/router.py`.
- Crear `tests/test_router.py`.

**Pasos:**

1. Definir una enumeración o tipo pequeño para `ollama` y `opencode`.
2. Normalizar mayúsculas, espacios y puntuación sin modificar el mensaje original.
3. Enviar únicamente saludos, despedidas y agradecimientos inequívocos a Ollama.
4. Enviar preguntas, mensajes compuestos y cualquier otro texto a OpenCode.
5. Cubrir español, variantes breves y casos ambiguos con pruebas de tabla.

## Tarea 3: cliente HTTP de OpenCode

**Archivos:**

- Crear `app/opencode_client.py`.
- Crear `tests/test_opencode_client.py`.

**Pasos:**

1. Escribir dobles de prueba para respuestas y sesión HTTP.
2. Probar creación de sesión, envío del mensaje, extracción de partes de texto y eliminación final.
3. Probar autenticación Basic sin registrar la contraseña.
4. Probar estados HTTP, JSON inválido, respuesta vacía, timeout y error de conexión.
5. Probar que un error durante el mensaje no omita la limpieza.
6. Implementar `OpenCodeClient` con una única sesión `aiohttp` reutilizable.
7. Implementar aborto mejor-esfuerzo antes de eliminar una sesión que exceda el timeout.

## Tarea 4: integración con Discord

**Archivos:**

- Modificar `app/discord_bot.py`.
- Modificar `tests/test_discord_bot.py`.

**Pasos:**

1. Crear clientes HTTP con timeouts independientes para Ollama y OpenCode.
2. Construir un semáforo con el límite configurado.
3. Elegir el backend mediante `router.py` después de los filtros de Discord.
4. Mantener el prompt de reglas y personalidad para Ollama.
5. Enviar a OpenCode el texto original sin el prompt de personalidad del bot.
6. Presentar mensajes distintos para worker desactivado, error y timeout.
7. No caer a Ollama cuando falla una consulta técnica.
8. Reutilizar `split_for_discord` para ambos caminos.
9. Probar el enrutamiento y la recuperación de errores sin conexión real.

## Tarea 5: configuración de agentes OpenCode

**Archivos:**

- Crear `opencode/opencode.jsonc`.
- Crear `opencode/instructions.md`.
- Crear `opencode/agents/capnet-research.md`.
- Crear `opencode/agents/capnet-docs.md`.
- Crear `opencode/agents/capnet-code.md`.

**Pasos:**

1. Validar el esquema contra la versión exacta instalada en la Beelink.
2. Negar todas las herramientas por defecto.
3. Permitir lectura, listado y búsqueda dentro de `capnet-workspace`.
4. Negar `.env`, credenciales, edición, escritura, shell general y publicación.
5. Permitir al agente principal únicamente los dos subagentes declarados.
6. Añadir instrucciones para comenzar por `brain-capnet/ai/`, verificar fuentes y responder en español.
7. Fijar un presupuesto corto de pasos.
8. Mantener la delegación desactivada si la versión instalada no permite restringirla de forma verificable.

## Tarea 6: servicio del host

**Archivos:**

- Crear `ops/opencode-capnet.service`.

**Pasos:**

1. Ejecutar OpenCode como usuario `poo` desde `/home/poo/capnet-workspace`.
2. Cargar configuración y agentes desde `/home/poo/poo-ia/opencode`.
3. Escuchar en `127.0.0.1:4096`.
4. Leer la contraseña desde un archivo de entorno privado del host.
5. Configurar reinicio automático y endurecimiento compatible con lectura del workspace.
6. Validar la unidad antes de instalarla.

## Tarea 7: documentación y ejemplos

**Archivos:**

- Modificar `.env.example`.
- Modificar `README.md`.
- Modificar `.gitignore` o `.dockerignore` solo si las comprobaciones muestran una omisión.

**Pasos:**

1. Documentar las variables nuevas sin valores reales.
2. Explicar autenticación ChatGPT/Codex y que no se usará una API key.
3. Documentar activación, rollback, logs y prueba directa del worker.
4. Explicar la futura migración a OpenCode Go como operación manual opcional.
5. Añadir advertencias de consumo de cuota y secretos.

## Tarea 8: verificación local y publicación

**Pasos:**

1. Ejecutar toda la suite unitaria.
2. Validar sintaxis Python y configuración Docker.
3. Revisar diferencias, secretos accidentales y espacios inválidos.
4. Confirmar que no se alteraron archivos ajenos al alcance.
5. Crear un commit de implementación y hacer `push` a `main`.

## Tarea 9: instalación segura en la Beelink

**Pasos:**

1. Actualizar `/home/poo/poo-ia` desde GitHub.
2. Instalar OpenCode con el método oficial sin modificar los repositorios del workspace.
3. Confirmar la versión y ajustar únicamente la configuración que dependa del esquema instalado.
4. Iniciar el flujo OAuth de ChatGPT/Codex para que el propietario lo autorice.
5. Generar una contraseña local aleatoria sin imprimirla ni guardarla en Git.
6. Instalar e iniciar `opencode-capnet.service`.
7. Verificar loopback, autenticación y estado del proveedor.

## Tarea 10: integración y extremo a extremo

**Pasos:**

1. Guardar el estado Git de los 20 repositorios.
2. Probar por HTTP una consulta documental verificable.
3. Confirmar que la respuesta contiene rutas fuente.
4. Comprobar que no puede leer `.env` ni modificar un archivo.
5. Reconstruir Poo-IA con `OPENCODE_ENABLED=false` y ejecutar una prueba de salud.
6. Activar OpenCode y reiniciar el bot.
7. Probar un saludo y una consulta técnica desde Discord.
8. Comparar nuevamente el estado Git de los 20 repositorios.
9. Si cualquier prueba falla, desactivar OpenCode y conservar el camino de Ollama.

## Condición de finalización

La implementación termina cuando el bot responde localmente a una charla breve, obtiene de OpenCode una respuesta documental con fuentes desde Discord, mantiene limpios los repositorios y puede volver al comportamiento anterior cambiando una sola variable.
