"""Administración de la pasarela. Corre dentro del contenedor:
    docker exec -i pasarela-ia-api python -m pasarela.admin <orden> ...
Desde tu máquina: ops/pia <orden> ...  Cada cambio queda en la tabla cambio con quién lo ordenó, la cita y la hora.
Órdenes: proyectos y llaves; cuentas, créditos, saldos, topes, presupuesto, conciliación y avisos; lecturas (quién ve
el uso de quién en GET /v1/uso?proyectos=todos); objeto registrar y objeto ver (archivos y lotes que nacieron fuera
de la pasarela); usuarios con papel y proyectos asignados (usuario alta|baja|papel|asignar|quitar|lista)."""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import avisos as modulo_avisos
from . import webhooks as modulo_webhooks
from .bolsa import CLAVE_PRESUPUESTO, MICRO, calcular, cuadre, fecha, leer, pagado_visible, usd
from .config import cargar
from .esquema import (ajuste, cambio, concesion, conciliacion, crear_motor, cuenta, fuente_estado, lectura,
                      objeto, proyecto, registro, tope, usuario, usuario_proyecto)
from .llaves import hash_llave, nueva_llave
from .saldo import vence_estimado
from .tiempo import ahora

NOMBRE = re.compile(r"[a-z][a-z0-9-]{1,30}")         # con fullmatch: `$` aceptaba un salto de línea al final
CLASES = ("produccion", "extra")
MODOS = ("aviso", "corte")
SI_NO = ("si", "sí", "no")
TIPOS_OBJETO = ("archivo", "lote")
PAPELES = ("dueno", "finanzas", "responsable", "lector")        # ASCII en la base
CORREO = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")          # algo@algo.algo; con fullmatch por el salto de línea
ID_OBJETO = re.compile(r"[A-Za-z0-9_-]{1,120}")      # se usa con fullmatch: `$` aceptaba un salto de línea al final


POR_MAX = 80                                          # cambio.ordenado_por y usuario.por son String(80)


def _sin_control(texto: str, campo: str) -> str:
    """Nada de caracteres de control (NUL, saltos de línea, tabuladores, escapes) hacia la base: el NUL tumba a
    psycopg y lo demás confunde al leerlo. Regresa el texto tal cual."""
    if any(unicodedata.category(ch) == "Cc" for ch in texto or ""):
        raise SystemExit(f"{campo} trae caracteres de control (saltos de línea, tabuladores u otros invisibles); "
                         f"escríbelo en una sola línea.")
    return texto


def _validar(por: str, cita: str) -> None:
    if not (por or "").strip():
        raise SystemExit("Falta --por con quién lo ordenó.")
    if not (cita or "").strip():
        raise SystemExit("Falta --cita con las palabras de quien lo ordenó.")
    _sin_control(por, "--por")
    _sin_control(cita, "--cita")
    if len(por.strip()) > POR_MAX:
        raise SystemExit(f"--por es demasiado largo: hasta {POR_MAX} caracteres.")


def _cambio(c, por: str, cita: str, accion: str, detalle: dict) -> None:
    _validar(por, cita)
    c.execute(insert(cambio).values(ts=ahora(), ordenado_por=por, cita=cita.strip(), accion=accion,
                                    detalle=json.dumps(detalle, ensure_ascii=False)))


def alta(motor, nombre: str, clase: str, por: str, cita: str) -> str:
    if not NOMBRE.fullmatch(nombre):
        raise SystemExit("Nombre inválido: minúsculas, números y guiones, de 2 a 31 caracteres.")
    if clase not in CLASES:
        raise SystemExit("La clase es produccion o extra.")
    llave = nueva_llave(nombre)
    try:
        with motor.begin() as c:
            if c.execute(select(proyecto.c.nombre).where(proyecto.c.nombre == nombre)).first() is not None:
                raise SystemExit(f"Ya existe el proyecto {nombre}.")
            _cambio(c, por, cita, "proyecto-alta", {"nombre": nombre, "clase": clase})
            c.execute(insert(proyecto).values(nombre=nombre, clase=clase, llave_hash=hash_llave(llave),
                                              huella=llave[-4:], activo=True, dueno="admin", creado=ahora()))
    except IntegrityError:                    # otra alta igual al mismo tiempo: la consulta de arriba no la vio
        raise SystemExit(f"Ya existe el proyecto {nombre}.") from None
    return llave


def _actualizar(motor, nombre: str, valores: dict, por: str, cita: str, accion: str) -> None:
    with motor.begin() as c:
        _cambio(c, por, cita, accion, {"nombre": nombre, **{k: v for k, v in valores.items() if k != "llave_hash"}})
        if not c.execute(update(proyecto).where(proyecto.c.nombre == nombre).values(**valores)).rowcount:
            raise SystemExit(f"No existe el proyecto {nombre}.")


def rotar(motor, nombre: str, por: str, cita: str) -> str:
    """Rotar no reactiva: un proyecto dado de baja se queda así y no deja bitácora."""
    with motor.connect() as c:
        activo = c.execute(select(proyecto.c.activo).where(proyecto.c.nombre == nombre)).scalar()
    if activo is False:                       # None: no existe, eso lo dice _actualizar
        raise SystemExit(f"El proyecto {nombre} está dado de baja.")
    llave = nueva_llave(nombre)
    _actualizar(motor, nombre, {"llave_hash": hash_llave(llave), "huella": llave[-4:]}, por, cita, "proyecto-rotar")
    return llave


def baja(motor, nombre: str, por: str, cita: str) -> None:
    _actualizar(motor, nombre, {"activo": False}, por, cita, "proyecto-baja")


def clase(motor, nombre: str, nueva: str, por: str, cita: str) -> None:
    if nueva not in CLASES:
        raise SystemExit("La clase es produccion o extra.")
    _actualizar(motor, nombre, {"clase": nueva}, por, cita, "proyecto-clase")


def reactivar(motor, fuente: str, por: str, cita: str) -> None:
    with motor.begin() as c:
        _cambio(c, por, cita, "fuente-reactivar", {"fuente": fuente})
        if not c.execute(update(fuente_estado).where(fuente_estado.c.nombre == fuente)
                         .values(agotada_hasta=None, motivo=None)).rowcount:
            raise SystemExit(f"La fuente {fuente} no está marcada como agotada.")


