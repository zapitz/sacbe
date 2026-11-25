import io
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from pasarela import admin, avisos
from pasarela.avisos import Aviso
from pasarela.esquema import ajuste, cambio, concesion, conciliacion, crear_motor, cuenta, tope
from pasarela.tiempo import ahora

CITA = "«sí al plan»"
POR = "admin"
M = 1_000_000


def d(texto):
    return datetime.strptime(texto, "%Y-%m-%d")


def cuantos(motor, tabla):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(tabla)).scalar()


def una(motor, tabla):
    with motor.connect() as c:
        return dict(c.execute(select(tabla)).one()._mapping)


def con_org1(motor):
    admin.cuenta_agregar(motor, "org1", "dueno@example.com", False, d("2027-11-07"), POR, CITA)


def test_cuenta_agregar_y_bitacora(motor):
    con_org1(motor)
    f = una(motor, cuenta)
    assert (f["tiene_tarjeta"], f["activa"], f["llave_vence"]) == (False, True, d("2027-11-07"))
    assert una(motor, cambio)["accion"] == "cuenta-alta"
    with pytest.raises(SystemExit):
        con_org1(motor)
    assert cuantos(motor, cambio) == 1                       # el intento repetido no deja bitácora


def test_cuenta_cambiar(motor):
    con_org1(motor)
    admin.cuenta_cambiar(motor, "org1", POR, CITA, tarjeta=True, activa=False)
    f = una(motor, cuenta)
    assert (f["tiene_tarjeta"], f["activa"]) == (True, False)
    with pytest.raises(SystemExit):
        admin.cuenta_cambiar(motor, "org1", POR, CITA)
    with pytest.raises(SystemExit):
        admin.cuenta_cambiar(motor, "nadie", POR, CITA, activa=True)
    assert cuantos(motor, cambio) == 2


def test_credito_de_consola(motor):
    con_org1(motor)
    i, vence = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-05"), d("2026-11-17"), False, None,
                                     POR, CITA)
    f = una(motor, concesion)
    assert (f["id"], f["monto_micro"], f["origen"], f["confirmada"], f["vence"]) == \
           (i, 200 * M, "consola", True, d("2026-11-17"))
    assert vence == d("2026-11-17")


def test_credito_estimado_vence_a_los_33_dias(motor):
    """aunque la última confirmada haya durado 12 días (ciclo parcial), la estimada dura 33."""
    con_org1(motor)
    admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-05"), d("2026-11-17"), False, None, POR, CITA)
    i, vence = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-15"), None, True, None, POR, CITA)
    assert vence == d("2026-12-18")
    with motor.connect() as c:
        f = dict(c.execute(select(concesion).where(concesion.c.id == i)).one()._mapping)
    assert (f["origen"], f["confirmada"]) == ("estimada", False)


def test_credito_estimado_sin_historia_tambien_33_dias(motor):
    con_org1(motor)
    assert admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-15"), None, True, None, POR, CITA)[1] == \
           d("2026-12-18")


@pytest.mark.parametrize("cuenta_,vence,estimada", [("org1", None, False), ("org1", "2026-11-01", False),
                                                    ("nadie", "2026-11-17", False)])
def test_credito_que_no_cuadra_no_escribe(motor, cuenta_, vence, estimada):
    con_org1(motor)
    with pytest.raises(SystemExit):
        admin.credito_agregar(motor, cuenta_, 200 * M, d("2026-11-05"), d(vence) if vence else None, estimada,
                              None, POR, CITA)
    assert cuantos(motor, concesion) == 0 and cuantos(motor, cambio) == 1      # solo el alta de la cuenta


def test_credito_lleva_su_nota(motor):
    con_org1(motor)
    admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-03"), d("2026-12-03"), False,
                          "otorgada estimada: vence menos 30 días", POR, CITA)
    with motor.connect() as c:
        detalle = c.execute(select(cambio.c.detalle).where(cambio.c.accion == "credito-alta")).scalar()
    assert json.loads(detalle)["nota"] == "otorgada estimada: vence menos 30 días"


def test_credito_confirmar(motor):
    con_org1(motor)
    i, _ = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-15"), None, True, None, POR, CITA)
    admin.credito_confirmar(motor, i, 200 * M, d("2026-11-15"), d("2026-11-28"), POR, "«ya la vi en la consola»")
    f = una(motor, concesion)
    assert (f["origen"], f["confirmada"], f["vence"]) == ("consola", True, d("2026-11-28"))
    with pytest.raises(SystemExit):
        admin.credito_confirmar(motor, 999, 1 * M, d("2026-11-15"), d("2026-11-28"), POR, CITA)


