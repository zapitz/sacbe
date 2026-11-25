from datetime import date, timedelta

from pasarela.saldo import Concesion, proyectar, repartir, vence_estimado

M = 1_000_000
D = date


def dias(desde: date, hasta: date, micro: int) -> dict[date, int]:
    """Gasto parejo de `micro` por día, de `desde` a `hasta` inclusive."""
    return {desde + timedelta(days=i): micro for i in range((hasta - desde).days + 1)}


def test_una_concesion():
    c = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    r = repartir([c], {D(2026, 11, 6): 30 * M, D(2026, 11, 7): 20 * M}, D(2026, 11, 12))
    assert r.restante == {1: 150 * M} and r.perdido == {} and r.pagado_por_dia == {}


def test_enciman_se_gasta_primero_la_que_vence_antes():
    """Org1: 5-nov a 17-nov y 15-nov a 27-nov. Los días 15 y 16 se cobran a la primera, no a las dos."""
    a = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    b = Concesion(2, 200 * M, D(2026, 11, 15), D(2026, 11, 27), False)
    r = repartir([b, a], dias(D(2026, 11, 5), D(2026, 11, 20), 10 * M), D(2026, 11, 20))
    assert r.perdido == {1: 80 * M}               # 12 días (5 al 16) a US$10 = 120; se pierden 80 el 17
    assert r.restante == {2: 160 * M}             # 17 al 20: 4 días a US$10
    assert r.pagado_por_dia == {}


def test_lo_que_no_cubre_ninguna_es_pagado():
    a = Concesion(1, 10 * M, D(2026, 11, 1), D(2026, 12, 1), True)
    r = repartir([a], {D(2026, 11, 2): 15 * M}, D(2026, 11, 3))
    assert r.restante == {1: 0} and r.pagado_por_dia == {D(2026, 11, 2): 5 * M}


def test_sin_concesiones_todo_es_pagado():
    r = repartir([], {D(2026, 11, 2): 3 * M, D(2026, 11, 4): 0}, D(2026, 11, 5))
    assert r.pagado_por_dia == {D(2026, 11, 2): 3 * M} and r.restante == {}


def test_concesion_futura_conserva_su_monto():
    b = Concesion(2, 200 * M, D(2026, 11, 15), D(2026, 11, 27), False)
    assert repartir([b], {D(2026, 11, 12): 5 * M}, D(2026, 11, 12)).restante == {2: 200 * M}


def test_gasto_antes_de_la_concesion_no_la_toca():
    a = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    r = repartir([a], {D(2026, 11, 4): 7 * M}, D(2026, 11, 6))
    assert r.restante == {1: 200 * M} and r.pagado_por_dia == {D(2026, 11, 4): 7 * M}


def test_proyectar_perdida_al_ritmo():
    """Hoy 12-nov, quedan 100 y vence el 17: del 13 al 16 son 4 días a US$10, se pierden 60."""
    a = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    assert proyectar([a], {1: 100 * M}, 10 * M, D(2026, 11, 13)) == {1: 60 * M}


def test_proyectar_sin_ritmo_se_pierde_todo_y_con_mucho_nada():
    a = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    assert proyectar([a], {1: 100 * M}, 0, D(2026, 11, 13)) == {1: 100 * M}
    assert proyectar([a], {1: 100 * M}, 50 * M, D(2026, 11, 13)) == {1: 0}


def test_proyectar_dos_concesiones_la_primera_absorbe_el_ritmo():
    a = Concesion(1, 200 * M, D(2026, 11, 5), D(2026, 11, 17), True)
    b = Concesion(2, 200 * M, D(2026, 11, 15), D(2026, 11, 27), False)
    p = proyectar([a, b], {1: 30 * M, 2: 200 * M}, 10 * M, D(2026, 11, 13))
    assert p == {1: 0, 2: 90 * M}        # a se acaba el 15; b paga del 16 al 26 (11 días = 110) y le quedan 90


def test_vence_estimado():
    """33 días desde la otorgada, sin mirar lo que duró la anterior."""
    assert vence_estimado(D(2026, 10, 31)) == D(2026, 12, 3)          # 33 días
    assert vence_estimado(D(2026, 11, 15)) == D(2026, 12, 18)
