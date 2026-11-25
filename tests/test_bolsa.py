from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, insert, select

from pasarela.bolsa import Bolsa, calcular, leer
from pasarela.config import cargar
from pasarela.esquema import ajuste, aviso, cambio, concesion, conciliacion, cuenta, registro, tope
from pasarela.orden import SinCredito

M = 1_000_000
AHORA = datetime(2026, 11, 12, 15, 0)
BASE = {"ruta": "v1/messages", "modelo": "m", "status": 200, "lote": False, "entrada": 1, "salida": 1,
        "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0, "latencia_ms": 1, "request_id": None,
        "reintentos": 0}


def nov(d, h=12):
    return datetime(2026, 11, d, h)


def poner_cuenta(motor, nombre, tarjeta, activa=True, llave_vence=None):
    with motor.begin() as c:
        c.execute(insert(cuenta).values(nombre=nombre, correo_plan=f"{nombre}@ejemplo.mx", tiene_tarjeta=tarjeta,
                                        activa=activa, llave_vence=llave_vence, creada=AHORA))


def poner_concesion(motor, cuenta_, usd, otorgada, vence, confirmada=True):
    with motor.begin() as c:
        return c.execute(insert(concesion).values(
            cuenta=cuenta_, monto_micro=usd * M, otorgada=otorgada, vence=vence,
            origen="consola" if confirmada else "estimada", confirmada=confirmada, creada=AHORA)
        ).inserted_primary_key[0]


def gasto(motor, proyecto_, fuente, usd, ts):
    with motor.begin() as c:
        c.execute(insert(registro).values(ts=ts, proyecto=proyecto_, fuente=fuente, costo_micro=int(usd * M), **BASE))


def conciliado(motor, cuenta_, dia, usd, recibido=AHORA):
    with motor.begin() as c:
        c.execute(insert(conciliacion).values(cuenta=cuenta_, dia=dia, micro=int(usd * M), recibido=recibido))


def ya_conciliada(motor, cuenta_):
    """Una conciliación de US$0 en un día lejos de toda ventana: la cuenta ya se cuadró alguna vez con Anthropic y el gasto de la prueba no cambia."""
    conciliado(motor, cuenta_, datetime(2026, 1, 1), 0)


def foto(motor, config, t=AHORA):
    return calcular(leer(motor, t), config, t)


def viva(f, nombre):
    return next(v for v in f.vivas if v.nombre == nombre)


def dos_cuentas(motor):
    poner_cuenta(motor, "org1", False)
    poner_cuenta(motor, "org2", True)


def test_sin_cuentas_el_orden_es_el_del_env(motor, config):
    f, _ = foto(motor, config)
    assert f.con_cuentas is False
    b = Bolsa(motor, config)
    assert b.recalcular(set(), AHORA)
    assert [c.fuente for c in b.ordenar("produccion", set())] == ["org1", "org2"]
    assert [c.fuente for c in b.ordenar("extra", set())] == ["org1"]


def test_saldo_por_cuenta(motor, config):
    dos_cuentas(motor)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    poner_concesion(motor, "org2", 200, nov(3, 0), datetime(2026, 12, 3))
    gasto(motor, "crm", "org1", 30, nov(10))
    gasto(motor, "crm", "org2", 20, nov(11))
    f, _ = foto(motor, config)
    assert f.con_cuentas
    u, r = viva(f, "org1"), viva(f, "org2")
    assert (u.libre_micro, u.vence, u.tarjeta) == (170 * M, date(2026, 11, 17), False)
    assert (r.libre_micro, r.vence, r.tarjeta) == (180 * M, date(2026, 12, 3), True)
    assert f.gasto_mes == {"crm": 50 * M}


def test_dia_conciliado_reemplaza_lo_registrado(motor, config):
    poner_cuenta(motor, "org1", False)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    gasto(motor, "crm", "org1", 5, nov(10))
    gasto(motor, "crm", "org1", 3, nov(11))
    conciliado(motor, "org1", nov(10, 0), 8)
    f, _ = foto(motor, config)
    assert viva(f, "org1").libre_micro == 189 * M
    assert f.conciliada == {"org1": AHORA}