def test_topes(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.alta(motor, "lotes", "extra", POR, CITA)
    assert admin.tope_poner(motor, "lotes", 5 * M, None, POR, CITA) == "corte"
    assert admin.tope_poner(motor, "crm", 5 * M, None, POR, CITA) == "aviso"
    admin.tope_poner(motor, "crm", 7 * M, "corte", POR, CITA)
    with motor.connect() as c:
        filas = {p: (t, m) for p, t, m in c.execute(select(tope.c.proyecto, tope.c.tope_mensual_micro, tope.c.modo))}
    assert filas == {"lotes": (5 * M, "corte"), "crm": (7 * M, "corte")}
    admin.tope_quitar(motor, "crm", POR, CITA)
    assert cuantos(motor, tope) == 1
    for llamada in (lambda: admin.tope_quitar(motor, "crm", POR, CITA),
                    lambda: admin.tope_poner(motor, "nadie", 1 * M, None, POR, CITA),
                    lambda: admin.tope_poner(motor, "crm", 0, None, POR, CITA)):
        with pytest.raises(SystemExit):
            llamada()


def test_presupuesto(motor):
    admin.presupuesto_poner(motor, 50 * M, POR, CITA)
    admin.presupuesto_poner(motor, 80 * M, POR, CITA)
    assert una(motor, ajuste)["valor"] == 80 * M and cuantos(motor, cambio) == 2


def test_conciliacion_reemplaza_el_dia(motor):
    datos = {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 1500}, {"dia": "2026-11-10", "micro": 0}]}}
    assert admin.conciliacion_cargar(motor, datos, POR, CITA) == 2
    admin.conciliacion_cargar(motor, {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 2500}]}}, POR, CITA)
    with motor.connect() as c:
        filas = {f"{dia:%Y-%m-%d}": m for dia, m in c.execute(select(conciliacion.c.dia, conciliacion.c.micro))}
    assert filas == {"2026-11-09": 2500, "2026-11-10": 0} and cuantos(motor, cambio) == 2


@pytest.mark.parametrize("malo", [
    {"cuentas": {"org1": [{"dia": "HOY", "micro": 1}]}},
    {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": -1}]}},
    {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 1.5}]}},
    {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": True}]}},
    {"cuentas": {"org1": [{"dia": "9-nov", "micro": 1}]}},
    {"cuentas": {"Mala": []}},
    {"nada": 1},
    [],
])
def test_conciliacion_rechaza_datos_raros(motor, malo):
    if isinstance(malo, dict) and "cuentas" in malo and "org1" in malo["cuentas"]:
        for x in malo["cuentas"]["org1"]:
            if x["dia"] == "HOY":
                x["dia"] = f"{ahora():%Y-%m-%d}"
    with pytest.raises(SystemExit):
        admin.conciliacion_cargar(motor, malo, POR, CITA)
    assert cuantos(motor, conciliacion) == 0 and cuantos(motor, cambio) == 0


def base_en_archivo(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/a.db"
    monkeypatch.setenv("PIA_DB_URL", url)
    monkeypatch.setenv("PIA_FUENTES", "org1:gratis,org2:pagado")
    monkeypatch.setenv("PIA_LLAVE_ORG1", "sk-falsa-org1")
    monkeypatch.setenv("PIA_LLAVE_ORG2", "sk-falsa-org2")
    return url


def test_main_avisos(tmp_path, monkeypatch, capsys):
    motor = crear_motor(base_en_archivo(tmp_path, monkeypatch))
    avisos.avisar(motor, [Aviso("a", "pasarela-ia: uno"), Aviso("b", "pasarela-ia: dos")])
    admin.main(["avisos", "--pendientes"])
    lineas = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["texto"] for x in lineas] == ["pasarela-ia: uno", "pasarela-ia: dos"]
    admin.main(["avisos", "--marcar", str(lineas[0]["id"])])
    assert capsys.readouterr().out.strip() == "marcados 1"
    admin.main(["avisos", "--pendientes", "--max", "5"])
    assert [json.loads(x)["texto"] for x in capsys.readouterr().out.splitlines()] == ["pasarela-ia: dos"]


def test_main_conciliacion_lee_stdin(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 1500}]}})))
    admin.main(["conciliacion", "cargar", "--por", "launchd conciliar (Mac)", "--cita", CITA])
    assert capsys.readouterr().out.strip() == "conciliación: 1 días cargados"


