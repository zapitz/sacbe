"""Pasarela automática para programas en Python que hablan con Anthropic por httpx o httpx2 (SDK o suelto).

Se monta en el contenedor (solo lectura) y se pone su carpeta en PYTHONPATH; Python la carga solo al
arrancar cualquier proceso (uvicorn, gunicorn, celery, scripts). No hace nada si faltan
PASARELA_IA_URL (p. ej. http://pasarela-ia-api:8080/anthropic) o PASARELA_IA_LLAVE.

Aplica la regla de respaldo directo de docs/conectar-programas.md a nivel de HTTPTransport y AsyncHTTPTransport
de httpx y de httpx2 (el SDK 0.x usa httpx; el 1.x y el 2.x, httpx2), para no tocar el código ni la imagen de cada
programa:
  - Solo POST /v1/messages y /v1/messages/count_tokens a api.anthropic.com van al gestor, con la llave interna.
  - Regreso a directo (la petición ORIGINAL, con su llave) si falla la conexión, si el gestor responde
    502/503/504 o un error cuyo error.message empieza con «pasarela-ia:». Luego 60 s directo.
  - Los errores de Anthropic se devuelven tal cual. Un ReadTimeout NO se repite (pagaría dos veces);
    3 seguidos abren el enfriamiento.
Para quitarla: borrar PYTHONPATH y el montaje del compose y recrear el contenedor.
"""

import json
import logging
import os
import sys
import threading
import time

_log = logging.getLogger("pasarela_ia")

HOST = "api.anthropic.com"
RUTAS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
PREFIJO = "pasarela-ia:"
CONEXION_S = 3.0
ENFRIAMIENTO_S = 60.0
CAIDA = frozenset({502, 503, 504})
TIMEOUTS_SEGUIDOS = 3

_estado = {"hasta": 0.0, "timeouts": 0, "enrutadas": 0, "regresos": 0}
_candado = threading.Lock()


def _config():
    url = (os.environ.get("PASARELA_IA_URL") or "").strip().rstrip("/")
    llave = (os.environ.get("PASARELA_IA_LLAVE") or "").strip()
    return (url, llave) if url and llave else None


