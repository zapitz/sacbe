"""Panel de créditos de IA. Solo lectura: HTML armado en el servidor con SVG
en línea, sin JavaScript ni frameworks, claro u oscuro según el aparato, legible en iPhone.
Es puro: recibe la foto de la bolsa y lo que se sabe de la salud y regresa texto. La ruta vive en app.py y solo
existe en el contenedor del panel (PIA_PANEL=1). Todo texto que viene de la base o de otro contenedor pasa por
escape()."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from html import escape

from .acciones import APLICA, UNA_VEZ
from .bolsa import MICRO, Foto, al_80, al_dia, cuadre, fecha, pagado_visible, usd
from .identidad import Quien
from .orden import UMBRAL_MICRO
from .tiempo import NOMBRE_ZONA, en_local

ROJO_PERDIDA = 20 * MICRO             # rojo si se pierden más de US$20
DIAS_ROJO = 3                         # rojo si faltan menos de 3 días y queda saldo (más de US$1)
SIN_FOTO = "Todavía no hay cálculo de saldos."
TITULO = "Sacbé · créditos de IA"
REFRESCO_S = 60                       # la página se vuelve a pedir sola (meta refresh, sin JavaScript)


@dataclass(frozen=True)
class Barra:
    id: int
    cuenta: str
    monto: int
    restante: int
    perdida: int                      # 0 si la cuenta no tiene datos
    vence: date
    dias: int
    confirmada: bool
    color: str                        # verde | ambar | rojo | gris
    en_uso: bool
    cuadre: str
    en_saldo: bool = True             # False: estimada en una cuenta con tarjeta, se ve en gris y no suma
    ritmo: int | None = None          # US$ al día con que se calculó la pérdida (mismo ritmo que la proyección)


@dataclass(frozen=True)
class Programa:
    nombre: str
    gasto: int
    tope: int | None
    modo: str | None
    cortado: bool
    color: str
    huella: str | None = None         # últimos 4 de la llave: solo dueño y responsable
    clase: str | None = None          # produccion | extra, para el formulario del dueño


@dataclass(frozen=True)
class Proxima:
    id: int
    cuenta: str
    monto: int
    otorgada: date
    vence: date
    confirmada: bool


@dataclass(frozen=True)
class Salud:
    bien: bool
    texto: str


@dataclass(frozen=True)
class UsuarioV:
    correo: str
    nombre: str
    papel: str
    activo: bool
    proyectos: tuple[str, ...]


@dataclass(frozen=True)
class Acciones:
    """Lo que hace falta para dibujar los formularios. Solo existe con PIA_PANEL_SECRETO y para dueño o responsable."""
    ficha: str
    dueno: bool
    rotables: frozenset[str]          # proyectos cuya llave puede rotar quien ve
    proyectos: tuple[str, ...]        # activos, para elegir en los formularios del dueño


@dataclass(frozen=True)
class Vista:
    t: datetime
    hay_foto: bool
    barras: tuple[Barra, ...]
    programas: tuple[Programa, ...]
    pagado: int | None                # None: ninguna cuenta con tarjeta se ha cuadrado nunca
    pagado_nota: str
    presupuesto: int | None
    tipo_cambio: Decimal
    en_uso: str | None
    en_uso_tarjeta: bool
    proximas: tuple[Proxima, ...]
    salud: tuple[Salud, ...]
    tipo_cambio_supuesto: bool = False  # True: falta PIA_TIPO_CAMBIO y se usa 20
    quien: str = "Panel interno · dueño"    # lo que se ve arriba: el correo y el papel, o el panel interno
    completa: bool = True               # False: responsable o lector, que solo ven sus proyectos
    acciones: Acciones | None = None    # None: sin formularios (panel interno, finanzas, lector, o falta PIA_PANEL_SECRETO)
    usuarios: tuple[UsuarioV, ...] | None = None    # solo el dueño (también en el panel interno, sin botones)
    nota: str = ""                      # arriba de todo; en el panel interno dice dónde se hacen los cambios


def color_de(restante: int, perdida: int, dias: int, sin_datos: bool) -> str:
    """Verde si se usa todo, ámbar si se pierde algo, rojo si se pierden más de US$20 o faltan
    menos de 3 días con saldo. Gris si la cuenta nunca se ha cuadrado: no se sabe."""
    if sin_datos:
        return "gris"
    if perdida > ROJO_PERDIDA or (dias < DIAS_ROJO and restante > UMBRAL_MICRO):
        return "rojo"
    return "ambar" if perdida > 0 else "verde"


def plural(n: int, uno: str, varios: str) -> str:
    return uno if n == 1 else varios


PAPELES = {"dueno": "dueño", "finanzas": "finanzas", "responsable": "responsable", "lector": "lector"}


NOTA_SIN_HOST = "Para hacer cambios entra al panel con login."


def nota_interna(host: str) -> str:
    """La nota del panel interno: dónde se hacen los cambios, con el host configurado en PIA_SACBE_HOST."""
    return f"Para hacer cambios entra a https://{host}" if host else NOTA_SIN_HOST


def quien_texto(q: Quien | None) -> str:
    if q is None or q.puerta == "interno":
        return "Panel interno · dueño"
    return f"{q.correo} · {PAPELES.get(q.papel, q.papel)}"


def _acciones(quien: Quien | None, proyectos: list[dict] | None, usuarios: list[dict] | None,
              ficha: str | None) -> Acciones | None:
    dueno = quien is None or quien.papel == "dueno"
    if not ficha or proyectos is None or not (dueno or quien.papel == "responsable"):
        return None
    activos = tuple(p["nombre"] for p in proyectos if p.get("activo"))
    rotables = frozenset(activos if dueno else (n for n in activos if n in quien.proyectos))
    return Acciones(ficha, dueno, rotables, activos)


def armar_vista(foto: Foto | None, en_uso: str | None, salud: tuple[Salud, ...], tipo_cambio: Decimal,
                t: datetime, tipo_cambio_supuesto: bool = False, quien: Quien | None = None,
                proyectos: list[dict] | None = None, usuarios: list[dict] | None = None,
                ficha: str | None = None, host: str = "") -> Vista:
    """`quien` None es el panel interno (dueño). Dueño y finanzas ven todo; responsable y lector (y cualquier
    papel que no se conozca) solo el gasto de sus proyectos: sin concesiones, pagado, próximas ni salud del gestor.
    `proyectos` (admin.proyectos) da la huella de la llave, que solo ven dueño y responsable, y hace que el dueño vea
    también los proyectos activos sin gasto ni tope (para poder cambiarlos). `ficha` y `usuarios` arman los formularios
    del dueño y del responsable; sin ficha (falta PIA_PANEL_SECRETO, o es el panel interno, que es solo de lectura) el
    panel solo muestra. `host` es PIA_SACBE_HOST, para la nota del panel interno. El dueño ve la lista de usuarios
    con o sin ficha."""
    ve = quien is None or quien.ve_todo()
    texto = quien_texto(quien)
    acciones = _acciones(quien, proyectos, usuarios, ficha)
    es_dueno = quien is None or quien.papel == "dueno"
    extra = {"usuarios": tuple(UsuarioV(u["correo"], u["nombre"], u["papel"], bool(u["activo"]), tuple(u["proyectos"]))
                               for u in usuarios) if es_dueno and usuarios is not None else None,
             "nota": nota_interna(host) if quien is None or quien.puerta == "interno" else ""}
    if foto is None:
        return Vista(t, False, (), (), None, "", None, tipo_cambio, None, False, (), salud if ve else (),
                     tipo_cambio_supuesto, texto, ve, acciones, **extra)
    hoy = t.date()
    tarjeta = {v.nombre: v.tarjeta for v in foto.vivas}
    vigentes = sorted((e for e in foto.concesiones if e.cuenta in tarjeta and e.otorgada <= hoy < e.vence),
                      key=lambda e: (e.vence, e.id))
    gastando = next((e.id for e in vigentes if e.cuenta == en_uso and e.cuenta_en_saldo and e.restante_micro > 0),
                    None)
    barras = []
    for e in vigentes:
        # la estimada de una cuenta con tarjeta se ve, en gris y sin pérdida, porque no entra al saldo.
        sin = e.cuenta in foto.sin_datos or not e.cuenta_en_saldo
        dias = (e.vence - hoy).days
        barras.append(Barra(e.id, e.cuenta, e.monto_micro, e.restante_micro, 0 if sin else e.perdida_micro, e.vence,
                            dias, e.confirmada, color_de(e.restante_micro, e.perdida_micro, dias, sin),
                            e.id == gastando, cuadre(foto.conciliada.get(e.cuenta), foto.ts), e.cuenta_en_saldo,
                            e.ritmo_micro))
    programas = []
    # Quien solo ve lo suyo ve también sus proyectos sin gasto este mes (con US$0.00), no solo los que ya gastaron.
    suyos = quien.proyectos if quien is not None and not ve else frozenset()
    dueno = quien is None or quien.papel == "dueno"
    con_huella = dueno or quien.papel == "responsable"
    por_nombre = {x["nombre"]: x for x in proyectos or ()}
    if dueno:                           # el dueño ve todos los activos, para poder cambiarlos aunque no hayan gastado
        suyos = frozenset(n for n, x in por_nombre.items() if x.get("activo"))
    for p in sorted(set(foto.gasto_mes) | set(foto.topes) | suyos, key=lambda p: (-foto.gasto_mes.get(p, 0), p)):
        g = foto.gasto_mes.get(p, 0)
        if p in foto.bajas:                 # dado de baja: no se lista, aunque haya gastado (el gasto sigue en la foto)
            continue
        if p in por_nombre and not por_nombre[p].get("activo"):     # dado de baja después de la última foto
            continue
        if quien is not None and not quien.ve_proyecto(p):
            continue
        lim, modo = foto.topes.get(p, (None, None))
        cortado = p in foto.cortados
        if cortado or (lim and g >= lim):
            color = "rojo"
        elif lim and al_80(g, lim):
            color = "ambar"
        else:
            color = "verde"
        datos = por_nombre.get(p) or {}
        programas.append(Programa(p, g, lim, modo, cortado, color, datos.get("huella") if con_huella else None,
                                  datos.get("clase")))
    if not ve:
        return Vista(t, True, (), tuple(programas), None, "", None, tipo_cambio, None, False, (), (),
                     tipo_cambio_supuesto, texto, False, acciones, **extra)
    pagado, sin = pagado_visible(foto)
    if pagado is None:
        nota = f"{', '.join(sin)} nunca {plural(len(sin), 'se ha', 'se han')} cuadrado con Anthropic."
    else:
        nota = f"No cuenta {', '.join(sin)}: sin datos." if sin else ""
    proximas = tuple(Proxima(e.id, e.cuenta, e.monto_micro, e.otorgada, e.vence, e.confirmada)
                     for e in sorted(foto.concesiones, key=lambda e: (e.otorgada, e.id))
                     if e.cuenta in tarjeta and e.otorgada > hoy)
    return Vista(t, True, tuple(barras), tuple(programas), pagado, nota, foto.presupuesto_micro, tipo_cambio,
                 en_uso, bool(en_uso and tarjeta.get(en_uso)), proximas, salud, tipo_cambio_supuesto, texto, True,
                 acciones, **extra)


def hace(segundos: int) -> str:
    s = max(0, segundos)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    d = s // 86400
    return "1 día" if d == 1 else f"{d} días"


def salud_de(api: dict | None, base: bool, foto: Foto | None, orden_s: float, lista_ts: datetime | None,
             n_modelos: int, sin_precio: tuple[str, ...], catalogo_s: float, t: datetime) -> tuple[Salud, ...]:
    """`api` es el /salud de pasarela-ia-api (None si no contestó); lo demás es del propio panel."""
    s = []
    if api is None:
        s.append(Salud(False, "El gestor no contesta. Las apps que pasan por él pueden estar fallando."))
    else:
        bien = api.get("ok") is True and api.get("base") is True
        n = api.get("proyectos", 0)
        s.append(Salud(bien, f"El gestor contesta y tiene {n} {plural(n, 'programa', 'programas')} con llave." if bien
                       else "El gestor contesta pero no está al día con la base."))
        agotadas = api.get("agotadas") or []
        if agotadas:
            quedaron = plural(len(agotadas), "se quedó", "se quedaron")
            s.append(Salud(False, f"Anthropic dice que {quedaron} sin crédito: {', '.join(map(str, agotadas))}."))
        fallas = api.get("fallas_registro") or 0
        if fallas:
            perdidas = plural(fallas, "Se perdió 1 fila", f"Se perdieron {fallas} filas")
            s.append(Salud(False, f"{perdidas} de registro desde que arrancó."))
    s.append(Salud(base, "La base responde." if base else "La base no responde."))
    if foto is None:
        s.append(Salud(False, SIN_FOTO))
    else:
        edad = int((t - foto.ts).total_seconds())
        s.append(Salud(al_dia(edad, orden_s), f"Saldos calculados hace {hace(edad)}."))
    if lista_ts is None:
        s.append(Salud(False, "Todavía no hay catálogo de modelos."))
    else:
        edad = int((t - lista_ts).total_seconds())
        s.append(Salud(catalogo_s <= 0 or edad < 2 * catalogo_s + 600,
                       f"Catálogo de modelos: {n_modelos} con precio, leído hace {hace(edad)}."))
    if sin_precio:
        nuevos = plural(len(sin_precio), "Modelo nuevo", "Modelos nuevos")
        s.append(Salud(False, f"{nuevos} sin precio: {', '.join(sin_precio)}."))
    return tuple(s)


ESTILO = """
:root{color-scheme:light dark;--fondo:#f5f4f0;--tarjeta:#fff;--texto:#1c1c1a;--suave:#6a6964;--linea:#e2e0d9;
--pista:#ebe9e3;--verde:#2e7d4f;--ambar:#a86400;--rojo:#c0392b;--gris:#8c8b85;--acento:#2856a3;--sobre-acento:#fff}
@media (prefers-color-scheme:dark){:root{--fondo:#141412;--tarjeta:#1e1e1b;--texto:#ecebe5;--suave:#a2a19a;
--linea:#33322d;--pista:#2b2a26;--verde:#52b67d;--ambar:#e3a33a;--rojo:#ec6b5e;--gris:#8e8d87;--acento:#86aefc;
--sobre-acento:#10213f}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--fondo);color:var(--texto);
font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
main{max-width:720px;margin:0 auto;padding:20px 16px 48px}
header{margin:0 0 24px}
h1{font-size:1.5rem;line-height:1.2;margin:0 0 4px}
h2{display:flex;align-items:center;gap:8px;font-size:.95rem;font-weight:600;color:var(--suave);margin:0 0 10px}
section{margin:0 0 28px}
.hora,.ayuda,.suave,.quien{color:var(--suave)}
.quien{font-size:.9rem;margin:0 0 4px}
.hora{display:flex;align-items:center;gap:6px;font-size:.9rem;margin:0}
.ayuda{font-size:.85rem;margin:-4px 0 10px}
.tarjeta .ayuda{margin:8px 0 0}
.tarjeta{background:var(--tarjeta);border:1px solid var(--linea);border-radius:14px;padding:14px 16px;margin:0 0 10px}
.fila{display:flex;justify-content:space-between;align-items:baseline;gap:4px 12px;flex-wrap:wrap}
.nombre{font-weight:600}
.cifra{font-variant-numeric:tabular-nums}
.icono{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;
stroke-linejoin:round}
.barra{display:block;width:100%;height:12px;margin:10px 0 8px;border-radius:6px;overflow:hidden}
.barra .pista{fill:var(--pista)}
.barra .pierde{fill-opacity:.32}
@media (prefers-color-scheme:dark){.barra .pierde{fill-opacity:.5}}
.verde .uso,.verde .pierde{fill:var(--verde)}
.ambar .uso,.ambar .pierde{fill:var(--ambar)}
.rojo .uso,.rojo .pierde{fill:var(--rojo)}
.gris .uso,.gris .pierde{fill:var(--gris)}
.texto-verde{color:var(--verde)}
.texto-ambar{color:var(--ambar)}
.texto-rojo{color:var(--rojo)}
.texto-gris{color:var(--suave)}
.insignia{font-size:.75rem;font-weight:700;letter-spacing:.02em;padding:2px 9px;border-radius:999px;
background:var(--acento);color:var(--sobre-acento)}
.etiqueta{font-size:.8rem;color:var(--suave);border:1px solid var(--linea);border-radius:999px;padding:1px 9px;
white-space:nowrap}
.lista{list-style:none;margin:0;padding:4px 16px}
.lista li{display:flex;align-items:flex-start;gap:10px;padding:10px 0;border-top:1px solid var(--linea)}
.lista li:first-child{border-top:0}
.lista .bien{color:var(--verde)}
.lista .mal{color:var(--rojo)}
.vacio{color:var(--suave);font-style:italic;margin:0}
.huella{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
.acciones{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 0}
.acciones details[open]{flex-basis:100%}
summary{display:inline-block;list-style:none;cursor:pointer;font-size:.85rem;color:var(--acento);
border:1px solid var(--linea);border-radius:999px;padding:3px 11px}
summary::-webkit-details-marker{display:none}
.peligro summary{color:var(--rojo)}
.forma{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 0}
.forma label{display:flex;align-items:center;gap:6px;font-size:.9rem}
input,select,button{font:inherit;font-size:.9rem;padding:6px 10px;border-radius:8px;border:1px solid var(--linea);
background:var(--tarjeta);color:var(--texto);max-width:100%}
input[type=checkbox]{width:18px;height:18px;padding:0}
button{background:var(--acento);border-color:var(--acento);color:var(--sobre-acento);font-weight:600;cursor:pointer}
.peligro button{background:var(--rojo);border-color:var(--rojo);color:#fff}
.llave{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.95rem;word-break:break-all;
user-select:all;-webkit-user-select:all;background:var(--pista);border-radius:8px;padding:12px;margin:12px 0}
.aviso{font-weight:600;color:var(--rojo)}
.enlaces{margin:8px 0 0;font-size:.9rem}
.enlaces a{color:var(--acento)}
@media (max-width:420px){body{font-size:15px}.tarjeta{padding:12px 14px}}
"""

ICONOS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true">
<symbol id="i-saldo" viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/>
<path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/></symbol>
<symbol id="i-programas" viewBox="0 0 24 24"><path d="M4 6h16M4 12h11M4 18h7"/></symbol>
<symbol id="i-tarjeta" viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/>
<path d="M3 10h18M7 15h4"/></symbol>
<symbol id="i-calendario" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="15" rx="2"/>
<path d="M4 10h16M9 3v4M15 3v4"/></symbol>
<symbol id="i-servidor" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="7" rx="1.5"/>
<rect x="4" y="13" width="16" height="7" rx="1.5"/><path d="M8 7.5h.01M8 16.5h.01"/></symbol>
<symbol id="i-reloj" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
<symbol id="i-bien" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></symbol>
<symbol id="i-alerta" viewBox="0 0 24 24"><path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17h.01"/></symbol>
<symbol id="i-usuarios" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>
<path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14.3c2.1.7 3.5 2.8 3.5 5.7"/></symbol>
<symbol id="i-mas" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></symbol>
<symbol id="i-llave" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M16 7l3 3"/></symbol>
</svg>"""


def icono(nombre: str) -> str:
    return f'<svg class="icono" aria-hidden="true"><use href="#i-{nombre}"/></svg>'


def svg_barra(total: int, lleno: int, claro: int, color: str, etiqueta: str) -> str:
    """Barra de 0 a `total`: `lleno` en color sólido y, pegado a su derecha, `claro` (parte de `lleno`) en el mismo
    color más tenue. Las medidas van en por ciento del ancho; lo que pasa de `total` se corta."""
    total = max(total, 1)
    claro = max(0, min(claro, lleno))
    solido = min(100.0, max(0, lleno - claro) * 100 / total)
    tenue = min(100.0 - solido, claro * 100 / total)
    return (f'<svg class="barra {color}" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" '
            f'aria-label="{escape(etiqueta)}"><rect class="pista" width="100" height="10"/>'
            f'<rect class="uso" width="{solido:.2f}" height="10"/>'
            f'<rect class="pierde" x="{solido:.2f}" width="{tenue:.2f}" height="10"/></svg>')


def _concesion(b: Barra) -> str:
    insignia = '<span class="insignia">en uso</span>' if b.en_uso else ""
    falta = "falta 1 día" if b.dias == 1 else f"faltan {b.dias} días"
    etiqueta = b.cuadre
    if not b.en_saldo:
        pierde, etiqueta = "Estimada en una cuenta con tarjeta", "no entra al saldo"
    else:
        if b.color == "gris":
            pierde = "sin datos para saber cuánto se pierde"
        elif b.perdida > 0:
            pierde = f"se pierden {usd(b.perdida)}"
        else:
            pierde = "se usa completa"
        # El ritmo es el de la proyección (promedio de los 7 días anteriores a hoy), no uno aparte.
        pierde = f"a {usd(b.ritmo)} al día (últimos 7 días): {pierde}" if b.ritmo is not None else pierde[0].upper() + pierde[1:]
    por_confirmar = "" if b.confirmada else " · por confirmar"
    # la estimada de tarjeta dibuja lo que queda en el tono tenue, para no confundirla con «sin datos».
    claro = b.perdida if b.en_saldo else b.restante
    return (f'<article class="tarjeta">\n'
            f'<div class="fila"><span class="nombre">{escape(b.cuenta)} '
            f'<span class="suave">#{b.id}{por_confirmar}</span></span>{insignia}</div>\n'
            f'{svg_barra(b.monto, b.restante, claro, b.color, f"Quedan {usd(b.restante)} de {usd(b.monto)}")}\n'
            f'<div class="fila"><span class="cifra">{usd(b.restante)} de {usd(b.monto)}</span>'
            f'<span class="suave">vence el {fecha(b.vence)} · {falta}</span></div>\n'
            f'<div class="fila"><span class="texto-{b.color}">{pierde}</span>'
            f'<span class="etiqueta">{escape(etiqueta)}</span></div>\n</article>')


def _concesiones(v: Vista) -> str:
    t = [f'<section>\n<h2>{icono("saldo")}Concesiones vigentes</h2>']
    if not v.hay_foto:
        t.append(f'<p class="vacio">{SIN_FOTO}</p>')
    elif not v.barras:
        t.append('<p class="vacio">No hay concesiones vigentes.</p>')
    else:
        t.append('<p class="ayuda">La barra es lo que queda de cada concesión. La parte clara es lo que se perdería '
                 'al ritmo de gasto de los últimos 7 días.</p>')
        t += [_concesion(b) for b in v.barras]
    t.append("</section>")
    return "\n".join(t)


CLASES = (("produccion", "Puede usar tarjeta"), ("extra", "Solo crédito gratis"))
PAPELES_FORMA = (("dueno", "dueño"), ("finanzas", "finanzas"), ("responsable", "responsable"), ("lector", "lector"))


def _oculto(nombre: str, valor: str) -> str:
    return f'<input type="hidden" name="{nombre}" value="{escape(valor)}">'


def _forma(a: Acciones, accion: str, cuerpo: str, boton: str, ocultos: tuple = ()) -> str:
    """Un formulario POST a /panel/accion con la acción, la ficha del día y los campos fijos."""
    fijos = "".join(_oculto(k, x) for k, x in (("accion", accion), ("ficha", a.ficha), *ocultos))
    return (f'<form class="forma" method="post" action="/panel/accion">{fijos}{cuerpo}'
            f'<button type="submit">{escape(boton)}</button></form>')


def _cambio(a: Acciones, resumen: str, accion: str, cuerpo: str, boton: str, ocultos: tuple = ()) -> str:
    return f'<details><summary>{escape(resumen)}</summary>{_forma(a, accion, cuerpo, boton, ocultos)}</details>'


def _peligro(a: Acciones, resumen: str, accion: str, boton: str, ocultos: tuple = (), cuerpo: str = "") -> str:
    """Baja, rotar y quitar: dentro de un <details> y con la casilla «confirmo» obligatoria (no hay JavaScript)."""
    casilla = '<label><input type="checkbox" name="confirmo" value="si" required>confirmo</label>'
    return (f'<details class="peligro"><summary>{escape(resumen)}</summary>'
            f'{_forma(a, accion, cuerpo + casilla, boton, ocultos)}</details>')


def _elegir(nombre: str, etiqueta: str, opciones, elegido: str | None = None, requerido: bool = True) -> str:
    """Un <select>. Con una opción de valor vacío va sin `required`: el navegador la tomaría por «sin elegir»."""
    ops = "".join(f'<option value="{escape(x)}"{" selected" if x == elegido else ""}>{escape(texto)}</option>'
                  for x, texto in opciones)
    req = " required" if requerido else ""
    return f'<label>{escape(etiqueta)}<select name="{nombre}"{req}>{ops}</select></label>'


def _acciones_programa(a: Acciones, p: Programa) -> str:
    fijo = (("proyecto", p.nombre),)
    t = []
    if a.dueno:
        t.append(_cambio(a, "Clase", "proyecto-clase", _elegir("clase", "Clase", CLASES, p.clase), "Cambiar clase",
                         fijo))
        modos = (("", "según su clase"), ("aviso", "solo avisa"), ("corte", "se corta"))
        tope = (f'<label>US$<input name="usd" inputmode="decimal" size="7" required '
                f'value="{escape(str(Decimal(p.tope) / MICRO)) if p.tope else ""}"></label>'
                f'{_elegir("modo", "Al llegar", modos, p.modo, requerido=False)}')
        t.append(_cambio(a, "Tope", "tope-poner", tope, "Poner tope", fijo))
        if p.tope:
            t.append(_peligro(a, "Quitar tope", "tope-quitar", "Quitar el tope", fijo))
    if p.nombre in a.rotables:
        t.append(_peligro(a, "Rotar la llave", "proyecto-rotar", "Rotar y ver la llave nueva", fijo))
    if a.dueno:
        t.append(_peligro(a, "Dar de baja", "proyecto-baja", "Dar de baja el proyecto", fijo))
    return f'<div class="acciones">{"".join(t)}</div>\n' if t else ""


def _programas(v: Vista) -> str:
    t = [f'<section>\n<h2>{icono("programas")}Gasto del mes por programa</h2>']
    if v.acciones is not None:
        t.append(f'<p class="ayuda">Cada cambio de un proyecto {APLICA}.</p>')
    if not v.hay_foto:                # sin foto no se sabe, no se afirma que nadie gastó
        t.append(f'<p class="vacio">{SIN_FOTO}</p>')
    elif not v.programas:
        t.append('<p class="vacio">Ningún programa ha gastado este mes.</p>' if v.completa
                 else '<p class="vacio">No tienes proyectos asignados.</p>')
    for p in v.programas:
        if p.tope:
            modo = "se corta al llegar" if p.modo == "corte" else "solo avisa"
            estado = ("cortado hasta el día 1" if p.cortado
                      else f"{p.gasto * 100 // p.tope}% de su tope de {usd(p.tope)}, {modo}")
            barra = svg_barra(p.tope, p.gasto, 0, p.color, f"{usd(p.gasto)} de {usd(p.tope)}") + "\n"
        else:
            estado, barra = "sin tope", ""
        huella = f'<span class="suave huella">llave ····{escape(p.huella)}</span>' if p.huella else ""
        acciones = _acciones_programa(v.acciones, p) if v.acciones is not None else ""
        t.append(f'<article class="tarjeta">\n<div class="fila"><span class="nombre">{escape(p.nombre)}</span>'
                 f'<span class="cifra">{usd(p.gasto)}</span></div>\n{barra}'
                 f'<div class="fila"><span class="suave texto-{p.color if p.cortado else "gris"}">{estado}</span>'
                 f'{huella}</div>\n{acciones}</article>')
    t.append("</section>")
    return "\n".join(t)


def _nuevo_proyecto(a: Acciones) -> str:
    cuerpo = (f'<label>Nombre<input name="nombre" required pattern="[a-z][a-z0-9-]{{1,30}}" maxlength="31" '
              f'autocomplete="off" autocapitalize="none" spellcheck="false"></label>'
              f'{_elegir("clase", "Clase", CLASES, "extra")}')
    return (f'<section>\n<h2>{icono("mas")}Nuevo proyecto</h2>\n<article class="tarjeta">\n'
            f'<p class="ayuda">Minúsculas, números y guiones. La llave se muestra una sola vez al crearlo.</p>\n'
            f'{_forma(a, "proyecto-alta", cuerpo, "Crear y ver la llave")}\n</article>\n</section>')


def _usuario(a: Acciones | None, u: UsuarioV) -> str:
    """La tarjeta de un usuario; con `a` None (panel interno o sin ficha), sin botones."""
    fijo = (("correo", u.correo),)
    proyectos = ", ".join(u.proyectos) if u.proyectos else "sin proyectos"
    estado = "activo" if u.activo else "de baja"
    t = [f'<article class="tarjeta">\n<div class="fila"><span class="nombre">{escape(u.nombre)}</span>'
         f'<span class="etiqueta">{escape(PAPELES.get(u.papel, u.papel))} · {estado}</span></div>\n'
         f'<div class="fila"><span class="suave">{escape(u.correo)}</span></div>\n'
         f'<div class="fila"><span class="suave">Proyectos: {escape(proyectos)}</span></div>']
    if a is not None and u.activo:
        f = [_cambio(a, "Papel", "usuario-papel", _elegir("papel", "Papel", PAPELES_FORMA, u.papel), "Cambiar papel",
                     fijo)]
        libres = [n for n in a.proyectos if n not in u.proyectos]
        if libres:
            f.append(_cambio(a, "Asignar proyecto", "usuario-asignar",
                             _elegir("proyecto", "Proyecto", [(n, n) for n in libres]), "Asignar", fijo))
        if u.proyectos:
            f.append(_peligro(a, "Quitar proyecto", "usuario-quitar", "Quitar", fijo,
                              _elegir("proyecto", "Proyecto", [(n, n) for n in u.proyectos])))
        f.append(_peligro(a, "Dar de baja", "usuario-baja", "Dar de baja al usuario", fijo))
        t.append(f'<div class="acciones">{"".join(f)}</div>')
    elif a is not None:
        t.append('<p class="ayuda">Para que vuelva a entrar, dale de alta otra vez con su correo.</p>')
    t.append("</article>")
    return "\n".join(t)


def _usuarios(v: Vista) -> str:
    a = v.acciones if v.acciones is not None and v.acciones.dueno else None
    t = [f'<section>\n<h2>{icono("usuarios")}Usuarios</h2>',
         '<p class="ayuda">Quién entra por el panel con login y qué ve.</p>']
    t += [_usuario(a, u) for u in v.usuarios or ()]
    if not v.usuarios:
        t.append('<p class="vacio">No hay usuarios dados de alta.</p>')
    if a is None:
        t.append("</section>")
        return "\n".join(t)
    cuerpo = ('<label>Correo<input type="email" name="correo" required maxlength="120" autocomplete="off" '
              'autocapitalize="none" spellcheck="false"></label>'
              '<label>Nombre<input name="nombre" required maxlength="80" autocomplete="off"></label>'
              f'{_elegir("papel", "Papel", PAPELES_FORMA, "lector")}')
    t.append(f'<article class="tarjeta">\n<div class="fila"><span class="nombre">Dar de alta</span></div>\n'
             f'{_forma(a, "usuario-alta", cuerpo, "Dar de alta")}\n</article>')
    t.append("</section>")
    return "\n".join(t)


def _pagado(v: Vista) -> str:
    t = [f'<section>\n<h2>{icono("tarjeta")}Pagado con tarjeta este mes</h2>\n<article class="tarjeta">']
    if not v.hay_foto:
        t.append(f'<p class="vacio">{SIN_FOTO}</p>')
    elif v.pagado is None:
        t.append(f'<div class="fila"><span class="nombre texto-gris">Sin datos</span></div>\n'
                 f'<p class="ayuda">{escape(v.pagado_nota)}</p>')
    else:
        mxn = Decimal(v.pagado) / MICRO * v.tipo_cambio
        if v.presupuesto:
            color = "rojo" if v.pagado > v.presupuesto else "ambar" if al_80(v.pagado, v.presupuesto) else "verde"
            meta = f"presupuesto {usd(v.presupuesto)}"
            barra = svg_barra(v.presupuesto, v.pagado, 0, color, f"{usd(v.pagado)} de {usd(v.presupuesto)}") + "\n"
        else:
            meta, barra = "sin presupuesto", ""
        cambio = (f"suponiendo el dólar a {v.tipo_cambio} pesos" if v.tipo_cambio_supuesto
                  else f"a {v.tipo_cambio} pesos por dólar")
        t.append(f'<div class="fila"><span class="nombre cifra">{usd(v.pagado)}</span>'
                 f'<span class="suave">{meta}</span></div>\n{barra}'
                 f'<div class="fila"><span class="suave">Unos MX${mxn:,.0f} {cambio}</span></div>')
        if v.pagado_nota:
            t.append(f'<p class="ayuda">{escape(v.pagado_nota)}</p>')
    if v.en_uso_tarjeta:
        t.append(f'<p class="ayuda">Producción gasta ahora en {escape(v.en_uso)}, la cuenta con tarjeta.</p>')
    t.append("</article>\n</section>")
    return "\n".join(t)


def _proximas(v: Vista) -> str:
    t = [f'<section>\n<h2>{icono("calendario")}Próximas concesiones</h2>']
    if not v.hay_foto:
        t.append(f'<p class="vacio">{SIN_FOTO}</p>')
    elif not v.proximas:
        t.append('<p class="vacio">No hay concesiones por llegar.</p>')
    else:
        t.append('<ul class="lista tarjeta">')
        for p in v.proximas:
            estado = "confirmada en la consola" if p.confirmada else "estimada, por confirmar"
            t.append(f'<li>{icono("calendario")}<div><div class="nombre">{escape(p.cuenta)} · {usd(p.monto)}</div>'
                     f'<div class="suave">del {fecha(p.otorgada)} al {fecha(p.vence)} · {estado}</div></div></li>')
        t.append("</ul>")
    t.append("</section>")
    return "\n".join(t)


def _salud(v: Vista) -> str:
    t = [f'<section>\n<h2>{icono("servidor")}Salud del gestor</h2>\n<ul class="lista tarjeta">']
    for s in v.salud:
        clase, ic = ("bien", "bien") if s.bien else ("mal", "alerta")
        t.append(f'<li><span class="{clase}">{icono(ic)}</span><span>{escape(s.texto)}</span></li>')
    t.append("</ul>\n</section>")
    return "\n".join(t)


def pagina(v: Vista) -> str:
    local = en_local(v.t)
    # Sin foto no hay nada actualizado que presumir: la hora es solo la de la página.
    hora = "Actualizado a las" if v.hay_foto else "Página generada a las"
    # Con formularios no hay meta refresh: recargar a medio llenar borraría lo escrito.
    refresco = f'<meta http-equiv="refresh" content="{REFRESCO_S}">\n' if v.acciones is None else ""
    nota = f'<p class="ayuda">{escape(v.nota)}</p>\n' if v.nota else ""
    cabeza = f"""<!doctype html>
<html lang="es-MX">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresco}<meta name="robots" content="noindex">
<title>{TITULO}</title>
<style>{ESTILO}</style>
</head>
<body>
{ICONOS}
<main>
<header>
<h1>{TITULO}</h1>
<p class="quien">{escape(v.quien)}</p>
{nota}<p class="hora">{icono("reloj")}{hora} {local:%H:%M} ({NOMBRE_ZONA})</p>
<p class="enlaces"><a href="/panel/costo">Costos por día, modelo y programa</a></p>
</header>"""
    if v.completa:
        cuerpo = [_concesiones(v), _programas(v)]
        if v.acciones is not None and v.acciones.dueno:
            cuerpo.append(_nuevo_proyecto(v.acciones))
        if v.usuarios is not None:
            cuerpo.append(_usuarios(v))
        cuerpo += [_pagado(v), _proximas(v), _salud(v)]
    else:
        cuerpo = ['<p class="ayuda">Ves solo tus proyectos.</p>', _programas(v)]
    return "\n".join([cabeza, *cuerpo, "</main>\n</body>\n</html>\n"])


def pagina_llave(proyecto: str, llave: str, accion: str) -> str:
    """La única vez que se ve una llave completa: después de crear o rotar. Sin meta refresh (se perdería) y la
    respuesta va con no-store y no-referrer. La llave no se registra en ningún lado."""
    que = "Proyecto creado" if accion == "proyecto-alta" else "Llave rotada"
    antes = "" if accion == "proyecto-alta" else " La llave anterior deja de servir."
    return f"""<!doctype html>
<html lang="es-MX">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{TITULO}</title>
<style>{ESTILO}</style>
</head>
<body>
{ICONOS}
<main>
<header>
<h1>{TITULO}</h1>
</header>
<section>
<h2>{icono("llave")}{que}: {escape(proyecto)}</h2>
<article class="tarjeta">
<p class="aviso">{UNA_VEZ}</p>
<code class="llave">{escape(llave)}</code>
<p class="ayuda">Toca la llave para seleccionarla.{antes} El cambio {APLICA}.</p>
</article>
<p><a href="/panel">Volver al panel</a></p>
</section>
</main>
</body>
</html>
"""
