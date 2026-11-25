"""Cliente de Anthropic que pasa por Sacbé y regresa a la conexión directa si el gestor falla.

Usa dos clientes del SDK oficial `anthropic` (0.x, 1.x o 2.x): uno hacia el gestor, con la llave pia_, y otro
directo a api.anthropic.com, con la llave de Anthropic del programa. La regla es la de docs/conectar-programas.md:

  - Pasa a directo si no se pudo conectar con el gestor, si el gestor responde 502, 503 o 504, o si responde un
    error cuyo error.message empieza con «pasarela-ia:». Después del cambio, se queda ENFRIAMIENTO_S en directo.
  - Un timeout de lectura nunca se repite (la petición pudo llegar a Anthropic y cobrarse). Tres seguidos abren
    el enfriamiento.
  - Los errores propios de Anthropic se devuelven tal cual.
  - El cliente del gestor tiene max_retries=0 y 3 s para conectar.

Uso:
    cliente = ClienteConRespaldo.desde_entorno()
    respuesta = cliente.llamar(lambda c: c.messages.create(model="claude-...", max_tokens=512, messages=[...]))
"""
import os
import threading
import time

import anthropic

PREFIJO = "pasarela-ia:"
ENFRIAMIENTO_S = 60.0
TIMEOUTS_SEGUIDOS = 3
CONEXION_S = 3.0
CAIDA = frozenset({502, 503, 504})
# Fallas en las que la petición no llegó al gestor (o llegó rota) y se puede mandar directo sin pagar dos veces.
DE_CONEXION = frozenset({"ConnectError", "ConnectTimeout", "ReadError", "WriteError", "WriteTimeout",
                         "RemoteProtocolError"})
DIRECTO = "https://api.anthropic.com"


def _mensaje(error: anthropic.APIStatusError) -> str:
    cuerpo = error.body
    if isinstance(cuerpo, dict):
        detalle = cuerpo.get("error")
        if isinstance(detalle, dict) and isinstance(detalle.get("message"), str):
            return detalle["message"]
    return ""


class ClienteConRespaldo:
    def __init__(self, url_gestor: str, llave_gestor: str, llave_directa: str, *, reloj=time.monotonic,
                 http_gestor=None, http_directo=None, timeout_s: float = 600.0):
        extra_gestor = {"http_client": http_gestor} if http_gestor is not None else {}
        extra_directo = {"http_client": http_directo} if http_directo is not None else {}
        self.gestor = anthropic.Anthropic(base_url=url_gestor, api_key=llave_gestor, max_retries=0,
                                          timeout=anthropic.Timeout(timeout_s, connect=CONEXION_S), **extra_gestor)
        self.directo = anthropic.Anthropic(base_url=DIRECTO, api_key=llave_directa,
                                           timeout=anthropic.Timeout(timeout_s), **extra_directo)
        self._reloj = reloj
        self._candado = threading.Lock()
        self._hasta = 0.0
        self._timeouts = 0

    @classmethod
    def desde_entorno(cls, **opciones) -> "ClienteConRespaldo":
        """PASARELA_IA_URL (por ejemplo https://ia.example.com/anthropic), PASARELA_IA_LLAVE y ANTHROPIC_API_KEY."""
        return cls(os.environ["PASARELA_IA_URL"], os.environ["PASARELA_IA_LLAVE"], os.environ["ANTHROPIC_API_KEY"],
                   **opciones)

    def en_directo(self) -> bool:
        return self._reloj() < self._hasta

    def _regresar(self) -> None:
        with self._candado:
            self._hasta = self._reloj() + ENFRIAMIENTO_S

    def llamar(self, fn):
        """`fn` recibe un cliente de Anthropic y hace la llamada, por ejemplo lambda c: c.messages.create(...)."""
        if self.en_directo():
            return fn(self.directo)
        try:
            respuesta = fn(self.gestor)
        except anthropic.APIStatusError as e:
            with self._candado:
                self._timeouts = 0
            if e.status_code in CAIDA or _mensaje(e).startswith(PREFIJO):
                self._regresar()
                return fn(self.directo)
            raise                                 # error de Anthropic: tal cual
        except anthropic.APIConnectionError as e:  # APITimeoutError es una subclase: se distingue por la causa
            if type(e.__cause__).__name__ in DE_CONEXION:
                with self._candado:
                    self._timeouts = 0
                self._regresar()
                return fn(self.directo)
            if isinstance(e, anthropic.APITimeoutError):
                with self._candado:
                    self._timeouts += 1
                    abrir = self._timeouts >= TIMEOUTS_SEGUIDOS
                    if abrir:
                        self._timeouts = 0
                if abrir:
                    self._regresar()
            raise                                 # timeout de lectura: no se repite
        with self._candado:
            self._timeouts = 0
        return respuesta
