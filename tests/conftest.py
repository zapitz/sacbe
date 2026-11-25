import os

# Antes de importar pasarela: las pruebas muestran las horas con un desfase fijo de -05:00.
os.environ["PIA_ZONA_HORARIA"] = "-05:00"

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from pasarela.app import crear_app
from pasarela.config import cargar
from pasarela.esquema import crear_motor, metadata, proyecto
from pasarela.llaves import hash_llave
from pasarela.tiempo import ahora

# El .env de producción más dos apagadores de ciclos: sin refresco de proyectos y sin leer /v1/models.
ENV = {"PIA_DB_URL": "sqlite://", "PIA_FUENTES": "org1:gratis,org2:pagado",
       "PIA_LLAVE_ORG1": "sk-falsa-org1", "PIA_LLAVE_ORG2": "sk-falsa-org2", "PIA_REFRESCO_S": "0",
       "PIA_CATALOGO_S": "0"}
LLAVES = {"prod": "pia_crm_" + "p" * 32, "extra": "pia_lotes_" + "e" * 32}


@pytest.fixture
def config():
    return cargar(ENV)


@pytest.fixture
def motor():
    """SQLite en memoria; con PIA_PRUEBAS_DB (compose.pruebas.yaml) apunta a Postgres y se limpia cada prueba."""
    url = os.environ.get("PIA_PRUEBAS_DB", "sqlite://")
    m = crear_motor(url)
    if not url.startswith("sqlite"):
        metadata.drop_all(m)
        metadata.create_all(m)
    yield m
    m.dispose()


class Falso:
    """Anthropic de mentira: guarda lo que recibe y contesta según la llave real (o '*' para todas)."""

    def __init__(self):
        self.recibidas: list[httpx.Request] = []
        self.respuestas = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.recibidas.append(request)
        f = self.respuestas.get(request.headers.get("x-api-key")) or self.respuestas["*"]
        return f(request)


@pytest.fixture
def falso():
    return Falso()


@pytest.fixture
def llaves():
    return LLAVES


@pytest.fixture
def app(config, motor, falso):
    """La app con crm (produccion) y lotes (extra), sin cliente: para llamarla como ASGI sin TestClient."""
    with motor.begin() as c:
        for nombre, clase, llave in (("crm", "produccion", LLAVES["prod"]), ("lotes", "extra", LLAVES["extra"])):
            c.execute(insert(proyecto).values(nombre=nombre, clase=clase, llave_hash=hash_llave(llave),
                                              huella=llave[-4:], activo=True, dueno="admin", creado=ahora()))
    return crear_app(config, motor, httpx.MockTransport(falso))


@pytest.fixture
def cliente(app):
    with TestClient(app) as c:
        yield c
