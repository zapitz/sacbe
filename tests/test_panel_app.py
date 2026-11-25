"""La ruta /panel: solo en el contenedor del panel, solo lectura, con la salud del gestor de al lado."""
import threading
import time

import anyio
import httpx
import pytest
from conftest import ENV, LLAVES
from fastapi.testclient import TestClient
from sqlalchemy import insert
from test_identidad import AUD, EQUIPO, POR, CITA, Certs, Par

from pasarela import admin, identidad
from pasarela import app as app_modulo
from pasarela.app import crear_app
from pasarela.config import cargar
from pasarela.esquema import cuenta, registro
from pasarela.identidad import VerificadorAccess
from pasarela.tiempo import ahora

SALUD = {"ok": True, "base": True, "proyectos": 2, "agotadas": [], "fallas_registro": 0}


def app_panel(motor, falso, **env):
    return crear_app(cargar({**ENV, "PIA_PANEL": "1", **env}), motor, httpx.MockTransport(falso))


def test_config_del_panel():
    assert cargar(ENV).panel is False
    assert all(cargar({**ENV, "PIA_PANEL": v}).panel for v in ("1", "si", "sí"))
    assert cargar(ENV).salud_api_url == "http://pasarela-ia-api:8080/salud"
    assert not any(cargar({**ENV, "PIA_PANEL": v}).panel for v in ("", "0", "no", "false"))
    assert cargar({**ENV, "PIA_PANEL_SALUD_URL": "http://otro:9/salud"}).salud_api_url == "http://otro:9/salud"


def test_sin_pia_panel_no_hay_panel(cliente):
    assert cliente.get("/panel").status_code == 404


def test_panel(motor, falso):
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=SALUD)
    with motor.begin() as c:
        c.execute(insert(cuenta).values(nombre="org1", correo_plan="u@ejemplo.mx", tiene_tarjeta=False,
                                        activa=True, creada=ahora()))
    with TestClient(app_panel(motor, falso)) as c:
        r = c.get("/panel")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "Sacbé · créditos de IA" in r.text and "Panel interno · dueño" in r.text and "El gestor contesta y tiene 2 programas con llave." in r.text
    assert [str(x.url) for x in falso.recibidas] == ["http://pasarela-ia-api:8080/salud"]   # nada a Anthropic


def test_panel_con_el_gestor_caido(motor, falso):
    def caido(req):
        raise httpx.ConnectError("no")
    falso.respuestas["*"] = caido
    with TestClient(app_panel(motor, falso)) as c:
        r = c.get("/panel")
    assert r.status_code == 200 and "El gestor no contesta." in r.text


def test_panel_con_el_gestor_lento(motor, falso, monkeypatch):
    """El tope de la sonda es de tiempo total: un gestor que no contesta a tiempo se lee como caído."""
    monkeypatch.setattr(app_modulo, "ESPERA_SONDA_S", 0.2)

    async def lento(req):
        await anyio.sleep(30)
        return httpx.Response(200, json=SALUD)
    falso.respuestas["*"] = lento
    with TestClient(app_panel(motor, falso)) as c:
        t0 = time.monotonic()
        r = c.get("/panel")
        espera = time.monotonic() - t0
    assert r.status_code == 200 and "El gestor no contesta." in r.text
    assert espera < 5


def test_panel_no_abre_por_la_puerta_publica(motor, falso):
    with TestClient(app_panel(motor, falso)) as c:
        r = c.get("/panel", headers={"x-pia-puerta": "publica", "x-api-key": LLAVES["prod"]})
    assert r.status_code == 404 and falso.recibidas == []


def test_panel_pasa_si_el_tipo_de_cambio_es_supuesto(motor, falso, monkeypatch):
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=SALUD)
    vistos = []
    original = app_modulo.panel.armar_vista

    def espia(*a, **kw):
        vistos.append(a[-1])
        return original(*a, **kw)
    monkeypatch.setattr(app_modulo.panel, "armar_vista", espia)
    for env, supuesto in (({}, True), ({"PIA_TIPO_CAMBIO": "18.5"}, False)):
        with TestClient(app_panel(motor, falso, **env)) as c:
            assert c.get("/panel").status_code == 200
        assert vistos[-1] is supuesto


