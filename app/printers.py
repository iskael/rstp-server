"""Telemetría de impresión de las impresoras asociadas a cada cámara.

Dos orígenes:

- **Bambu Lab** (H2S y familia): MQTT sobre TLS en el puerto 8883 de la propia
  impresora, usuario ``bblp`` y contraseña = código de acceso de la pantalla.
  Requiere modo *LAN Only*. La conexión se mantiene abierta y se cachea el
  último reporte; al conectar se pide un ``pushall`` para tener el estado
  completo sin esperar al siguiente evento.
- **OctoPrint**: API REST con cabecera ``X-Api-Key``.

La configuración vive en ``data/printers.json`` (fuera del repo, porque lleva
credenciales). Formato::

    {
      "<nombre-del-stream-en-go2rtc>": {
        "type": "bambu",
        "host": "192.168.0.223",
        "serial": "SERIAL_DE_LA_IMPRESORA",
        "code": "CODIGO_DE_ACCESO",
        "label": "Bambu Lab H2S"
      },
      "<otro-stream>": {
        "type": "octoprint",
        "url": "http://192.168.0.196:5000",
        "api_key": "...",
        "label": "Artillery"
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# La Bambu informa el restante en minutos; OctoPrint en segundos.
_BAMBU_STATES = {
    "RUNNING": "imprimiendo",
    "PAUSE": "en pausa",
    "FINISH": "terminada",
    "FAILED": "fallida",
    "IDLE": "inactiva",
    "PREPARE": "preparando",
    "SLICING": "procesando",
}


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BambuPrinter:
    """Mantiene una conexión MQTT viva y cachea el último reporte."""

    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.serial = cfg["serial"]
        self.code = cfg["code"]
        self.label = cfg.get("label", "Bambu Lab")
        self._report: dict = {}
        self._last_seen: float = 0.0
        # job_id -> epoch en que lo vimos por primera vez, para el transcurrido.
        self._first_seen: dict[str, float] = {}
        self._client = None

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"rstp-server-{self.serial}")
        client.username_pw_set("bblp", self.code)
        # El certificado de la impresora es autofirmado: se cifra igual, pero
        # no se puede validar contra una CA.
        client.tls_set(cert_reqs=ssl.CERT_NONE,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._client = client
        try:
            client.connect_async(self.host, 8883, keepalive=60)
            client.loop_start()
        except Exception as e:  # noqa: BLE001
            log.warning("bambu %s: no se pudo iniciar MQTT: %s", self.host, e)

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # -------- callbacks MQTT --------
    def _on_connect(self, client, _u, _f, rc, _props=None):
        log.info("bambu %s: conectado (rc=%s)", self.host, rc)
        client.subscribe(f"device/{self.serial}/report")
        client.publish(
            f"device/{self.serial}/request",
            json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}),
        )

    def _on_message(self, _c, _u, msg):
        try:
            payload = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        chunk = payload.get("print")
        if not isinstance(chunk, dict):
            return
        # Los reportes son incrementales: se acumulan sobre el último estado.
        self._report.update(chunk)
        self._last_seen = time.time()

    # -------- lectura --------
    def snapshot(self) -> dict:
        r = self._report
        out = {
            "source": "bambu",
            "label": self.label,
            "online": bool(r) and (time.time() - self._last_seen) < 120,
            "state": None,
            "printing": False,
            "job": None,
            "percent": None,
            "remaining_s": None,
            "elapsed_s": None,
            "elapsed_estimated": False,
            "eta_epoch": None,
            "layer": None,
            "layers_total": None,
            "nozzle": _num(r.get("nozzle_temper")),
            "nozzle_target": _num(r.get("nozzle_target_temper")),
            "bed": _num(r.get("bed_temper")),
            "bed_target": _num(r.get("bed_target_temper")),
        }
        if not r:
            return out

        gcode_state = r.get("gcode_state")
        out["state"] = _BAMBU_STATES.get(gcode_state, gcode_state)
        out["printing"] = gcode_state in ("RUNNING", "PREPARE")
        out["job"] = r.get("subtask_name") or r.get("gcode_file")
        out["percent"] = _num(r.get("mc_percent"))
        out["layer"] = r.get("layer_num")
        out["layers_total"] = r.get("total_layer_num")

        remaining_min = _num(r.get("mc_remaining_time"))
        if remaining_min is not None:
            out["remaining_s"] = int(remaining_min * 60)
            if out["printing"]:
                out["eta_epoch"] = int(time.time() + remaining_min * 60)

        out["elapsed_s"], out["elapsed_estimated"] = self._elapsed(r, out)
        return out

    def _elapsed(self, r: dict, out: dict) -> tuple[Optional[int], bool]:
        """Transcurrido. La Bambu no lo publica, así que hay dos caminos.

        Si vimos empezar el trabajo, se mide de verdad. Si no, se estima a
        partir del avance y el restante, que asume ritmo parejo y por eso se
        marca como estimado.
        """
        job_id = str(r.get("job_id") or r.get("subtask_id") or "")
        if not out["printing"] or not job_id:
            self._first_seen.pop(job_id, None)
            return None, False

        now = time.time()
        pct = out["percent"]
        first = self._first_seen.get(job_id)
        if first is None:
            # Solo cuenta como inicio real si lo agarramos casi desde cero.
            if pct is not None and pct <= 1:
                self._first_seen[job_id] = now
                return 0, False
            first = None

        if first is not None:
            return int(now - first), False

        remaining = out["remaining_s"]
        if pct and remaining is not None and 0 < pct < 100:
            return int(remaining * pct / (100 - pct)), True
        return None, False


class OctoPrintPrinter:
    """Lee el estado por la API REST. Es barato, se consulta a demanda."""

    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        self.api_key = cfg["api_key"]
        self.label = cfg.get("label", "OctoPrint")

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(
            f"{self.url}{path}", headers={"X-Api-Key": self.api_key})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    def snapshot(self) -> dict:
        out = {
            "source": "octoprint",
            "label": self.label,
            "online": False,
            "state": None,
            "printing": False,
            "job": None,
            "percent": None,
            "remaining_s": None,
            "elapsed_s": None,
            "elapsed_estimated": False,
            "eta_epoch": None,
            "layer": None,
            "layers_total": None,
            "nozzle": None,
            "nozzle_target": None,
            "bed": None,
            "bed_target": None,
        }
        try:
            job = self._get("/api/job")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug("octoprint %s no responde: %s", self.url, e)
            return out

        out["online"] = True
        state = job.get("state") or ""
        out["state"] = state.lower()
        out["printing"] = state.startswith("Printing")

        progress = job.get("progress") or {}
        out["percent"] = _num(progress.get("completion"))
        elapsed = progress.get("printTime")
        left = progress.get("printTimeLeft")
        out["elapsed_s"] = int(elapsed) if elapsed is not None else None
        out["remaining_s"] = int(left) if left is not None else None
        if out["printing"] and left is not None:
            out["eta_epoch"] = int(time.time() + left)

        f = (job.get("job") or {}).get("file") or {}
        out["job"] = f.get("display") or f.get("name")

        try:
            printer = self._get("/api/printer")
            temps = printer.get("temperature") or {}
            tool = temps.get("tool0") or {}
            bed = temps.get("bed") or {}
            out["nozzle"] = _num(tool.get("actual"))
            out["nozzle_target"] = _num(tool.get("target"))
            out["bed"] = _num(bed.get("actual"))
            out["bed_target"] = _num(bed.get("target"))
        except (urllib.error.URLError, OSError, ValueError):
            # La impresora puede estar desconectada del USB y OctoPrint vivo.
            pass
        return out


class PrinterHub:
    """Agrupa las impresoras, indexadas por nombre de stream de go2rtc."""

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.printers: dict[str, Any] = {}

    def start(self) -> None:
        if not self.config_path.exists():
            log.info("sin %s: no hay telemetría de impresoras",
                     self.config_path)
            return
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            log.warning("no se pudo leer %s: %s", self.config_path, e)
            return

        for stream, entry in cfg.items():
            kind = (entry or {}).get("type")
            try:
                if kind == "bambu":
                    printer = BambuPrinter(entry)
                elif kind == "octoprint":
                    printer = OctoPrintPrinter(entry)
                else:
                    log.warning("tipo desconocido para %s: %r", stream, kind)
                    continue
                printer.start()
                self.printers[stream] = printer
                log.info("telemetría activa para %s (%s)", stream, kind)
            except Exception as e:  # noqa: BLE001
                log.warning("no se pudo iniciar %s: %s", stream, e)

    def stop(self) -> None:
        for printer in self.printers.values():
            printer.stop()
        self.printers.clear()

    async def snapshot(self) -> dict:
        """Estado de todas las impresoras. Las lecturas HTTP van a un hilo."""
        async def one(stream: str, printer: Any) -> tuple[str, dict]:
            try:
                return stream, await asyncio.to_thread(printer.snapshot)
            except Exception as e:  # noqa: BLE001
                log.debug("snapshot de %s falló: %s", stream, e)
                return stream, {"source": "error", "online": False}

        results = await asyncio.gather(
            *(one(s, p) for s, p in self.printers.items()))
        return dict(results)
