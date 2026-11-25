from datetime import date

import pytest

from pasarela.orden import CuentaViva, SinCredito, apta, ordenar_por_saldo

M = 1_000_000


def viva(nombre, tarjeta=False, libre=0, vence=None, reciente=False, pos=0):
    return CuentaViva(nombre, tarjeta, libre, vence, reciente, pos)


U = viva("org1", libre=50 * M, vence=date(2026, 11, 17), pos=0)
R = viva("org2", tarjeta=True, libre=150 * M, vence=date(2026, 12, 3), pos=1)
G, P = "gratis", "pagado"


def nombres(lista):
    return [(c.fuente, c.tipo) for c in lista]


def test_produccion_la_que_vence_antes_primero():
    assert nombres(ordenar_por_saldo("produccion", [R, U], set())) == [("org1", G), ("org2", P)]


def test_dos_sin_tarjeta_por_vencimiento():
    otra = viva("otra", libre=10 * M, vence=date(2026, 11, 14), pos=2)
    assert nombres(ordenar_por_saldo("extra", [U, otra], set())) == [("otra", G), ("org1", G)]


def test_extra_solo_usa_cuentas_sin_tarjeta():
    assert nombres(ordenar_por_saldo("extra", [U, R], set())) == [("org1", G)]


def test_extra_nunca_usa_la_tarjeta_aunque_este_cuadrada():
    r = viva("org2", tarjeta=True, libre=150 * M, vence=date(2026, 12, 3), reciente=True, pos=1)
    assert nombres(ordenar_por_saldo("extra", [U, r], set())) == [("org1", G)]
    assert not apta(r, "extra")
    assert apta(r, "produccion")


def test_sin_tarjeta_en_cero_se_sigue_usando_despues_de_las_que_tienen_saldo():
    u0 = viva("org1", libre=0, vence=None, pos=0)
    assert nombres(ordenar_por_saldo("produccion", [u0, R], set())) == [("org2", P), ("org1", G)]
    assert nombres(ordenar_por_saldo("extra", [u0, R], set())) == [("org1", G)]


def test_umbral_de_un_dolar():
    justo = viva("org1", libre=1 * M, vence=date(2026, 11, 17), pos=0)
    assert not apta(justo, "produccion")
    assert apta(viva("org1", libre=1 * M + 1, vence=date(2026, 11, 17)), "extra")


def test_tarjeta_sin_saldo_gratis_va_al_final_para_produccion():
    r0 = viva("org2", tarjeta=True, libre=0, pos=1)
    assert nombres(ordenar_por_saldo("produccion", [U, r0], set())) == [("org1", G), ("org2", P)]
    assert nombres(ordenar_por_saldo("extra", [U, r0], set())) == [("org1", G)]


def test_tarjeta_marcada_va_al_final_y_nunca_se_quita():
    otra = viva("otra", tarjeta=True, libre=0, pos=2)
    assert nombres(ordenar_por_saldo("produccion", [U, R, otra], {"org2"})) == \
           [("org1", G), ("otra", P), ("org2", P)]
    assert nombres(ordenar_por_saldo("produccion", [U, R], {"org1", "org2"})) == [("org2", P)]


def test_extra_todo_marcado():
    with pytest.raises(SinCredito, match="gratis"):
        ordenar_por_saldo("extra", [U, R], {"org1", "org2"})


def test_produccion_sin_tarjeta_y_todo_marcado():
    with pytest.raises(SinCredito, match="agotadas"):
        ordenar_por_saldo("produccion", [U], {"org1"})


def test_empate_por_posicion_del_env():
    a = viva("a", libre=5 * M, vence=date(2026, 11, 20), pos=1)
    b = viva("b", libre=5 * M, vence=date(2026, 11, 20), pos=0)
    assert [c.fuente for c in ordenar_por_saldo("extra", [a, b], set())] == ["b", "a"]


def test_los_mensajes_de_sin_credito_son_los_mismos_que_en_el_plan_a():
    from pasarela.config import Fuente
    from pasarela.orden import ordenar
    for clase, vivas, fuentes in (("extra", [U], [Fuente("org1", "gratis", "sk-falsa-1")]),
                                  ("produccion", [U], [Fuente("org1", "gratis", "sk-falsa-1")])):
        with pytest.raises(SinCredito) as a:
            ordenar(clase, fuentes, {"org1"})
        with pytest.raises(SinCredito) as b:
            ordenar_por_saldo(clase, vivas, {"org1"})
        assert str(a.value) == str(b.value)
