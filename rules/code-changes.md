# Cambios de código

- Solo una petición explícita del propietario autoriza preparar un cambio mediante el worker Codex.
- Una petición ambigua o meramente informativa continúa siendo investigación y nunca se convierte en una modificación por decisión del modelo.
- Antes de Codex, usa la investigación documental de solo lectura para identificar el repositorio, archivos y restricciones relevantes, salvo que la excepción mecánica aprobada aplique.
- Informa qué cambió, qué pruebas se ejecutaron y su resultado. No declares éxito sin evidencia del worker.
- Preparar un cambio no autoriza publicarlo. Solo una petición explícita de crear, armar o publicar el PR autoriza Git y GitHub.
- Nunca fusiones un PR ni despliegues a producción desde este flujo.
- El brain se consulta como documentación y nunca se usa como repositorio de ejecución, ni por memoria ni por citas. Resuelve un servicio disponible antes de crear su worktree.
- El preflight debe confirmar ese mismo repositorio y sus archivos; un resultado incompleto o contradictorio detiene la ejecución.
- El código y su PR pertenecen solo al repositorio de ejecución. Después el worker prepara una bitácora determinista en un worktree separado del brain, vinculada al trabajo, validación y PR. Esa documentación no se mezcla con el diff del servicio y su integración al brain es separada.
