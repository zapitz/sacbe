"""Copia en memoria de proyectos y fuentes agotadas.
Si la base falla, la pasarela sigue con la última copia: producción no se cae por un error de cuentas."""
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import insert, select, update

from .avisos import Aviso, escribir
from .esquema import fuente_estado, proyecto
from .llaves import hash_llave
from .tiempo import ahora


@dataclass(frozen=True)
class Proyecto:
    nombre: str
    clase: str                        # produccion | extra


class Catalogo:
    def __init__(self, motor, agotada_s: int):
        self.motor = motor
        self.agotada_s = agotada_s
        self._por_hash: dict[str, Proyecto] = {}
        self._agotadas: dict[str, datetime] = {}
        self._candado = threading.Lock()
        self.cargado = False
        self.ultimo_refresco: datetime | None = None

    def refrescar(self) -> bool:
        try:
            with self.motor.connect() as c:
                filas = c.execute(select(proyecto.c.nombre, proyecto.c.clase, proyecto.c.llave_hash)
                                  .where(proyecto.c.activo.is_(True))).all()
                agotadas = c.execute(select(fuente_estado.c.nombre, fuente_estado.c.agotada_hasta)).all()
        except Exception as e:  # noqa: BLE001 (cualquier falla de la base deja la copia anterior)
            print(f"pasarela: no pude refrescar el catálogo ({type(e).__name__})", file=sys.stderr)
            return False
        with self._candado:
            self._por_hash = {h: Proyecto(n, cl) for n, cl, h in filas}
            self._agotadas = {n: hasta for n, hasta in agotadas if hasta}
            self.cargado = True
            self.ultimo_refresco = ahora()
        return True

    def proyecto_por_llave(self, llave: str) -> Proyecto | None:
        return self._por_hash.get(hash_llave(llave))

    def cuantos(self) -> int:
        return len(self._por_hash)

    def agotadas(self) -> set[str]:
        t = ahora()
        return {n for n, hasta in self._agotadas.items() if hasta > t}

    def marcar_agotada(self, fuente: str, motivo: str, tipo: str = "gratis") -> None:
        """Marca en memoria y en fuente_estado, y deja el aviso del día en la misma transacción: un INSERT
        que tolera la clave repetida, sin consultar antes ni abrir otra conexión. `tipo` es el de la candidata
        (pagado = cuenta con tarjeta) y decide qué dice el aviso."""
        t = ahora()
        hasta = t + timedelta(seconds=self.agotada_s)
        sigue = ("Producción la reintenta al final; extra nunca la usa." if tipo == "pagado"
                 else "Se sigue con la siguiente cuenta con saldo.")
        with self._candado:
            self._agotadas = {**self._agotadas, fuente: hasta}
        try:
            with self.motor.begin() as c:
                n = c.execute(update(fuente_estado).where(fuente_estado.c.nombre == fuente)
                              .values(agotada_hasta=hasta, motivo=motivo[:500])).rowcount
                if not n:
                    c.execute(insert(fuente_estado).values(nombre=fuente, agotada_hasta=hasta, motivo=motivo[:500]))
                try:                          # va en SAVEPOINT: si falla, la marca de fuente_estado se guarda igual
                    escribir(c, Aviso(
                        f"agotada:{fuente}:{t:%Y-%m-%d}",
                        f"pasarela-ia: la cuenta {fuente} se quedó sin crédito. {sigue}"))
                except Exception as e:  # noqa: BLE001
                    print(f"pasarela: no pude dejar el aviso de {fuente} agotada ({type(e).__name__})",
                          file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"pasarela: no pude guardar que {fuente} está agotada ({type(e).__name__})", file=sys.stderr)