def uso(motor, desde: datetime) -> list[dict]:
    with motor.connect() as c:
        filas = c.execute(
            select(registro.c.proyecto, registro.c.fuente, func.count().label("peticiones"),
                   func.sum(registro.c.costo_micro).label("micro"), func.sum(registro.c.entrada).label("entrada"),
                   func.sum(registro.c.salida).label("salida"))
            .where(registro.c.ts >= desde).group_by(registro.c.proyecto, registro.c.fuente)
            .order_by(registro.c.proyecto, registro.c.fuente)).all()
    # Postgres devuelve SUM() de BigInteger como Decimal; se convierte para que la división en main funcione.
    return [{**dict(f._mapping), **{k: int(f._mapping[k] or 0) for k in ("peticiones", "micro", "entrada", "salida")}}
            for f in filas]


SILENCIO_H = 48          # horas sin peticiones para avisar
BASE_DIAS = 7            # días de referencia antes del silencio
BASE_MINIMA = 14         # peticiones en esos días para que el programa cuente como «con tráfico real»


def silencios(motor, t: datetime | None = None) -> list[dict]:
    """Programas activos de clase produccion con tráfico real (BASE_MINIMA peticiones en los BASE_DIAS previos) que llevan SILENCIO_H
    horas sin pasar por el gestor. Atrapa cualquier causa: SDK nuevo, .env restaurado, programa caído o
    llamadas que salen directo. Los que nunca tuvieron tráfico no avisan (serían falsas alarmas)."""
    t = t or ahora()
    corte = t - timedelta(hours=SILENCIO_H)
    inicio = corte - timedelta(days=BASE_DIAS)
    with motor.connect() as c:
        activos = set(c.execute(select(proyecto.c.nombre).where(   # los extra corren por ratos: no se vigilan
            proyecto.c.activo.is_(True), proyecto.c.clase == "produccion")).scalars())
        filas = c.execute(
            select(registro.c.proyecto,
                   func.sum(case((registro.c.ts < corte, 1), else_=0)).label("antes"),
                   func.sum(case((registro.c.ts >= corte, 1), else_=0)).label("ahora"),
                   func.max(registro.c.ts).label("ultima"))
            .where(registro.c.ts >= inicio).group_by(registro.c.proyecto)).all()
    return [{"proyecto": f.proyecto, "antes": int(f.antes or 0), "ultima": f.ultima}
            for f in filas if f.proyecto in activos and int(f.antes or 0) >= BASE_MINIMA and int(f.ahora or 0) == 0]


def avisar_silencios(motor, t: datetime | None = None) -> int:
    """Escribe un aviso por programa callado; la clave lleva el día de su última petición, así se manda una vez."""
    lista = [modulo_avisos.Aviso(
        clave=f"silencio:{f['proyecto']}:{f['ultima']:%Y-%m-%d}",
        texto=(f"pasarela-ia: {f['proyecto']} lleva {SILENCIO_H} h sin pasar por el gestor (antes: {f['antes']} "
               f"peticiones en {BASE_DIAS} días; última {f['ultima']:%d-%m %H:%M} UTC). Puede estar saliendo "
               "directo a Anthropic (SDK nuevo, .env restaurado) o estar caído."))
        for f in silencios(motor, t)]
    modulo_avisos.avisar(motor, lista)
    return len(lista)


def cambios(motor, n: int) -> list[dict]:
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(select(cambio).order_by(cambio.c.id.desc()).limit(n))]


def proyectos(motor) -> list[dict]:
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(
            select(proyecto.c.nombre, proyecto.c.clase, proyecto.c.huella, proyecto.c.activo, proyecto.c.creado)
            .order_by(proyecto.c.nombre))]


def _fecha(texto: str | None, campo: str) -> datetime | None:
    if texto is None:
        return None
    try:
        return datetime.strptime(texto, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise SystemExit(f"{campo}: fecha inválida {texto!r}; va como AAAA-MM-DD.") from None


MICRO_MAX = 2**63 - 1                  # BigInteger de la base


def _micro(texto: str, campo: str) -> int:
    try:
        d = Decimal(str(texto))
    except InvalidOperation:
        raise SystemExit(f"{campo}: {texto!r} no es un monto en dólares.") from None
    if not d.is_finite():
        raise SystemExit(f"{campo}: {texto!r} no es un número.")
    if d < 0:
        raise SystemExit(f"{campo}: el monto no puede ser negativo.")
    try:
        micro = int((d * MICRO).to_integral_value(rounding=ROUND_HALF_UP))
    except ArithmeticError:                # 1e9999999 desborda Decimal antes de llegar al tope
        micro = MICRO_MAX + 1
    if micro > MICRO_MAX:
        raise SystemExit(f"{campo}: el monto es demasiado grande.")
    return micro


def _entero_positivo(texto: str) -> int:
    try:
        n = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{texto!r} no es un entero") from None
    if n < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o más")
    return n


def _usd_texto(micro: int) -> str:
    return str(Decimal(micro) / MICRO)


def _dia(t: datetime | None) -> str | None:
    return f"{t:%Y-%m-%d}" if t else None


def cuenta_agregar(motor, nombre: str, correo: str, tarjeta: bool, llave_vence: datetime | None,
                   por: str, cita: str) -> None:
    _validar(por, cita)
    if not NOMBRE.fullmatch(nombre):
        raise SystemExit("Nombre inválido: minúsculas, números y guiones, de 2 a 31 caracteres.")
    _sin_control(correo or "", "--correo-plan")
    if "@" not in (correo or ""):
        raise SystemExit("--correo-plan debe ser el correo con el que entras a esa organización en la consola de Anthropic.")
    with motor.begin() as c:
        if c.execute(select(cuenta.c.nombre).where(cuenta.c.nombre == nombre)).first() is not None:
            raise SystemExit(f"La cuenta {nombre} ya existe; para cambiarla usa: cuenta cambiar {nombre}.")
        _cambio(c, por, cita, "cuenta-alta", {"nombre": nombre, "correo_plan": correo, "tiene_tarjeta": tarjeta,
                                              "llave_vence": _dia(llave_vence)})
        c.execute(insert(cuenta).values(nombre=nombre, correo_plan=correo, tiene_tarjeta=tarjeta, activa=True,
                                        llave_vence=llave_vence, creada=ahora()))


def cuenta_cambiar(motor, nombre: str, por: str, cita: str, tarjeta: bool | None = None,
                   activa: bool | None = None, llave_vence: datetime | None = None) -> None:
    _validar(por, cita)
    valores = {k: v for k, v in {"tiene_tarjeta": tarjeta, "activa": activa, "llave_vence": llave_vence}.items()
               if v is not None}
    if not valores:
        raise SystemExit("Nada que cambiar: usa --tarjeta, --activa o --llave-vence.")
    with motor.begin() as c:
        _cambio(c, por, cita, "cuenta-cambio",
                {"nombre": nombre, **{k: _dia(v) if isinstance(v, datetime) else v for k, v in valores.items()}})
        if not c.execute(update(cuenta).where(cuenta.c.nombre == nombre).values(**valores)).rowcount:
            raise SystemExit(f"No existe la cuenta {nombre}.")


def cuentas(motor) -> list[dict]:
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(select(cuenta).order_by(cuenta.c.nombre))]


