"""Lecturas: un programa puede ver el uso del mes de otros, solo de los que el administrador le autorice (tabla lectura)."""
import json

import pytest
from sqlalchemy import func, select

from pasarela import admin
from pasarela.esquema import cambio, crear_motor, lectura

CITA = "«que el crm vea a lotes»"
POR = "admin"


def cuantos(motor, tabla):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(tabla)).scalar()


def con_proyectos(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.alta(motor, "lotes", "extra", POR, CITA)
    admin.alta(motor, "zeta", "extra", POR, CITA)


def test_agregar_ver_y_quitar_dejan_bitacora(motor):
    con_proyectos(motor)
    antes = cuantos(motor, cambio)
    admin.lectura_agregar(motor, "crm", "zeta", POR, CITA)
    admin.lectura_agregar(motor, "crm", "lotes", POR, CITA)
    admin.lectura_agregar(motor, "lotes", "crm", POR, CITA)
    assert [(f["lector"], f["visto"]) for f in admin.lecturas(motor)] == [
        ("crm", "lotes"), ("crm", "zeta"), ("lotes", "crm")]
    assert [f["visto"] for f in admin.lecturas(motor, "crm")] == ["lotes", "zeta"]
    admin.lectura_quitar(motor, "crm", "zeta", POR, CITA)
    assert [(f["lector"], f["visto"]) for f in admin.lecturas(motor)] == [("crm", "lotes"), ("lotes", "crm")]
    with motor.connect() as c:
        filas = c.execute(select(cambio.c.accion, cambio.c.detalle, cambio.c.cita, cambio.c.ordenado_por)
                          .order_by(cambio.c.id)).all()[antes:]
    assert [f.accion for f in filas] == ["lectura-alta"] * 3 + ["lectura-quitar"]
    assert json.loads(filas[-1].detalle) == {"lector": "crm", "visto": "zeta"}
    assert {(f.cita, f.ordenado_por) for f in filas} == {(CITA, POR)}


@pytest.mark.parametrize("lector,visto,motivo", [
    ("nadie", "lotes", "No existe el proyecto nadie"),
    ("crm", "nadie", "No existe el proyecto nadie"),
    ("crm", "crm", "ya ve su propio uso"),
])
def test_agregar_valida_los_proyectos(motor, lector, visto, motivo):
    con_proyectos(motor)
    antes = cuantos(motor, cambio)
    with pytest.raises(SystemExit, match=motivo):
        admin.lectura_agregar(motor, lector, visto, POR, CITA)
    assert cuantos(motor, lectura) == 0 and cuantos(motor, cambio) == antes


def test_agregar_repetida_o_quitar_inexistente_no_escribe(motor):
    con_proyectos(motor)
    admin.lectura_agregar(motor, "crm", "lotes", POR, CITA)
    antes = cuantos(motor, cambio)
    with pytest.raises(SystemExit, match="ya puede ver"):
        admin.lectura_agregar(motor, "crm", "lotes", POR, CITA)
    with pytest.raises(SystemExit, match="no puede ver"):
        admin.lectura_quitar(motor, "crm", "zeta", POR, CITA)
    with pytest.raises(SystemExit, match="No existe el proyecto nadie"):
        admin.lectura_quitar(motor, "nadie", "lotes", POR, CITA)
    assert cuantos(motor, lectura) == 1 and cuantos(motor, cambio) == antes


def test_sin_por_o_cita_no_escribe(motor):
    con_proyectos(motor)
    antes = cuantos(motor, cambio)
    for por, cita in (("", CITA), (POR, "  ")):
        with pytest.raises(SystemExit):
            admin.lectura_agregar(motor, "crm", "lotes", por, cita)
    assert cuantos(motor, lectura) == 0 and cuantos(motor, cambio) == antes


def test_main_lectura(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    motor = crear_motor(f"sqlite:///{tmp_path}/a.db")
    con_proyectos(motor)
    admin.main(["lectura", "agregar", "crm", "lotes", "--por", POR, "--cita", CITA])
    admin.main(["lectura", "agregar", "lotes", "zeta", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["lectura", "ver"])
    salida = capsys.readouterr().out
    assert "crm ve a lotes" in salida and "lotes ve a zeta" in salida
    admin.main(["lectura", "ver", "crm"])
    assert "lotes ve a zeta" not in capsys.readouterr().out
    admin.main(["lectura", "quitar", "crm", "lotes", "--por", POR, "--cita", CITA])
    assert [(f["lector"], f["visto"]) for f in admin.lecturas(motor)] == [("lotes", "zeta")]
    with pytest.raises(SystemExit):
        admin.main(["lectura", "agregar", "crm", "zeta", "--cita", CITA])
