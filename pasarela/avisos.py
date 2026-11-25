"""Avisos para las personas. El gestor los escribe en la tabla aviso; ops/vigia.sh los manda y los marca.
La clave única evita mandar dos veces lo mismo, también cuando api y panel recalculan a la vez."""
import json
from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from .esquema import aviso, cambio
from .tiempo import ahora

LARGO_CLAVE = 160


@dataclass(frozen=True)
class Aviso:
    clave: str
    texto: str
    cambio: dict | None = None        # si trae, al escribir el aviso queda también una fila automática en cambio


def _existentes(motor, claves: list[str]) -> set[str]:
    with motor.connect() as c:
        return set(c.execute(select(aviso.c.clave_unica).where(aviso.c.clave_unica.in_(claves))).scalars())


def escribir(c, a: Aviso) -> bool:
    """Inserta el aviso (y su cambio, si trae) en la conexión abierta `c`, sin consultar antes. Si la clave ya está,
    no escribe nada y regresa False. Va en un SAVEPOINT: el choque no aborta la transacción de quien llama (en
    Postgres un error la dejaría inservible) ni le quita lo que ya hizo en ella. No hace commit."""
    t = ahora()
    try:
        with c.begin_nested():
            c.execute(insert(aviso).values(ts=t, clave_unica=a.clave[:LARGO_CLAVE], texto=a.texto, enviado=None))
            if a.cambio is not None:
                c.execute(insert(cambio).values(ts=t, ordenado_por="pasarela (automático)", cita=a.texto,
                                                accion=a.cambio["accion"],
                                                detalle=json.dumps(a.cambio, ensure_ascii=False)))
    except IntegrityError:
        return False                  # otro la escribió primero; su cambio es el que queda
    return True


def avisar(motor, lista: list[Aviso]) -> set[str]:
    """Escribe los avisos cuya clave no está. Regresa las claves que ya quedaron en la tabla (nuevas o de antes)."""
    if not lista:
        return set()
    claves = [a.clave[:LARGO_CLAVE] for a in lista]
    ya = _existentes(motor, claves)
    for a, clave in zip(lista, claves):
        if clave in ya:
            continue
        with motor.begin() as c:
            escribir(c, a)
        ya.add(clave)
    return ya


def pendientes(motor, maximo: int = 10) -> list[dict]:
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(
            select(aviso.c.id, aviso.c.texto).where(aviso.c.enviado.is_(None)).order_by(aviso.c.id).limit(maximo))]


def marcar(motor, ids: list[int]) -> int:
    with motor.begin() as c:
        return c.execute(update(aviso).where(aviso.c.id.in_(ids), aviso.c.enviado.is_(None))
                         .values(enviado=ahora())).rowcount