def credito_agregar(motor, cuenta_: str, monto_micro: int, otorgada: datetime, vence: datetime | None,
                    estimada: bool, nota: str | None, por: str, cita: str) -> tuple[int, datetime]:
    """Una concesión vista en la consola (Facturación, créditos promocionales) o una estimada por la fecha
    en que suele renovarse. Una estimada sin --vence vence 33 días después de --otorgada."""
    _validar(por, cita)
    if nota:
        _sin_control(nota, "--nota")
    if monto_micro <= 0:
        raise SystemExit("El monto debe ser mayor que cero.")
    with motor.begin() as c:
        if c.execute(select(cuenta.c.nombre).where(cuenta.c.nombre == cuenta_)).first() is None:
            raise SystemExit(f"No existe la cuenta {cuenta_}; primero: cuenta agregar {cuenta_} ...")
        if vence is None:
            if not estimada:
                raise SystemExit("Falta --vence; solo una concesión --estimada puede ir sin él.")
            v = vence_estimado(otorgada.date())
            vence = datetime(v.year, v.month, v.day)
        if vence <= otorgada:
            raise SystemExit("--vence debe ser después de --otorgada.")
        origen = "estimada" if estimada else "consola"
        detalle = {"cuenta": cuenta_, "monto_usd": _usd_texto(monto_micro), "otorgada": _dia(otorgada),
                   "vence": _dia(vence), "origen": origen}
        if nota:
            detalle["nota"] = nota
        _cambio(c, por, cita, "credito-alta", detalle)
        id_ = c.execute(insert(concesion).values(cuenta=cuenta_, monto_micro=monto_micro, otorgada=otorgada,
                                                 vence=vence, origen=origen, confirmada=not estimada,
                                                 creada=ahora())).inserted_primary_key[0]
    return int(id_), vence


def credito_confirmar(motor, id_: int, monto_micro: int, otorgada: datetime, vence: datetime,
                      por: str, cita: str) -> None:
    _validar(por, cita)
    if monto_micro <= 0 or vence <= otorgada:
        raise SystemExit("Monto mayor que cero y --vence después de --otorgada.")
    with motor.begin() as c:
        _cambio(c, por, cita, "credito-confirmar", {"id": id_, "monto_usd": _usd_texto(monto_micro),
                                                    "otorgada": _dia(otorgada), "vence": _dia(vence)})
        if not c.execute(update(concesion).where(concesion.c.id == id_).values(
                monto_micro=monto_micro, otorgada=otorgada, vence=vence, origen="consola",
                confirmada=True)).rowcount:
            raise SystemExit(f"No existe la concesión {id_}.")


def credito_ajustar(motor, id_: int, por: str, cita: str, monto_micro: int | None = None,
                    otorgada: datetime | None = None, vence: datetime | None = None) -> dict:
    """Corrige monto o fechas de una concesión sin tocar si está confirmada ni su origen (para eso está confirmar).
    Revisa vence > otorgada con los valores ya combinados. La bitácora guarda antes y ahora de lo que se pidió."""
    _validar(por, cita)
    pedidos = {k: v for k, v in {"monto_micro": monto_micro, "otorgada": otorgada, "vence": vence}.items()
               if v is not None}
    if not pedidos:
        raise SystemExit("Nada que ajustar: usa --monto, --otorgada o --vence.")
    if monto_micro is not None and monto_micro <= 0:
        raise SystemExit("El monto debe ser mayor que cero.")
    with motor.begin() as c:
        fila = c.execute(select(concesion).where(concesion.c.id == id_)).first()
        if fila is None:
            raise SystemExit(f"No existe la concesión {id_}.")
        actual = dict(fila._mapping)
        nuevo = {**actual, **pedidos}
        if nuevo["vence"] <= nuevo["otorgada"]:
            raise SystemExit("--vence debe quedar después de --otorgada.")

        def texto(k, v):
            return _usd_texto(v) if k == "monto_micro" else _dia(v)

        _cambio(c, por, cita, "credito-ajuste", {"id": id_,
                                                 "antes": {k: texto(k, actual[k]) for k in pedidos},
                                                 "ahora": {k: texto(k, v) for k, v in pedidos.items()}})
        c.execute(update(concesion).where(concesion.c.id == id_).values(**pedidos))
    return nuevo


def tope_poner(motor, nombre: str, limite_micro: int, modo: str | None, por: str, cita: str) -> str:
    _validar(por, cita)
    if modo is not None and modo not in MODOS:
        raise SystemExit("El modo es aviso o corte.")
    if limite_micro <= 0:
        raise SystemExit("El tope debe ser mayor que cero; para quitarlo usa --quitar.")
    with motor.begin() as c:
        clase_ = c.execute(select(proyecto.c.clase).where(proyecto.c.nombre == nombre)).scalar()
        if clase_ is None:
            raise SystemExit(f"No existe el proyecto {nombre}.")
        modo = modo or ("corte" if clase_ == "extra" else "aviso")
        _cambio(c, por, cita, "tope", {"proyecto": nombre, "usd": _usd_texto(limite_micro), "modo": modo})
        c.execute(delete(tope).where(tope.c.proyecto == nombre))
        c.execute(insert(tope).values(proyecto=nombre, tope_mensual_micro=limite_micro, modo=modo,
                                      actualizado=ahora()))
    return modo


def tope_quitar(motor, nombre: str, por: str, cita: str) -> None:
    _validar(por, cita)
    with motor.begin() as c:
        _cambio(c, por, cita, "tope-quitar", {"proyecto": nombre})
        if not c.execute(delete(tope).where(tope.c.proyecto == nombre)).rowcount:
            raise SystemExit(f"El proyecto {nombre} no tiene tope.")


