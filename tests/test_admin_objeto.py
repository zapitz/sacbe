"""ops/pia objeto: dar de alta a mano un archivo o un lote que nació fuera de la pasarela."""
import json

import pytest
from sqlalchemy import func, select

from pasarela import admin
from pasarela.almacen import registrar_costo_de_lote
from pasarela.esquema import cambio, objeto

CITA = "«registra los archivos viejos del crm»"
POR = "admin"
CUENTAS = {"org1", "org2"}


def cuantos(motor, tabla):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(tabla)).scalar()


def test_registrar_deja_el_objeto_y_la_bitacora(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    antes = cuantos(motor, cambio)
    admin.objeto_registrar(motor, "file_011CNha8iCJcU1wXNR6q4V8w", "archivo", "org2", "crm", CUENTAS, POR, CITA)
    o = admin.objeto_ver(motor, "file_011CNha8iCJcU1wXNR6q4V8w")
    assert (o["tipo"], o["fuente"], o["proyecto"], o["costo_registrado"]) == ("archivo", "org2", "crm", False)
    with motor.connect() as c:
        f = c.execute(select(cambio).order_by(cambio.c.id.desc())).first()
    assert cuantos(motor, cambio) == antes + 1 and (f.accion, f.cita, f.ordenado_por) == ("objeto-registrar", CITA, POR)
    assert json.loads(f.detalle) == {"id": "file_011CNha8iCJcU1wXNR6q4V8w", "tipo": "archivo", "cuenta": "org2",
                                     "proyecto": "crm"}
    assert admin.objeto_ver(motor, "file_nada") is None


@pytest.mark.parametrize("id_,tipo,cuenta,proyecto,por,cita,motivo", [
    ("file_1", "foto", "org2", "crm", POR, CITA, "El tipo es archivo o lote"),
    ("file 1", "archivo", "org2", "crm", POR, CITA, "Id inválido"),
    ("file_1\n", "archivo", "org2", "crm", POR, CITA, "Id inválido"),
    ("f" * 121, "archivo", "org2", "crm", POR, CITA, "Id inválido"),
    ("file_1", "archivo", "otra", "crm", POR, CITA, "La cuenta otra no está en PIA_FUENTES"),
    ("file_1", "archivo", "org2", "nadie", POR, CITA, "No existe el proyecto nadie"),
    ("file_1", "archivo", "org2", "crm", " ", CITA, "Falta --por"),
    ("file_1", "archivo", "org2", "crm", POR, " ", "Falta --cita"),
], ids=["tipo", "id-con-espacio", "id-con-salto-de-linea", "id-largo", "cuenta", "proyecto", "sin-por", "sin-cita"])
def test_registrar_valida_y_no_escribe(motor, id_, tipo, cuenta, proyecto, por, cita, motivo):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    antes = cuantos(motor, cambio)
    with pytest.raises(SystemExit, match=motivo):
        admin.objeto_registrar(motor, id_, tipo, cuenta, proyecto, CUENTAS, por, cita)
    assert cuantos(motor, objeto) == 0 and cuantos(motor, cambio) == antes


def test_un_objeto_registrado_no_cambia_de_dueno(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.alta(motor, "lotes", "extra", POR, CITA)
    admin.objeto_registrar(motor, "msgbatch_1", "lote", "org1", "lotes", CUENTAS, POR, CITA)
    antes = cuantos(motor, cambio)
    with pytest.raises(SystemExit, match="msgbatch_1 ya está registrado: lote de lotes en la cuenta org1"):
        admin.objeto_registrar(motor, "msgbatch_1", "lote", "org2", "crm", CUENTAS, POR, CITA)
    assert admin.objeto_ver(motor, "msgbatch_1")["proyecto"] == "lotes" and cuantos(motor, cambio) == antes


def test_main_registrar_y_ver(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    monkeypatch.setenv("PIA_FUENTES", "org1:gratis,org2:pagado")
    monkeypatch.setenv("PIA_LLAVE_ORG1", "sk-falsa-org1")
    monkeypatch.setenv("PIA_LLAVE_ORG2", "sk-falsa-org2")
    admin.main(["alta", "crm", "--clase", "produccion", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["objeto", "registrar", "file_viejo", "--tipo", "archivo", "--cuenta", "org2", "--proyecto", "crm",
                "--por", POR, "--cita", CITA])
    assert capsys.readouterr().out.strip() == "file_viejo: archivo de crm en la cuenta org2"
    admin.main(["objeto", "ver", "file_viejo"])
    assert capsys.readouterr().out.startswith("file_viejo: archivo de crm en la cuenta org2 · desde 20")
    admin.main(["objeto", "ver", "file_nada"])
    assert capsys.readouterr().out.strip() == "file_nada: no está registrado"
    with pytest.raises(SystemExit):
        admin.main(["objeto", "registrar", "file_otro", "--tipo", "archivo", "--cuenta", "org2", "--proyecto", "crm"])


def test_un_lote_registrado_a_mano_nace_con_el_costo_ya_contado(motor):
    """No se sabe qué costó ni se tiene su fila de uso; si no, bajar sus resultados sumaría un costo que nadie
    cobró por esta vía. Los archivos conservan costo_registrado=False (no tienen costo propio)."""
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.objeto_registrar(motor, "msgbatch_viejo", "lote", "org2", "crm", CUENTAS, POR, CITA)
    admin.objeto_registrar(motor, "file_viejo", "archivo", "org2", "crm", CUENTAS, POR, CITA)
    assert admin.objeto_ver(motor, "msgbatch_viejo")["costo_registrado"] is True
    assert admin.objeto_ver(motor, "file_viejo")["costo_registrado"] is False
    assert registrar_costo_de_lote(motor, "msgbatch_viejo", {}) is False               # bajar sus resultados no cobra
