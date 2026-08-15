# rstp-server

Servidor Linux que lee video de una o varias cámaras IP por **RTSP** y lo
re-publica por **HTTP MJPEG** en un puerto distinto por cámara, compatible con
consumidores como **OctoPrint**, dashboards web o integraciones LAN.

## Qué hace

- GUI web para registrar, editar, pausar y eliminar cámaras.
- Stream MJPEG por cámara (`/video_feed`, `?action=stream`, `stream.mjpg`).
- Snapshot JPG por cámara (`/snapshot`, `?action=snapshot`, `snapshot.jpg`).
- Captura periódica de snapshots a disco.
- *Janitor* automático que purga capturas antiguas cuando el disco supera un
  umbral configurable.
- Servicio `systemd` para operar 24/7.

## Requisitos

### Requisitos funcionales

- Una máquina Linux con `systemd`.
- Python **3.11 o superior**.
- `ffmpeg` instalado y accesible en `PATH`.
- Acceso de red desde el servidor hacia las cámaras RTSP.
- Puertos libres:
  - `8080` para la GUI y API.
  - Un puerto HTTP distinto por cada cámara, por ejemplo `8001`, `8002`, etc.

### Paquetes recomendados en Debian/Ubuntu

```bash
sudo apt update
sudo apt install -y git ffmpeg python3 python3-venv python3-pip
```

### Requisitos para despliegue desde GitHub

- El servidor debe poder leer el repositorio `git@github.com:iskael/rstp-server.git`.
- Las llaves SSH del servidor deben estar autorizadas en GitHub.

## Estructura operativa

- Código fuente: `/opt/rstp-server`
- Entorno virtual: `/opt/rstp-server/.venv`
- Base de datos SQLite: `/opt/rstp-server/data/cameras.db`
- Capturas: `/opt/rstp-server/data/captures`
- Servicio: `rstp-server.service`

`data/` y `.venv/` viven en el servidor y no forman parte del repositorio.

## Instalación local para desarrollo

```bash
git clone https://github.com/iskael/rstp-server.git
cd rstp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Abrir <http://localhost:8080>.

## Instalación completa en servidor para correr 24/7

### 1. Preparar el sistema

```bash
sudo apt update
sudo apt install -y git ffmpeg python3 python3-venv python3-pip
```

### 2. Verificar acceso Git por SSH

Desde el servidor:

```bash
ssh -T git@github.com
```

Si GitHub reconoce la llave, continúa.

### 3. Bootstrap inicial

Clona el repo, crea el usuario de servicio, crea el `venv`, instala
dependencias y habilita el servicio:

```bash
git clone https://github.com/iskael/rstp-server.git
cd rstp-server
sudo bash scripts/bootstrap_server.sh
```

Esto deja el servicio instalado en `/opt/rstp-server`.

### 4. Verificar servicio

```bash
systemctl status rstp-server
journalctl -u rstp-server -f
```

### 5. Abrir la GUI

Por defecto:

```text
http://IP_DEL_SERVIDOR:8080
```

## Actualizaciones futuras desde Git

El despliegue ya no necesita copiar archivos desde tu máquina local. El flujo
correcto es:

1. Hacer cambios localmente.
2. Commit y push a `main` en GitHub.
3. Entrar al servidor y ejecutar:

```bash
sudo bash /opt/rstp-server/scripts/update_server.sh
```

Ese script hace:

- `git fetch` y `git pull --ff-only`
- reinstalación de dependencias desde `requirements.txt`
- ajuste de permisos a `rstp:rstp`
- reinicio del servicio

## Migrar una instalación existente a despliegue por Git

Si ya existe `/opt/rstp-server` corriendo pero fue copiado manualmente,
convierte ese árbol a checkout Git en el propio servidor:

```bash
cd /opt/rstp-server
sudo git init
sudo git remote add origin git@github.com:iskael/rstp-server.git
sudo git fetch origin
sudo git checkout -B main origin/main
sudo chown -R rstp:rstp /opt/rstp-server
sudo systemctl restart rstp-server
```

Notas:

- `.venv/` y `data/` quedan como contenido local del servidor.
- A partir de ahí, las siguientes actualizaciones deben hacerse con
  `scripts/update_server.sh`.

## Configuración y uso

1. Abre la GUI.
2. En **Añadir cámara**, ingresa:
   - nombre descriptivo
   - URL RTSP completa
   - puerto HTTP libre para exponerla
   - FPS y calidad JPEG
3. Guarda la cámara.
4. Prueba los endpoints publicados.

Ejemplos:

- Stream: `http://SERVIDOR:8001/?action=stream`
- Snapshot: `http://SERVIDOR:8001/?action=snapshot`
- Stream alternativo: `http://SERVIDOR:8001/video_feed`