def _aviso(msg):
    # logging puede no estar configurado todavía; stderr siempre llega a docker logs.
    try:
        print(f"pasarela_ia: {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _regresar(motivo, estado_http=None):
    with _candado:
        _estado["regresos"] += 1
        _estado["hasta"] = time.monotonic() + ENFRIAMIENTO_S
    _aviso(f"regreso a conexion directa (motivo={motivo} estado={estado_http or '-'}), directo {int(ENFRIAMIENTO_S)}s")


def _aplica(request):
    return (request.method == "POST" and request.url.host == HOST and request.url.path in RUTAS
            and time.monotonic() >= _estado["hasta"])


def _mensaje(contenido):
    try:
        m = json.loads(contenido)["error"]["message"]
        return m if isinstance(m, str) else ""
    except Exception:
        return ""


def _instalar(httpx):
    """Parcha una librería con la forma de httpx (httpx o httpx2, que usa el SDK de Anthropic 1.x y 2.x)."""
    if getattr(httpx, "_pasarela_ia_instalada", False):
        return
    errores_conexion = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                        httpx.WriteError, httpx.WriteTimeout, httpx.RemoteProtocolError)

    def hacia_gestor(request, url, llave):
        headers = [(k, v) for k, v in request.headers.raw
                   if k.lower() not in (b"x-api-key", b"authorization", b"host")]
        bearer = "authorization" in request.headers
        if bearer:
            headers.append((b"authorization", f"Bearer {llave}".encode()))
        if "x-api-key" in request.headers or not bearer:
            headers.append((b"x-api-key", llave.encode()))
        ext = dict(request.extensions)
        t = dict(ext.get("timeout") or {})
        previo = t.get("connect")
        t["connect"] = CONEXION_S if previo is None else min(previo, CONEXION_S)
        ext["timeout"] = t
        return httpx.Request(request.method, url + request.url.raw_path.decode("ascii"),
                             headers=headers, content=request.content, extensions=ext)

    def copia_plana(resp):
        headers = [(k, v) for k, v in resp.headers.raw
                   if k.lower() not in (b"content-encoding", b"content-length", b"transfer-encoding")]
        return httpx.Response(resp.status_code, headers=headers, content=resp.content,
                              extensions=resp.extensions)

    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request

    def handle_request(self, request):
        cfg = _config()
        if cfg is None or not _aplica(request):
            return original_sync(self, request)
        request.read()
        try:
            resp = original_sync(self, hacia_gestor(request, *cfg))
        except errores_conexion as e:
            _regresar(type(e).__name__)
            return original_sync(self, request)
        except (httpx.ReadTimeout, httpx.PoolTimeout):
            with _candado:
                _estado["timeouts"] += 1
                abrir = _estado["timeouts"] >= TIMEOUTS_SEGUIDOS
                if abrir:
                    _estado["timeouts"] = 0
            if abrir:
                _regresar("timeouts_seguidos")
            raise
        _estado["timeouts"] = 0
        if resp.status_code in CAIDA:
            resp.close()
            _regresar("gestor_caido", resp.status_code)
            return original_sync(self, request)
        if resp.status_code < 400:
            _estado["enrutadas"] += 1
            return resp
        try:
            resp.read()
        finally:
            resp.close()
        if _mensaje(resp.content).startswith(PREFIJO):
            _regresar("error_propio_gestor", resp.status_code)
            return original_sync(self, request)
        _estado["enrutadas"] += 1
        return copia_plana(resp)

    async def handle_async_request(self, request):
        cfg = _config()
        if cfg is None or not _aplica(request):
            return await original_async(self, request)
        await request.aread()
        try:
            resp = await original_async(self, hacia_gestor(request, *cfg))
        except errores_conexion as e:
            _regresar(type(e).__name__)
            return await original_async(self, request)
        except (httpx.ReadTimeout, httpx.PoolTimeout):
            with _candado:
                _estado["timeouts"] += 1
                abrir = _estado["timeouts"] >= TIMEOUTS_SEGUIDOS
                if abrir:
                    _estado["timeouts"] = 0
            if abrir:
                _regresar("timeouts_seguidos")
            raise
        _estado["timeouts"] = 0
        if resp.status_code in CAIDA:
            await resp.aclose()
            _regresar("gestor_caido", resp.status_code)
            return await original_async(self, request)
        if resp.status_code < 400:
            _estado["enrutadas"] += 1
            return resp
        try:
            await resp.aread()
        finally:
            await resp.aclose()
        if _mensaje(resp.content).startswith(PREFIJO):
            _regresar("error_propio_gestor", resp.status_code)
            return await original_async(self, request)
        _estado["enrutadas"] += 1
        return copia_plana(resp)

    httpx.HTTPTransport.handle_request = handle_request
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request
    httpx._pasarela_ia_instalada = True


LIBRERIAS = ("httpx", "httpx2")
_instaladas = []


def estado():
    return dict(_estado, activa=_config() is not None, librerias=list(_instaladas))


def _cliente_de_anthropic():
    """Qué librería HTTP importa el SDK de Anthropic instalado, sin importarlo (no arrancar nada pesado)."""
    import importlib.util
    spec = importlib.util.find_spec("anthropic")
    if spec is None or not spec.submodule_search_locations:
        return None
    base = os.path.join(list(spec.submodule_search_locations)[0], "_base_client.py")
    try:
        with open(base, encoding="utf-8") as f:
            fuente = f.read()
    except OSError:
        return "desconocida"
    import re
    nombres = re.findall(r"^\s*(?:import|from)\s+(httpx\w*)\b", fuente, re.M)
    return nombres[0] if nombres else "desconocida"


def _arrancar():
    import importlib
    for nombre in LIBRERIAS:
        try:
            mod = importlib.import_module(nombre)
        except ImportError:
            continue
        try:
            _instalar(mod)
            _instaladas.append(nombre)
        except Exception as e:  # nunca impedir que el programa arranque
            _aviso(f"no se pudo instalar en {nombre} ({type(e).__name__}); sale directo")
    usa = _cliente_de_anthropic()
    if usa is not None and usa not in _instaladas:
        # El SDK sale por una librería que no sabemos parchar: sus llamadas irían directo sin que el gestor las vea.
        _aviso(f"ALERTA el SDK de anthropic usa '{usa}' y no esta cubierta; sus llamadas salen directo sin el gestor")
    elif _instaladas and (os.getpid() == 1 or os.environ.get("PASARELA_IA_AVISO") == "1"):
        _aviso(f"activa hacia {_config()[0]} en {', '.join(_instaladas)} (regreso automatico a directo)")


if _config() is not None:
    try:
        _arrancar()
    except Exception as e:  # nunca impedir que el programa arranque
        _aviso(f"no se pudo instalar ({type(e).__name__}); sale directo")
