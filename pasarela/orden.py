"""Qué cuenta usar. Gratis primero, en el orden del .env. La que cobra, solo para producción."""
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .config import Fuente


@dataclass(frozen=True)
class Candidata:
    fuente: str
    tipo: str                         # gratis | pagado


class SinCredito(Exception):
    pass


def sin_credito(clase: str) -> SinCredito:
    """Único lugar de los mensajes de SinCredito: los usan ordenar y ordenar_por_saldo."""
    return SinCredito("sin crédito gratis disponible" if clase == "extra" else "todas las fuentes están agotadas")


def ordenar(clase: str, fuentes: Sequence[Fuente], agotadas: set[str]) -> list[Candidata]:
    """Una gratis marcada se salta. Una pagada marcada solo se va al final: producción nunca se detiene
    por una marca, en el peor caso hace una llamada que vuelve a fallar y la marca otra vez."""
    gratis = [Candidata(f.nombre, f.tipo) for f in fuentes if f.tipo == "gratis" and f.nombre not in agotadas]
    pagadas = [Candidata(f.nombre, f.tipo) for f in fuentes if f.tipo == "pagado"]
    pagadas = [c for c in pagadas if c.fuente not in agotadas] + [c for c in pagadas if c.fuente in agotadas]
    candidatas = gratis + (pagadas if clase == "produccion" else [])
    if not candidatas:
        raise sin_credito(clase)
    return candidatas


UMBRAL_MICRO = 1_000_000              # US$1: menos que esto no cuenta como saldo gratis


@dataclass(frozen=True)
class CuentaViva:
    """Lo que la bolsa sabe de una cuenta en su última foto (pasarela/bolsa.py)."""
    nombre: str
    tarjeta: bool                     # puede cobrar dinero real
    libre_micro: int                  # saldo gratis estimado de las concesiones vigentes
    vence: date | None                # vencimiento de la concesión vigente con saldo que vence antes
    conciliada_reciente: bool         # informativo (panel): su última conciliación llegó hace menos de 36 h; no decide el orden
    posicion: int                     # lugar en PIA_FUENTES; desempata


def apta(c: CuentaViva, clase: str) -> bool:
    """¿Se puede gastar aquí como crédito gratis? Una cuenta con tarjeta solo cuenta para producción: su llave
    la comparten apps que no pasan por la pasarela y el estimado no ve su gasto, así que extra nunca la usa."""
    return c.libre_micro > UMBRAL_MICRO and (not c.tarjeta or clase == "produccion")


def ordenar_por_saldo(clase: str, vivas: Sequence[CuentaViva], agotadas: set[str]) -> list[Candidata]:
    """1. Con saldo gratis estimado mayor a US$1, de la que vence antes a la que vence después (las dos clases).
    2. Sin tarjeta y sin saldo estimado, mientras Anthropic no la marque: ahí el estimado no puede costar dinero.
    3. Solo producción: las de tarjeta que quedan; las marcadas al final, nunca se quitan."""
    def clave(c: CuentaViva):
        return (c.vence or date.max, c.posicion)

    primero = sorted((c for c in vivas if c.nombre not in agotadas and apta(c, clase)), key=clave)
    usadas = {c.nombre for c in primero}
    sin_tarjeta = sorted((c for c in vivas if not c.tarjeta and c.nombre not in agotadas and c.nombre not in usadas),
                         key=clave)
    con_tarjeta: list[CuentaViva] = []
    if clase == "produccion":
        resto = sorted((c for c in vivas if c.tarjeta and c.nombre not in usadas), key=lambda c: c.posicion)
        con_tarjeta = [c for c in resto if c.nombre not in agotadas] + [c for c in resto if c.nombre in agotadas]
    lista = [Candidata(c.nombre, "pagado" if c.tarjeta else "gratis") for c in primero + sin_tarjeta + con_tarjeta]
    if not lista:
        raise sin_credito(clase)
    return lista


PATRON_SALDO = re.compile(r"credit balance|purchase credits|usage limit|spend limit|billing", re.I)


def es_falta_de_saldo(status: int, cuerpo: bytes) -> bool:
    """Anthropic avisa la falta de saldo antes de empezar a responder, así que se puede reintentar en otra cuenta."""
    if status == 402:
        return True
    if status not in (400, 403, 429):
        return False
    try:
        d = json.loads(cuerpo)
    except ValueError:
        return False
    err = d.get("error") if isinstance(d, dict) else None
    if not isinstance(err, dict):
        return False
    if err.get("type") == "billing_error":
        return True
    return bool(PATRON_SALDO.search(str(err.get("message", ""))))
