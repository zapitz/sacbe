"""La regla de respaldo directo de Sacbé, como transporte de httpx o httpx2.

Envuelve el transporte normal del cliente. Solo actúa sobre POST /v1/messages y /v1/messages/count_tokens hacia
api.anthropic.com; todo lo demás pasa sin tocar. La regla es la de docs/conectar-programas.md:

  1. Pasa a directo (la petición ORIGINAL, con la llave de Anthropic del programa) si no se pudo conectar con el
     gestor, si responde 502, 503 o 504, o si responde un error cuyo error.message empieza con «pasarela-ia:».
  2. Después del cambio se queda ENFRIAMIENTO_S en directo.
  3. Un timeout de lectura no se repite en directo (la petición pudo cobrarse). Tres seguidos abren el enfriamiento.
  4. Los errores propios de Anthropic se devuelven tal cual.
  5. La conexión al gestor tiene CONEXION_S para conectar.
"""
from __future__ import annotations

import json
import sys
import threading
import time

HOST = "api.anthropic.com"
RUTAS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
PREFIJO = "pasarela-ia:"
CONEXION_S = 3.0
ENFRIAMIENTO_S = 60.0
CAIDA = frozenset({502, 503, 504})
TIMEOUTS_SEGUIDOS = 3
QUITAR_RESPUESTA = (b"content-encoding", b"content-length", b"transfer-encoding")


class Estado:
    """Cuándo vuelve a probarse el gestor y cuántos timeouts van seguidos. Uno por URL de gestor y proceso."""

    def __init__(self, reloj=None):
        self.reloj = reloj or (lambda: time.monotonic())
        self.candado = threading.Lock()
        self.hasta = 0.0
        self.timeouts = 0
        self.enrutadas = 0
        self.regresos = 0

    def en_directo(self) -> bool:
        return self.reloj() < self.hasta

    def regresar(self, motivo: str, estado_http=None) -> None:
        with self.candado:
            self.regresos += 1
            self.timeouts = 0
            self.hasta = self.reloj() + ENFRIAMIENTO_S
        try:
            print(f"sacbe: regreso a conexion directa (motivo={motivo} estado={estado_http or '-'}), "
                  f"directo {int(ENFRIAMIENTO_S)}s", file=sys.stderr, flush=True)
        except Exception:
            pass

    def timeout(self) -> None:
        with self.candado:
            self.timeouts += 1
            abrir = self.timeouts >= TIMEOUTS_SEGUIDOS
        if abrir:
            self.regresar("timeouts_seguidos")

    def bien(self) -> None:
        with self.candado:
            self.timeouts = 0
            self.enrutadas += 1

    def resumen(self) -> dict:
        return {"en_directo": self.en_directo(), "enrutadas": self.enrutadas, "regresos": self.regresos,
                "timeouts_seguidos": self.timeouts}


_estados: dict[str, Estado] = {}
_candado_estados = threading.Lock()


def estado_de(url: str) -> Estado:
    with _candado_estados:
        if url not in _estados:
            _estados[url] = Estado()
        return _estados[url]


def _mensaje(contenido: bytes) -> str:
    try:
        m = json.loads(contenido)["error"]["message"]
        return m if isinstance(m, str) else ""
    except Exception:
        return ""


_clases: dict[str, tuple] = {}


def clases(lib):
    """(Transporte, TransporteAsincrono) para una librería con la forma de httpx (httpx o httpx2)."""
    if lib.__name__ in _clases:
        return _clases[lib.__name__]
    de_conexion = (lib.ConnectError, lib.ConnectTimeout, lib.ReadError, lib.WriteError, lib.WriteTimeout,
                   lib.RemoteProtocolError)
    de_lectura = (lib.ReadTimeout, lib.PoolTimeout)

    class _Base:
        def __init__(self, interno, url: str, llave: str, estado: Estado | None = None):
            self.interno = interno
            self.url = url.strip().rstrip("/")
            self.llave = llave.strip()
            self.estado = estado or estado_de(self.url)

        def _aplica(self, request) -> bool:
            return (request.method == "POST" and request.url.host == HOST and request.url.path in RUTAS
                    and not self.estado.en_directo())

        def _hacia_gestor(self, request):
            headers = [(k, v) for k, v in request.headers.raw
                       if k.lower() not in (b"x-api-key", b"authorization", b"host")]
            bearer = "authorization" in request.headers
            if bearer:
                headers.append((b"authorization", f"Bearer {self.llave}".encode()))
            if "x-api-key" in request.headers or not bearer:
                headers.append((b"x-api-key", self.llave.encode()))
            ext = dict(request.extensions)
            t = dict(ext.get("timeout") or {})
            previo = t.get("connect")
            t["connect"] = CONEXION_S if previo is None else min(previo, CONEXION_S)
            ext["timeout"] = t
            return lib.Request(request.method, self.url + request.url.raw_path.decode("ascii"),
                               headers=headers, content=request.content, extensions=ext)

        @staticmethod
        def _plana(resp):
            headers = [(k, v) for k, v in resp.headers.raw if k.lower() not in QUITAR_RESPUESTA]
            return lib.Response(resp.status_code, headers=headers, content=resp.content, extensions=resp.extensions)

    class Transporte(_Base, lib.BaseTransport):
        def handle_request(self, request):
            if not self._aplica(request):
                return self.interno.handle_request(request)
            request.read()
            try:
                resp = self.interno.handle_request(self._hacia_gestor(request))
            except de_conexion as e:
                self.estado.regresar(type(e).__name__)
                return self.interno.handle_request(request)
            except de_lectura:
                self.estado.timeout()
                raise
            if resp.status_code in CAIDA:
                resp.close()
                self.estado.regresar("gestor_caido", resp.status_code)
                return self.interno.handle_request(request)
            if resp.status_code < 400:
                self.estado.bien()
                return resp
            try:
                resp.read()
            finally:
                resp.close()
            if _mensaje(resp.content).startswith(PREFIJO):
                self.estado.regresar("error_propio_gestor", resp.status_code)
                return self.interno.handle_request(request)
            self.estado.bien()
            return self._plana(resp)

        def close(self):
            self.interno.close()

    class TransporteAsincrono(_Base, lib.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if not self._aplica(request):
                return await self.interno.handle_async_request(request)
            await request.aread()
            try:
                resp = await self.interno.handle_async_request(self._hacia_gestor(request))
            except de_conexion as e:
                self.estado.regresar(type(e).__name__)
                return await self.interno.handle_async_request(request)
            except de_lectura:
                self.estado.timeout()
                raise
            if resp.status_code in CAIDA:
                await resp.aclose()
                self.estado.regresar("gestor_caido", resp.status_code)
                return await self.interno.handle_async_request(request)
            if resp.status_code < 400:
                self.estado.bien()
                return resp
            try:
                await resp.aread()
            finally:
                await resp.aclose()
            if _mensaje(resp.content).startswith(PREFIJO):
                self.estado.regresar("error_propio_gestor", resp.status_code)
                return await self.interno.handle_async_request(request)
            self.estado.bien()
            return self._plana(resp)

        async def aclose(self):
            await self.interno.aclose()

    _clases[lib.__name__] = (Transporte, TransporteAsincrono)
    return _clases[lib.__name__]
