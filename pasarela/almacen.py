"""Escrituras de la pasarela en la base. Si fallan, se anota en stderr y la petición sigue."""
import json
import sys

from datetime import datetime, timedelta

from sqlalchemy import and_, func, insert, or_, select, text, update

from .esquema import lectura, objeto, proyecto, registro
from .tiempo import ahora


def guardar_registro(motor, fila: dict) -> bool:
    try:
        with motor.begin() as c:
            c.execute(insert(registro).values(ts=ahora(), **fila))
        return True
    except Exception as e:  # noqa: BLE001
        resumen = {k: fila.get(k) for k in ("proyecto", "fuente", "ruta", "modelo", "status", "costo_micro")}
        print(f"pasarela: registro perdido ({type(e).__name__}) {json.dumps(resumen)}", file=sys.stderr)
        return False


def probar_base(motor) -> None:
    with motor.connect() as c:
        c.execute(text("SELECT 1"))


def guardar_objeto(motor, id_: str, tipo: str, fuente: str, proyecto: str) -> None:
    with motor.begin() as c:
        c.execute(insert(objeto).values(id=id_, tipo=tipo, fuente=fuente, proyecto=proyecto,
                                        creado=ahora(), costo_registrado=False))


def insertar_si_no_esta(dialecto: str):
    """INSERT en objeto que no falla si el id ya está (ON CONFLICT DO NOTHING), con la variante del dialecto."""
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as insertar
    else:
        from sqlalchemy.dialects.sqlite import insert as insertar
    return insertar(objeto).on_conflict_do_nothing(index_elements=["id"])


def guardar_objetos(motor, ids, tipo: str, fuente: str, proyecto: str) -> int:
    """Registra objetos que nacieron dentro de una respuesta (archivos de herramientas de servidor). El primero que
    registra un id se lo queda: si ya está, de este proyecto o de otro, no se toca. Regresa cuántos
    entraron. Un solo INSERT ... ON CONFLICT DO NOTHING con todos los ids: sin SAVEPOINT ni errores de llave que
    atrapar. Cuenta con RETURNING, porque en Postgres el rowcount de este INSERT llega en -1."""
    ids = sorted(set(ids))
    if not ids:
        return 0
    t = ahora()
    filas = [dict(id=i, tipo=tipo, fuente=fuente, proyecto=proyecto, creado=t, costo_registrado=False) for i in ids]
    sentencia = insertar_si_no_esta(motor.dialect.name).values(filas).returning(objeto.c.id)
    with motor.begin() as c:
        return len(c.execute(sentencia).all())


def marcar_borrado(motor, id_: str, proyecto: str) -> bool:
    """Un archivo que el proyecto borró por la pasarela deja de salir en su lista. La fila se queda con tipo
    «borrado»: sigue diciendo de qué cuenta y de qué proyecto era y sigue sirviendo de cursor."""
    with motor.begin() as c:
        return c.execute(update(objeto).where(objeto.c.id == id_, objeto.c.proyecto == proyecto,
                                              objeto.c.tipo == "archivo").values(tipo="borrado")).rowcount == 1


CADUCO_TRAS = timedelta(hours=1)      # un objeto que Anthropic no tiene y lleva registrado más de esto se marca «caduco»


def marcar_caducos(motor, proyecto: str, tipo: str, ids: list[str]) -> int:
    """Lo que Anthropic ya no tiene (archivo omitido en una respuesta 200, lote con 404 not_found_error) y lleva más de
    CADUCO_TRAS registrado pasa a tipo «caduco»: deja de salir en la lista y de preguntarse, y se porta como un
    «borrado» (sigue fijando cuenta y proyecto y sirviendo de cursor). Solo toca filas de ese proyecto y de ese tipo.
    Regresa cuántas cambió."""
    if not ids:
        return 0
    limite = ahora() - CADUCO_TRAS
    with motor.begin() as c:
        return c.execute(update(objeto).where(objeto.c.id.in_(ids), objeto.c.proyecto == proyecto,
                                              objeto.c.tipo == tipo, objeto.c.creado < limite)
                         .values(tipo="caduco")).rowcount


