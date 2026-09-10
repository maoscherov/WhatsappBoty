# Remedia Agent 0.2.0 — instalación en la farmacia

`agent.exe` (6,5 MB, sin dependencias). Corre como servicio de Windows en la PC
que ve al ERP ObServer Gestión. Solo hace conexiones salientes: al ERP en la red
local y a Remedia por HTTPS. No abre ningún puerto.

## Antes de ir a la farmacia

1. **Sucursal y token en Remedia.** Con la API desplegada y tu `BO_KEY`:

   ```
   curl -X POST "https://cerca.remedia.ar/bo/branches?key=BO_KEY" -H "Content-Type: application/json" -d "{\"branch_id\":\"farmacia-mutual\",\"nombre\":\"Farmacia Mutual Independencia\"}"
   ```

   La respuesta trae el `token` **una sola vez**. Guardalo. Si se pierde:
   `POST /bo/branches/farmacia-mutual/rotate-token?key=BO_KEY`.

2. Anotar la **IP del servidor del ERP** en la red de la farmacia (el que corre
   `ServiciosGestion.exe`, puerto 60064). Ejemplo: `http://192.168.1.156:60064`.

## En la PC de la farmacia

1. Copiar `agent.exe` a cualquier carpeta (por ejemplo el Escritorio). El
   instalador lo copia solo a `C:\ProgramData\RemediaAgent\`.

2. Verificar que el ERP responde desde esa PC. En PowerShell:

   ```
   Invoke-WebRequest -Headers @{Accept="application/json"} http://192.168.1.156:60064/api/productos/lote/1 | Select-Object StatusCode
   ```

   - `200` → bien.
   - `401` → en ObServer hay que habilitar la **API de productos** (flag `API_Productos`). El agente igual se instala y lo reporta como "no autorizado".
   - No conecta → revisar IP y que la PC del ERP esté encendida.

3. Abrir una consola **como administrador** (clic derecho → "Ejecutar como administrador") y correr, en una sola línea:

   ```
   agent.exe install --token TOKEN --erp http://192.168.1.156:60064 --branch farmacia-mutual
   ```

   Esto escribe la configuración, registra el servicio `RemediaAgent` con
   arranque automático, lo inicia, y deja el icono de bandeja arrancando al
   iniciar sesión cualquier usuario. El icono aparece enseguida.

4. Esperar 2 o 3 minutos (la primera carga sube ~54.000 productos) y comprobar:

   - El icono de bandeja en **verde**. Clic derecho → "Ver detalle…" muestra
     productos leídos, lectura completa en segundos y "Websocket: conectado".
   - O por consola: `agent.exe status`.
   - Desde Remedia: `GET /bo/branches?key=BO_KEY` muestra la sucursal con
     heartbeat reciente, `erp_status: ok` y `catalog_count` cercano a 54.000.

## Qué significa el color del icono

| Color | Significa | Qué hacer |
|---|---|---|
| Verde | Todo bien. | Nada. |
| Amarillo | Hay lotes sin enviar, se cortó la conexión con Remedia o hace más de 1 h que no sincroniza. | Suele resolverse solo (reintenta). Si dura, revisar internet. |
| Rojo | El ERP no responde o no autoriza, o Remedia rechazó el token. | Clic derecho → "Ver detalle…" dice cuál. ERP: PC del ERP apagada o API deshabilitada. Token: "Configuración…" y cargar el nuevo. |
| Gris | El servicio no está corriendo. | `services.msc` → iniciar "Remedia Agent", o avisar a soporte. |

**"Websocket: desconectado" con "Reconexiones" subiendo cada pocos segundos**
(en "Ver detalle…"): hay **dos agentes con el mismo token**, por ejemplo uno de
prueba en otra PC. Remedia admite una sola conexión por sucursal y cada uno
reemplaza al otro. Desinstalar el que sobra (`agent.exe uninstall`).

## Cambiar token o direcciones después

Clic derecho en el icono → **Configuración…**. No hace falta administrador.
"Probar" verifica cada dato y muestra los milisegundos. "Guardar" pide
confirmación y aplica el cambio en caliente. Un token o dirección de Remedia
inválidos se rechazan; un ERP apagado se guarda con aviso.

## Archivos

- Config: `C:\ProgramData\RemediaAgent\agent.toml`
- Estado local: `C:\ProgramData\RemediaAgent\state.sqlite`
- Logs (7 días): `C:\ProgramData\RemediaAgent\logs\` (o clic derecho → "Abrir carpeta de logs")

## Actualizar a una versión nueva

Mismo comando `agent.exe install ...` con el `agent.exe` nuevo, como administrador.
Si el servicio ya existe lo detiene, cierra el icono, reemplaza el ejecutable,
actualiza la configuración y lo vuelve a arrancar. La configuración y el estado
local se conservan.

Si aparece "The specified service already exists (os error 1073)" es que el
`agent.exe` es anterior a la 0.2.0 con soporte de actualización: correr antes
`agent.exe uninstall`.

## Desinstalar

Consola como administrador: `agent.exe uninstall`. Cierra el icono, quita el
autoarranque y elimina el servicio. No borra la carpeta de datos.

## Después, del lado de Remedia

Cuando la sucursal esté en verde, en Railway setear `DEFAULT_BRANCH_ID=farmacia-mutual`
y redesplegar: recién ahí el bot deja de usar el CSV y pasa a leer el catálogo
del ERP. Hasta entonces el sync entra a la base sin afectar la operación.

Checksum SHA-256 de este `agent.exe`: empieza con `7176cfc47c4027d2`.