def test_tarjeta_si_la_tabla_o_el_env_lo_dicen(motor, config):
    poner_cuenta(motor, "org1", False)
    poner_cuenta(motor, "org2", False)                  # la tabla dice sin tarjeta, PIA_FUENTES dice pagado
    f, _ = foto(motor, config)
    assert viva(f, "org2").tarjeta is True


def test_estimada_no_cuenta_en_una_cuenta_con_tarjeta(motor, config):
    dos_cuentas(motor)
    poner_concesion(motor, "org2", 200, nov(3, 0), datetime(2026, 12, 3), confirmada=False)
    poner_concesion(motor, "org1", 200, nov(10, 0), nov(22, 0), confirmada=False)
    f, _ = foto(motor, config)
    assert viva(f, "org2").libre_micro == 0 and viva(f, "org1").libre_micro == 200 * M
    assert [c.cuenta_en_saldo for c in sorted(f.concesiones, key=lambda c: c.cuenta)] == [True, False]


@pytest.mark.parametrize("horas,esperado", [(10, True), (40, False)])
def test_conciliacion_reciente(motor, config, horas, esperado):
    dos_cuentas(motor)
    conciliado(motor, "org2", nov(11, 0), 0, recibido=AHORA - timedelta(hours=horas))
    f, _ = foto(motor, config)
    assert viva(f, "org2").conciliada_reciente is esperado


def test_topes(motor, config):
    with motor.begin() as c:
        c.execute(insert(tope).values(proyecto="crm", tope_mensual_micro=1 * M, modo="corte", actualizado=AHORA))
        c.execute(insert(tope).values(proyecto="lotes", tope_mensual_micro=10 * M, modo="aviso", actualizado=AHORA))
        c.execute(insert(tope).values(proyecto="nada", tope_mensual_micro=10 * M, modo="corte", actualizado=AHORA))
    gasto(motor, "crm", "org1", 2, nov(11))
    gasto(motor, "lotes", "org1", 8.5, nov(11))
    gasto(motor, "lotes", "org1", 99, datetime(2026, 10, 31, 23))      # otro mes
    f, av = foto(motor, config)
    assert f.cortados == {"crm"}
    claves = {a.clave for a in av}
    assert {"tope:crm:2026-11:100", "tope:lotes:2026-11:80"} <= claves
    assert not any(c.startswith("tope:nada") for c in claves)


def test_pagado_cada_200_pesos(motor, config):
    dos_cuentas(motor)
    ya_conciliada(motor, "org2")
    gasto(motor, "crm", "org2", 12, nov(11))           # org2 sin concesiones: todo es pagado
    f, av = foto(motor, config)
    assert f.pagado_mes_micro == 12 * M
    a = next(a for a in av if a.clave.startswith("pagado:"))
    assert a.clave == "pagado:2026-11:1"
    assert "MX$200" in a.texto and "crm US$12.00" in a.texto and "supuesto" in a.texto
    otra = cargar({"PIA_DB_URL": "sqlite://", "PIA_FUENTES": "org1:gratis,org2:pagado",
                   "PIA_LLAVE_ORG1": "sk-falsa-u", "PIA_LLAVE_ORG2": "sk-falsa-r", "PIA_TIPO_CAMBIO": "18"})
    _, av18 = calcular(leer(motor, AHORA), otra, AHORA)
    a18 = next(a for a in av18 if a.clave.startswith("pagado:"))
    assert "a 18 MXN por USD" in a18.texto and "supuesto" not in a18.texto


def test_gasto_de_la_gratis_no_es_pagado(motor, config):
    gasto(motor, "crm", "org1", 50, nov(11))
    f, av = foto(motor, config)
    assert f.pagado_mes_micro == 0 and not any(a.clave.startswith("pagado:") for a in av)


def test_presupuesto_excedido(motor, config):
    dos_cuentas(motor)
    ya_conciliada(motor, "org2")
    with motor.begin() as c:
        c.execute(insert(ajuste).values(clave="presupuesto_pagado_micro", valor=5 * M, actualizado=AHORA))
    gasto(motor, "crm", "org2", 12, nov(11))
    f, av = foto(motor, config)
    assert f.presupuesto_micro == 5 * M
    assert "presupuesto:2026-11" in {a.clave for a in av}