### Integración con OctoPrint

- **Stream URL**: `http://SERVIDOR:8001/?action=stream`
- **Snapshot URL**: `http://SERVIDOR:8001/?action=snapshot`

## Capturas y janitor

- Las capturas se guardan en:
  `data/captures/<camera_id>/<YYYY-MM-DD>/<HH MM SS>.jpg`
- El *janitor* corre cada 5 minutos por defecto.
- Cuando el filesystem supera el umbral alto, purga JPG antiguos hasta volver
  al umbral bajo.
- Los umbrales y el intervalo se editan en la sección **Almacenamiento** de la
  GUI.
- También se puede ejecutar manualmente desde la GUI o por API.

## Servicio `systemd`

Comandos útiles:

```bash
systemctl status rstp-server
journalctl -u rstp-server -f
systemctl restart rstp-server
systemctl stop rstp-server
systemctl start rstp-server
```

El servicio actual usa:

- usuario/grupo: `rstp`
- directorio de trabajo: `/opt/rstp-server`
- base de datos: `/opt/rstp-server/data/cameras.db`
- web UI/API: puerto `8080`

## API

- `GET /api/cameras` — listado JSON de cámaras y estado.
- `GET /api/disk` — uso del disco y último reporte del janitor.
- `GET /api/wall` — estado de los streams de go2rtc y telemetría de las
  impresoras, para la vista mosaico.
- `GET /api/printers` — impresoras configuradas y proveedores disponibles,
  sin exponer credenciales.
- `GET /api/printers/discover` — busca impresoras Bambu en la red por SSDP.
- `POST /printers` — alta o edición de una impresora.
- `POST /printers/{stream}/delete` — quita una impresora.
- `POST /janitor/run` — ejecuta una pasada del janitor inmediatamente.

## Vista mosaico (todas las cámaras en una pantalla)

`/static/wall.html` muestra todas las cámaras a la vez, con nombre, semáforo de
estado, codec y bitrate. Hay un enlace directo en la cabecera de la GUI.

