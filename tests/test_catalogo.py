from sqlalchemy import create_engine, insert

from pasarela.catalogo import Catalogo
from pasarela.esquema import proyecto
from pasarela.llaves import hash_llave, nueva_llave
from pasarela.tiempo import ahora


def alta(motor, nombre, clase, llave, activo=True):
    with motor.begin() as c:
        c.execute(insert(proyecto).values(nombre=nombre, clase=clase, llave_hash=hash_llave(llave),
                                          huella=llave[-4:], activo=activo, dueno="admin", creado=ahora()))


def test_busca_por_llave(motor):
    llave = nueva_llave("lotes")
    alta(motor, "lotes", "extra", llave)
    alta(motor, "viejo", "extra", nueva_llave("viejo"), activo=False)
    cat = Catalogo(motor, agotada_s=3600)
    assert cat.refrescar() and cat.cargado and cat.cuantos() == 1
    assert cat.proyecto_por_llave(llave).clase == "extra"
    assert cat.proyecto_por_llave("pia_lotes_otra") is None


def test_agotada_se_comparte_por_la_base(motor):
    Catalogo(motor, agotada_s=3600).marcar_agotada("org1", "saldo")
    otro = Catalogo(motor, agotada_s=3600)
    otro.refrescar()
    assert otro.agotadas() == {"org1"}


def test_agotada_vence(motor):
    cat = Catalogo(motor, agotada_s=-1)
    cat.marcar_agotada("org1", "saldo")
    assert cat.agotadas() == set()


def test_marcar_agotada_no_muta_el_dict_que_se_esta_leyendo(motor):
    cat = Catalogo(motor, agotada_s=3600)
    antes = cat._agotadas
    cat.marcar_agotada("org1", "saldo")
    assert antes == {} and cat._agotadas is not antes and cat.agotadas() == {"org1"}


def test_si_la_base_falla_se_queda_con_la_copia(motor):
    llave = nueva_llave("crm")
    alta(motor, "crm", "produccion", llave)
    cat = Catalogo(motor, agotada_s=3600)
    cat.refrescar()
    cat.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    assert cat.refrescar() is False
    assert cat.proyecto_por_llave(llave).nombre == "crm"
    cat.marcar_agotada("org1", "saldo")        # no revienta aunque no pueda guardar
    assert cat.agotadas() == {"org1"}
