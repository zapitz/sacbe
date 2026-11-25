from datetime import datetime

import pytest
from sqlalchemy import func, select

from pasarela import admin
from pasarela.almacen import guardar_registro
from pasarela.catalogo import Catalogo
from pasarela.esquema import cambio, proyecto
from pasarela.llaves import hash_llave

CITA = "«dale a todo lo que estás proponiendo»"


def test_alta_guarda_hash_y_bitacora(motor):
    llave = admin.alta(motor, "lotes", "extra", "admin", CITA)
    assert llave.startswith("pia_lotes_")
    with motor.connect() as c:
        p = dict(c.execute(select(proyecto)).one()._mapping)
        ch = dict(c.execute(select(cambio)).one()._mapping)
    assert p["llave_hash"] == hash_llave(llave) and p["huella"] == llave[-4:]
    assert llave not in str(p) and llave not in str(ch)
    assert (ch["cita"], ch["accion"], ch["ordenado_por"]) == (CITA, "proyecto-alta", "admin")


def test_sin_cita_no_cambia_nada(motor):
    with pytest.raises(SystemExit):
        admin.alta(motor, "lotes", "extra", "admin", "   ")
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(proyecto)).scalar() == 0
        assert c.execute(select(func.count()).select_from(cambio)).scalar() == 0


def test_por_en_blanco_no_cambia_nada(motor):
    with pytest.raises(SystemExit):
        admin.alta(motor, "lotes", "extra", "  ", CITA)
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(proyecto)).scalar() == 0
        assert c.execute(select(func.count()).select_from(cambio)).scalar() == 0


def test_nombre_invalido(motor):
    with pytest.raises(SystemExit):
        admin.alta(motor, "Lotes_Mac", "extra", "admin", CITA)


def test_rotar_invalida_la_vieja(motor):
    vieja = admin.alta(motor, "lotes", "extra", "admin", CITA)
    nueva = admin.rotar(motor, "lotes", "admin", "«rótala»")
    cat = Catalogo(motor, 3600)
    cat.refrescar()
    assert cat.proyecto_por_llave(vieja) is None and cat.proyecto_por_llave(nueva).nombre == "lotes"


def test_rotar_dado_de_baja_no_lo_reactiva(motor):
    vieja = admin.alta(motor, "lotes", "extra", "admin", CITA)
    admin.baja(motor, "lotes", "admin", "«ya no»")
    with pytest.raises(SystemExit) as e:
        admin.rotar(motor, "lotes", "admin", "«rótala»")
    assert e.value.code == "El proyecto lotes está dado de baja."
    with motor.connect() as c:
        p = dict(c.execute(select(proyecto)).one()._mapping)
        assert c.execute(select(func.count()).select_from(cambio)).scalar() == 2      # alta y baja, nada más
    assert p["activo"] is False and p["llave_hash"] == hash_llave(vieja)


def test_baja_y_clase(motor):
    llave = admin.alta(motor, "crm", "extra", "admin", CITA)
    admin.clase(motor, "crm", "produccion", "admin", "«es de producción»")
    cat = Catalogo(motor, 3600)
    cat.refrescar()
    assert cat.proyecto_por_llave(llave).clase == "produccion"
    admin.baja(motor, "crm", "admin", "«ya no»")
    cat.refrescar()
    assert cat.proyecto_por_llave(llave) is None
    with pytest.raises(SystemExit):
        admin.baja(motor, "noexiste", "admin", "«x»")


def test_reactivar_fuente(motor):
    cat = Catalogo(motor, 3600)
    cat.marcar_agotada("org1", "saldo")
    admin.reactivar(motor, "org1", "admin", "«ya le cargaron crédito»")
    cat.refrescar()
    assert cat.agotadas() == set()


def test_reactivar_sin_marca_no_deja_bitacora(motor):
    with pytest.raises(SystemExit):
        admin.reactivar(motor, "nadie", "admin", "«x»")
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(cambio)).scalar() == 0


def test_uso_por_proyecto_y_fuente(motor):
    base = {"ruta": "v1/messages", "modelo": "m", "status": 200, "lote": False, "entrada": 10, "salida": 5,
            "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0, "latencia_ms": 1,
            "request_id": None, "reintentos": 0}
    guardar_registro(motor, {**base, "proyecto": "crm", "fuente": "org1", "costo_micro": 100})
    guardar_registro(motor, {**base, "proyecto": "crm", "fuente": "org1", "costo_micro": 50})
    guardar_registro(motor, {**base, "proyecto": "crm", "fuente": "org2", "costo_micro": 7})
    filas = admin.uso(motor, datetime(2000, 1, 1))
    assert [(f["proyecto"], f["fuente"], f["peticiones"], f["micro"], f["entrada"]) for f in filas] == \
           [("crm", "org1", 2, 150, 20), ("crm", "org2", 1, 7, 10)]
    assert all(type(f[k]) is int for f in filas for k in ("peticiones", "micro", "entrada", "salida"))


def test_main_imprime_la_llave_una_vez(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    admin.main(["alta", "lotes", "--clase", "extra", "--por", "admin", "--cita", CITA])
    salida = capsys.readouterr().out.strip().splitlines()
    assert len(salida) == 1 and salida[0].startswith("pia_lotes_")
    admin.main(["proyectos"])
    assert "lotes" in capsys.readouterr().out
    admin.main(["cambios"])
    assert CITA in capsys.readouterr().out


@pytest.mark.parametrize("orden", [["alta", "lotes", "--clase", "extra"], ["rotar", "lotes"], ["baja", "lotes"],
                                   ["clase", "lotes", "extra"], ["reactivar", "org1"]])
def test_main_sin_por_no_corre(tmp_path, monkeypatch, orden):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    with pytest.raises(SystemExit):
        admin.main([*orden, "--cita", CITA])
    assert not (tmp_path / "a.db").exists()          # ni siquiera abrió la base