Esta vista **no usa los streams MJPEG de este servidor**: se apoya en
[go2rtc](https://github.com/AlexxIT/go2rtc), que corre al lado en el puerto
`1984` y entrega el H.264 por WebRTC **sin transcodificar**. Un `ffmpeg` de este
servidor consume ~20% de CPU por cámara; go2rtc se queda en ~1% con varias
cámaras en vivo, y solo se conecta a la cámara cuando alguien está mirando.

Cada recuadro es un `iframe` al reproductor propio de go2rtc
(`/stream.html?src=<nombre>`), que negocia WebRTC y cae solo a MSE o MJPEG si
hace falta.

### Por qué el estado pasa por `/api/wall`

`GET /api/streams` de go2rtc devuelve las URLs RTSP **con las credenciales de
las cámaras**. Para que el navegador no las reciba nunca, la vista no consulta a
go2rtc directamente: pide el estado a `/api/wall` de este servidor, que hace la
consulta del lado del servidor y devuelve solo `name`, `connected`,
`bytes_recv` y `codec`.

Por lo mismo, **go2rtc no debe quedar con `api.origin: "*"`**: eso permitiría a
cualquier web abierta en el navegador leer esas credenciales. go2rtc solo acepta
`"*"` o nada, así que la opción correcta es no ponerla.

La URL de go2rtc se puede cambiar con la variable de entorno `GO2RTC_API`
(por defecto `http://127.0.0.1:1984`).

### Telemetría de impresión sobre cada cámara

Cada recuadro puede mostrar el estado de la impresora que enfoca: nombre del
trabajo, barra de avance, porcentaje, tiempo restante, hora estimada de término,
capa actual y temperaturas.

Se configura desde la GUI, en el panel **Impresoras**: se elige la cámara, el
proveedor, y se completan los campos que ese proveedor pide. El desplegable de
cámaras se llena solo con los streams que tenga go2rtc en ese momento.

Si la cámara todavía no existe, se elige **«+ cámara nueva…»** y se dan nombre y
URL RTSP en el mismo formulario: al guardar se crea el stream en go2rtc (que lo
persiste en su propio `go2rtc.yaml`) y la telemetría, en un solo paso. Para las
Bambu hay un atajo que arma la URL RTSPS sola a partir de la IP y el código de
acceso, que son los mismos datos que ya pide la telemetría.

Quitar una impresora **no borra la cámara**: el video sigue en la vista, solo
deja de mostrarse el avance de impresión. Para sacar la cámara se usa la
interfaz de go2rtc. Los cambios se
aplican en caliente, sin reiniciar el servicio. Para las Bambu hay un botón
**Buscar impresoras en la red** que completa IP y número de serie solo,
escuchando el anuncio SSDP.

Al editar, los campos de credencial se dejan en blanco para conservar la que ya
estaba guardada; la API nunca devuelve secretos, solo si están puestos o no.

Por debajo se guarda en `data/printers.json` (ver `printers.example.json`), que
también se puede editar a mano. Cada clave es el **nombre del stream en
go2rtc**, y así la telemetría cae en el recuadro de la cámara correspondiente.
Ese archivo vive en `data/` porque lleva credenciales y `data/` está en
`.gitignore`; se escribe de forma atómica y con permisos `600`.

### Agregar un proveedor nuevo

El formulario se dibuja a partir del diccionario `PROVIDERS` de
`app/printers.py`: nombre, ayuda y lista de campos. Para soportar otra marca
alcanza con agregar una entrada ahí y una clase con un método `snapshot()` que
devuelva el mismo diccionario de campos; la GUI se adapta sola.

Orígenes soportados:

- `"type": "bambu"` — MQTT sobre TLS contra la propia impresora (puerto 8883),
  usuario `bblp` y contraseña = **código de acceso** de la pantalla. Requiere
  modo *LAN Only*. Se mantiene una conexión abierta y se cachea el último
  reporte; al conectar se pide un `pushall` para no esperar al siguiente evento.
  El **serial** se puede leer del anuncio SSDP que la impresora emite a
  `239.255.255.250:1990` (campo `USN`), escuchando en el puerto UDP `2021`.
- `"type": "octoprint"` — API REST con cabecera `X-Api-Key`. La clave está en
  `~/.octoprint/config.yaml`, bajo `api: key:`.

Diferencia a tener en cuenta: **OctoPrint informa el tiempo transcurrido real y
la Bambu no.** Para la Bambu, si se ve arrancar el trabajo se mide de verdad; si
no, se estima a partir del avance y el restante asumiendo ritmo parejo. En la
vista ese caso aparece con `~` delante para no dar por exacto algo que no lo es.

### Una sola conexión por cámara

Varias cámaras baratas (las Wyze con firmware RTSP, entre otras) aceptan **un
solo cliente RTSP** y su servicio se cae si reciben dos. Si una cámara va a estar
en go2rtc y además en este servidor, conviene que este servidor la lea *desde*
go2rtc y no de la cámara: usar `rtsp://127.0.0.1:8554/<nombre>` como URL RTSP.
Así la cámara ve un único cliente.

## Seguridad y operación

- No incluye autenticación; está pensado para LAN privada.
- Si vas a exponerlo fuera de tu red, ponlo detrás de un reverse proxy con TLS
  y autenticación.
- Hay un proceso `ffmpeg` por cámara, independientemente del número de clientes
  HTTP conectados.
- Solo se guardan snapshots JPG en disco; no hay grabación de video continua.
