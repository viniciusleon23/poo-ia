# Confirmación de recepción y progreso del bot

El propietario pidió integrar lo validado y reducir la incertidumbre durante las esperas. El PR 1 ya se fusionó y las cuatro bitácoras anteriores se integraron a brain/main. Esta mejora comunica recepción, etapas confirmadas y espera prolongada sin inventar porcentaje, resultado ni duración estimada.

## Causa y comportamiento

El adaptador solo mostraba typing durante `handle`; AWS y chat preparaban su única respuesta al terminar. Los trabajos pesados emitían únicamente «en cola» y el resultado. El worker no publicaba fases y una consulta de inventario podía bloquear la entrega. Además, los guards trataban cualquier outbox como resultado terminado: añadir un ACK sin corregirlos habría impedido ejecutar o recuperar la consulta.

Una reacción 👀 indica recepción antes de esperar inventario o el bloqueo de conversación. La reacción y typing son opcionales y tienen un timeout corto; sus fallos no interrumpen el trabajo. Los trabajos pesados envían confirmación durable inmediata. Una consulta directa que tarda más de 500 ms envía su confirmación; las respuestas instantáneas conservan una sola salida. El refresco de inventario tiene un máximo de dos segundos para no bloquear indefinidamente los avisos.

El núcleo emite investigación documental y revisión previa. El worker persiste `phase` antes de preparar el repositorio, ejecutar Codex, validar, documentar o publicar. El núcleo traduce únicamente fases permitidas a mensajes en español. Si transcurren 60 segundos sin cambio de etapa, envía un recordatorio con la última etapa confirmada. Un monitor comprueba el plazo cada segundo. Los trabajos en cola también reciben seguimiento. Una operación completada, preparada o cancelada deja de producir avisos.

## Persistencia y recuperación

`ack` y `progress` nunca se incorporan a memoria. `request_has_final_outbox` reconoce solo `response` y `error`, por lo que un recibo no impide continuar ni recuperar una consulta. Una respuesta ya guardada sigue impidiendo repetir AWS o generar de nuevo el CSV.

Solo el progreso pendiente más reciente queda habilitado para entrega. Las filas anteriores conservan su clave de deduplicación y se cierran mediante `completed_at`, sin inventar ACKs ni IDs remotos. La respuesta final cierra progresos pendientes y bloquea avisos posteriores; conserva la confirmación inicial. Outbox revalida cada parte antes del envío para omitir snapshots obsoletos. No requiere migración.

La etapa, revisión y hora se conservan en el checkpoint operativo. Un fallo al comunicar progreso se registra y reintenta sin cancelar el backend ni abandonar un trabajo remoto. La consulta directa cancela su temporizador al finalizar; el cierre del núcleo detiene todos los monitores. Durante el arranque persiste la recuperación secuencial existente: una consulta directa recuperada lenta puede retrasar la conexión inicial de Discord; sus avisos se guardan para entrega posterior.

## Validación y operación

Probar operaciones bloqueadas con eventos, recepción mientras `handle` sigue pendiente, etapas observables antes de operaciones del worker, recordatorios con reloj controlado, FIFO, repetición de fases, cancelación, errores de feedback, reinicio, protección de CSV, memoria y eliminación de progreso atrasado. Ejecutar suite completa en Linux, integrar la rama probada y desplegar con respaldo privado de SQLite/manifiestos e imagen anterior. Registrar el resultado en el brain y refrescar su snapshot; reiniciar OpenCode tras el reemplazo atómico del snapshot para evitar que conserve un directorio eliminado.
