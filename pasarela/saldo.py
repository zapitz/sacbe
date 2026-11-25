"""Reparto del gasto entre las concesiones de una cuenta. Puro: sin base ni reloj, días en UTC.
Se supone que Anthropic consume primero la concesión vigente que vence antes. La conciliación corrige el total
por cuenta; el error queda solo en cómo se reparte el saldo entre concesiones de la misma cuenta."""
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Mapping, Sequence

UN_DIA = timedelta(days=1)
DURACION_ESTIMADA = timedelta(days=33)   # vigencia supuesta de una concesión estimada que se da de alta sin --vence


@dataclass(frozen=True)
class Concesion:
    id: int
    monto_micro: int
    otorgada: date                    # primer día con crédito
    vence: date                       # primer día sin crédito
    confirmada: bool


@dataclass(frozen=True)
class Reparto:
    restante: dict[int, int]          # lo que le queda a cada concesión que no ha vencido al cierre de `hasta`
    perdido: dict[int, int]           # lo que le quedaba a cada concesión el día que venció
    pagado_por_dia: dict[date, int]   # gasto que ninguna concesión cubrió


def repartir(concesiones: Sequence[Concesion], gasto_por_dia: Mapping[date, int], hasta: date) -> Reparto:
    """Recorre día por día hasta `hasta` inclusive. El gasto de cada día va a la concesión vigente que vence antes;
    si se acaba, a la siguiente; lo que sobra es pagado. El día de su vencimiento, lo que le quede se pierde."""
    orden = sorted(concesiones, key=lambda c: (c.vence, c.id))
    restante = {c.id: c.monto_micro for c in orden}
    perdido: dict[int, int] = {}
    pagado: dict[date, int] = {}
    fechas = [c.otorgada for c in orden] + [d for d, g in gasto_por_dia.items() if g > 0]
    d = min(fechas, default=hasta + UN_DIA)
    while d <= hasta:
        for c in orden:
            if c.vence <= d and c.id not in perdido:
                perdido[c.id] = restante[c.id]
        g = gasto_por_dia.get(d, 0)
        for c in orden:
            if g <= 0:
                break
            if c.otorgada <= d < c.vence:
                toma = min(g, restante[c.id])
                restante[c.id] -= toma
                g -= toma
        if g > 0:
            pagado[d] = g
        d += UN_DIA
    return Reparto({i: r for i, r in restante.items() if i not in perdido}, perdido, pagado)


def proyectar(concesiones: Sequence[Concesion], restante: Mapping[int, int], ritmo_micro: int,
              desde: date) -> dict[int, int]:
    """Lo que se perdería de cada concesión si desde `desde` se gasta `ritmo_micro` cada día."""
    vivas = [replace(c, monto_micro=restante[c.id], otorgada=max(c.otorgada, desde))
             for c in concesiones if c.id in restante and c.vence > desde]
    if not vivas:
        return {}
    fin = max(c.vence for c in vivas)
    gasto = {}
    d = desde
    while d < fin:
        gasto[d] = ritmo_micro
        d += UN_DIA
    return repartir(vivas, gasto, fin).perdido


def vence_estimado(otorgada: date) -> date:
    """Una concesión estimada vence DURACION_ESTIMADA después de otorgarse. No se copia lo que duró la concesión
    anterior: una primera concesión puede cubrir un ciclo parcial y daría una vigencia demasiado corta."""
    return otorgada + DURACION_ESTIMADA