@pytest.mark.parametrize("t,marca", [(AHORA, "5d"), (datetime(2026, 11, 15, 12), "2d"), (nov(10), None)])
def test_perdida_a_5_y_a_2_dias(motor, config, t, marca):
    poner_cuenta(motor, "org1", False)
    ya_conciliada(motor, "org1")
    i = poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    for d in range(5, 10):
        gasto(motor, "lotes", "org1", 1, nov(d))
    _, av = foto(motor, config, t)
    claves = {a.clave for a in av if a.clave.startswith("perdida:")}
    assert claves == ({f"perdida:{i}:{marca}"} if marca else set())


def test_llave_por_vencer(motor, config):
    poner_cuenta(motor, "org1", False, llave_vence=nov(20, 0))
    poner_cuenta(motor, "org2", True, llave_vence=datetime(2026, 12, 30))
    _, av = foto(motor, config)
    assert {a.clave for a in av if a.clave.startswith("llave:")} == {"llave:org1:2026-11-20"}


def test_cuenta_inactiva_no_se_usa(motor, config):
    poner_cuenta(motor, "org1", False, activa=False)
    poner_cuenta(motor, "org2", True)
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    assert [v.nombre for v in b.foto.vivas] == ["org2"]
    assert [c.fuente for c in b.ordenar("produccion", set())] == ["org2"]
    with pytest.raises(SinCredito):
        b.ordenar("extra", set())


def test_cuenta_sin_llave_en_el_env(motor, config):
    dos_cuentas(motor)
    poner_cuenta(motor, "otra", False)
    f, _ = foto(motor, config)
    assert f.sin_llave == ("otra",) and "otra" not in {v.nombre for v in f.vivas}


def test_recalcular_escribe_cada_aviso_una_vez(motor, config):
    with motor.begin() as c:
        c.execute(insert(tope).values(proyecto="crm", tope_mensual_micro=1 * M, modo="corte", actualizado=AHORA))
    gasto(motor, "crm", "org1", 2, nov(11))
    b = Bolsa(motor, config)
    assert b.recalcular(set(), AHORA) and b.recalcular(set(), AHORA)
    assert Bolsa(motor, config).recalcular(set(), AHORA)          # otro contenedor con su propia memoria
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(aviso)).scalar() == 1
    assert b.cortado("crm") and not b.cortado("lotes")


def test_cambio_de_cuenta_en_uso(motor, config):
    dos_cuentas(motor)
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    assert b.en_uso == "org1"
    b.recalcular({"org1"}, AHORA)
    assert b.en_uso == "org2"
    with motor.connect() as c:
        claves = list(c.execute(select(aviso.c.clave_unica)).scalars())
        ch = [dict(f._mapping) for f in c.execute(select(cambio))]
    assert claves == ["en-uso:2026-11-12:org1:org2"]
    assert [(x["ordenado_por"], x["accion"]) for x in ch] == [("pasarela (automático)", "cuenta-en-uso")]


def test_base_caida_conserva_la_foto(motor, config):
    b = Bolsa(motor, config)
    assert b.recalcular(set(), AHORA)
    anterior = b.foto
    b.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    assert b.recalcular(set(), AHORA) is False and b.foto is anterior


def test_sumas_son_enteros(motor, config):
    """En Postgres SUM() regresa Decimal."""
    dos_cuentas(motor)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    gasto(motor, "crm", "org1", 1.5, nov(10))
    f, _ = foto(motor, config)
    assert all(type(v) is int for v in f.gasto_mes.values())
    assert all(type(v.libre_micro) is int for v in f.vivas)