def _existen(c, *nombres: str) -> None:
    for nombre in nombres:
        if c.execute(select(proyecto.c.nombre).where(proyecto.c.nombre == nombre)).scalar() is None:
            raise SystemExit(f"No existe el proyecto {nombre}.")


def lectura_agregar(motor, lector: str, visto: str, por: str, cita: str) -> None:
    _validar(por, cita)
    if lector == visto:
        raise SystemExit(f"{lector} ya ve su propio uso.")
    with motor.begin() as c:
        _existen(c, lector, visto)
        if c.execute(select(lectura.c.lector).where(lectura.c.lector == lector,
                                                    lectura.c.visto == visto)).scalar() is not None:
            raise SystemExit(f"{lector} ya puede ver a {visto}.")
        _cambio(c, por, cita, "lectura-alta", {"lector": lector, "visto": visto})
        c.execute(insert(lectura).values(lector=lector, visto=visto, creado=ahora()))


def lectura_quitar(motor, lector: str, visto: str, por: str, cita: str) -> None:
    _validar(por, cita)
    with motor.begin() as c:
        _existen(c, lector, visto)
        if not c.execute(delete(lectura).where(lectura.c.lector == lector, lectura.c.visto == visto)).rowcount:
            raise SystemExit(f"{lector} no puede ver a {visto}.")
        _cambio(c, por, cita, "lectura-quitar", {"lector": lector, "visto": visto})


def lecturas(motor, lector: str | None = None) -> list[dict]:
    q = select(lectura).order_by(lectura.c.lector, lectura.c.visto)
    if lector is not None:
        q = q.where(lectura.c.lector == lector)
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(q)]


def _correo(texto: str) -> str:
    correo = _sin_control((texto or "").strip().lower(), "El correo")
    if len(correo) > 120 or not CORREO.fullmatch(correo):
        raise SystemExit(f"Correo inválido: {texto!r}; va como nombre@dominio.com.")
    return correo


def _papel(texto: str) -> str:
    if texto not in PAPELES:
        raise SystemExit(f"Papel inválido: {texto!r}; el papel es dueno, finanzas, responsable o lector.")
    return texto


def _otro_dueno(c, correo: str) -> bool:
    return c.execute(select(func.count()).select_from(usuario).where(
        usuario.c.papel == "dueno", usuario.c.activo.is_(True), usuario.c.correo != correo)).scalar() > 0


def _usuario(c, correo: str) -> dict:
    fila = c.execute(select(usuario).where(usuario.c.correo == correo)).first()
    if fila is None:
        raise SystemExit(f"No existe el usuario {correo}.")
    return dict(fila._mapping)


def usuario_alta(motor, correo: str, nombre: str, papel: str, por: str, cita: str) -> None:
    """Un usuario dado de baja que vuelve a darse de alta se reactiva con el nombre y el papel nuevos."""
    _validar(por, cita)
    correo, papel, nombre = _correo(correo), _papel(papel), _sin_control((nombre or "").strip(), "El nombre")
    if not nombre or len(nombre) > 80:
        raise SystemExit("Falta el nombre del usuario (hasta 80 caracteres).")
    try:
        with motor.begin() as c:
            existente = c.execute(select(usuario.c.activo).where(usuario.c.correo == correo)).first()
            if existente is not None and existente.activo:
                raise SystemExit(f"El usuario {correo} ya existe.")
            _cambio(c, por, cita, "usuario-alta", {"correo": correo, "nombre": nombre, "papel": papel})
            if existente is None:
                c.execute(insert(usuario).values(correo=correo, nombre=nombre, papel=papel, activo=True,
                                                 creado=ahora(), por=por.strip()))
            else:
                c.execute(update(usuario).where(usuario.c.correo == correo)
                          .values(nombre=nombre, papel=papel, activo=True))
    except IntegrityError:                    # otra alta del mismo correo al mismo tiempo
        raise SystemExit(f"El usuario {correo} ya existe.") from None


def usuario_baja(motor, correo: str, por: str, cita: str) -> None:
    _validar(por, cita)
    correo = _correo(correo)
    with motor.begin() as c:
        u = _usuario(c, correo)
        if not u["activo"]:
            raise SystemExit(f"El usuario {correo} ya está de baja.")
        if u["papel"] == "dueno" and not _otro_dueno(c, correo):
            raise SystemExit("No se puede dar de baja al último dueño activo.")
        # La baja le quita sus proyectos: si vuelve, el dueño se los asigna de nuevo.
        quitados = list(c.execute(select(usuario_proyecto.c.proyecto).where(usuario_proyecto.c.correo == correo)
                                  .order_by(usuario_proyecto.c.proyecto)).scalars())
        _cambio(c, por, cita, "usuario-baja", {"correo": correo, "proyectos_quitados": quitados})
        c.execute(delete(usuario_proyecto).where(usuario_proyecto.c.correo == correo))
        c.execute(update(usuario).where(usuario.c.correo == correo).values(activo=False))


def usuario_papel(motor, correo: str, papel: str, por: str, cita: str) -> None:
    _validar(por, cita)
    correo, papel = _correo(correo), _papel(papel)
    with motor.begin() as c:
        u = _usuario(c, correo)
        if u["papel"] == "dueno" and u["activo"] and papel != "dueno" and not _otro_dueno(c, correo):
            raise SystemExit("No se le puede quitar el papel al último dueño activo.")
        _cambio(c, por, cita, "usuario-papel", {"correo": correo, "de": u["papel"], "a": papel})
        c.execute(update(usuario).where(usuario.c.correo == correo).values(papel=papel))


def usuario_asignar(motor, correo: str, proyecto_: str, por: str, cita: str) -> None:
    _validar(por, cita)
    correo = _correo(correo)
    try:
        with motor.begin() as c:
            _usuario(c, correo)
            _existen(c, proyecto_)
            if c.execute(select(usuario_proyecto.c.correo).where(usuario_proyecto.c.correo == correo,
                                                                 usuario_proyecto.c.proyecto == proyecto_)).first():
                raise SystemExit(f"{correo} ya tiene el proyecto {proyecto_}.")
            _cambio(c, por, cita, "usuario-asignar", {"correo": correo, "proyecto": proyecto_})
            c.execute(insert(usuario_proyecto).values(correo=correo, proyecto=proyecto_))
    except IntegrityError:                    # la misma asignación al mismo tiempo
        raise SystemExit(f"{correo} ya tiene el proyecto {proyecto_}.") from None