# Sacbé: la puerta pública con Cloudflare Access

PAR = Par("kid-1")
CAB = "cf-access-jwt-assertion"
SIN_ACCESO = "Tu correo no tiene acceso. Pídeselo al administrador."


def app_sacbe(motor, falso, **env):
    env = {"PIA_SACBE_HOST": "testserver", **env}         # el Host que manda TestClient
    app = crear_app(cargar({**ENV, "PIA_SACBE": "1", "PIA_ACCESS_AUD": AUD, "PIA_ACCESS_EQUIPO": EQUIPO, **env}),
                    motor, httpx.MockTransport(falso))
    app.state.pia.verificador = VerificadorAccess(EQUIPO, AUD, traer_certs=Certs(PAR))
    return app


@pytest.fixture
def usuarios(motor, falso):
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=SALUD)
    admin.usuario_alta(motor, "dueno@example.com", "Admin", "dueno", POR, CITA)
    admin.usuario_alta(motor, "rosa@example.com", "Rosa", "responsable", POR, CITA)
    admin.usuario_alta(motor, "lucia@example.com", "Lucia", "lector", POR, CITA)
    for n, clase in (("crm", "produccion"), ("lotes", "extra"), ("viejo", "extra")):
        admin.alta(motor, n, clase, POR, CITA)
    admin.baja(motor, "viejo", POR, CITA)
    admin.usuario_asignar(motor, "rosa@example.com", "crm", POR, CITA)
    admin.usuario_asignar(motor, "lucia@example.com", "viejo", POR, CITA)
    with motor.begin() as c:
        for p in ("crm", "lotes", "viejo"):
            c.execute(insert(registro).values(
                ts=ahora(), proyecto=p, fuente="org1", ruta="/v1/messages", modelo="m", status=200, lote=False,
                entrada=1, salida=1, cache_5m=0, cache_1h=0, cache_lectura=0, busquedas=0, costo_micro=1_500_000,
                latencia_ms=1, reintentos=0))
    identidad.olvidar_usuarios()


def entrar(c, correo, **extra):
    return c.get("/panel", headers={CAB: PAR.token(email=correo), **extra})


def test_sacbe_sin_jwt_o_con_jwt_malo_es_403(motor, falso, usuarios):
    with TestClient(app_sacbe(motor, falso)) as c:
        for cab in ({}, {CAB: "basura"}, {CAB: Par("kid-1").token()}):
            r = c.get("/panel", headers=cab)
            assert r.status_code == 403 and SIN_ACCESO in r.text and "<script" not in r.text
            assert r.headers["cache-control"] == "no-store"
    assert [str(x.url) for x in falso.recibidas] == []         # ni la sonda del gestor para quien no entró


def test_sacbe_correo_sin_alta_o_dado_de_baja_es_403(motor, falso, usuarios):
    admin.usuario_alta(motor, "baja@example.com", "Baja", "lector", POR, CITA)
    admin.usuario_baja(motor, "baja@example.com", POR, CITA)
    identidad.olvidar_usuarios()
    with TestClient(app_sacbe(motor, falso)) as c:
        for correo in ("intruso@example.net", "baja@example.com"):
            r = entrar(c, correo)
            assert r.status_code == 403 and SIN_ACCESO in r.text and correo not in r.text


def test_sacbe_dueno_ve_todo_y_se_ve_quien_es(motor, falso, usuarios):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = entrar(c, "Dueno@Example.com")
    assert r.status_code == 200 and "dueno@example.com · dueño" in r.text
    assert "Sacbé · créditos de IA" in r.text
    for visible in ("Concesiones vigentes", "Pagado con tarjeta", "Próximas concesiones", "Salud del gestor"):
        assert visible in r.text
    assert "crm" in r.text and "lotes" in r.text
    assert '<span class="nombre">viejo</span>' not in r.text      # el de baja no se lista entre los programas
    assert "Proyectos: viejo" in r.text                            # (la tarjeta de Lucia sí dice que lo tiene)
    assert [str(x.url) for x in falso.recibidas] == ["http://pasarela-ia-api:8080/salud"]