def test_tipo_de(motor, config):
    dos_cuentas(motor)
    poner_concesion(motor, "org2", 200, nov(3, 0), datetime(2026, 12, 3))
    conciliado(motor, "org2", nov(11, 0), 0, recibido=AHORA - timedelta(hours=2))
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    assert (b.tipo_de("org2"), b.tipo_de("org1"), b.tipo_de("nadie")) == ("pagado", "gratis", None)
    # extra nunca gasta en una cuenta con tarjeta, ni con saldo gratis ni con conciliación de hace 2 h
    assert viva(b.foto, "org2").conciliada_reciente and viva(b.foto, "org2").libre_micro == 200 * M
    assert [c.fuente for c in b.ordenar("extra", set())] == ["org1"]
    assert not hasattr(b, "puede_extra")


def dic(d, h=12):
    return datetime(2026, 12, d, h)


def test_concesion_vencida_del_mes_anterior_sigue_repartiendo(motor, config):
    """la concesión A vence el 1-dic pero el gasto del 25 al 30-nov es suyo, no de B."""
    poner_cuenta(motor, "org1", False)
    poner_concesion(motor, "org1", 200, nov(20, 0), datetime(2026, 12, 1))
    poner_concesion(motor, "org1", 200, nov(25, 0), datetime(2026, 12, 25))
    for d in range(25, 31):
        gasto(motor, "lotes", "org1", 10, nov(d))
    f, _ = foto(motor, config, dic(5))
    assert viva(f, "org1").libre_micro == 200 * M


def test_concesion_vencida_no_infla_el_pagado_del_mes(motor, config):
    """Misma superposición en una cuenta con tarjeta: el pagado de diciembre es solo lo que ninguna cubrió."""
    poner_cuenta(motor, "org2", True)
    ya_conciliada(motor, "org2")
    poner_concesion(motor, "org2", 200, nov(20, 0), datetime(2026, 12, 1))
    poner_concesion(motor, "org2", 200, nov(25, 0), datetime(2026, 12, 25))
    for d in range(25, 31):
        gasto(motor, "crm", "org2", 60, nov(d))
    for d in range(1, 5):
        gasto(motor, "crm", "org2", 60, dic(d))
    f, _ = foto(motor, config, dic(5))
    # A cubre 200 de noviembre, B los otros 160 y 40 de diciembre; el resto de diciembre (200) es pagado
    assert f.pagado_mes_micro == 200 * M


def test_la_ventana_se_cierra_en_cadena(motor, config):
    """Cada pasada de la ventana encuentra una concesión más vieja que también repartió gasto."""
    poner_cuenta(motor, "org1", False)
    poner_concesion(motor, "org1", 200, datetime(2026, 10, 15), datetime(2026, 12, 20))        # C, la vigente
    poner_concesion(motor, "org1", 100, datetime(2026, 10, 1), datetime(2026, 10, 20))         # D
    poner_concesion(motor, "org1", 100, datetime(2026, 9, 20), datetime(2026, 10, 5))          # E
    for d in range(1, 5):
        gasto(motor, "lotes", "org1", 30, datetime(2026, 10, d))        # 120: E 100, D 20
    for d in range(15, 20):
        gasto(motor, "lotes", "org1", 30, datetime(2026, 10, d))        # 150: D 80, C 70
    f, _ = foto(motor, config, dic(5))
    assert viva(f, "org1").libre_micro == 130 * M and viva(f, "org1").vence == date(2026, 12, 20)


def test_concesion_invalida_se_ignora(motor, config):
    poner_cuenta(motor, "org1", False)
    poner_concesion(motor, "org1", 200, nov(10, 0), nov(10, 0))          # vence == otorgada
    poner_concesion(motor, "org1", 100, nov(5, 0), nov(20, 0))
    f, _ = foto(motor, config)
    assert viva(f, "org1").libre_micro == 100 * M and len(f.concesiones) == 1


def claves_pagado(motor):
    with motor.connect() as c:
        return sorted(c.execute(select(aviso.c.clave_unica).where(aviso.c.clave_unica.like("pagado:%"))).scalars())


def test_aviso_pagado_estable_y_un_solo_salto(motor, config):
    dos_cuentas(motor)
    ya_conciliada(motor, "org2")
    gasto(motor, "crm", "org2", 42, nov(11))                      # US$42 a 20 = MX$840: pasos 1 a 4 de golpe
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    b.recalcular(set(), AHORA)
    assert claves_pagado(motor) == ["pagado:2026-11:4"]
    gasto(motor, "crm", "org2", 20, nov(11))                      # US$62 = MX$1,240: paso 6
    b.recalcular(set(), AHORA)
    b.recalcular(set(), AHORA)
    assert claves_pagado(motor) == ["pagado:2026-11:4", "pagado:2026-11:6"]


