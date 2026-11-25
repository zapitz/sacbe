from sqlalchemy import func, insert, select

from pasarela import avisos
from pasarela.avisos import Aviso
from pasarela.esquema import aviso, cambio
from pasarela.tiempo import ahora


def cuantos(motor, tabla):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(tabla)).scalar()


def test_avisar_escribe_una_vez_por_clave(motor):
    assert avisos.avisar(motor, [Aviso("agotada:org1:2026-11-12", "uno")]) == {"agotada:org1:2026-11-12"}
    assert avisos.avisar(motor, [Aviso("agotada:org1:2026-11-12", "otra vez")]) == {"agotada:org1:2026-11-12"}
    assert cuantos(motor, aviso) == 1


def test_carrera_entre_contenedores_no_truena_ni_duplica(motor, monkeypatch):
    """api y panel recalculan a la vez: el segundo choca con la clave única y se calla, sin dejar su cambio."""
    avisos.avisar(motor, [Aviso("en-uso:2026-11-12:org1:org2", "uno")])
    monkeypatch.setattr(avisos, "_existentes", lambda m, claves: set())
    r = avisos.avisar(motor, [Aviso("en-uso:2026-11-12:org1:org2", "uno", {"accion": "cuenta-en-uso"})])
    assert r == {"en-uso:2026-11-12:org1:org2"}
    assert cuantos(motor, aviso) == 1 and cuantos(motor, cambio) == 0


def test_aviso_con_cambio_deja_bitacora_automatica(motor):
    avisos.avisar(motor, [Aviso("en-uso:2026-11-12:org1:org2", "la cuenta en uso cambió de org1 a org2",
                                {"accion": "cuenta-en-uso", "antes": "org1", "ahora": "org2"})])
    with motor.connect() as c:
        f = dict(c.execute(select(cambio)).one()._mapping)
    assert (f["ordenado_por"], f["accion"], f["cita"]) == \
           ("pasarela (automático)", "cuenta-en-uso", "la cuenta en uso cambió de org1 a org2")


def test_pendientes_y_marcar(motor):
    avisos.avisar(motor, [Aviso("a", "primero"), Aviso("b", "segundo"), Aviso("c", "tercero")])
    p = avisos.pendientes(motor, maximo=2)
    assert [x["texto"] for x in p] == ["primero", "segundo"]
    assert avisos.marcar(motor, [p[0]["id"]]) == 1
    assert avisos.marcar(motor, [p[0]["id"]]) == 0                  # ya estaba marcado
    assert [x["texto"] for x in avisos.pendientes(motor)] == ["segundo", "tercero"]


def test_clave_larga_se_recorta_igual_al_buscar_y_al_guardar(motor):
    larga = "x" * 300
    avisos.avisar(motor, [Aviso(larga, "uno")])
    avisos.avisar(motor, [Aviso(larga, "uno")])
    assert cuantos(motor, aviso) == 1


def test_escribir_dentro_de_una_transaccion_abierta_tolera_la_clave_repetida(motor):
    """Para quien ya tiene su propia transacción: el choque no la aborta y lo demás de ella se conserva."""
    with motor.begin() as c:
        c.execute(insert(cambio).values(ts=ahora(), ordenado_por="x", cita="y", accion="z", detalle="{}"))
        assert avisos.escribir(c, Aviso("k", "uno", {"accion": "cuenta-en-uso"})) is True
        assert avisos.escribir(c, Aviso("k", "dos", {"accion": "cuenta-en-uso"})) is False
        assert avisos.escribir(c, Aviso("otra", "tres")) is True
    assert cuantos(motor, aviso) == 2
    assert cuantos(motor, cambio) == 2                  # el suyo y el del primer aviso; el choque no dejó el suyo


def test_escribir_como_primera_sentencia_se_deshace_si_quien_llama_falla(motor):
    """En SQLite el SAVEPOINT no debe abrir ni cerrar por su cuenta la transacción de quien llama."""
    try:
        with motor.begin() as c:
            avisos.escribir(c, Aviso("k", "uno", {"accion": "cuenta-en-uso"}))
            raise RuntimeError("falla de quien llama")
    except RuntimeError:
        pass
    assert cuantos(motor, aviso) == 0 and cuantos(motor, cambio) == 0