def usuario_quitar(motor, correo: str, proyecto_: str, por: str, cita: str) -> None:
    _validar(por, cita)
    correo = _correo(correo)
    with motor.begin() as c:
        _usuario(c, correo)
        if not c.execute(delete(usuario_proyecto).where(usuario_proyecto.c.correo == correo,
                                                        usuario_proyecto.c.proyecto == proyecto_)).rowcount:
            raise SystemExit(f"{correo} no tiene el proyecto {proyecto_}.")
        _cambio(c, por, cita, "usuario-quitar", {"correo": correo, "proyecto": proyecto_})


def _con_proyectos(c, filas) -> list[dict]:
    asignados: dict[str, list[str]] = {}
    for f in c.execute(select(usuario_proyecto).order_by(usuario_proyecto.c.proyecto)):
        asignados.setdefault(f.correo, []).append(f.proyecto)
    return [{**dict(f._mapping), "proyectos": asignados.get(f.correo, [])} for f in filas]


def usuarios(motor) -> list[dict]:
    with motor.connect() as c:
        return _con_proyectos(c, c.execute(select(usuario).order_by(usuario.c.correo)).all())


def usuario_de(motor, correo: str) -> dict | None:
    """Solo activos; None si no existe, está de baja o el correo no es un correo."""
    correo = (correo or "").strip().lower()
    with motor.connect() as c:
        filas = c.execute(select(usuario).where(usuario.c.correo == correo, usuario.c.activo.is_(True))).all()
        return _con_proyectos(c, filas)[0] if filas else None


def objeto_registrar(motor, id_: str, tipo: str, cuenta_: str, proyecto_: str, cuentas_validas, por: str,
                     cita: str) -> None:
    """Da de alta a mano un archivo o un lote que nació fuera de la pasarela (o que no se pudo registrar), para que
    quede fijado a su cuenta y solo lo use su proyecto. `cuentas_validas`: los nombres de PIA_FUENTES. Nunca cambia
    de dueño un objeto ya registrado. Un lote a mano nace con el costo ya contado: no se sabe qué costó y
    bajar sus resultados no debe sumar uno inventado (los archivos no tienen costo propio)."""
    _validar(por, cita)
    if tipo not in TIPOS_OBJETO:
        raise SystemExit("El tipo es archivo o lote.")
    if not ID_OBJETO.fullmatch(id_):
        raise SystemExit("Id inválido: letras, números, guion y guion bajo, hasta 120 caracteres.")
    if cuenta_ not in cuentas_validas:
        raise SystemExit(f"La cuenta {cuenta_} no está en PIA_FUENTES.")
    with motor.begin() as c:
        _existen(c, proyecto_)
        ya = c.execute(select(objeto.c.proyecto, objeto.c.fuente, objeto.c.tipo).where(objeto.c.id == id_)).first()
        if ya is not None:
            raise SystemExit(f"{id_} ya está registrado: {ya.tipo} de {ya.proyecto} en la cuenta {ya.fuente}.")
        _cambio(c, por, cita, "objeto-registrar", {"id": id_, "tipo": tipo, "cuenta": cuenta_, "proyecto": proyecto_})
        c.execute(insert(objeto).values(id=id_, tipo=tipo, fuente=cuenta_, proyecto=proyecto_, creado=ahora(),
                                        costo_registrado=(tipo == "lote")))


def objeto_ver(motor, id_: str) -> dict | None:
    with motor.connect() as c:
        f = c.execute(select(objeto).where(objeto.c.id == id_)).first()
    return dict(f._mapping) if f else None


def presupuesto_poner(motor, limite_micro: int, por: str, cita: str) -> None:
    _validar(por, cita)
    with motor.begin() as c:
        _cambio(c, por, cita, "presupuesto", {"usd": _usd_texto(limite_micro)})
        c.execute(delete(ajuste).where(ajuste.c.clave == CLAVE_PRESUPUESTO))
        c.execute(insert(ajuste).values(clave=CLAVE_PRESUPUESTO, valor=limite_micro, actualizado=ahora()))


def conciliacion_cargar(motor, datos, por: str, cita: str) -> int:
    """Totales por día cerrado que manda `ops/pia conciliar`. Un día que ya estaba se reemplaza."""
    _validar(por, cita)
    if not isinstance(datos, dict) or not isinstance(datos.get("cuentas"), dict):
        raise SystemExit("La conciliación no trae «cuentas».")
    hoy = ahora().date()
    filas = []
    for nombre, dias in datos["cuentas"].items():
        if not isinstance(nombre, str) or not NOMBRE.fullmatch(nombre) or not isinstance(dias, list):
            raise SystemExit(f"Cuenta inválida en la conciliación: {str(nombre)[:40]!r}.")
        for x in dias:
            try:
                dia = datetime.strptime(x["dia"], "%Y-%m-%d")
                micro = x["micro"]
            except (KeyError, TypeError, ValueError):
                raise SystemExit(f"Día inválido en la conciliación de {nombre}.") from None
            if not isinstance(micro, int) or isinstance(micro, bool) or micro < 0:
                raise SystemExit(f"Monto inválido en la conciliación de {nombre} ({x['dia']}).")
            if micro > MICRO_MAX:
                raise SystemExit(f"Monto demasiado grande en la conciliación de {nombre} ({x['dia']}).")
            if dia.date() >= hoy:
                raise SystemExit("Solo se concilian días cerrados (antes de hoy, UTC).")
            filas.append((nombre, dia, micro))
    t = ahora()
    with motor.begin() as c:
        _cambio(c, por, cita, "conciliacion", {"cuentas": sorted(datos["cuentas"]), "dias": len(filas),
                                               "total_usd": _usd_texto(sum(m for _, _, m in filas))})
        for nombre, dia, micro in filas:
            c.execute(delete(conciliacion).where(conciliacion.c.cuenta == nombre, conciliacion.c.dia == dia))
            c.execute(insert(conciliacion).values(cuenta=nombre, dia=dia, micro=micro, recibido=t))
    return len(filas)


