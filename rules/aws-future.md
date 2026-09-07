# AWS y DynamoDB

- AWS se consulta únicamente cuando está habilitado y el propietario lo solicita. El núcleo usa el endpoint autenticado del worker; no ejecuta comandos AWS ni entrega credenciales a modelos.
- AWS se limita a DynamoDB y CloudWatch Logs. Se pueden listar y describir tablas, consultar una muestra de registros de una tabla explícita, listar grupos y leer eventos recientes de un grupo explícito. Todo funciona en modo consulta. No hay comandos libres ni escrituras habilitadas; cualquier modificación necesita una instrucción explícita adicional del propietario.
- El worker utiliza el perfil del usuario del servidor y sus permisos existentes. Los errores IAM no autorizan cambiarlos.
- Nunca copies credenciales a Discord, Git, memoria, prompts, bitácoras ni contenedores de pruebas.
- Solo afirma resultados que devuelva el worker. Informa los límites de los listados y errores reales; una consulta fallida no es un inventario vacío.
- Los resultados AWS se entregan sin incorporarse a la memoria conversacional ni enviarse a OpenCode u Ollama.
