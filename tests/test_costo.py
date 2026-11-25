"""Página de costos del panel: suma por día y serie, filtros, «Otros», colores fijos y quién ve qué."""
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import insert
from test_panel_app import SALUD, app_panel

from pasarela import costo
from pasarela.esquema import registro
from pasarela.identidad import Quien

T = datetime(2026, 11, 25, 12, 0)
BASE = {"ruta": "v1/messages", "status": 200, "lote": False, "entrada": 10, "salida": 5, "cache_5m": 0,
        "cache_1h": 0, "cache_lectura": 3, "busquedas": 0, "latencia_ms": 1, "request_id": None, "reintentos": 0}


def poner(motor, dias_atras, proyecto, fuente, modelo, micro):
    with motor.begin() as c:
        c.execute(insert(registro).values(ts=T - timedelta(days=dias_atras, hours=1), proyecto=proyecto,
                                          fuente=fuente, modelo=modelo, costo_micro=micro, **BASE))


def test_suma_por_dia_y_modelo(motor):
    poner(motor, 0, "crm", "org2", "claude-sonnet-4-5", 2_000_000)
    poner(motor, 0, "crm", "org2", "claude-haiku-4-5", 500_000)
    poner(motor, 1, "web", "org1", "claude-sonnet-4-5", 1_000_000)
    poner(motor, 40, "web", "org1", "claude-sonnet-4-5", 9_000_000)       # fuera de 30 días
    d = costo.leer(motor, costo.Filtros(), None, T)
    assert d.costo == 3_500_000 and d.peticiones == 3 and d.entrada == 30 and d.cache_lectura == 9
    assert d.celdas[(T.date(), "claude-sonnet-4-5")] == 2_000_000
    assert d.series == ("claude-sonnet-4-5", "claude-haiku-4-5") and len(d.dias) == 30


def test_filtro_por_programa_no_cambia_colores(motor):
    poner(motor, 0, "crm", "org2", "claude-sonnet-4-5", 2_000_000)
    poner(motor, 0, "web", "org2", "claude-haiku-4-5", 500_000)
    todo = costo.leer(motor, costo.Filtros(), None, T)
    solo = costo.leer(motor, costo.Filtros(programa="web"), None, T)
    assert solo.series == ("claude-haiku-4-5",) and solo.costo == 500_000
    assert solo.color["claude-haiku-4-5"] == todo.color["claude-haiku-4-5"]


def test_mas_de_siete_series_van_a_otros(motor):
    for i in range(9):
        poner(motor, 0, f"p{i}", "org2", "m", (10 - i) * 1_000_000)
    d = costo.leer(motor, costo.Filtros(agrupar="programa"), None, T)
    assert len(d.series) == 8 and d.series[-1] == costo.OTROS and d.color[costo.OTROS] == 8
    assert d.totales_serie[costo.OTROS] == 5_000_000     # los dos más chicos: 3 + 2


def test_responsable_solo_ve_lo_suyo(motor):
    poner(motor, 0, "crm", "org2", "m", 2_000_000)
    poner(motor, 0, "web", "org2", "m", 1_000_000)
    q = Quien("r@example.com", "responsable", frozenset({"web"}), "sacbe")
    d = costo.leer(motor, costo.leer_filtros({"programa": "crm"}, {"web"}), q, T)
    assert d.costo == 1_000_000 and d.programas == ("web",)


def test_filtros_invalidos_toman_el_valor_por_omision():
    f = costo.leer_filtros({"dias": "999", "agrupar": "x", "programa": "<script>"}, None)
    assert (f.dias, f.agrupar) == (30, "modelo")


def test_pagina_escapa_y_trae_grafica(motor):
    poner(motor, 0, "<b>malo</b>", "org2", "claude-sonnet-4-5", 2_000_000)
    d = costo.leer(motor, costo.Filtros(agrupar="programa"), None, T)
    html = costo.pagina(costo.Filtros(agrupar="programa"), d, "dueño", "hora", "Sacbé", "", "")
    assert "<b>malo</b>" not in html and "&lt;b&gt;malo&lt;/b&gt;" in html
    assert '<svg class="grafica"' in html and "<title>" in html and "Ver tabla" in html and "<script" not in html


def test_ruta_del_panel(motor, falso):
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=SALUD)
    poner(motor, 0, "crm", "org2", "claude-sonnet-4-5", 2_000_000)
    with TestClient(app_panel(motor, falso)) as c:
        r = c.get("/panel/costo?dias=7&agrupar=cuenta")
        assert '/panel/costo' in c.get("/panel").text
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "Costos" in r.text and "org2" in r.text


def test_sin_panel_no_hay_costos(cliente):
    assert cliente.get("/panel/costo").status_code == 404
