# Activación de validación uv y consultas AWS

El propietario autorizó activar ambos componentes después de instalar uv, Python 3.13, dependencias de Tasks y el perfil AWS del servidor. El worker debe producir evidencia automática de pruebas y Discord debe permitir consultas AWS de solo lectura. Se conserva la separación brain → repositorio de ejecución → bitácora documental.

## Validación

El validador conserva su comparación entre cambio y commit base, pero separa comandos Git de comandos de prueba del repositorio. Los segundos requieren Docker; habilitar el validador sin ese ejecutor nunca habilita ejecución en el host.

Cada proyecto uv prepara una imagen identificada por la huella de sus metadatos bloqueados y versión Python. El contexto de construcción contiene solo esos metadatos y un Dockerfile controlado por el worker. La construcción descarga dependencias sin credenciales del usuario; las pruebas posteriores no tienen red. Una copia efímera excluye Git, entornos, credenciales y enlaces externos. Docker aplica límites de CPU, memoria, procesos y tiempo, usuario sin privilegios y raíz de solo lectura. No se monta HOME, workspace completo ni socket Docker.

El staging y registros de propiedad viven en WORKER_DATA_ROOT para funcionar con PrivateTmp. El manager limpia contenedores al cancelar o recuperar un proceso terminado, preservando trabajos vivos tras reiniciar el manager. Un límite interno termina contenedores aunque desaparezca el cliente.

La primera versión soporta Python con uv. Proyectos incompatibles producen unavailable con motivo, sin pruebas en el host. Codex recibe instrucciones de delegar ejecución de tests e instalación de dependencias al harness. Su acceso de edición sigue correspondiendo al usuario poo; este cambio no crea otra identidad para Codex.

## Consultas AWS

AWS_ENABLED se valida en núcleo y worker. El núcleo necesita su worker autenticado. El alcance confirmado por el propietario es exclusivamente DynamoDB y CloudWatch. El endpoint permite listar y describir tablas, muestras de registros de una tabla explícita, listar grupos CloudWatch Logs y leer eventos de la última hora en un grupo explícito. Las muestras evalúan como máximo diez elementos y los logs muestran hasta veinte eventos. STS y Lambda no son acciones públicas del bot. El worker construye argumentos estáticos sin shell, usa AWS CLI con perfil local y proyecta campos no sensibles. Las respuestas acotadas indican truncamiento y los errores no incluyen salidas sin filtrar.

Las claves no entran en núcleo, modelos, memoria conversacional, bitácora ni contenedores de pruebas. No se habilitan mutaciones cloud ni lectura de valores de gestores de secretos. Cualquier modificación cloud futura requiere una instrucción explícita adicional; activar consultas no la autoriza. Las operaciones dependen de los permisos IAM existentes; no se modifican políticas.

## Verificación y despliegue

Cubrir gates de configuración, allowlist, parser, ausencia de shell y secretos, aislamiento, limpieza, éxito/fallo/baseline y regresiones de routing. En Linux ejecutar pruebas reales de Tasks y una prueba sintética de aislamiento. Probar listados DynamoDB y CloudWatch por API autenticada, sin enviar mensajes Discord durante la verificación. Desplegar con copia privada de configuración y versión anterior para rollback; registrar evidencia en un worktree separado del brain.
