"""Panel: vista y HTML, sin la app (pasarela/panel.py es puro)."""
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from pasarela import bolsa, panel
from pasarela.bolsa import EstadoConcesion, Foto
from pasarela.identidad import Quien
from pasarela.orden import CuentaViva

M = 1_000_000
T = datetime(2026, 11, 12, 15, 42)                     # 10:42 en UTC-05:00 (la zona de conftest.py)


def foto_ejemplo(sin_datos=frozenset({"org2"}), org1="org1"):
    vivas = (CuentaViva(org1, False, 73_280_000, date(2026, 11, 17), True, 0),
             CuentaViva("org2", True, 200 * M, date(2026, 12, 3), False, 1))
    conc = (EstadoConcesion(1, org1, 200 * M, date(2026, 11, 5), date(2026, 11, 17), True, True, 73_280_000,
                            41_100_000, 8_220_000),
            EstadoConcesion(2, org1, 200 * M, date(2026, 11, 15), date(2026, 12, 18), False, True, 200 * M, 0,
                            8_220_000),
            EstadoConcesion(3, "org2", 200 * M, date(2026, 10, 31), date(2026, 12, 3), True, True, 200 * M, 200 * M,
                            1_520_000))
    return Foto(True, vivas, conc, frozenset({"lotes"}), {"crm": 12_500_000, "lotes": 20 * M, "demo-alt": 3 * M},
                {"lotes": (20 * M, "corte"), "crm": (30 * M, "aviso")}, 0, 50 * M,
                {org1: datetime(2026, 11, 12, 13, 0)}, (), frozenset({"org2"}), datetime(2026, 11, 12, 15, 41, 20),
                sin_datos=sin_datos)


SALUD_API = {"ok": True, "base": True, "proyectos": 9, "agotadas": [], "fallas_registro": 0}


def vista(foto=None, en_uso="org1", api=SALUD_API, quien=None):
    foto = foto_ejemplo() if foto is None else foto
    s = panel.salud_de(api, True, foto, 60, datetime(2026, 11, 12, 13, 10), 14, (), 21600, T)
    return panel.armar_vista(foto, en_uso, s, Decimal("18.63"), T, quien=quien, host="panel.example.com")


def quien(papel, *proyectos, correo="rosa@example.com"):
    return Quien(correo, papel, frozenset(proyectos), "sacbe")