def imprimir_saldos(f) -> None:
    if not f.con_cuentas:
        print("La tabla cuenta está vacía: el orden es el de PIA_FUENTES.")
    for v in f.vivas:
        sin = v.nombre in f.sin_datos
        vence = fecha(v.vence) if v.vence else "sin concesión vigente"
        print(f"{v.nombre:<10} {'con tarjeta' if v.tarjeta else 'sin tarjeta':<11} libre {usd(v.libre_micro):>11}"
              f" · vence {vence} · {cuadre(f.conciliada.get(v.nombre), f.ts)}")
        for e in (x for x in f.concesiones if x.cuenta == v.nombre):
            estado = "consola" if e.confirmada else "por confirmar"
            nota = "" if e.cuenta_en_saldo else " · no entra al saldo (estimada en cuenta con tarjeta)"
            perdida = "pérdida sin datos" if sin else f"se pierden {usd(e.perdida_micro)} al ritmo actual"
            print(f"  #{e.id} {usd(e.monto_micro)} del {fecha(e.otorgada)} al {fecha(e.vence)} · {estado}"
                  f" · quedan {usd(e.restante_micro)} · {perdida}{nota}")
    pres = usd(f.presupuesto_micro) if f.presupuesto_micro is not None else "sin presupuesto"
    pagado_micro, sin_datos = pagado_visible(f)
    if pagado_micro is None:
        pagado = f"sin datos ({', '.join(sin_datos)} nunca se ha cuadrado con Anthropic)"
    else:
        pagado = usd(pagado_micro) + (f" (no cuenta {', '.join(sin_datos)}: sin datos)" if sin_datos else "")
    print(f"Pagado con tarjeta este mes: {pagado} · presupuesto {pres}")
    for p in sorted(set(f.gasto_mes) | set(f.topes)):
        lim = f.topes.get(p)
        cola = f" de {usd(lim[0])} ({lim[1]}{', cortado' if p in f.cortados else ''})" if lim else ""
        print(f"{p:<16} {usd(f.gasto_mes.get(p, 0))}{cola}")
    for n in f.sin_llave:
        print(f"Ojo: la cuenta {n} está en la tabla pero no en PIA_FUENTES.")


