"""GET /v1/uso: cada programa ve solo su propio uso del mes, con su llave, por la puerta interna y por la pública."""
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from pasarela import proxy, puerta
from pasarela.esquema import lectura, proyecto, registro
from pasarela.llaves import hash_llave
from pasarela.tiempo import ahora

PUB = {"x-pia-puerta": "publica", "x-real-ip": "203.0.113.9"}
RAIZ = Path(__file__).resolve().parents[1]
BASE = {"ruta": "v1/messages", "modelo": "m", "status": 200, "lote": False, "entrada": 1, "salida": 1,
        "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0, "latencia_ms": 1,
        "request_id": None, "reintentos": 0}


def fila(cliente, proyecto_, micro, ts):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(registro).values(ts=ts, proyecto=proyecto_, fuente="org1", costo_micro=micro, **BASE))


def inicio_de_mes():
    return ahora().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def test_uso_del_mes_solo_del_proyecto(cliente, llaves, falso):
    t = ahora().replace(microsecond=0)
    fila(cliente, "crm", 1_234_567, t - timedelta(seconds=5))
    fila(cliente, "crm", 100, t)
    fila(cliente, "crm", 9_000_000, inicio_de_mes() - timedelta(days=2))      # mes pasado: no cuenta
    fila(cliente, "lotes", 5_000_000, t)                                      # de otro programa: no se ve
    r = cliente.get("/v1/uso", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"proyecto": "crm", "mes": f"{t:%Y-%m}", "peticiones": 2, "usd": 1.2347,
                        "ultima": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "cuenta_en_uso": "org1"}
    assert falso.recibidas == []


def test_uso_sin_peticiones(cliente, llaves):
    r = cliente.get("/v1/uso", headers={"authorization": f"Bearer {llaves['extra']}"})
    assert r.status_code == 200
    assert r.json() | {"mes": None} == {"proyecto": "lotes", "mes": None, "peticiones": 0, "usd": 0,
                                       "ultima": None, "cuenta_en_uso": "org1"}


def test_ultima_puede_ser_de_otro_mes(cliente, llaves):
    viejo = (inicio_de_mes() - timedelta(days=3)).replace(hour=10)
    fila(cliente, "crm", 1_000_000, viejo)
    d = cliente.get("/v1/uso", headers={"x-api-key": llaves["prod"]}).json()
    assert (d["peticiones"], d["usd"], d["ultima"]) == (0, 0, viejo.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_sin_cuenta_disponible_es_null(cliente, llaves):
    """lotes es extra: nunca usa org2 (tarjeta); con org1 agotada no le queda ninguna."""
    estado = cliente.app.state.pia
    estado.catalogo.marcar_agotada("org1", "sin saldo")
    assert "org1" in estado.catalogo.agotadas()
    assert cliente.get("/v1/uso", headers={"x-api-key": llaves["extra"]}).json()["cuenta_en_uso"] is None
    assert cliente.get("/v1/uso", headers={"x-api-key": llaves["prod"]}).json()["cuenta_en_uso"] == "org2"


def test_uso_sin_llave_401(cliente):
    for h in ({}, {"x-api-key": "pia_nadie_" + "x" * 32}, {"authorization": "Bearer "}):
        r = cliente.get("/v1/uso", headers=h)
        assert r.status_code == 401
        assert r.json() == {"type": "error", "error": {"type": "authentication_error",
                                                       "message": "pasarela-ia: llave interna inválida"}}


def test_uso_con_base_congelada_es_503(cliente, llaves, monkeypatch):
    monkeypatch.setattr(proxy, "ESPERA_USO_S", 0.2)
    soltar = threading.Event()
    monkeypatch.setattr(proxy, "uso_de", lambda *_: soltar.wait(1.5))
    try:
        t0 = time.monotonic()
        r = cliente.get("/v1/uso", headers={"x-api-key": llaves["prod"]})
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
    assert r.json()["error"]["message"].startswith("pasarela-ia: ") and tardo < 1


def test_uso_con_base_caida_es_503(cliente, llaves, monkeypatch):
    def rota(*_):
        raise RuntimeError("base caída")
    monkeypatch.setattr(proxy, "uso_de", rota)
    r = cliente.get("/v1/uso", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 503 and r.json()["error"]["message"].startswith("pasarela-ia: ")


def test_uso_por_la_puerta_publica(cliente, llaves):
    r = cliente.get("/v1/uso", headers={**PUB, "x-api-key": llaves["extra"]})
    assert r.status_code == 200 and r.json()["proyecto"] == "lotes" and "x-pia-fuente" not in r.headers


def test_llaves_malas_al_uso_castigan_la_ip(cliente):
    malas = [cliente.get("/v1/uso", headers={**PUB, "x-api-key": "pia_nadie_" + "x" * 32}).status_code
             for _ in range(20)]
    assert malas == [401] * 20
    assert cliente.get("/v1/uso", headers=PUB).status_code == 429


def test_los_ejemplos_de_proxy_y_la_puerta_abren_el_uso():
    assert "Path(`/v1/uso`)" in (RAIZ / "docs/ejemplos/traefik.yml").read_text()
    assert " /v1/uso " in (RAIZ / "docs/ejemplos/Caddyfile").read_text()
    assert "/v1/uso" in puerta.EXACTAS


def bloques_caddy() -> dict[str, list[str]]:
    """Los sitios activos del Caddyfile de ejemplo (sin comentarios), con sus líneas."""
    sitios, actual, nivel = {}, None, 0
    for linea in (RAIZ / "docs/ejemplos/Caddyfile").read_text().splitlines():
        x = linea.strip()
        if not x or x.startswith("#"):
            continue
        if nivel == 0 and x.endswith("{") and x != "{":
            actual = x[:-1].strip()
            sitios[actual] = []
        elif actual is not None:
            sitios[actual].append(x)
        nivel += x.count("{") - x.count("}")
        if nivel == 0:
            actual = None
    return sitios


def test_caddy_marca_la_api_publica_y_solo_abre_su_lista():
    """La API pública solo pasa la lista de puerta.py, siempre con X-Pia-Puerta, y lo demás es 404."""
    sitios = bloques_caddy()
    api = sitios["ia.example.com"]
    assert "path /anthropic/v1/* /v1/catalogo /v1/uso /salud" in api
    assert api.count("header_up X-Pia-Puerta publica") == 1 and "respond 404" in api
    assert sum(x.startswith("reverse_proxy ") for x in api) == 1
    assert set(sitios) == {"ia.example.com", "panel.example.com"}      # el panel interno no se publica aquí
    assert not any("pasarela-ia-panel" in x for x in sitios["panel.example.com"])


def test_traefik_marca_la_api_publica():
    regla = (RAIZ / "docs/ejemplos/traefik.yml").read_text()
    assert 'X-Pia-Puerta: "publica"' in regla and "middlewares: [pasarela-ia-puerta]" in regla
    assert "pasarela-ia-panel" not in regla.split("http:", 1)[1]


def otro(cliente, nombre, clase="extra", activo=True):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(proyecto).values(nombre=nombre, clase=clase, llave_hash=hash_llave(f"pia_{nombre}_" + "z" * 32),
                                          huella="zzzz", activo=activo, dueno="admin", creado=ahora()))


def puede_ver(cliente, lector, visto):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(lectura).values(lector=lector, visto=visto, creado=ahora()))