@pytest.mark.parametrize("restante,perdida,dias,sin,color", [
    (100 * M, 0, 10, False, "verde"), (100 * M, 5 * M, 10, False, "ambar"), (100 * M, 25 * M, 10, False, "rojo"),
    (5 * M, 0, 2, False, "rojo"), (M // 2, 0, 2, False, "verde"), (100 * M, 25 * M, 1, True, "gris")])
def test_color(restante, perdida, dias, sin, color):
    assert panel.color_de(restante, perdida, dias, sin) == color


def test_barras_solo_de_las_vigentes():
    v = vista()
    assert [b.id for b in v.barras] == [1, 3]               # la #2 empieza el 15-nov: va en próximas
    assert [p.id for p in v.proximas] == [2]
    org1, org2 = v.barras
    assert (org1.color, org1.en_uso, org1.cuadre, org1.dias) == ("rojo", True, "cuadrado a las 08:00", 5)
    assert (org2.color, org2.en_uso, org2.cuadre, org2.perdida) == ("gris", False, "sin datos", 0)


def test_en_uso_en_la_cuenta_de_tarjeta():
    v = vista(en_uso="org2")
    assert [b.en_uso for b in v.barras] == [False, True] and v.en_uso_tarjeta


def test_pagado_sin_datos_y_con_datos():
    assert vista().pagado is None and "org2" in vista().pagado_nota
    v = vista(foto_ejemplo(sin_datos=frozenset()))
    assert v.pagado == 0 and v.pagado_nota == ""


def test_programas_por_gasto_y_color():
    v = vista()
    assert [(p.nombre, p.color, p.cortado) for p in v.programas] == \
           [("lotes", "rojo", True), ("crm", "verde", False), ("demo-alt", "verde", False)]


def test_programa_dado_de_baja_no_sale_ni_con_gasto():
    """Spec de Sacbé: los proyectos con activo=False no se listan, hayan gastado o no este mes; uno activo sin gasto
    pero con tope sigue saliendo. El gasto sigue en la foto: solo se oculta en la vista."""
    base = foto_ejemplo()
    f = replace(base, gasto_mes={**base.gasto_mes, "viejo": 0, "demo-alt": 3 * M},
                topes={**base.topes, "viejo": (5 * M, "corte"), "callado": (5 * M, "aviso")},
                bajas=frozenset({"viejo", "demo-alt"}))
    nombres = [p.nombre for p in vista(f).programas]
    assert "viejo" not in nombres and "demo-alt" not in nombres and "callado" in nombres
    assert f.gasto_mes["demo-alt"] == 3 * M
    html = panel.pagina(vista(f))
    assert "viejo" not in html and "demo-alt" not in html


def test_salud():
    textos = [s.texto for s in vista().salud]
    assert textos[0] == "El gestor contesta y tiene 9 programas con llave."
    assert "Saldos calculados hace 40 s." in textos
    malo = panel.salud_de(None, False, None, 60, None, 0, ("claude-opus-6",), 21600, T)
    assert [s.bien for s in malo] == [False] * 5
    assert malo[-1].texto == "Modelo nuevo sin precio: claude-opus-6."


def test_hace():
    assert [panel.hace(s) for s in (5, 125, 7300, 90000, 200000)] == ["5 s", "2 min", "2 h", "1 día", "2 días"]


def test_pagina():
    html = panel.pagina(vista())
    assert html.startswith("<!doctype html>") and '<html lang="es-MX">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "prefers-color-scheme:dark" in html and '<meta http-equiv="refresh" content="60">' in html
    assert "Actualizado a las 10:42 (UTC-05:00)" in html
    assert html.count('class="insignia"') == 1 and html.count('<article class="tarjeta">\n<div class="fila">'
                                                              '<span class="nombre">org1') == 1
    assert "vence el 17-nov · faltan 5 días" in html and "a US$8.22 al día (últimos 7 días): se pierden US$41.10" in html
    assert "cuadrado a las 08:00" in html
    assert "a US$1.52 al día (últimos 7 días): sin datos para saber cuánto se pierde" in html
    assert "cortado hasta el día 1" in html and "41% de su tope de US$30.00, solo avisa" in html
    assert "del 15-nov al 18-dic · estimada, por confirmar" in html
    assert "<script" not in html and chr(0x2014) not in html


def test_titulo_y_quien_ve():
    html = panel.pagina(vista())
    assert "<title>Sacbé · créditos de IA</title>" in html and "<h1>Sacbé · créditos de IA</h1>" in html
    assert '<p class="quien">Panel interno · dueño</p>' in html
    for papel, texto in (("dueno", "dueño"), ("finanzas", "finanzas"), ("responsable", "responsable"),
                         ("lector", "lector")):
        assert f'<p class="quien">rosa@example.com · {texto}</p>' in panel.pagina(vista(quien=quien(papel)))


def test_ritmo_visible_se_usa_completa_y_se_pierde():
    f = foto_ejemplo()
    sin_perdida = replace(f, concesiones=(replace(f.concesiones[0], perdida_micro=0, ritmo_micro=1_520_000),)
                          + f.concesiones[1:])
    assert "a US$1.52 al día (últimos 7 días): se usa completa" in panel.pagina(vista(sin_perdida))
    caro = replace(f, concesiones=(replace(f.concesiones[0], perdida_micro=170_400_000, ritmo_micro=0),)
                   + f.concesiones[1:])
    assert "a US$0.00 al día (últimos 7 días): se pierden US$170.40" in panel.pagina(vista(caro))


def test_la_concesion_estimada_de_tarjeta_no_muestra_ritmo():
    html = panel.pagina(vista(foto_con_estimada(), en_uso="org2"))
    assert "Estimada en una cuenta con tarjeta" in html and html.count("al día (últimos 7 días)") == 2


def test_vista_de_finanzas_es_la_del_dueno():
    d, f = vista(quien=quien("dueno")), vista(quien=quien("finanzas"))
    assert f.completa and d.completa
    assert (f.barras, f.programas, f.pagado, f.proximas, f.salud) == (d.barras, d.programas, d.pagado, d.proximas,
                                                                      d.salud)


@pytest.mark.parametrize("papel", ["responsable", "lector"])
def test_responsable_y_lector_solo_ven_sus_proyectos(papel):
    v = vista(quien=quien(papel, "crm", "sin-gasto"))
    assert not v.completa
    assert [p.nombre for p in v.programas] == ["crm", "sin-gasto"]       # el suyo sin gasto sale con US$0.00
    assert v.barras == () and v.proximas == () and v.salud == () and v.pagado is None
    html = panel.pagina(v)
    assert "Ves solo tus proyectos" in html
    for oculto in ("Concesiones vigentes", "Pagado con tarjeta", "Próximas concesiones", "Salud del gestor",
                   "lotes", "demo-alt", "org1", "org2", "El gestor", "presupuesto"):
        assert oculto not in html
    assert "Gasto del mes por programa" in html and "US$12.50" in html and "US$20.00" not in html


def test_vista_parcial_sin_proyectos_y_sin_foto():
    html = panel.pagina(vista(quien=quien("lector")))
    assert "No tienes proyectos asignados." in html and "lotes" not in html
    sin = panel.pagina(panel.armar_vista(None, None, (), Decimal("20"), T, quien=quien("lector", "crm")))
    assert "Todavía no hay cálculo de saldos." in sin and "Ves solo tus proyectos" in sin


def test_un_papel_raro_no_ve_nada_de_dinero():
    v = vista(quien=quien("inventado", "crm"))
    assert not v.completa and v.programas == ()


def test_el_correo_y_los_nombres_se_escapan():
    q = quien("responsable", "<b>crm</b>", correo="<script>x</script>@ejemplo.mx")
    f = replace(foto_ejemplo(), gasto_mes={"<b>crm</b>": M})
    html = panel.pagina(vista(f, quien=q))
    assert "<script>x" not in html and "&lt;script&gt;x&lt;/script&gt;@ejemplo.mx" in html
    assert "<b>crm</b>" not in html and "&lt;b&gt;crm&lt;/b&gt;" in html


def test_pagina_sin_emojis():
    html = panel.pagina(vista())
    emoji = "[" + chr(0x1F000) + "-" + chr(0x1FAFF) + chr(0x2600) + "-" + chr(0x27BF) + "]"
    assert not re.search(emoji, html)


def test_pagina_escapa_lo_que_viene_de_la_base():
    v = vista(foto_ejemplo(org1="<b>x</b>"), en_uso="<b>x</b>",
              api={**SALUD_API, "agotadas": ["<i>org2</i>"]})
    html = panel.pagina(v)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<i>org2</i>" not in html


def test_pagina_sin_foto():
    v = panel.armar_vista(None, None, (), Decimal("20"), T)
    html = panel.pagina(v)
    assert html.count("Todavía no hay cálculo de saldos.") == 4         # tampoco programas ni próximas
    assert "Ningún programa ha gastado" not in html and "No hay concesiones por llegar" not in html


def test_barra_no_se_sale():
    svg = panel.svg_barra(10 * M, 25 * M, 0, "rojo", "x")
    assert 'width="100.00"' in svg and 'x="100.00" width="0.00"' in svg


def foto_con_estimada():
    """una concesión estimada de org2 (con tarjeta), vigente y que vence antes que la #3."""
    f = foto_ejemplo(sin_datos=frozenset())
    estimada = EstadoConcesion(4, "org2", 50 * M, date(2026, 11, 1), date(2026, 11, 25), False, False, 50 * M, 0)
    return replace(f, concesiones=f.concesiones + (estimada,))


def test_estimada_de_tarjeta_sale_gris_y_no_entra_al_saldo():
    v = vista(foto_con_estimada(), en_uso="org2")
    assert [b.id for b in v.barras] == [1, 4, 3]
    est = v.barras[1]
    assert (est.color, est.en_saldo, est.en_uso, est.perdida) == ("gris", False, False, 0)
    assert [b.id for b in v.barras if b.en_uso] == [3]          # la insignia se queda en la que sí se gasta
    assert v.pagado == vista(foto_ejemplo(sin_datos=frozenset()), en_uso="org2").pagado
    html = panel.pagina(v)
    assert html.count("no entra al saldo") == 1 and html.count('class="insignia"') == 1


def test_sin_foto_no_dice_actualizado():
    html = panel.pagina(panel.armar_vista(None, None, (), Decimal("20"), T))
    assert "Actualizado" not in html and "Página generada a las 10:42 (UTC-05:00)" in html


def test_plurales():
    uno = {**SALUD_API, "proyectos": 1, "agotadas": ["org2"], "fallas_registro": 1}
    textos = [s.texto for s in panel.salud_de(uno, True, None, 60, None, 0, ("claude-opus-6",), 21600, T)]
    assert "El gestor contesta y tiene 1 programa con llave." in textos
    assert "Anthropic dice que se quedó sin crédito: org2." in textos
    assert "Se perdió 1 fila de registro desde que arrancó." in textos
    assert "Modelo nuevo sin precio: claude-opus-6." in textos
    varios = {**SALUD_API, "agotadas": ["org2", "org1"], "fallas_registro": 3}
    textos = [s.texto for s in panel.salud_de(varios, True, None, 60, None, 0, ("a", "b"), 21600, T)]
    assert "Anthropic dice que se quedaron sin crédito: org2, org1." in textos
    assert "Se perdieron 3 filas de registro desde que arrancó." in textos
    assert "Modelos nuevos sin precio: a, b." in textos


def test_estimada_de_tarjeta_se_dibuja_tenue():
    """lo que queda de la estimada va en el tono tenue; la de «sin datos» sigue sólida."""
    html = panel.pagina(vista(foto_con_estimada(), en_uso="org2"))
    assert ('aria-label="Quedan US$50.00 de US$50.00"><rect class="pista" width="100" height="10"/>'
            '<rect class="uso" width="0.00" height="10"/><rect class="pierde" x="0.00" width="100.00"') in html
    html = panel.pagina(vista())
    assert '<rect class="uso" width="100.00" height="10"/><rect class="pierde" x="100.00" width="0.00"' in html


def test_tipo_de_cambio_supuesto():
    f = foto_ejemplo(sin_datos=frozenset())
    s = panel.salud_de(SALUD_API, True, f, 60, None, 0, (), 21600, T)
    supuesto = panel.pagina(panel.armar_vista(f, "org1", s, Decimal("20"), T, True))
    assert "Unos MX$0 suponiendo el dólar a 20 pesos" in supuesto and "pesos por dólar" not in supuesto
    dado = panel.pagina(panel.armar_vista(f, "org1", s, Decimal("18.63"), T))
    assert "Unos MX$0 a 18.63 pesos por dólar" in dado and "suponiendo" not in dado


def test_pagina_es_la_maqueta():
    html = panel.pagina(vista())
    main = html[html.index("<main>"):html.index("</main>") + len("</main>")]
    assert main == MAQUETA


def test_ayudantes_compartidos():
    assert [bolsa.al_80(g, 100) for g in (79, 80, 100)] == [False, True, True]
    assert [bolsa.al_dia(e, 60) for e in (184, 185)] == [True, False] and bolsa.al_dia(10_000, 0)
    assert bolsa.pagado_visible(foto_ejemplo()) == (None, ["org2"])
    assert bolsa.pagado_visible(foto_ejemplo(sin_datos=frozenset())) == (0, [])


MAQUETA = """\
<main>
<header>
<h1>Sacbé · créditos de IA</h1>
<p class="quien">Panel interno · dueño</p>
<p class="ayuda">Para hacer cambios entra a https://panel.example.com</p>
<p class="hora"><svg class="icono" aria-hidden="true"><use href="#i-reloj"/></svg>Actualizado a las 10:42 (UTC-05:00)</p>
<p class="enlaces"><a href="/panel/costo">Costos por día, modelo y programa</a></p>
</header>
<section>
<h2><svg class="icono" aria-hidden="true"><use href="#i-saldo"/></svg>Concesiones vigentes</h2>
<p class="ayuda">La barra es lo que queda de cada concesión. La parte clara es lo que se perdería al ritmo de gasto de los últimos 7 días.</p>
<article class="tarjeta">
<div class="fila"><span class="nombre">org1 <span class="suave">#1</span></span><span class="insignia">en uso</span></div>
<svg class="barra rojo" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="Quedan US$73.28 de US$200.00"><rect class="pista" width="100" height="10"/><rect class="uso" width="16.09" height="10"/><rect class="pierde" x="16.09" width="20.55" height="10"/></svg>
<div class="fila"><span class="cifra">US$73.28 de US$200.00</span><span class="suave">vence el 17-nov · faltan 5 días</span></div>
<div class="fila"><span class="texto-rojo">a US$8.22 al día (últimos 7 días): se pierden US$41.10</span><span class="etiqueta">cuadrado a las 08:00</span></div>
</article>
<article class="tarjeta">
<div class="fila"><span class="nombre">org2 <span class="suave">#3</span></span></div>
<svg class="barra gris" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="Quedan US$200.00 de US$200.00"><rect class="pista" width="100" height="10"/><rect class="uso" width="100.00" height="10"/><rect class="pierde" x="100.00" width="0.00" height="10"/></svg>
<div class="fila"><span class="cifra">US$200.00 de US$200.00</span><span class="suave">vence el 3-dic · faltan 21 días</span></div>
<div class="fila"><span class="texto-gris">a US$1.52 al día (últimos 7 días): sin datos para saber cuánto se pierde</span><span class="etiqueta">sin datos</span></div>
</article>
</section>
<section>
<h2><svg class="icono" aria-hidden="true"><use href="#i-programas"/></svg>Gasto del mes por programa</h2>
<article class="tarjeta">
<div class="fila"><span class="nombre">lotes</span><span class="cifra">US$20.00</span></div>
<svg class="barra rojo" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="US$20.00 de US$20.00"><rect class="pista" width="100" height="10"/><rect class="uso" width="100.00" height="10"/><rect class="pierde" x="100.00" width="0.00" height="10"/></svg>
<div class="fila"><span class="suave texto-rojo">cortado hasta el día 1</span></div>
</article>
<article class="tarjeta">
<div class="fila"><span class="nombre">crm</span><span class="cifra">US$12.50</span></div>
<svg class="barra verde" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="US$12.50 de US$30.00"><rect class="pista" width="100" height="10"/><rect class="uso" width="41.67" height="10"/><rect class="pierde" x="41.67" width="0.00" height="10"/></svg>
<div class="fila"><span class="suave texto-gris">41% de su tope de US$30.00, solo avisa</span></div>
</article>
<article class="tarjeta">
<div class="fila"><span class="nombre">demo-alt</span><span class="cifra">US$3.00</span></div>
<div class="fila"><span class="suave texto-gris">sin tope</span></div>
</article>
</section>
<section>
<h2><svg class="icono" aria-hidden="true"><use href="#i-tarjeta"/></svg>Pagado con tarjeta este mes</h2>
<article class="tarjeta">
<div class="fila"><span class="nombre texto-gris">Sin datos</span></div>
<p class="ayuda">org2 nunca se ha cuadrado con Anthropic.</p>
</article>
</section>
<section>
<h2><svg class="icono" aria-hidden="true"><use href="#i-calendario"/></svg>Próximas concesiones</h2>
<ul class="lista tarjeta">
<li><svg class="icono" aria-hidden="true"><use href="#i-calendario"/></svg><div><div class="nombre">org1 · US$200.00</div><div class="suave">del 15-nov al 18-dic · estimada, por confirmar</div></div></li>
</ul>
</section>
<section>
<h2><svg class="icono" aria-hidden="true"><use href="#i-servidor"/></svg>Salud del gestor</h2>
<ul class="lista tarjeta">
<li><span class="bien"><svg class="icono" aria-hidden="true"><use href="#i-bien"/></svg></span><span>El gestor contesta y tiene 9 programas con llave.</span></li>
<li><span class="bien"><svg class="icono" aria-hidden="true"><use href="#i-bien"/></svg></span><span>La base responde.</span></li>
<li><span class="bien"><svg class="icono" aria-hidden="true"><use href="#i-bien"/></svg></span><span>Saldos calculados hace 40 s.</span></li>
<li><span class="bien"><svg class="icono" aria-hidden="true"><use href="#i-bien"/></svg></span><span>Catálogo de modelos: 14 con precio, leído hace 2 h.</span></li>
</ul>
</section>
</main>"""