def test_sacbe_responsable_ve_solo_lo_suyo_y_no_llama_al_gestor(motor, falso, usuarios):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = entrar(c, "rosa@example.com")
    assert r.status_code == 200 and "rosa@example.com · responsable" in r.text
    assert "Ves solo tus proyectos" in r.text and "crm" in r.text
    for oculto in ("lotes", "Concesiones vigentes", "Pagado con tarjeta", "Próximas concesiones", "Salud del gestor"):
        assert oculto not in r.text
    assert falso.recibidas == []


def test_sacbe_lector_con_solo_un_proyecto_de_baja_no_ve_nada(motor, falso, usuarios):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = entrar(c, "lucia@example.com")
    assert r.status_code == 200 and "No tienes proyectos asignados." in r.text and "viejo" not in r.text


def test_sacbe_con_la_base_caida_es_503_y_no_403(motor, falso, usuarios, monkeypatch):
    def cae(m, c):
        raise RuntimeError("base caída")
    monkeypatch.setattr(admin, "usuario_de", cae)
    with TestClient(app_sacbe(motor, falso)) as c:
        r = entrar(c, "dueno@example.com")
        sin_jwt = c.get("/panel")
    assert r.status_code == 503 and "La base no responde, reintenta en un minuto." in r.text
    assert SIN_ACCESO not in r.text and "dueno@" not in r.text
    assert sin_jwt.status_code == 403         # sin JWT ni se toca la base


def test_sacbe_con_la_base_colgada_es_503(motor, falso, usuarios, monkeypatch):
    monkeypatch.setattr(app_modulo, "ESPERA_QUIEN_S", 0.2)
    soltar = threading.Event()
    monkeypatch.setattr(admin, "usuario_de", lambda m, c: soltar.wait(1.5))
    try:
        with TestClient(app_sacbe(motor, falso)) as c:
            t0 = time.monotonic()
            r = entrar(c, "dueno@example.com")
            tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 503 and tardo < 1.2


def test_sacbe_solo_sirve_panel_y_salud(motor, falso, usuarios):
    with TestClient(app_sacbe(motor, falso)) as c:
        for ruta in ("/anthropic/v1/models", "/anthropic/v1/messages", "/v1/uso", "/v1/catalogo", "/", "/docs",
                     "/openapi.json", "/otro"):
            for metodo in ("GET", "POST"):
                r = c.request(metodo, ruta, headers={CAB: PAR.token(email="dueno@example.com"),
                                                     "x-api-key": LLAVES["prod"]})
                assert r.status_code in (404, 405), (metodo, ruta, r.status_code)
                assert r.status_code == 404 or metodo == "POST"
        assert c.get("/v1/uso", headers={"x-api-key": LLAVES["prod"]}).status_code == 404
        assert c.get("/salud").status_code in (200, 503)
    assert falso.recibidas == []
    app = app_sacbe(motor, falso)
    assert {getattr(r, "path", None) for r in app.routes} == {"/salud", "/panel", "/panel/accion", "/panel/costo",
                                                                  "/panel/llave/{token}"}  # sin proxy
    assert not any(m.cls.__name__ == "Puerta" for m in app.user_middleware)


def test_el_panel_de_interno_sigue_con_el_proxy_y_sin_jwt(motor, falso):
    """Sin sacbe todo sigue igual: el proxy está montado y el panel no pide JWT."""
    with TestClient(app_panel(motor, falso)) as c:
        assert c.get("/panel").status_code == 200
        assert c.get("/anthropic/v1/models").status_code == 401        # el proxy contesta: pide llave
        assert c.get("/v1/uso").status_code == 401
