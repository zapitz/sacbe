"""Vigía de programas callados: avisa solo por los que tenían tráfico real y dejaron de pasar por el gestor."""
from datetime import datetime, timedelta

from sqlalchemy import insert, select

from pasarela import admin
from pasarela.esquema import aviso, registro

AHORA = datetime(2026, 11, 25, 12, 0)
BASE = {"fuente": "org2", "ruta": "v1/messages", "modelo": "m", "status": 200, "lote": False, "entrada": 1,
        "salida": 1, "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0, "costo_micro": 1,
        "latencia_ms": 1, "request_id": None, "reintentos": 0}


def _peticiones(motor, nombre, n, hace_h):
    with motor.begin() as c:
        for i in range(n):
            c.execute(insert(registro).values(ts=AHORA - timedelta(hours=hace_h, minutes=i), proyecto=nombre, **BASE))


def _alta(motor, *nombres):
    for n in nombres:
        admin.alta(motor, n, "produccion", "prueba", "«prueba»")


def test_avisa_al_que_tenia_trafico_y_se_callo(motor):
    _alta(motor, "web", "viva", "nueva")
    _peticiones(motor, "web", 20, hace_h=72)       # tenía tráfico y lleva 72 h sin nada
    _peticiones(motor, "viva", 20, hace_h=72)
    _peticiones(motor, "viva", 1, hace_h=1)          # sigue pasando
    _peticiones(motor, "nueva", 2, hace_h=72)        # solo la prueba de conexión: no cuenta
    assert [f["proyecto"] for f in admin.silencios(motor, AHORA)] == ["web"]


def test_el_aviso_se_escribe_una_sola_vez(motor):
    _alta(motor, "web")
    _peticiones(motor, "web", 20, hace_h=72)
    assert admin.avisar_silencios(motor, AHORA) == 1
    admin.avisar_silencios(motor, AHORA + timedelta(minutes=5))
    with motor.connect() as c:
        textos = list(c.execute(select(aviso.c.texto)).scalars())
    assert len(textos) == 1 and "web lleva 48 h sin pasar por el gestor" in textos[0]


def test_los_extra_no_se_vigilan(motor):
    admin.alta(motor, "lote", "extra", "prueba", "«prueba»")
    _peticiones(motor, "lote", 300, hace_h=72)        # corre por ratos y se calla: es normal
    assert admin.silencios(motor, AHORA) == []


def test_los_de_baja_no_avisan(motor):
    _alta(motor, "viejo")
    admin.baja(motor, "viejo", "prueba", "«prueba»")
    _peticiones(motor, "viejo", 20, hace_h=72)
    assert admin.silencios(motor, AHORA) == []