def test_main_saldos(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    hoy = ahora().date()
    otorgada, vence = f"{hoy - timedelta(days=2):%Y-%m-%d}", f"{hoy + timedelta(days=10):%Y-%m-%d}"
    admin.main(["cuenta", "agregar", "org1", "--correo-plan", "a@ejemplo.mx", "--tarjeta", "no",
                "--por", POR, "--cita", CITA])
    admin.main(["credito", "agregar", "--cuenta", "org1", "--monto", "200", "--otorgada", otorgada,
                "--vence", vence, "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["saldos"])
    salida = capsys.readouterr().out
    assert "org1" in salida and "US$200.00" in salida and "sin datos" in salida
    assert "sk-falsa" not in salida


@pytest.mark.parametrize("orden", [
    ["cuenta", "agregar", "x1", "--correo-plan", "a@b.mx", "--tarjeta", "no"],
    ["cuenta", "cambiar", "x1", "--activa", "no"],
    ["credito", "agregar", "--cuenta", "x1", "--monto", "1", "--otorgada", "2026-11-01", "--vence", "2026-11-02"],
    ["credito", "confirmar", "1", "--monto", "1", "--otorgada", "2026-11-01", "--vence", "2026-11-02"],
    ["credito", "ajustar", "1", "--vence", "2026-11-02"],
    ["tope", "x1", "--usd", "1"],
    ["presupuesto", "--usd", "1"],
    ["conciliacion", "cargar"],
])
def test_main_de_dinero_sin_por_no_corre(tmp_path, monkeypatch, orden):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    with pytest.raises(SystemExit):
        admin.main([*orden, "--cita", CITA])
    assert not (tmp_path / "a.db").exists()


def test_credito_con_vence_igual_o_anterior_a_otorgada_se_rechaza(motor):
    """saldo.repartir da por perdida del todo una concesión que vence en o antes de su otorgada."""
    con_org1(motor)
    for vence in ("2026-11-05", "2026-11-04"):
        with pytest.raises(SystemExit, match="--vence debe ser después de --otorgada"):
            admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-05"), d(vence), False, None, POR, CITA)
    assert cuantos(motor, concesion) == 0 and cuantos(motor, cambio) == 1
    i, _ = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-15"), None, True, None, POR, CITA)
    with pytest.raises(SystemExit, match="después de --otorgada"):
        admin.credito_confirmar(motor, i, 200 * M, d("2026-11-15"), d("2026-11-15"), POR, CITA)
    assert una(motor, concesion)["confirmada"] is False


def test_main_credito_con_vence_igual_a_otorgada_no_escribe(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    admin.main(["cuenta", "agregar", "org1", "--correo-plan", "a@ejemplo.mx", "--tarjeta", "no",
                "--por", POR, "--cita", CITA])
    with pytest.raises(SystemExit):
        admin.main(["credito", "agregar", "--cuenta", "org1", "--monto", "200", "--otorgada", "2026-11-05",
                    "--vence", "2026-11-05", "--por", POR, "--cita", CITA])
    motor = crear_motor(f"sqlite:///{tmp_path}/a.db")
    assert cuantos(motor, concesion) == 0


@pytest.mark.parametrize("valor,esperado", [("si", True), ("sí", True), ("no", False)])
def test_main_tarjeta_acepta_si_con_y_sin_acento(tmp_path, monkeypatch, valor, esperado):
    base_en_archivo(tmp_path, monkeypatch)
    admin.main(["cuenta", "agregar", "org2", "--correo-plan", "a@ejemplo.mx", "--tarjeta", valor,
                "--por", POR, "--cita", CITA])
    assert una(crear_motor(f"sqlite:///{tmp_path}/a.db"), cuenta)["tiene_tarjeta"] is esperado


def test_main_dinero_con_decimales_se_guarda_exacto(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    admin.main(["presupuesto", "--usd", "12.345678", "--por", POR, "--cita", CITA])
    assert una(crear_motor(f"sqlite:///{tmp_path}/a.db"), ajuste)["valor"] == 12_345_678
    assert capsys.readouterr().out.strip() == "presupuesto pagado del mes: US$12.35"
    for malo in ("-1", "abc", "nan"):
        with pytest.raises(SystemExit):
            admin.main(["presupuesto", "--usd", malo, "--por", POR, "--cita", CITA])


@pytest.mark.parametrize("malo", ["0", "-1", "abc"])
def test_main_avisos_max_debe_ser_al_menos_uno(tmp_path, monkeypatch, malo):
    base_en_archivo(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        admin.main(["avisos", "--pendientes", "--max", malo])


@pytest.mark.parametrize("texto,mensaje", [
    ("9223372036854.775808", "demasiado grande"),       # 2^63 micro
    ("1e9999999", "demasiado grande"),                  # desborda Decimal: ArithmeticError
    ("NaN", "no es un número"),
    ("Infinity", "no es un número"),
    ("-Infinity", "no es un número"),
    ("-1", "negativo"),
])
def test_micro_rechaza_con_mensaje_claro(texto, mensaje):
    with pytest.raises(SystemExit, match=mensaje):
        admin._micro(texto, "--usd")


def test_micro_acepta_el_maximo_exacto():
    assert admin._micro("9223372036854.775807", "--usd") == 2**63 - 1


def test_conciliacion_cargar_rechaza_micro_mayor_a_63_bits(motor):
    datos = {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 2**63}]}}
    with pytest.raises(SystemExit, match="demasiado grande"):
        admin.conciliacion_cargar(motor, datos, POR, CITA)
    assert cuantos(motor, conciliacion) == 0
    admin.conciliacion_cargar(motor, {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 2**63 - 1}]}}, POR, CITA)
    assert cuantos(motor, conciliacion) == 1