def test_clave_larga_no_se_reescribe(motor, config, monkeypatch):
    """La bolsa compara con la clave ya truncada a LARGO_CLAVE, igual que avisar: la segunda vuelta no llama a avisar."""
    from pasarela import avisos as modulo_avisos
    from pasarela import bolsa as modulo
    # Los nombres de proyecto miden hasta 40 (Postgres los corta); se achica el largo de clave para forzar el truncado.
    LARGO_CLAVE = 20
    monkeypatch.setattr(modulo, "LARGO_CLAVE", LARGO_CLAVE)
    monkeypatch.setattr(modulo_avisos, "LARGO_CLAVE", LARGO_CLAVE)
    with motor.begin() as c:
        c.execute(insert(tope).values(proyecto="p" * 40, tope_mensual_micro=1 * M, modo="aviso", actualizado=AHORA))
    gasto(motor, "p" * 40, "org1", 2, nov(11))
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    assert b._escritas and all(len(k) <= LARGO_CLAVE for k in b._escritas)
    llamadas = []
    monkeypatch.setattr(modulo, "avisar", lambda motor_, lista: llamadas.append(lista) or set())
    assert b.recalcular(set(), AHORA)
    assert llamadas == []


def cuantos_avisos(motor):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(aviso)).scalar()


def test_tabla_cuenta_vacia_no_da_avisos_de_dinero(motor, config):
    """sin filas en cuenta, el gasto de org2 (pagado en PIA_FUENTES) no cuenta como pagado."""
    with motor.begin() as c:
        c.execute(insert(ajuste).values(clave="presupuesto_pagado_micro", valor=1 * M, actualizado=AHORA))
    gasto(motor, "crm", "org2", 15, nov(11))
    f, av = foto(motor, config)
    assert f.pagado_mes_micro == 0 and not f.con_cuentas
    assert not any(a.clave.startswith(("pagado:", "presupuesto:")) for a in av)
    b = Bolsa(motor, config)
    assert b.recalcular(set(), AHORA) and b.recalcular(set(), AHORA)
    assert cuantos_avisos(motor) == 0


def test_la_primera_vuelta_no_escribe_aviso_de_cuenta_en_uso(motor, config):
    """Sin estado previo no hay cambio que avisar, aunque la primera cuenta ya esté agotada."""
    dos_cuentas(motor)
    b = Bolsa(motor, config)
    assert b.recalcular({"org1"}, AHORA)
    assert b.en_uso == "org2" and cuantos_avisos(motor) == 0
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(cambio)).scalar() == 0


def test_ventana_que_llega_al_tope_de_vueltas_avisa_una_linea(motor, monkeypatch, capsys):
    from pasarela import bolsa as modulo
    poner_cuenta(motor, "org1", False)
    poner_concesion(motor, "org1", 100, nov(5, 0), nov(20, 0))
    leer(motor, AHORA)
    assert capsys.readouterr().err == ""                         # converge: sin línea
    monkeypatch.setattr(modulo, "VUELTAS_VENTANA", 1)
    leer(motor, AHORA)
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "ventana" in err and "sk-" not in err


def test_tipo_de_toma_como_tarjeta_una_cuenta_inactiva(motor):
    """Aunque la fila esté inactiva y PIA_FUENTES no diga pagado, un extra fijado sigue recibiendo el 402."""
    otra = cargar({"PIA_DB_URL": "sqlite://", "PIA_FUENTES": "org1:gratis,org2:gratis",
                   "PIA_LLAVE_ORG1": "sk-falsa-u", "PIA_LLAVE_ORG2": "sk-falsa-r"})
    poner_cuenta(motor, "org1", False)
    poner_cuenta(motor, "org2", True, activa=False)
    b = Bolsa(motor, otra)
    b.recalcular(set(), AHORA)
    assert b.viva("org2") is None
    assert (b.tipo_de("org2"), b.tipo_de("org1"), b.tipo_de("nadie")) == ("pagado", "gratis", None)


