"""Configuración desde el entorno. Las llaves reales solo viven en el .env del servidor."""
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping

TIPOS = ("gratis", "pagado")


@dataclass(frozen=True)
class Fuente:
    nombre: str
    tipo: str                         # gratis: la org no tiene tarjeta, nunca cobra · pagado: puede cobrar
    llave: str = field(repr=False)


@dataclass(frozen=True)
class Config:
    db_url: str = field(repr=False)
    fuentes: tuple[Fuente, ...]
    anthropic_url: str = "https://api.anthropic.com"
    refresco_s: float = 30.0
    agotada_s: int = 3600
    tipo_cambio: Decimal = Decimal("20")          # MXN por USD, solo para los avisos de MX$200
    tipo_cambio_supuesto: bool = True             # True: falta PIA_TIPO_CAMBIO y se usa 20
    orden_s: float = 60.0                         # cada cuánto se recalcula la bolsa (0: nunca, para pruebas)
    catalogo_s: float = 21600.0                   # cada cuánto se relee /v1/models (0: nunca, para pruebas)
    panel: bool = False                           # True solo en pasarela-ia-panel: sirve /panel
    salud_api_url: str = "http://pasarela-ia-api:8080/salud"   # el panel pregunta aquí por la salud del gestor
    sacbe: bool = False                           # True en pasarela-ia-sacbe: la puerta pública con Cloudflare Access
    access_aud: str = ""                          # AUD de la app de Access (PIA_ACCESS_AUD)
    access_equipo: str = ""                       # p. ej. tu-equipo.cloudflareaccess.com (PIA_ACCESS_EQUIPO)
    panel_secreto: str = field(default="", repr=False)   # firma las fichas de los formularios (PIA_PANEL_SECRETO)
    sacbe_host: str = ""                          # el único Host al que contesta el panel con login (PIA_SACBE_HOST)


def variable_llave(nombre: str) -> str:
    """Nombre de la variable del .env con la llave de una cuenta. La usan cargar y la bolsa (aviso de llave por vencer)."""
    return f"PIA_LLAVE_{nombre.upper().replace('-', '_')}"


def cargar(env: Mapping[str, str] = os.environ) -> Config:
    db = env.get("PIA_DB_URL")
    if not db:
        raise ValueError("Falta PIA_DB_URL")
    fuentes = []
    for parte in [p.strip() for p in env.get("PIA_FUENTES", "").split(",") if p.strip()]:
        nombre, _, tipo = parte.partition(":")
        if tipo not in TIPOS:
            raise ValueError(f"Fuente {nombre!r}: el tipo {tipo!r} no es gratis ni pagado")
        clave = variable_llave(nombre)
        llave = env.get(clave, "")
        if not llave:
            raise ValueError(f"Falta {clave}")
        fuentes.append(Fuente(nombre, tipo, llave))
    if not fuentes:
        raise ValueError("PIA_FUENTES está vacío")
    tc_texto = env.get("PIA_TIPO_CAMBIO", "").strip()
    tipo_cambio = Decimal("20")
    if tc_texto:
        try:
            tipo_cambio = Decimal(tc_texto)
        except InvalidOperation:
            raise ValueError("PIA_TIPO_CAMBIO no es un número (MXN por USD, por ejemplo 18.45)") from None
        if not tipo_cambio.is_finite() or tipo_cambio <= 0:
            raise ValueError("PIA_TIPO_CAMBIO debe ser un número mayor que cero")
    # Sacbé falla cerrada: PIA_SACBE es "1", "0" o no existe; cualquier otra cosa, incluso
    # vacío, no arranca. Y un contenedor con PIA_ACCESS_AUD nunca arranca como API o panel interno (el .env común no
    # la lleva: va en sacbe.env, solo del servicio pasarela-ia-sacbe).
    texto_sacbe = env.get("PIA_SACBE")
    if texto_sacbe not in (None, "0", "1"):
        raise ValueError(f"PIA_SACBE debe ser 1 o 0, no {texto_sacbe!r}")
    sacbe = texto_sacbe == "1"
    if "PIA_ACCESS_AUD" in env and not sacbe:
        raise ValueError("PIA_ACCESS_AUD está definida pero PIA_SACBE no es 1: va solo en sacbe.env")
    access_aud = env.get("PIA_ACCESS_AUD", "").strip()
    access_equipo = env.get("PIA_ACCESS_EQUIPO", "").strip().lower().removeprefix("https://").strip("/")
    sacbe_host = env.get("PIA_SACBE_HOST", "").strip().lower()
    if sacbe:
        for clave, valor in (("PIA_ACCESS_AUD", access_aud), ("PIA_ACCESS_EQUIPO", access_equipo),
                             ("PIA_SACBE_HOST", sacbe_host)):
            if not valor:
                raise ValueError(f"Con PIA_SACBE=1 falta {clave}")
    return Config(db_url=db, fuentes=tuple(fuentes),
                  anthropic_url=env.get("PIA_ANTHROPIC_URL", "https://api.anthropic.com").rstrip("/"),
                  refresco_s=float(env.get("PIA_REFRESCO_S", "30")),
                  agotada_s=int(env.get("PIA_AGOTADA_S", "3600")),
                  tipo_cambio=tipo_cambio, tipo_cambio_supuesto=not tc_texto,
                  orden_s=float(env.get("PIA_ORDEN_S", "60")),
                  catalogo_s=float(env.get("PIA_CATALOGO_S", "21600")),
                  panel=env.get("PIA_PANEL", "").strip().lower() in ("1", "si", "sí"),
                  salud_api_url=env.get("PIA_PANEL_SALUD_URL", "http://pasarela-ia-api:8080/salud"),
                  sacbe=sacbe, access_aud=access_aud, access_equipo=access_equipo,
                  panel_secreto=env.get("PIA_PANEL_SECRETO", ""),
                  sacbe_host=sacbe_host)
