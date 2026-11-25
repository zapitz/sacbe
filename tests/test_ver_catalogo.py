"""GET /v1/catalogo con la llave del programa, por la puerta interna y por la pública."""
from datetime import datetime
from pathlib import Path

from pasarela import modelos, puerta

PUB = {"x-pia-puerta": "publica", "x-real-ip": "203.0.113.9"}
LISTA = [modelos.Modelo("claude-opus-5-5", "Claude Opus 5.5", "opus", datetime(2026, 10, 20))]


def con_catalogo(cliente):
    cliente.app.state.pia.modelos.poner(modelos.armar(LISTA, datetime(2026, 11, 12, 6, 0)))


def test_catalogo_con_llave_de_cualquier_clase(cliente, llaves, falso):
    con_catalogo(cliente)
    for llave in (llaves["prod"], llaves["extra"]):
        r = cliente.get("/v1/catalogo", headers={"x-api-key": llave})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        marca = r.json()["marcas"][0]
        assert marca["modelos"][0]["id"] == "claude-opus-5-5"
        assert marca["alias"] == [{"id": "claude-ultimo-opus", "apunta_a": "claude-opus-5-5"}]
    assert falso.recibidas == []                     # no le pregunta nada a Anthropic


def test_catalogo_con_bearer(cliente, llaves):
    con_catalogo(cliente)
    assert cliente.get("/v1/catalogo", headers={"authorization": f"Bearer {llaves['prod']}"}).status_code == 200


def test_catalogo_sin_llave_401(cliente):
    con_catalogo(cliente)
    for h in ({}, {"x-api-key": "pia_nadie_" + "x" * 32}):
        r = cliente.get("/v1/catalogo", headers=h)
        assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"


def test_catalogo_sin_cargar_503(cliente, llaves):
    r = cliente.get("/v1/catalogo", headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 503 and "catálogo de modelos" in r.json()["error"]["message"]


def test_catalogo_por_la_puerta_publica(cliente, llaves):
    con_catalogo(cliente)
    r = cliente.get("/v1/catalogo", headers={**PUB, "x-api-key": llaves["extra"]})
    assert r.status_code == 200 and "x-pia-fuente" not in r.headers
    assert r.json()["marcas"][0]["ruta"] == "/anthropic"


def test_llaves_malas_al_catalogo_castigan_la_ip(cliente):
    con_catalogo(cliente)
    malas = [cliente.get("/v1/catalogo", headers={**PUB, "x-api-key": "pia_nadie_" + "x" * 32}).status_code
             for _ in range(20)]
    assert malas == [401] * 20
    assert cliente.get("/v1/catalogo", headers=PUB).status_code == 429


def test_los_ejemplos_de_proxy_y_la_puerta_abren_el_catalogo():
    ejemplos = Path(__file__).resolve().parents[1] / "docs/ejemplos"
    assert "Path(`/v1/catalogo`)" in (ejemplos / "traefik.yml").read_text()
    assert " /v1/catalogo " in (ejemplos / "Caddyfile").read_text()
    assert "/v1/catalogo" in puerta.EXACTAS
