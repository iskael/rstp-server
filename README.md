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
- `POST /janitor/run` — ejecuta una pasada del janitor inmediatamente.

## Seguridad y operación

- No incluye autenticación; está pensado para LAN privada.
- Si vas a exponerlo fuera de tu red, ponlo detrás de un reverse proxy con TLS
  y autenticación.
- Hay un proceso `ffmpeg` por cámara, independientemente del número de clientes
  HTTP conectados.
- Solo se guardan snapshots JPG en disco; no hay grabación de video continua.
