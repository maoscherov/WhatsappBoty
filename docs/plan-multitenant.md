# Plan — Plataforma multi-cliente

> Estado: **propuesta, sin implementar**. Redactado el 2026-08-10 a pedido de Mariano.
> Ejecutar recién con aprobación explícita.

## Objetivo

Pasar de "un despliegue por cliente" a **una plataforma que atiende a varios comercios**,
donde dar de alta uno nuevo sea una tarea de configuración y no de infraestructura.

Horizonte declarado: **5 o más clientes en 6 meses** (farmacia Mutual Independencia,
Mascotas del Oeste, y los que sigan).

## Decisiones ya tomadas

| Punto | Decisión |
|---|---|
| Identificación del comercio | **Cada cliente tiene su propio número de WhatsApp.** El `phone_number_id` que viene en cada mensaje identifica al comercio. |
| Acceso al panel | **Los clientes entran a su propio panel** y ven solo sus datos. |
| Cobros | Cada comercio cobra en **su propia cuenta** (Payway o Mercado Pago). La plata no pasa por nosotros. |
| Pasarela | Elegible por comercio desde el panel (ya implementado: clave `payment_provider`). |

## Estado actual (lo que hay que cambiar)

1. **Un solo comercio por despliegue.** Todo sale de variables de entorno: número de
   WhatsApp, credenciales, catálogo, padrón.
2. **Datos sin separar.** Las conversaciones viven en Redis con clave `session:{telefono}`
   y los mensajes en Postgres sin columna de comercio. Dos clientes en el mismo sistema
   se pisarían.
3. **Catálogo único en memoria.** `SKUService` es un singleton que carga un CSV.
4. **Autenticación insuficiente.** Hoy el panel se abre con una única clave compartida
   (`BO_KEY`) que da acceso total. **Esto es incompatible con que los clientes entren**:
   necesitamos usuarios, contraseñas y permisos por comercio.
5. **Lógica duplicada** entre `webhook.py` y `simulate.py` — ya se desincronizó una vez.

## Arquitectura propuesta

### Identificación del comercio
El webhook de WhatsApp trae `metadata.phone_number_id` en cada mensaje. Ese valor es
la llave: se busca el comercio en el registro y todo el procesamiento del mensaje ocurre
con **su** contexto (catálogo, credenciales, configuración, textos).

Si un `phone_number_id` no está registrado, el mensaje se descarta con log — nunca se
atiende con la configuración de otro.

### Registro de comercios (Postgres)
Tabla `tenants`: id, nombre, `wa_phone_number_id`, token de WhatsApp, proveedor de
WhatsApp (meta/kapso), credenciales de pasarela, dominio propio, estado (activo/pausado),
fecha de alta. Las credenciales cifradas en reposo.

### Aislamiento de datos
- **Redis**: todas las claves pasan a llevar el prefijo del comercio
  (`t:{tenant}:session:{telefono}`, `t:{tenant}:payway:pending:{id}`, etc.).
- **Postgres**: columna `tenant_id` en `messages`, `orders` y embeddings, con índice
  compuesto. Toda consulta filtra por comercio — sin excepción.
- **Catálogo**: `SKUService` deja de ser singleton y pasa a un registro por comercio,
  con carga perezosa y recarga independiente.
- **Configuración**: `ConfigService` con prefijo por comercio; los valores por defecto
  siguen siendo los mismos.

### Panel multi-cliente
- **Usuarios reales** con contraseña (hash), en lugar de la clave compartida.
- **Roles**: `admin` (nosotros, ve todos los comercios), `dueño` (su comercio completo),
  `operador` (atiende conversaciones de su comercio).
- **Todo endpoint filtra por el comercio del usuario**, no por lo que venga en la request.
- El selector de comercio del panel se vuelve real para el rol admin.

## Fases

### Fase 0 — Preparación (no cambia el comportamiento)
- Unificar la lógica de `webhook.py` y `simulate.py` en un servicio común. **Prerequisito**:
  hacer multitenant dos copias de la misma lógica duplica el error.
- Centralizar el armado de claves de Redis en un solo lugar (hoy están dispersas).
- Tests de regresión del flujo completo antes de tocar nada estructural.

### Fase 1 — Registro de comercios
- Migración de la tabla `tenants` + alta/edición desde el panel (solo admin).
- Resolución del comercio por `phone_number_id` en el webhook.
- La farmacia queda cargada como primer comercio, con su configuración actual.

### Fase 2 — Aislamiento de datos
- Prefijos de comercio en Redis, con migración de las claves existentes.
- `tenant_id` en las tablas de Postgres y en todas las consultas.
- Catálogo y configuración por comercio.
- **Criterio de aceptación**: con dos comercios cargados, ninguna consulta de uno
  devuelve un dato del otro. Se prueba explícitamente.

### Fase 3 — Autenticación y permisos
- Usuarios, contraseñas y roles; reemplazo de la clave compartida.
- Filtrado por comercio en todos los endpoints del panel.
- Registro de quién hizo cada acción.

### Fase 4 — Alta autogestionada
- Alta de un comercio nuevo desde el panel: datos, número de WhatsApp, credenciales,
  carga inicial del catálogo.
- Conexión del número vía Kapso (evita el trámite manual en Meta por cada cliente).

### Fase 5 — Migración
- Mascotas del Oeste entra directo a la plataforma.
- La farmacia se migra al final, con ventana de mantenimiento y vuelta atrás preparada.

## Camino paralelo recomendado (para no frenar la venta)

Mascotas del Oeste puede arrancar **antes** de que la plataforma esté lista, con un
despliegue propio como el actual: aislamiento total, cero desarrollo, y sin riesgo para
la farmacia que ya está en producción. Se migra en la Fase 5.

La decisión de esperar o arrancar así depende de la fecha que se comprometa con ellos.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| **Fuga de datos entre comercios** — el peor escenario, y son datos de salud | Filtrado por comercio en una sola capa, no repartido; pruebas explícitas de aislamiento; revisión dedicada |
| Romper la farmacia, que está en producción | Se migra última, con vuelta atrás preparada |
| La migración de claves de Redis pierde conversaciones activas | Ventana de mantenimiento fuera de horario y respaldo previo |
| El alcance crece sin control | Fases cerradas; la Fase 1 y 2 ya habilitan el negocio |

## Preguntas abiertas

- ¿Los comercios ven métricas de su operación, o solo conversaciones y pedidos?
- ¿Cada comercio con su dominio (`cliente.remedia.ar`) o todos bajo el mismo panel?
- ¿Hay datos que ScalaUp necesite ver agregados entre comercios (facturación, uso)?
- ¿Se cobra por comercio, por conversación, o por transacción? Puede requerir medición.
