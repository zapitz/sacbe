"""Saldos estimados por cuenta, gasto del mes y avisos de dinero.
Se recalcula cada PIA_ORDEN_S segundos (60) fuera del camino de las peticiones; el proxy solo lee la última foto.
Si la base falla se queda la foto anterior. Sin foto, o con la tabla cuenta vacía, el orden es el de PIA_FUENTES."""
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from .avisos import LARGO_CLAVE, Aviso, avisar
from .config import Config, variable_llave
from .esquema import ajuste, concesion, conciliacion, cuenta, proyecto, registro, tope
from .orden import Candidata, CuentaViva, SinCredito, ordenar, ordenar_por_saldo
from .saldo import Concesion, proyectar, repartir
from .tiempo import ahora, en_local

MICRO = 1_000_000
PERDIDA_AVISO = 10 * MICRO            # avisar si se van a perder US$10 o más
DIAS_AVISO_PERDIDA = (5, 2)
RECIENTE = timedelta(hours=36)        # solo informativo: no decide el orden, extra nunca usa tarjeta
PASO_MXN = 200
VUELTAS_VENTANA = 10                  # tope de pasadas al ampliar la ventana de gasto en leer()
DIAS_LLAVE = 15
CLAVE_PRESUPUESTO = "presupuesto_pagado_micro"
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def usd(micro: int) -> str:
    return f"US${micro / MICRO:,.2f}"


def fecha(d: date) -> str:
    return f"{d.day}-{MESES[d.month - 1]}"


def a_dia(valor) -> date:
    """func.date() regresa texto en SQLite y date en Postgres; una columna DateTime regresa datetime."""
    if isinstance(valor, date) and not isinstance(valor, datetime):
        return valor
    return date.fromisoformat(str(valor)[:10])