def pagina_de_objetos(motor, proyecto: str, tipo: str, n: int, despues: str | None = None,
                      antes: str | None = None) -> tuple[list[tuple[str, str]], bool] | None:
    """Hasta n objetos (id, cuenta) del proyecto, del más nuevo al más viejo (por creado y luego por id), y si quedan
    más en esa dirección. `despues`: los que siguen a ese id (más viejos). `antes`: los n inmediatamente anteriores
    a ese id (más nuevos), también del más nuevo al más viejo. El cursor es un id del mismo proyecto, de cualquier
    tipo (un archivo borrado sigue sirviendo); si no existe, regresa None."""
    orden = (objeto.c.creado.desc(), objeto.c.id.desc())
    q = select(objeto.c.id, objeto.c.fuente).where(objeto.c.proyecto == proyecto, objeto.c.tipo == tipo)
    with motor.connect() as c:
        cursor = despues if despues is not None else antes
        if cursor is not None:
            f = c.execute(select(objeto.c.creado, objeto.c.id)
                          .where(objeto.c.id == cursor, objeto.c.proyecto == proyecto)).first()
            if f is None:
                return None
            if despues is not None:
                q = q.where(or_(objeto.c.creado < f.creado, and_(objeto.c.creado == f.creado, objeto.c.id < f.id)))
            else:
                q = q.where(or_(objeto.c.creado > f.creado, and_(objeto.c.creado == f.creado, objeto.c.id > f.id)))
                orden = (objeto.c.creado.asc(), objeto.c.id.asc())
        filas = [(i, fuente) for i, fuente in c.execute(q.order_by(*orden).limit(n + 1))]
    hay_mas = len(filas) > n
    filas = filas[:n]
    if despues is None and antes is not None:
        filas.reverse()
    return filas, hay_mas


def objetos_entre(motor, proyecto: str, tipo: str, ids: list[str]) -> dict[str, str]:
    """De una lista de ids, los que son de ese proyecto y de ese tipo: id → cuenta."""
    with motor.connect() as c:
        filas = c.execute(select(objeto.c.id, objeto.c.fuente)
                          .where(objeto.c.proyecto == proyecto, objeto.c.tipo == tipo, objeto.c.id.in_(ids))).all()
    return {i: f for i, f in filas}


def objeto_de(motor, id_: str) -> dict | None:
    with motor.connect() as c:
        f = c.execute(select(objeto).where(objeto.c.id == id_)).first()
    return dict(f._mapping) if f else None


def uso_de(motor, proyecto: str, desde: datetime) -> tuple[int, int, datetime | None]:
    """(peticiones y micro-USD desde `desde`, última petición de cualquier mes) de un proyecto, para GET /v1/uso."""
    with motor.connect() as c:
        n, micro = c.execute(select(func.count(), func.coalesce(func.sum(registro.c.costo_micro), 0))
                             .where(registro.c.proyecto == proyecto, registro.c.ts >= desde)).one()
        ultima = c.execute(select(func.max(registro.c.ts)).where(registro.c.proyecto == proyecto)).scalar()
    return int(n), int(micro), ultima


def usos_de(motor, lector: str, desde: datetime) -> list[tuple[str, str, int, int, datetime | None]]:
    """(nombre, clase, peticiones y micro-USD desde `desde`, última petición) del lector y de cada proyecto activo
    que puede ver según la tabla lectura, para GET /v1/uso?proyectos=todos. Una sola consulta; el lector primero
    y los demás por nombre."""
    propio = proyecto.c.nombre == lector
    del_mes = (registro.c.proyecto == proyecto.c.nombre) & (registro.c.ts >= desde)
    n = select(func.count()).where(del_mes).scalar_subquery()
    micro = select(func.coalesce(func.sum(registro.c.costo_micro), 0)).where(del_mes).scalar_subquery()
    ultima = select(func.max(registro.c.ts)).where(registro.c.proyecto == proyecto.c.nombre).scalar_subquery()
    vistos = select(lectura.c.visto).where(lectura.c.lector == lector)
    with motor.connect() as c:
        filas = c.execute(select(proyecto.c.nombre, proyecto.c.clase, n, micro, ultima)
                          .where(propio | (proyecto.c.activo.is_(True) & proyecto.c.nombre.in_(vistos)))
                          .order_by(propio.desc(), proyecto.c.nombre)).all()
    return [(f[0], f[1], int(f[2]), int(f[3]), f[4]) for f in filas]


def registrar_costo_de_lote(motor, id_: str, fila: dict) -> bool | None:
    """Marca el costo del lote y escribe su fila en una sola transacción: True para quien lo marca primero,
    False si ya estaba contado (un lote no se cuenta dos veces si bajan sus resultados otra vez), None si la base
    falló. Si la fila no entra, la marca tampoco, y la siguiente descarga lo cuenta."""
    try:
        with motor.begin() as c:
            n = c.execute(update(objeto).where(objeto.c.id == id_, objeto.c.costo_registrado.is_(False))
                          .values(costo_registrado=True)).rowcount
            if n == 1:
                c.execute(insert(registro).values(ts=ahora(), **fila))
        return n == 1
    except Exception as e:  # noqa: BLE001
        print(f"pasarela: no pude registrar el costo del lote {id_} ({type(e).__name__})", file=sys.stderr)
        return None