def test_main_saldos_dice_cuadrado_despues_de_conciliar(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    hoy = ahora().date()
    admin.main(["cuenta", "agregar", "org1", "--correo-plan", "a@ejemplo.mx", "--tarjeta", "no",
                "--por", POR, "--cita", CITA])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"cuentas": {"org1": [{"dia": f"{hoy - timedelta(days=1):%Y-%m-%d}", "micro": 0}]}})))
    admin.main(["conciliacion", "cargar", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["saldos"])
    salida = capsys.readouterr().out
    lineas = salida.splitlines()
    assert any(l.startswith("org1") and "cuadrado a las" in l for l in lineas)    # org1 ya cuadrada
    assert any(l.startswith("org2") and l.endswith("sin datos") for l in lineas)     # org2 (del .env) nunca cuadrada


def test_credito_ajustar_no_toca_confirmada_ni_origen(motor):
    con_org1(motor)
    i, _ = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-15"), None, True, None, POR, CITA)
    n = admin.credito_ajustar(motor, i, POR, "«la de org1 vence el 17»", vence=d("2026-12-17"))
    f = una(motor, concesion)
    assert (f["otorgada"], f["vence"], f["monto_micro"], f["origen"], f["confirmada"]) == \
           (d("2026-11-15"), d("2026-12-17"), 200 * M, "estimada", False)
    assert (n["vence"], n["confirmada"]) == (d("2026-12-17"), False)
    with motor.connect() as c:
        ch = dict(c.execute(select(cambio).where(cambio.c.accion == "credito-ajuste")).one()._mapping)
    assert json.loads(ch["detalle"]) == {"id": i, "antes": {"vence": "2026-12-18"}, "ahora": {"vence": "2026-12-17"}}
    assert (ch["ordenado_por"], ch["cita"]) == (POR, "«la de org1 vence el 17»")


def test_credito_ajustar_una_confirmada_sigue_confirmada(motor):
    con_org1(motor)
    i, _ = admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-05"), d("2026-11-17"), False, None, POR, CITA)
    admin.credito_ajustar(motor, i, POR, CITA, monto_micro=150 * M, otorgada=d("2026-11-04"))
    f = una(motor, concesion)
    assert (f["monto_micro"], f["otorgada"], f["vence"], f["origen"], f["confirmada"]) == \
           (150 * M, d("2026-11-04"), d("2026-11-17"), "consola", True)


@pytest.mark.parametrize("cambios", [{}, {"monto_micro": 0}, {"otorgada": d("2026-11-17")},
                                     {"vence": d("2026-11-05")}])
def test_credito_ajustar_que_no_cuadra_no_escribe(motor, cambios):
    con_org1(motor)
    admin.credito_agregar(motor, "org1", 200 * M, d("2026-11-05"), d("2026-11-17"), False, None, POR, CITA)
    with pytest.raises(SystemExit):
        admin.credito_ajustar(motor, 1, POR, CITA, **cambios)
    with pytest.raises(SystemExit):
        admin.credito_ajustar(motor, 99, POR, CITA, vence=d("2026-12-01"))
    f = una(motor, concesion)
    assert (f["monto_micro"], f["otorgada"], f["vence"]) == (200 * M, d("2026-11-05"), d("2026-11-17"))
    assert cuantos(motor, cambio) == 2                       # alta de la cuenta y alta del crédito


def test_main_credito_ajustar(tmp_path, monkeypatch, capsys):
    base_en_archivo(tmp_path, monkeypatch)
    admin.main(["cuenta", "agregar", "org1", "--correo-plan", "a@ejemplo.mx", "--tarjeta", "no",
                "--por", POR, "--cita", CITA])
    admin.main(["credito", "agregar", "--cuenta", "org1", "--monto", "200", "--otorgada", "2026-11-15",
                "--estimada", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["credito", "ajustar", "1", "--vence", "2026-12-17", "--por", POR, "--cita", CITA])
    assert capsys.readouterr().out.strip() == "concesión #1: US$200.00 del 15-nov al 17-dic · sigue por confirmar"
