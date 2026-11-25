"""Página de costos del panel (/panel/costo): totales, barras apiladas por día y tabla, por modelo, programa o cuenta.

Sin JavaScript (la CSP del panel no lo permite): los filtros son enlaces y el detalle de cada barra sale en el
<title> del SVG, que el navegador muestra al pasar el cursor. Fechas en UTC, igual que la consola de Anthropic,
para poder comparar cifra con cifra.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from sqlalchemy import func, select

from .bolsa import MICRO
from .esquema import registro
from .identidad import Quien

RANGOS = (7, 30, 90)
AGRUPAR = {"modelo": registro.c.modelo, "programa": registro.c.proyecto, "cuenta": registro.c.fuente}
NOMBRE_AGRUPAR = {"modelo": "Modelo", "programa": "Programa", "cuenta": "Cuenta"}
SERIES_MAX = 7                     # la octava es «Otros»; nunca se generan colores nuevos
OTROS = "Otros"
SIN_MODELO = "(sin modelo)"


@dataclass(frozen=True)
class Filtros:
    dias: int = 30
    agrupar: str = "modelo"
    programa: str | None = None
    cuenta: str | None = None


def leer_filtros(q, visibles: set[str] | None) -> Filtros:
    """Toma los parámetros de la URL y descarta lo que no vale (sin error: se usa el valor por omisión)."""
    try:
        dias = int(q.get("dias", 30))
    except (TypeError, ValueError):
        dias = 30
    agrupar = q.get("agrupar", "modelo")
    programa = (q.get("programa") or "").strip() or None
    cuenta = (q.get("cuenta") or "").strip() or None
    if visibles is not None and programa is not None and programa not in visibles:
        programa = None
    return Filtros(dias if dias in RANGOS else 30, agrupar if agrupar in AGRUPAR else "modelo",
                   programa[:40] if programa else None, cuenta[:40] if cuenta else None)


@dataclass(frozen=True)
class Datos:
    agrupar: str
    desde: date
    hasta: date
    dias: tuple[date, ...]
    series: tuple[str, ...]                        # en orden de color; la última puede ser «Otros»
    color: dict[str, int]                          # serie -> ranura 1..8
    celdas: dict[tuple[date, str], int]            # (día, serie) -> micro
    totales_serie: dict[str, int]
    costo: int
    peticiones: int
    entrada: int
    salida: int
    cache_lectura: int
    cache_escritura: int
    programas: tuple[str, ...]                     # para los filtros
    cuentas: tuple[str, ...]


def _dia(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def leer(motor, f: Filtros, quien: Quien | None, t: datetime) -> Datos:
    hasta = t.date()
    desde = hasta - timedelta(days=f.dias - 1)
    inicio = datetime.combine(desde, datetime.min.time())
    columna = AGRUPAR[f.agrupar]
    dia = func.date(registro.c.ts)
    cond = [registro.c.ts >= inicio]
    visibles = None if quien is None or quien.ve_todo() else sorted(quien.proyectos)
    if visibles is not None:
        cond.append(registro.c.proyecto.in_(visibles or ["\x00"]))
    filtradas = list(cond)
    if f.programa:
        filtradas.append(registro.c.proyecto == f.programa)
    if f.cuenta:
        filtradas.append(registro.c.fuente == f.cuenta)
    with motor.connect() as c:
        filas = c.execute(select(dia.label("dia"), columna.label("serie"),
                                 func.coalesce(func.sum(registro.c.costo_micro), 0).label("micro"))
                          .where(*filtradas).group_by(dia, columna)).all()
        tot = c.execute(select(func.count(), func.coalesce(func.sum(registro.c.costo_micro), 0),
                               func.coalesce(func.sum(registro.c.entrada), 0),
                               func.coalesce(func.sum(registro.c.salida), 0),
                               func.coalesce(func.sum(registro.c.cache_lectura), 0),
                               func.coalesce(func.sum(registro.c.cache_5m + registro.c.cache_1h), 0))
                        .where(*filtradas)).one()
        # El color sigue a la serie, no a su lugar en el filtro: se reparte por gasto de 90 días sin filtrar.
        base90 = [registro.c.ts >= datetime.combine(hasta - timedelta(days=89), datetime.min.time())]
        if visibles is not None:
            base90.append(registro.c.proyecto.in_(visibles or ["\x00"]))
        orden = [r[0] or SIN_MODELO for r in c.execute(
            select(columna, func.sum(registro.c.costo_micro).label("m")).where(*base90)
            .group_by(columna).order_by(func.sum(registro.c.costo_micro).desc(), columna))]
        programas = tuple(sorted(r[0] for r in c.execute(select(registro.c.proyecto).where(*cond).distinct())))
        cuentas = tuple(sorted(r[0] for r in c.execute(select(registro.c.fuente).where(*cond).distinct())))
    color = {s: i + 1 for i, s in enumerate(orden[:SERIES_MAX])}
    celdas: dict[tuple[date, str], int] = {}
    totales: dict[str, int] = {}
    for d, s, m in filas:
        s = s or SIN_MODELO
        s = s if s in color else OTROS
        k = (_dia(d), s)
        celdas[k] = celdas.get(k, 0) + int(m)
        totales[s] = totales.get(s, 0) + int(m)
    if OTROS in totales:
        color[OTROS] = 8
    series = tuple(s for s in [*orden[:SERIES_MAX], OTROS] if totales.get(s))
    dias = tuple(desde + timedelta(days=i) for i in range(f.dias))
    return Datos(f.agrupar, desde, hasta, dias, series, color, celdas, totales, int(tot[1]), int(tot[0]), int(tot[2]),
                 int(tot[3]), int(tot[4]), int(tot[5]), programas, cuentas)


# ---------- dibujo ----------

MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def dia_corto(d: date) -> str:
    return f"{d.day} {MESES[d.month - 1]}"


def dinero(micro: int) -> str:
    v = micro / MICRO
    if v == 0:
        return "US$0"
    if v < 0.01:
        return "<US$0.01"
    return f"US${v:,.2f}"


def eje(micro: int) -> str:
    """Cifra corta para el eje: sin centavos cuando pasa de 10 dólares."""
    v = micro / MICRO
    return f"${v:,.0f}" if v >= 10 or v == 0 else f"${v:,.2f}"


def entero(n: int) -> str:
    return f"{n:,}"


def _escala(maximo: int) -> tuple[int, list[int]]:
    """Tope del eje y y sus marcas (4 divisiones con números redondos)."""
    if maximo <= 0:
        return MICRO, [0, MICRO // 4, MICRO // 2, 3 * MICRO // 4, MICRO]
    crudo = maximo / 4
    potencia = 10 ** (len(str(int(crudo))) - 1)
    for paso in (1, 2, 2.5, 5, 10):
        if paso * potencia >= crudo:
            break
    paso = int(paso * potencia) or 1
    return paso * 4, [paso * i for i in range(5)]


def grafica(d: Datos) -> str:
    ancho, alto, izq, abajo, arriba = 760, 240, 64, 26, 8
    alto_util = alto - abajo - arriba
    por_dia = {x: sum(d.celdas.get((x, s), 0) for s in d.series) for x in d.dias}
    tope, marcas = _escala(max(por_dia.values(), default=0))
    n = len(d.dias)
    hueco = (ancho - izq) / n
    barra = max(2.0, min(28.0, hueco * 0.7))
    t = [f'<svg class="grafica" viewBox="0 0 {ancho} {alto}" role="img" '
         f'aria-label="Costo diario por {NOMBRE_AGRUPAR[d.agrupar].lower()}">']
    for m in marcas:
        y = arriba + alto_util - alto_util * m / tope
        t.append(f'<line class="rejilla" x1="{izq}" x2="{ancho}" y1="{y:.1f}" y2="{y:.1f}"/>')
        t.append(f'<text class="eje" x="{izq - 8}" y="{y + 4:.1f}" text-anchor="end">{eje(m)}</text>')
    cada = 1 if n <= 10 else (5 if n <= 31 else 15)
    for i, x in enumerate(d.dias):
        cx = izq + hueco * i + hueco / 2
        if i % cada == 0 or i == n - 1:
            ancla = "end" if i == n - 1 and n > 10 else ("start" if i == 0 and n > 10 else "middle")
            xx = min(cx, ancho - 1) if ancla == "end" else (izq if ancla == "start" else cx)
            if ancla == "end":
                xx = ancho
            t.append(f'<text class="eje" x="{xx:.1f}" y="{alto - 6}" text-anchor="{ancla}">{dia_corto(x)}</text>')
        y = arriba + alto_util
        partes = [(s, d.celdas.get((x, s), 0)) for s in d.series if d.celdas.get((x, s), 0) > 0]
        detalle = "\n".join(f"{s}: {dinero(m)}" for s, m in sorted(partes, key=lambda p: -p[1]))
        titulo = f"{dia_corto(x)} (UTC) · total {dinero(por_dia[x])}" + (f"\n{detalle}" if detalle else "")
        t.append(f'<g class="dia"><title>{escape(titulo)}</title>')
        t.append(f'<rect class="blanco" x="{izq + hueco * i:.1f}" y="{arriba}" width="{hueco:.1f}" '
                 f'height="{alto_util}"/>')
        for s, m in partes:
            h = alto_util * m / tope
            if h < 0.6:
                h = 0.6
            y -= h
            t.append(f'<rect class="s{d.color[s]}" x="{cx - barra / 2:.1f}" y="{y:.1f}" width="{barra:.1f}" '
                     f'height="{h:.1f}"/>')
        t.append("</g>")
    t.append(f'<line class="base" x1="{izq}" x2="{ancho}" y1="{arriba + alto_util}" y2="{arriba + alto_util}"/>')
    t.append("</svg>")
    return "".join(t)


def leyenda(d: Datos) -> str:
    if not d.series:
        return ""
    t = ['<ul class="leyenda">']
    for s in d.series:
        t.append(f'<li><span class="muestra s{d.color[s]}"></span><span>{escape(s)}</span>'
                 f'<span class="cifra suave">{dinero(d.totales_serie.get(s, 0))}</span></li>')
    return "".join(t) + "</ul>"


def tabla(d: Datos) -> str:
    cab = "".join(f"<th>{escape(s)}</th>" for s in d.series)
    filas = []
    for x in reversed(d.dias):
        vals = [d.celdas.get((x, s), 0) for s in d.series]
        if not any(vals):
            continue
        celdas = "".join(f'<td class="cifra">{dinero(v) if v else "·"}</td>' for v in vals)
        filas.append(f'<tr><th scope="row">{dia_corto(x)}</th>{celdas}<td class="cifra">{dinero(sum(vals))}</td></tr>')
    if not filas:
        return ""
    total = "".join(f'<td class="cifra">{dinero(d.totales_serie.get(s, 0))}</td>' for s in d.series)
    return (f'<details class="tabla"><summary>Ver tabla</summary><div class="desliza"><table>'
            f'<thead><tr><th>Fecha (UTC)</th>{cab}<th>Total</th></tr></thead><tbody>{"".join(filas)}</tbody>'
            f'<tfoot><tr><th scope="row">Total</th>{total}<td class="cifra">{dinero(d.costo)}</td></tr></tfoot>'
            f'</table></div></details>')


def _enlace(f: Filtros, etiqueta: str, activo: bool, **cambio) -> str:
    datos = {"dias": f.dias, "agrupar": f.agrupar, "programa": f.programa, "cuenta": f.cuenta}
    datos.update(cambio)
    q = urlencode({k: v for k, v in datos.items() if v})
    clase = ' class="activo" aria-current="true"' if activo else ""
    return f'<a{clase} href="/panel/costo?{escape(q)}">{escape(etiqueta)}</a>'


def filtros(f: Filtros, d: Datos) -> str:
    rangos = "".join(_enlace(f, f"{n} días", f.dias == n, dias=n) for n in RANGOS)
    grupos = "".join(_enlace(f, NOMBRE_AGRUPAR[g], f.agrupar == g, agrupar=g) for g in AGRUPAR)
    progs = _enlace(f, "Todos", f.programa is None, programa=None) + "".join(
        _enlace(f, p, f.programa == p, programa=p) for p in d.programas)
    ctas = _enlace(f, "Todas", f.cuenta is None, cuenta=None) + "".join(
        _enlace(f, c, f.cuenta == c, cuenta=c) for c in d.cuentas)
    return (f'<nav class="filtros" aria-label="Filtros">'
            f'<div><span class="suave">Rango</span>{rangos}</div>'
            f'<div><span class="suave">Agrupar por</span>{grupos}</div>'
            f'<div><span class="suave">Programa</span>{progs}</div>'
            f'<div><span class="suave">Cuenta</span>{ctas}</div></nav>')


def mosaicos(d: Datos) -> str:
    def m(nombre, valor):
        return f'<div class="mosaico"><span class="suave">{nombre}</span><span class="grande cifra">{valor}</span></div>'
    return ('<div class="mosaicos">' + m("Costo total", dinero(d.costo)) + m("Peticiones", entero(d.peticiones))
            + m("Tokens de entrada", entero(d.entrada)) + m("Tokens de salida", entero(d.salida))
            + m("Lectura de caché", entero(d.cache_lectura)) + "</div>")


ESTILO_COSTO = """
main.ancho{max-width:1040px}
.volver{font-size:.9rem;color:var(--acento);text-decoration:none}
.filtros{display:flex;flex-direction:column;gap:8px;margin:0 0 18px}
.filtros div{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.filtros span{font-size:.8rem;min-width:88px}
.filtros a{font-size:.85rem;text-decoration:none;color:var(--texto);border:1px solid var(--linea);
border-radius:999px;padding:3px 11px;background:var(--tarjeta)}
.filtros a.activo{background:var(--acento);border-color:var(--acento);color:var(--sobre-acento);font-weight:600}
.mosaicos{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 16px}
.mosaico{background:var(--tarjeta);border:1px solid var(--linea);border-radius:14px;padding:12px 14px;
display:flex;flex-direction:column;gap:4px}
.mosaico .suave{font-size:.8rem}
.grande{font-size:1.35rem;font-weight:650}
.grafica{display:block;width:100%;height:auto}
.grafica .rejilla{stroke:var(--linea);stroke-width:1}
.grafica .base{stroke:var(--suave);stroke-width:1}
.grafica .eje{fill:var(--suave);font-size:11px;font-variant-numeric:tabular-nums}
.grafica .blanco{fill:transparent}
.grafica .dia:hover .blanco{fill:var(--pista)}
.grafica rect:not(.blanco){stroke:var(--tarjeta);stroke-width:1}
.leyenda{list-style:none;display:flex;flex-wrap:wrap;gap:6px 18px;margin:12px 0 0;padding:0;font-size:.85rem}
.leyenda li{display:flex;align-items:center;gap:6px}
.muestra{width:10px;height:10px;border-radius:3px;display:inline-block}
.s1{fill:#2a78d6;background:#2a78d6}.s2{fill:#eb6834;background:#eb6834}.s3{fill:#1baf7a;background:#1baf7a}
.s4{fill:#eda100;background:#eda100}.s5{fill:#e87ba4;background:#e87ba4}.s6{fill:#008300;background:#008300}
.s7{fill:#6250d6;background:#6250d6}.s8{fill:#8c8b85;background:#8c8b85}
@media (prefers-color-scheme:dark){
.s1{fill:#3987e5;background:#3987e5}.s2{fill:#d95926;background:#d95926}.s3{fill:#199e70;background:#199e70}
.s4{fill:#c98500;background:#c98500}.s5{fill:#d55181;background:#d55181}.s6{fill:#008300;background:#008300}
.s7{fill:#9085e9;background:#9085e9}.s8{fill:#8e8d87;background:#8e8d87}}
.tabla{margin:14px 0 0}
.desliza{overflow-x:auto;margin:10px 0 0}
table{border-collapse:collapse;font-size:.85rem;width:100%}
th,td{padding:6px 10px;border-bottom:1px solid var(--linea);text-align:right;white-space:nowrap}
th:first-child{text-align:left}
thead th{color:var(--suave);font-weight:600}
tfoot th,tfoot td{font-weight:650}
"""


def pagina(f: Filtros, d: Datos, quien_txt: str, hora_txt: str, titulo: str, estilo: str, iconos: str) -> str:
    if d.costo or d.peticiones:
        cuerpo = (f'<section class="tarjeta">{grafica(d)}{leyenda(d)}</section>{tabla(d)}')
    else:
        cuerpo = '<p class="vacio">No hay peticiones en este rango con estos filtros.</p>'
    rango = f"{dia_corto(d.desde)} al {dia_corto(d.hasta)} (fechas en UTC, como la consola de Anthropic)"
    return f"""<!doctype html>
<html lang="es-MX">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Costos · {titulo}</title>
<style>{estilo}{ESTILO_COSTO}</style>
</head>
<body>
{iconos}
<main class="ancho">
<header>
<a class="volver" href="/panel">← Volver al panel</a>
<h1>Costos</h1>
<p class="quien">{escape(quien_txt)}</p>
<p class="hora">{escape(hora_txt)}</p>
</header>
{filtros(f, d)}
{mosaicos(d)}
<p class="ayuda">{escape(rango)}. Pasa el cursor sobre una barra para ver el detalle del día.</p>
{cuerpo}
</main>
</body>
</html>
"""
