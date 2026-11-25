"""Cliente de Anthropic que pasa por Sacbé y regresa solo a la conexión directa si el gestor falla.

Cambia una línea:

    from anthropic import Anthropic      ->   from sacbe import Anthropic
    from anthropic import AsyncAnthropic ->   from sacbe import AsyncAnthropic

y define en el entorno SACBE_URL (por ejemplo https://ia.example.com/anthropic) y SACBE_LLAVE (la llave pia_ del
programa), además de ANTHROPIC_API_KEY, que queda para el respaldo directo. Todo lo demás del SDK oficial sigue
igual: messages, streaming, count_tokens, batches, archivos. Sin SACBE_URL o SACBE_LLAVE, es el cliente normal.

También se aceptan PASARELA_IA_URL y PASARELA_IA_LLAVE, los nombres de la primera versión.
"""
from __future__ import annotations

import os
import sys

import anthropic as _anthropic

from ._transporte import CONEXION_S, ENFRIAMIENTO_S, PREFIJO, Estado, _clases, clases, estado_de

__version__ = "0.2.0"
__all__ = ["Anthropic", "AsyncAnthropic", "configuracion", "estado", "__version__"]


def configuracion(url: str | None = None, llave: str | None = None):
    """(url, llave) del gestor, de los argumentos o del entorno; None si falta alguna."""
    url = (url or os.environ.get("SACBE_URL") or os.environ.get("PASARELA_IA_URL") or "").strip().rstrip("/")
    llave = (llave or os.environ.get("SACBE_LLAVE") or os.environ.get("PASARELA_IA_LLAVE") or "").strip()
    return (url, llave) if url and llave else None


def estado(url: str | None = None) -> dict:
    """Cuántas peticiones fueron por el gestor, cuántas veces regresó a directo y si está en directo ahora."""
    cfg = configuracion(url, "x")
    return estado_de(cfg[0]).resumen() if cfg else {}


def _libreria(cliente_por_defecto):
    """httpx o httpx2: la librería de la que hereda el cliente HTTP que trae el SDK instalado."""
    for c in cliente_por_defecto.__mro__[1:]:
        raiz = c.__module__.split(".")[0]
        if raiz.startswith("httpx"):
            return sys.modules[raiz]
    raise RuntimeError("sacbe: no se reconoce la librería HTTP del SDK de anthropic")


def _es_envoltura(t) -> bool:
    return any(isinstance(t, par) for par in _clases.values())


def _preparar(kwargs, asincrono: bool):
    url = kwargs.pop("sacbe_url", None)
    llave = kwargs.pop("sacbe_llave", None)
    interno = kwargs.pop("transporte", None)   # para pruebas o un proxy propio; por defecto el de la librería
    cfg = configuracion(url, llave)
    if cfg is None:
        return kwargs
    propio = kwargs.get("http_client")
    if propio is not None:
        if _es_envoltura(getattr(propio, "_transport", None)):
            return kwargs   # with_options() y copy() del SDK reusan el cliente que ya armamos
        raise TypeError("sacbe: no pases http_client; sacbe arma el suyo. Usa transporte= si necesitas otro.")
    base = kwargs.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL")
    if base and "api.anthropic.com" not in base:
        return kwargs   # va a otro lado a propósito: no se toca
    directa = kwargs.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not directa:
        # Sin llave de Anthropic no hay a dónde regresar: todo va al gestor y sus errores suben tal cual.
        kwargs["base_url"], kwargs["api_key"] = cfg
        return kwargs
    por_defecto = _anthropic.DefaultAsyncHttpxClient if asincrono else _anthropic.DefaultHttpxClient
    lib = _libreria(por_defecto)
    Transporte, TransporteAsincrono = clases(lib)
    if interno is None:
        interno = lib.AsyncHTTPTransport() if asincrono else lib.HTTPTransport()
    envoltura = (TransporteAsincrono if asincrono else Transporte)(interno, *cfg)
    kwargs["http_client"] = por_defecto(transport=envoltura)
    return kwargs


class Anthropic(_anthropic.Anthropic):
    """El cliente del SDK oficial, con la regla de respaldo de Sacbé. Acepta sacbe_url= y sacbe_llave=."""

    def __init__(self, **kwargs):
        super().__init__(**_preparar(kwargs, asincrono=False))


class AsyncAnthropic(_anthropic.AsyncAnthropic):
    """La versión asíncrona. Acepta sacbe_url= y sacbe_llave=."""

    def __init__(self, **kwargs):
        super().__init__(**_preparar(kwargs, asincrono=True))
