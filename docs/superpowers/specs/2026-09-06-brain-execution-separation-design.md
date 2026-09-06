# Separación entre conocimiento, ejecución y documentación

Diseño autorizado por el propietario el 6 de septiembre de 2026 tras el diagnóstico del trabajo 7387b1eb.

El flujo es: leer documentación del brain, resolver el repositorio de ejecución, preparar el cambio y publicar su PR cuando se solicite, y volver al brain para registrar el proceso como una etapa separada.

## Límites

- `brain-capnet` es una fuente documental, nunca un destino del worker de código ni del publicador de PR de servicios. La separación se valida también dentro del worker, no solo en el router.
- Una cita del brain no establece el repositorio activo de ejecución. Los nombres explícitos de servicios prevalecen sobre la memoria. Los alias conocidos se resuelven contra el inventario; la ambigüedad se explica antes de ejecutar.
- La preparación devuelve JSON con repositorio, archivos candidatos, estado, notas y datos faltantes. El núcleo rechaza resultados incompletos y contradicciones; el worker comprueba las rutas antes de Codex. El contenido del preflight sigue siendo evidencia no confiable.
- El PR contiene exclusivamente los cambios del repositorio de ejecución. No hay merges ni despliegues de los servicios Capnet automáticos.
- Una bitácora determinista registra el resultado en `Procesos/Poo-IA/<job_id>.md`, dentro de un worktree del brain separado, en `poo-ia/docs-<job_id>`. No recibe prompts, secretos ni logs completos. Se actualiza idempotentemente al preparar y publicar el PR. Su integración al historial compartido del brain queda separada; no se publica un PR documental automáticamente.
- Los errores de documentación se conservan aparte y no alteran ni ocultan el resultado del cambio de código. El bot muestra la ubicación y estado documental.

## Compatibilidad y verificación

Se conservan los manifiestos existentes y la autorización explícita para publicar. El contexto antiguo que apunte al brain se ignora y se limpia al actualizar. Se añaden regresiones para la petición original, repo explícito, aliases, consultas de capacidades, `editar`, repositorios contradictorios y rechazos del worker. La bitácora se prueba con repositorios Git temporales reales y el flujo completo con dobles de los proveedores; una prueba adicional en el servidor comprueba Codex real sin publicar cambios de diagnóstico.

La actualización del servidor usa una copia de respaldo del código y de SQLite, verifica que no haya trabajos activos, reconstruye la imagen y reinicia únicamente los componentes modificados. Ollama permanece sin cambios.