def medianoche(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def cuadre(recibido: datetime | None, t: datetime) -> str:
    """Qué tan cierto es el saldo de una cuenta: «sin datos» si nunca se ha cuadrado con Anthropic,
    «estimado» si el último cuadre tiene 36 h o más, y si no «cuadrado a las HH:MM» en la hora local del panel, con el día
    cuando no fue hoy."""
    if recibido is None:
        return "sin datos"
    local = en_local(recibido)
    if t - recibido >= RECIENTE:
        return f"estimado (último cuadre el {fecha(local.date())})"
    if local.date() == en_local(t).date():
        return f"cuadrado a las {local:%H:%M}"
    return f"cuadrado el {fecha(local.date())} a las {local:%H:%M}"


def al_80(gasto: int, limite: int) -> bool:
    """Ya va en el 80% del límite: aviso de tope y color ámbar del panel."""
    return gasto * 10 >= limite * 8


def al_dia(edad_s: float, cada_s: float) -> bool:
    """Una tarea que corre cada `cada_s` segundos sigue al día si su último resultado tiene menos de tres vueltas y
    5 s de margen. Con `cada_s` en 0 (apagada, en pruebas) siempre está al día."""
    return cada_s <= 0 or edad_s < 3 * cada_s + 5


@dataclass(frozen=True)
class EstadoConcesion:
    id: int
    cuenta: str
    monto_micro: int
    otorgada: date
    vence: date
    confirmada: bool
    cuenta_en_saldo: bool             # False: estimada en una cuenta con tarjeta, no entra al saldo
    restante_micro: int
    perdida_micro: int                # proyectada al ritmo de los últimos 7 días
    ritmo_micro: int | None = None    # ese ritmo diario, el mismo de la proyección; None si no entra al saldo


@dataclass(frozen=True)
class Foto:
    con_cuentas: bool                 # False: la tabla cuenta está vacía y el orden es el de PIA_FUENTES
    vivas: tuple[CuentaViva, ...]
    concesiones: tuple[EstadoConcesion, ...]
    cortados: frozenset[str]
    gasto_mes: dict[str, int]         # por proyecto, lo que registró la pasarela este mes (UTC)
    topes: dict[str, tuple[int, str]]
    pagado_mes_micro: int
    presupuesto_micro: int | None
    conciliada: dict[str, datetime]   # última conciliación recibida por cuenta
    sin_llave: tuple[str, ...]        # cuentas en la tabla que no están en PIA_FUENTES
    tarjetas: frozenset[str]          # toda fila de cuenta con tiene_tarjeta, activa o no (para el 402)
    ts: datetime
    sin_datos: frozenset[str] = frozenset()   # cuentas que nunca se han cuadrado con Anthropic
    bajas: frozenset[str] = frozenset()       # proyectos dados de baja (activo falso): el panel los oculta sin gasto


def pagado_visible(f: Foto) -> tuple[int | None, list[str]]:
    """Lo pagado con tarjeta este mes como se le muestra al dueño: None si ninguna cuenta con tarjeta se ha
    cuadrado nunca con Anthropic. Regresa también las cuentas con tarjeta que siguen sin datos, en orden."""
    con_tarjeta = sorted(v.nombre for v in f.vivas if v.tarjeta)
    sin = [n for n in con_tarjeta if n in f.sin_datos]
    if con_tarjeta and len(sin) == len(con_tarjeta):
        return None, sin
    return f.pagado_mes_micro, sin


@dataclass
class Datos:
    cuentas: list[dict]
    concesiones: list[dict]
    conciliado: dict[tuple[str, date], int]
    recibido: dict[str, datetime]
    registrado: dict[tuple[str, date], int]
    por_proyecto: dict[tuple[str, str], int]
    topes: dict[str, tuple[int, str]]
    presupuesto: int | None
    bajas: frozenset[str] = frozenset()


def leer(motor, t: datetime) -> Datos:
    hoy = t.date()
    mes = hoy.replace(day=1)
    with motor.connect() as c:
        cuentas = [dict(f._mapping) for f in c.execute(select(cuenta))]
        # La ventana de gasto llega a la concesión más vieja que se cargó, y esa ventana puede tocar a otra concesión
        # ya vencida que también repartió gasto: se amplía hasta que el conjunto no cambie.
        inicio = min(mes, hoy - timedelta(days=7))
        concs: list[dict] = []
        for _ in range(VUELTAS_VENTANA):
            nuevas = [dict(f._mapping) for f in c.execute(
                select(concesion).where(concesion.c.vence > medianoche(inicio)).order_by(concesion.c.id))]
            if [x["id"] for x in nuevas] == [x["id"] for x in concs]:
                break
            concs = nuevas
            inicio = min([inicio] + [x["otorgada"].date() for x in concs])
        else:
            print(f"pasarela: la ventana de gasto no se estabilizó en {VUELTAS_VENTANA} pasadas; "
                  "los saldos pueden salir incompletos", file=sys.stderr)
        conciliado_ = {(cu, a_dia(d)): int(m) for cu, d, m in c.execute(
            select(conciliacion.c.cuenta, conciliacion.c.dia, conciliacion.c.micro)
            .where(conciliacion.c.dia >= medianoche(inicio)))}
        recibido = {cu: r for cu, r in c.execute(
            select(conciliacion.c.cuenta, func.max(conciliacion.c.recibido)).group_by(conciliacion.c.cuenta))}
        dia = func.date(registro.c.ts)
        registrado = {(f, a_dia(d)): int(m or 0) for f, d, m in c.execute(
            select(registro.c.fuente, dia, func.sum(registro.c.costo_micro))
            .where(registro.c.ts >= medianoche(inicio)).group_by(registro.c.fuente, dia))}
        por_proyecto = {(p, f): int(m or 0) for p, f, m in c.execute(
            select(registro.c.proyecto, registro.c.fuente, func.sum(registro.c.costo_micro))
            .where(registro.c.ts >= medianoche(mes)).group_by(registro.c.proyecto, registro.c.fuente))}
        topes = {p: (int(lim), modo) for p, lim, modo in c.execute(
            select(tope.c.proyecto, tope.c.tope_mensual_micro, tope.c.modo))}
        pres = c.execute(select(ajuste.c.valor).where(ajuste.c.clave == CLAVE_PRESUPUESTO)).scalar()
        bajas = frozenset(c.execute(select(proyecto.c.nombre).where(proyecto.c.activo.is_(False))).scalars())
    return Datos(cuentas, concs, conciliado_, recibido, registrado, por_proyecto, topes,
                 int(pres) if pres is not None else None, bajas)


def gasto_de(datos: Datos, nombre: str) -> dict[date, int]:
    """Por día: lo conciliado con Anthropic si ese día ya se cuadró; si no, lo que registró la pasarela."""
    gasto = {d: m for (cu, d), m in datos.registrado.items() if cu == nombre}
    gasto.update({d: m for (cu, d), m in datos.conciliado.items() if cu == nombre})
    return gasto


def calcular(datos: Datos, config: Config, t: datetime) -> tuple[Foto, list[Aviso]]:
    hoy = t.date()
    mes = hoy.replace(day=1)
    clave_mes = f"{mes:%Y-%m}"
    filas = {r["nombre"]: r for r in datos.cuentas}
    avisos: list[Aviso] = []
    vivas: list[CuentaViva] = []
    estados: list[EstadoConcesion] = []
    tarjetas: set[str] = set()
    sin_datos: set[str] = set()
    pagado_mes = 0
    gasto_tarjeta_mes = 0
    for pos, f in enumerate(config.fuentes):
        fila = filas.get(f.nombre)
        if fila is not None and not fila["activa"]:
            continue
        tarjeta = f.tipo == "pagado" or bool(fila and fila["tiene_tarjeta"])
        if tarjeta:
            tarjetas.add(f.nombre)
        con_datos = f.nombre in datos.recibido   # alguna vez cuadrada con Anthropic; si no, sin avisos de dinero
        if not con_datos:
            sin_datos.add(f.nombre)
        todas = [Concesion(int(x["id"]), int(x["monto_micro"]), x["otorgada"].date(), x["vence"].date(),
                           bool(x["confirmada"])) for x in datos.concesiones
                 if x["cuenta"] == f.nombre and x["vence"] > x["otorgada"]]      # repartir exige vence > otorgada
        usadas = [c for c in todas if c.confirmada or not tarjeta]
        gasto = gasto_de(datos, f.nombre)
        rep = repartir(usadas, gasto, hoy)
        ritmo = sum(gasto.get(hoy - timedelta(days=i), 0) for i in range(1, 8)) // 7
        perdidas = proyectar(usadas, rep.restante, ritmo, hoy + timedelta(days=1))
        vigentes = [c for c in usadas if c.otorgada <= hoy < c.vence]
        libre = sum(rep.restante.get(c.id, 0) for c in vigentes)
        vence = min((c.vence for c in vigentes if rep.restante.get(c.id, 0) > 0), default=None)
        rec = datos.recibido.get(f.nombre)
        vivas.append(CuentaViva(f.nombre, tarjeta, libre, vence, rec is not None and t - rec < RECIENTE, pos))
        for c in todas:
            en_saldo = c in usadas
            rest = rep.restante.get(c.id, 0) if en_saldo else c.monto_micro
            perd = perdidas.get(c.id, 0) if en_saldo else 0
            estados.append(EstadoConcesion(c.id, f.nombre, c.monto_micro, c.otorgada, c.vence, c.confirmada,
                                           en_saldo, rest, perd, ritmo if en_saldo else None))
            dias = (c.vence - hoy).days
            if (con_datos and en_saldo and c.otorgada <= hoy and 0 < dias <= DIAS_AVISO_PERDIDA[0]
                    and perd >= PERDIDA_AVISO):
                marca = "2d" if dias <= DIAS_AVISO_PERDIDA[1] else "5d"
                avisos.append(Aviso(
                    f"perdida:{c.id}:{marca}",
                    f"pasarela-ia: a la concesión de {f.nombre} ({usd(c.monto_micro)}) le quedan {usd(rest)} y vence "
                    f"el {fecha(c.vence)} ({dias} días). Al ritmo de los últimos 7 días se pierden {usd(perd)}."))
        if tarjeta and filas and con_datos:   # tabla vacía o nunca cuadrada: no cuenta pagado
            pagado_mes += sum(v for d, v in rep.pagado_por_dia.items() if d >= mes)
            gasto_tarjeta_mes += sum(v for d, v in gasto.items() if mes <= d <= hoy)

    gasto_mes: dict[str, int] = {}
    for (p, _), m in datos.por_proyecto.items():
        gasto_mes[p] = gasto_mes.get(p, 0) + m
    cortados = set()
    for p, (limite, modo) in datos.topes.items():
        g = gasto_mes.get(p, 0)
        if limite <= 0:
            continue
        if g >= limite:
            if modo == "corte":
                cortados.add(p)
            sigue = ("Sus peticiones reciben 429 hasta el día 1." if modo == "corte"
                     else "Sigue funcionando (modo aviso).")
            avisos.append(Aviso(f"tope:{p}:{clave_mes}:100",
                                f"pasarela-ia: {p} llegó a su tope mensual: {usd(g)} de {usd(limite)}. {sigue}"))
        elif al_80(g, limite):
            avisos.append(Aviso(f"tope:{p}:{clave_mes}:80",
                                f"pasarela-ia: {p} va en {usd(g)} de su tope mensual de {usd(limite)} (80%)."))

    tc = config.tipo_cambio
    mxn = Decimal(pagado_mes) / MICRO * tc
    pasos = int(mxn // PASO_MXN)
    if pasos >= 1:
        por_prog: dict[str, int] = {}
        registrado_tarjeta = 0
        for (p, cu), m in datos.por_proyecto.items():
            if cu in tarjetas and cu not in sin_datos:
                por_prog[p] = por_prog.get(p, 0) + m
                registrado_tarjeta += m
        partes = [f"{p} {usd(m)}" for p, m in sorted(por_prog.items(), key=lambda x: (-x[1], x[0]))[:5]]
        fuera = max(0, gasto_tarjeta_mes - registrado_tarjeta)
        if fuera:
            partes.append(f"fuera de la pasarela {usd(fuera)}")
        nota = ", supuesto: falta PIA_TIPO_CAMBIO" if config.tipo_cambio_supuesto else ""
        avisos.append(Aviso(
            f"pagado:{clave_mes}:{pasos}",
            f"pasarela-ia: van MX${pasos * PASO_MXN:,} pagados con tarjeta en {MESES[mes.month - 1]} "
            f"(estimado {usd(pagado_mes)} = MX${mxn:,.2f}, a {tc} MXN por USD{nota}). "
            f"Gasto del mes en cuentas con tarjeta: {', '.join(partes) or 'sin registro por programa'}."))
    if filas and datos.presupuesto is not None and pagado_mes > datos.presupuesto:
        avisos.append(Aviso(
            f"presupuesto:{clave_mes}",
            f"pasarela-ia: lo pagado con tarjeta este mes ({usd(pagado_mes)}) pasó el presupuesto de "
            f"{usd(datos.presupuesto)}. Producción sigue; el corte duro es el tope del workspace en la consola."))
    for nombre, fila in filas.items():
        lv = fila.get("llave_vence")
        if lv is not None and (lv.date() - hoy).days <= DIAS_LLAVE:
            variable = variable_llave(nombre)
            avisos.append(Aviso(
                f"llave:{nombre}:{lv:%Y-%m-%d}",
                f"pasarela-ia: la llave de servicio de la cuenta {nombre} vence el {fecha(lv.date())}. "
                f"Hay que crear otra en la consola y cambiar {variable} en el .env del servidor."))

    sin_llave = tuple(sorted(set(filas) - {f.nombre for f in config.fuentes}))
    con_tarjeta = frozenset(n for n, r in filas.items() if r["tiene_tarjeta"])
    foto = Foto(bool(filas), tuple(vivas), tuple(estados), frozenset(cortados), gasto_mes, dict(datos.topes),
                pagado_mes, datos.presupuesto, dict(datos.recibido), sin_llave,
                con_tarjeta, t, sin_datos=frozenset(sin_datos), bajas=datos.bajas)
    return foto, avisos


class Bolsa:
    """Lo que el proxy consulta. Todo sale de la última foto, en memoria: ninguna petición lee la base por esto."""

    def __init__(self, motor, config: Config, escribe_avisos: bool = True):
        self.motor = motor
        self.config = config
        self.escribe_avisos = escribe_avisos         # False en pasarela-ia-sacbe: calcula la foto y no escribe nada
        self.foto: Foto | None = None
        self.en_uso: str | None = None              # primera cuenta para producción en la última foto
        self._escritas: set[str] = set()            # claves de aviso que ya están en la tabla
        self._dichas: set[str] = set()              # cuentas sin llave ya anotadas en stderr

    def recalcular(self, agotadas: set[str], t: datetime | None = None) -> bool:
        t = t or ahora()
        try:
            foto, avisos = calcular(leer(self.motor, t), self.config, t)
        except Exception as e:  # noqa: BLE001  la foto anterior sigue sirviendo
            print(f"pasarela: no pude recalcular los saldos ({type(e).__name__})", file=sys.stderr)
            return False
        self.foto = foto
        for n in foto.sin_llave:
            if n not in self._dichas:
                self._dichas.add(n)
                print(f"pasarela: la cuenta {n} está en la tabla cuenta pero no en PIA_FUENTES; no se usa",
                      file=sys.stderr)
        nueva = self.primera("produccion", agotadas)
        if self.en_uso is not None and nueva is not None and nueva != self.en_uso:
            avisos.append(Aviso(f"en-uso:{t:%Y-%m-%d}:{self.en_uso}:{nueva}",
                                f"pasarela-ia: la cuenta en uso cambió de {self.en_uso} a {nueva}.",
                                {"accion": "cuenta-en-uso", "antes": self.en_uso, "ahora": nueva}))
        if nueva is not None:
            self.en_uso = nueva
        nuevos = [a for a in avisos if a.clave[:LARGO_CLAVE] not in self._escritas]
        if nuevos and self.escribe_avisos:
            try:
                self._escritas |= avisar(self.motor, nuevos)
            except Exception as e:  # noqa: BLE001  se reintenta en la siguiente vuelta
                print(f"pasarela: no pude escribir avisos ({type(e).__name__})", file=sys.stderr)
        return True

    def primera(self, clase: str, agotadas: set[str]) -> str | None:
        try:
            return self.ordenar(clase, agotadas)[0].fuente
        except SinCredito:
            return None

    def ordenar(self, clase: str, agotadas: set[str]) -> list[Candidata]:
        """Orden de cuentas para la clase. extra nunca incluye una cuenta con tarjeta, aunque esté conciliada."""
        f = self.foto
        if f is None or not f.con_cuentas:
            return ordenar(clase, self.config.fuentes, agotadas)
        return ordenar_por_saldo(clase, f.vivas, agotadas)

    def viva(self, nombre: str) -> CuentaViva | None:
        f = self.foto
        if f is None or not f.con_cuentas:
            return None
        return next((v for v in f.vivas if v.nombre == nombre), None)

    def tipo_de(self, fuente: str) -> str | None:
        """pagado si PIA_FUENTES o cualquier fila de la tabla (activa o no) dicen que tiene tarjeta; None si la cuenta
        no está configurada."""
        env = next((f.tipo for f in self.config.fuentes if f.nombre == fuente), None)
        if env is None:
            return None
        f = self.foto
        return "pagado" if env == "pagado" or (f is not None and fuente in f.tarjetas) else "gratis"

    def cortado(self, proyecto: str) -> bool:
        f = self.foto
        return f is not None and proyecto in f.cortados