def test_todos_trae_el_propio_y_los_autorizados(cliente, llaves, falso):
    t = ahora().replace(microsecond=0)
    otro(cliente, "zeta", "produccion")
    otro(cliente, "baja", activo=False)
    otro(cliente, "ajeno")                                     # existe pero crm no lo puede ver
    for nombre in ("zeta", "lotes", "baja"):
        puede_ver(cliente, "crm", nombre)
    puede_ver(cliente, "lotes", "ajeno")                       # de otro lector: no cuenta para crm
    fila(cliente, "crm", 1_000_000, t)
    fila(cliente, "lotes", 2_500_000, t - timedelta(seconds=3))
    fila(cliente, "lotes", 9_000_000, inicio_de_mes() - timedelta(days=2))
    fila(cliente, "baja", 7_000_000, t)
    fila(cliente, "ajeno", 7_000_000, t)
    estado = cliente.app.state.pia
    estado.catalogo.marcar_agotada("org1", "sin saldo")
    r = cliente.get("/v1/uso?proyectos=todos", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    mes = f"{t:%Y-%m}"
    assert r.json() == {"proyectos": [
        {"proyecto": "crm", "mes": mes, "peticiones": 1, "usd": 1.0, "ultima": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "cuenta_en_uso": "org2"},
        {"proyecto": "lotes", "mes": mes, "peticiones": 1, "usd": 2.5,
         "ultima": (t - timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "cuenta_en_uso": None},
        {"proyecto": "zeta", "mes": mes, "peticiones": 0, "usd": 0, "ultima": None, "cuenta_en_uso": "org2"},
    ]}
    assert falso.recibidas == []


def test_todos_sin_lecturas_es_solo_el_propio(cliente, llaves):
    sencillo = cliente.get("/v1/uso", headers={"x-api-key": llaves["extra"]}).json()
    r = cliente.get("/v1/uso", params={"proyectos": "todos"}, headers={**PUB, "x-api-key": llaves["extra"]})
    assert r.status_code == 200 and r.json() == {"proyectos": [sencillo]}


@pytest.mark.parametrize("consulta", ["proyectos=", "proyectos=crm", "proyectos=TODOS", "proyectos=todos&proyectos=x"])
def test_otro_valor_de_proyectos_es_400(cliente, llaves, consulta):
    r = cliente.get(f"/v1/uso?{consulta}", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert r.json()["error"]["message"].startswith("pasarela-ia: ")


def test_todos_sin_llave_401(cliente):
    assert cliente.get("/v1/uso?proyectos=todos").status_code == 401


def test_todos_con_base_congelada_es_503(cliente, llaves, monkeypatch):
    monkeypatch.setattr(proxy, "ESPERA_USO_S", 0.2)
    soltar = threading.Event()
    monkeypatch.setattr(proxy, "usos_de", lambda *_: soltar.wait(1.5))
    try:
        t0 = time.monotonic()
        r = cliente.get("/v1/uso?proyectos=todos", headers={"x-api-key": llaves["prod"]})
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 503 and r.json()["error"]["message"].startswith("pasarela-ia: ") and tardo < 1


def test_todos_con_base_caida_es_503(cliente, llaves, monkeypatch):
    def rota(*_):
        raise RuntimeError("base caída")
    monkeypatch.setattr(proxy, "usos_de", rota)
    r = cliente.get("/v1/uso?proyectos=todos", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
