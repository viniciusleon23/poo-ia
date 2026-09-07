# CSV de consultas AWS

El propietario pidió recibir los resultados como CSV. El bot entregará un archivo adjunto para las operaciones de lectura existentes de DynamoDB y CloudWatch. Se conserva el flujo separado de documentación del brain y cambios en repositorios de ejecución; una consulta AWS no crea trabajos de código ni añade datos a memoria de modelos.

## Consulta y representación

Un sufijo explícito `en csv` selecciona el formato. `dámelo en csv` busca una consulta explícita entre las diez solicitudes AWS satisfactorias más recientes, anteriores al mensaje actual, en la misma conversación y generación de memoria. Vuelve a leer AWS y lo informa; no reconstruye registros desde el texto anterior. Sin contexto válido solicita la operación y recurso. Los reintentos de entrega de un CSV ya preparado reutilizan el adjunto persistido.

El worker mantiene las cinco acciones de lectura y sus límites: 25 recursos, 10 elementos DynamoDB evaluados o 20 eventos de la última hora. El archivo se construye desde JSON estructurado antes del recorte de texto. Las columnas DynamoDB son la unión ordenada de atributos. Booleanos, nulos y números mantienen representación explícita; objetos y listas usan JSON compacto, con números anidados como cadenas para preservar precisión. CloudWatch conserva timestamp UTC con milisegundos. CSV utiliza UTF-8 con BOM y CRLF.

El límite final es 128 KiB y la captura CLI es de hasta 1 MiB para este formato. Un exceso falla sin adjunto parcial; las páginas limitadas sí pueden exportarse y llevan una leyenda. La redacción existente se aplica antes de serializar. Los encabezados y cadenas con prefijos de fórmula se neutralizan; si esto crea nombres de columna repetidos se devuelve error.

## Contrato y entrega

La API añade `format` opcional (`text` o `csv`). Un éxito CSV contiene `attachment` con `filename`, `content_type`, `content_base64` y `sha256`. El cliente verifica basename ASCII terminado en `.csv`, MIME `text/csv`, base64 estricto, UTF-8, tamaño y hash. Los errores no llevan adjunto.

El núcleo representa los datos con `CsvAttachment` inmutable. La migración 004 persiste bytes y metadatos mediante una tabla ligada a outbox con eliminación en cascada, en la misma transacción que las partes. La idempotencia compara también el archivo. Solo la primera parte lleva el adjunto y su ACK se registra tras un envío Discord que contiene texto y archivo. Cada intento crea un `BytesIO` y `discord.File` nuevos. Los adjuntos fuerzan `exchange_on_complete=False`.

## Verificación y operación

Cubrir parser, consultas fuera del alcance, integridad y límites, números exactos, Unicode, comillas y saltos, redacción, fórmulas y colisiones, aislamiento entre conversaciones, olvido, reintentos, reinicio, idempotencia, retención y ausencia de memoria. Ejecutar la suite completa en Linux y consultar por API autenticada para verificar CSV reales sin publicar mensajes de prueba en Discord. Respaldar SQLite antes de migrar; registrar despliegue y pruebas en un worktree independiente del brain.