def test_cuenta_nunca_conciliada_no_da_avisos_de_dinero(motor, config):
    """Se siembran concesiones antes de la primera conciliación y la bolsa no ve el gasto
    real; residual 2: ORG2 sin llave de administrador. Ninguna de las dos da avisos de pérdida ni de pagado."""
    dos_cuentas(motor)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    poner_concesion(motor, "org2", 200, nov(3, 0), datetime(2026, 12, 3))
    with motor.begin() as c:
        c.execute(insert(ajuste).values(clave="presupuesto_pagado_micro", valor=1 * M, actualizado=AHORA))
    gasto(motor, "crm", "org2", 250, nov(11))              # más que la concesión: habría pagado
    f, av = foto(motor, config)
    assert f.sin_datos == {"org1", "org2"}
    assert f.pagado_mes_micro == 0
    assert not any(a.clave.startswith(("perdida:", "pagado:", "presupuesto:")) for a in av)


def test_la_primera_conciliacion_enciende_los_avisos(motor, config):
    poner_cuenta(motor, "org1", False)
    i = poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    _, av = foto(motor, config)
    assert not any(a.clave.startswith("perdida:") for a in av)
    conciliado(motor, "org1", nov(6, 0), 1)
    f, av = foto(motor, config)
    assert "org1" not in f.sin_datos
    assert f"perdida:{i}:5d" in {a.clave for a in av}


def test_el_orden_no_cambia_por_sin_datos(motor, config):
    """Sin datos solo apaga avisos: el orden sigue con el estimado. La concesión de org2 vence antes que
    la de org1: si sin datos pusiera su saldo en cero, org2 (con tarjeta) caería detrás de org1."""
    dos_cuentas(motor)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    poner_concesion(motor, "org2", 200, nov(3, 0), nov(15, 0))
    b = Bolsa(motor, config)
    b.recalcular(set(), AHORA)
    assert b.foto.sin_datos == {"org1", "org2"}
    assert [c.fuente for c in b.ordenar("produccion", set())] == ["org2", "org1"]


@pytest.mark.parametrize("recibido,texto", [
    (None, "sin datos"),
    (AHORA - timedelta(hours=2), "cuadrado a las 08:00"),
    (datetime(2026, 11, 12, 3, 0), "cuadrado el 11-nov a las 22:00"),
    (AHORA - timedelta(hours=40), "estimado (último cuadre el 10-nov)"),
])
def test_cuadre(recibido, texto):
    from pasarela.bolsa import cuadre
    assert cuadre(recibido, AHORA) == texto


def test_la_foto_sabe_que_programas_estan_dados_de_baja(motor, config):
    from pasarela.esquema import proyecto
    with motor.begin() as c:
        for nombre, activo in (("vivo", True), ("viejo", False)):
            c.execute(insert(proyecto).values(nombre=nombre, clase="extra", llave_hash=nombre * 8, huella="abcd",
                                              activo=activo, dueno="admin", creado=AHORA))
    f, _ = foto(motor, config)
    assert f.bajas == frozenset({"viejo"})


def test_la_concesion_lleva_el_ritmo_con_que_se_proyecto(motor, config):
    """El panel muestra este ritmo junto a la pérdida: es el promedio de los 7 días anteriores a hoy (el mismo que usa
    proyectar), y la estimada de una cuenta con tarjeta, que no entra al saldo, no lleva ritmo."""
    dos_cuentas(motor)
    poner_concesion(motor, "org1", 200, nov(5, 0), nov(17, 0))
    poner_concesion(motor, "org2", 200, nov(3, 0), datetime(2026, 12, 3), confirmada=False)
    for d in range(5, 10):
        gasto(motor, "lotes", "org1", 1, nov(d))
    f, _ = foto(motor, config)
    org1, org2 = sorted(f.concesiones, key=lambda c: c.cuenta)
    assert org1.ritmo_micro == 5 * M // 7
    assert org2.cuenta_en_saldo is False and org2.ritmo_micro is None
    assert org1.perdida_micro > 0