def _webhook(motor, a) -> None:
    wh = modulo_webhooks
    if a.sub in ("alta", "rotar", "cambiar"):
        _validar(a.por, a.cita)
        anotar = lambda c, accion, detalle: _cambio(c, a.por, a.cita, accion, detalle)  # noqa: E731
    if a.sub == "alta":
        if not NOMBRE.fullmatch(a.nombre):
            raise SystemExit("El nombre va en minúsculas, con letras, números y guiones.")
        wh.alta(motor, a.nombre, a.url, sys.stdin.read(), a.eventos, a.proyectos, anotar)
        print(f"webhook {a.nombre}: alta hacia {a.url}")
    elif a.sub == "rotar":
        wh.rotar(motor, a.nombre, sys.stdin.read(), anotar)
        print(f"webhook {a.nombre}: secreto nuevo; el anterior firma 24 h más")
    elif a.sub == "cambiar":
        wh.cambiar(motor, a.nombre, anotar, url=a.url, eventos=a.eventos, proyectos=a.proyectos,
                   activo=None if a.activo is None else a.activo in ("si", "sí"))
        print(f"webhook {a.nombre}: cambiado")
    elif a.sub == "lista":
        for s in wh.lista(motor):
            ent = ", ".join(f"{k} {v}" for k, v in sorted(s["entregas"].items())) or "sin entregas"
            print(f"{s['nombre']:<12} {'activo' if s['activo'] else 'apagado'} · {s['url']} · secreto …{s['huella']}"
                  f" · eventos {s['eventos']} · proyectos {s['proyectos'] or '-'} · {ent}")
    elif a.sub == "prueba":
        print(f"aviso de prueba en cola: {wh.encolar_prueba(motor, a.nombre)}")
    elif a.sub == "generar":
        def concesiones(t):
            return {e.id: e for e in calcular(leer(motor, t), cargar(), t)[0].concesiones}
        print(f"webhook: {wh.generar(motor, concesiones=concesiones)} eventos nuevos")
    elif a.sub == "entregar":
        def avisar(clave, texto):
            modulo_avisos.avisar(motor, [modulo_avisos.Aviso(clave, texto)])
        r = wh.entregar(motor, avisar=avisar)
        print(f"webhook: {r['entregados']} entregados · {r['reintentos']} por reintentar · {r['fallidos']} fallidos")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="pasarela.admin")
    sub = p.add_subparsers(dest="orden", required=True)

    def con_cita(s):
        s.add_argument("--cita", required=True, help="palabras exactas de quien lo ordenó")
        s.add_argument("--por", required=True, help="quién lo ordenó, por ejemplo «admin»")
        return s

    s = con_cita(sub.add_parser("alta"))
    s.add_argument("nombre")
    s.add_argument("--clase", required=True, choices=CLASES)
    con_cita(sub.add_parser("rotar")).add_argument("nombre")
    con_cita(sub.add_parser("baja")).add_argument("nombre")
    s = con_cita(sub.add_parser("clase"))
    s.add_argument("nombre")
    s.add_argument("nueva", choices=CLASES)
    con_cita(sub.add_parser("reactivar")).add_argument("fuente")
    sub.add_parser("uso").add_argument("--desde", help="AAAA-MM-DD en UTC; por defecto el día 1 del mes")
    sub.add_parser("cambios").add_argument("-n", type=int, default=20)
    sub.add_parser("proyectos")
    s = sub.add_parser("cuenta").add_subparsers(dest="sub", required=True)
    x = con_cita(s.add_parser("agregar"))
    x.add_argument("nombre")
    x.add_argument("--correo-plan", required=True)
    x.add_argument("--tarjeta", required=True, choices=SI_NO)
    x.add_argument("--llave-vence", help="AAAA-MM-DD, vencimiento de la llave de servicio")
    x = con_cita(s.add_parser("cambiar"))
    x.add_argument("nombre")
    x.add_argument("--tarjeta", choices=SI_NO)
    x.add_argument("--activa", choices=SI_NO)
    x.add_argument("--llave-vence")
    sub.add_parser("cuentas")
    s = sub.add_parser("credito").add_subparsers(dest="sub", required=True)
    x = con_cita(s.add_parser("agregar"))
    x.add_argument("--cuenta", required=True)
    x.add_argument("--monto", required=True, help="USD")
    x.add_argument("--otorgada", required=True, help="AAAA-MM-DD")
    x.add_argument("--vence", help="AAAA-MM-DD; primer día en que ya no cuenta. Sin él, una --estimada vence 33 días "
                                   "después de --otorgada")
    x.add_argument("--estimada", action="store_true", help="por confirmar en la consola")
    x.add_argument("--nota")
    x = con_cita(s.add_parser("confirmar"))
    x.add_argument("id", type=int)
    x.add_argument("--monto", required=True)
    x.add_argument("--otorgada", required=True)
    x.add_argument("--vence", required=True)
    x = con_cita(s.add_parser("ajustar"))
    x.add_argument("id", type=int)
    x.add_argument("--monto", help="USD")
    x.add_argument("--otorgada", help="AAAA-MM-DD")
    x.add_argument("--vence", help="AAAA-MM-DD; primer día en que ya no cuenta")
    sub.add_parser("saldos")
    x = con_cita(sub.add_parser("tope"))
    x.add_argument("proyecto")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--usd")
    g.add_argument("--quitar", action="store_true")
    x.add_argument("--modo", choices=MODOS)
    con_cita(sub.add_parser("presupuesto")).add_argument("--usd", required=True)
    s = sub.add_parser("conciliacion").add_subparsers(dest="sub", required=True)
    con_cita(s.add_parser("cargar"))
    s = sub.add_parser("lectura").add_subparsers(dest="sub", required=True)
    for accion in ("agregar", "quitar"):
        x = con_cita(s.add_parser(accion))
        x.add_argument("lector")
        x.add_argument("visto")
    s.add_parser("ver").add_argument("lector", nargs="?")
    s = sub.add_parser("usuario").add_subparsers(dest="sub", required=True)
    x = con_cita(s.add_parser("alta"))
    x.add_argument("correo")
    x.add_argument("--nombre", help="por defecto, lo que va antes de la arroba")
    x.add_argument("--papel", required=True, help="dueno, finanzas, responsable o lector")
    con_cita(s.add_parser("baja")).add_argument("correo")
    x = con_cita(s.add_parser("papel"))
    x.add_argument("correo")
    x.add_argument("--papel", required=True, help="dueno, finanzas, responsable o lector")
    for accion in ("asignar", "quitar"):
        x = con_cita(s.add_parser(accion))
        x.add_argument("correo")
        x.add_argument("proyecto")
    s.add_parser("lista")
    s = sub.add_parser("objeto").add_subparsers(dest="sub", required=True)
    x = con_cita(s.add_parser("registrar"))
    x.add_argument("id")
    x.add_argument("--tipo", required=True, choices=TIPOS_OBJETO)
    x.add_argument("--cuenta", required=True)
    x.add_argument("--proyecto", required=True)
    s.add_parser("ver").add_argument("id")
    sub.add_parser("silencios").add_argument("--avisar", action="store_true",
                                             help="escribe los avisos para que el vigía los mande")
    s = sub.add_parser("webhook", help="avisos firmados a otros programas").add_subparsers(dest="sub", required=True)
    x = con_cita(s.add_parser("alta", help="el secreto whsec_ llega por la entrada estándar"))
    x.add_argument("nombre")
    x.add_argument("--url", required=True)
    x.add_argument("--proyectos", required=True, help="separados por coma")
    x.add_argument("--eventos", default=",".join(modulo_webhooks.EVENTOS))
    con_cita(s.add_parser("rotar", help="el secreto nuevo llega por la entrada estándar")).add_argument("nombre")
    x = con_cita(s.add_parser("cambiar"))
    x.add_argument("nombre")
    x.add_argument("--url")
    x.add_argument("--proyectos")
    x.add_argument("--eventos")
    x.add_argument("--activo", choices=SI_NO)
    s.add_parser("lista")
    s.add_parser("prueba").add_argument("nombre")
    s.add_parser("generar")
    s.add_parser("entregar")
    x = sub.add_parser("avisos")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--pendientes", action="store_true")
    g.add_argument("--marcar", nargs="+", type=int)
    x.add_argument("--max", type=_entero_positivo, default=10)
    a = p.parse_args(argv)

    motor = crear_motor(os.environ["PIA_DB_URL"])
    if a.orden == "alta":
        print(alta(motor, a.nombre, a.clase, a.por, a.cita))
    elif a.orden == "rotar":
        print(rotar(motor, a.nombre, a.por, a.cita))
    elif a.orden == "baja":
        baja(motor, a.nombre, a.por, a.cita)
        print(f"{a.nombre}: dado de baja")
    elif a.orden == "clase":
        clase(motor, a.nombre, a.nueva, a.por, a.cita)
        print(f"{a.nombre}: ahora es {a.nueva}")
    elif a.orden == "reactivar":
        reactivar(motor, a.fuente, a.por, a.cita)
        print(f"{a.fuente}: reactivada")
    elif a.orden == "uso":
        desde = (datetime.strptime(a.desde, "%Y-%m-%d") if a.desde
                 else ahora().replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        total = 0
        for f in uso(motor, desde):
            micro = f["micro"]
            total += micro
            print(f"{f['proyecto']:<16} {f['fuente']:<8} {f['peticiones']:>7} pet · US${micro / 1e6:>9.4f}"
                  f" · entrada {f['entrada']:>11,} · salida {f['salida']:>10,}")
        print(f"total desde {desde:%Y-%m-%d}: US${total / 1e6:.4f}")
    elif a.orden == "silencios":
        lista = silencios(motor)
        for f in lista:
            print(f"{f['proyecto']:<16} callado desde {f['ultima']:%Y-%m-%d %H:%M} UTC · {f['antes']} pet antes")
        if a.avisar:
            avisar_silencios(motor)
        if not lista:
            print("ningún programa con tráfico real está callado")
    elif a.orden == "cambios":
        for f in cambios(motor, a.n):
            print(f"{f['ts']:%Y-%m-%d %H:%M} UTC · {f['ordenado_por']} · {f['accion']} · {f['detalle']} · {f['cita']}")
    elif a.orden == "proyectos":
        for f in proyectos(motor):
            print(f"{f['nombre']:<16} {f['clase']:<10} …{f['huella']} · {'activo' if f['activo'] else 'baja'}"
                  f" · desde {f['creado']:%Y-%m-%d}")
    elif a.orden == "cuenta" and a.sub == "agregar":
        cuenta_agregar(motor, a.nombre, a.correo_plan, a.tarjeta in ("si", "sí"), _fecha(a.llave_vence, "--llave-vence"),
                       a.por, a.cita)
        print(f"cuenta {a.nombre}: alta")
    elif a.orden == "cuenta" and a.sub == "cambiar":
        cuenta_cambiar(motor, a.nombre, a.por, a.cita,
                       tarjeta=None if a.tarjeta is None else a.tarjeta in ("si", "sí"),
                       activa=None if a.activa is None else a.activa in ("si", "sí"),
                       llave_vence=_fecha(a.llave_vence, "--llave-vence"))
        print(f"cuenta {a.nombre}: cambiada")
    elif a.orden == "cuentas":
        for f in cuentas(motor):
            print(f"{f['nombre']:<10} {f['correo_plan']:<28} {'con tarjeta' if f['tiene_tarjeta'] else 'sin tarjeta'}"
                  f" · {'activa' if f['activa'] else 'inactiva'} · llave vence {_dia(f['llave_vence']) or 'sin dato'}")
    elif a.orden == "credito" and a.sub == "agregar":
        otorgada = _fecha(a.otorgada, "--otorgada")
        monto = _micro(a.monto, "--monto")
        id_, vence = credito_agregar(motor, a.cuenta, monto, otorgada, _fecha(a.vence, "--vence"), a.estimada,
                                     a.nota, a.por, a.cita)
        print(f"concesión #{id_}: {a.cuenta} {usd(monto)} del {fecha(otorgada.date())} "
              f"al {fecha(vence.date())} ({'por confirmar' if a.estimada else 'consola'})")
    elif a.orden == "credito" and a.sub == "confirmar":
        credito_confirmar(motor, a.id, _micro(a.monto, "--monto"), _fecha(a.otorgada, "--otorgada"),
                          _fecha(a.vence, "--vence"), a.por, a.cita)
        print(f"concesión #{a.id}: confirmada")
    elif a.orden == "credito" and a.sub == "ajustar":
        n = credito_ajustar(motor, a.id, a.por, a.cita,
                            monto_micro=_micro(a.monto, "--monto") if a.monto is not None else None,
                            otorgada=_fecha(a.otorgada, "--otorgada"), vence=_fecha(a.vence, "--vence"))
        print(f"concesión #{a.id}: {usd(n['monto_micro'])} del {fecha(n['otorgada'].date())} al "
              f"{fecha(n['vence'].date())} · sigue {'confirmada' if n['confirmada'] else 'por confirmar'}")
    elif a.orden == "saldos":
        t = ahora()
        imprimir_saldos(calcular(leer(motor, t), cargar(), t)[0])
    elif a.orden == "tope":
        if a.quitar:
            tope_quitar(motor, a.proyecto, a.por, a.cita)
            print(f"{a.proyecto}: sin tope")
        else:
            limite = _micro(a.usd, "--usd")
            modo = tope_poner(motor, a.proyecto, limite, a.modo, a.por, a.cita)
            print(f"{a.proyecto}: tope mensual {usd(limite)} ({modo})")
    elif a.orden == "presupuesto":
        limite = _micro(a.usd, "--usd")
        presupuesto_poner(motor, limite, a.por, a.cita)
        print(f"presupuesto pagado del mes: {usd(limite)}")
    elif a.orden == "conciliacion":
        try:
            datos = json.loads(sys.stdin.read())
        except ValueError:
            raise SystemExit("La conciliación no llegó como JSON.") from None
        print(f"conciliación: {conciliacion_cargar(motor, datos, a.por, a.cita)} días cargados")
    elif a.orden == "lectura" and a.sub == "agregar":
        lectura_agregar(motor, a.lector, a.visto, a.por, a.cita)
        print(f"{a.lector} ve a {a.visto}")
    elif a.orden == "lectura" and a.sub == "quitar":
        lectura_quitar(motor, a.lector, a.visto, a.por, a.cita)
        print(f"{a.lector} ya no ve a {a.visto}")
    elif a.orden == "lectura":
        for f in lecturas(motor, a.lector):
            print(f"{f['lector']} ve a {f['visto']} · desde {f['creado']:%Y-%m-%d}")
    elif a.orden == "usuario" and a.sub == "alta":
        nombre = a.nombre if a.nombre is not None else a.correo.strip().split("@")[0]
        usuario_alta(motor, a.correo, nombre, a.papel, a.por, a.cita)
        print(f"{a.correo.strip().lower()}: alta como {a.papel}")
    elif a.orden == "usuario" and a.sub == "baja":
        usuario_baja(motor, a.correo, a.por, a.cita)
        print(f"{a.correo.strip().lower()}: dado de baja")
    elif a.orden == "usuario" and a.sub == "papel":
        usuario_papel(motor, a.correo, a.papel, a.por, a.cita)
        print(f"{a.correo.strip().lower()}: ahora es {a.papel}")
    elif a.orden == "usuario" and a.sub == "asignar":
        usuario_asignar(motor, a.correo, a.proyecto, a.por, a.cita)
        print(f"{a.correo.strip().lower()}: tiene el proyecto {a.proyecto}")
    elif a.orden == "usuario" and a.sub == "quitar":
        usuario_quitar(motor, a.correo, a.proyecto, a.por, a.cita)
        print(f"{a.correo.strip().lower()}: ya no tiene el proyecto {a.proyecto}")
    elif a.orden == "usuario":
        for f in usuarios(motor):
            print(f"{f['correo']:<32} {f['papel']:<11} {'activo' if f['activo'] else 'baja':<6}"
                  f" · {f['nombre']} · proyectos: {', '.join(f['proyectos']) or 'ninguno'}")
    elif a.orden == "objeto" and a.sub == "registrar":
        objeto_registrar(motor, a.id, a.tipo, a.cuenta, a.proyecto, {f.nombre for f in cargar().fuentes}, a.por,
                         a.cita)
        print(f"{a.id}: {a.tipo} de {a.proyecto} en la cuenta {a.cuenta}")
    elif a.orden == "objeto":
        o = objeto_ver(motor, a.id)
        print(f"{o['id']}: {o['tipo']} de {o['proyecto']} en la cuenta {o['fuente']} · "
              f"desde {o['creado']:%Y-%m-%d %H:%M} UTC" if o else f"{a.id}: no está registrado")
    elif a.orden == "webhook":
        _webhook(motor, a)
    elif a.orden == "avisos":
        if a.pendientes:
            for x in modulo_avisos.pendientes(motor, a.max):
                print(json.dumps(x, ensure_ascii=False))
        else:
            print(f"marcados {modulo_avisos.marcar(motor, a.marcar)}")


if __name__ == "__main__":
    main()
