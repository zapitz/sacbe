"""Pool de conexiones hacia Anthropic con límites explícitos."""
import httpx
from datos import MSG, OK, responde

from pasarela import app as modulo_app


def test_el_cliente_lleva_limites_y_esperas_explicitos(config, motor, monkeypatch):
    vistos = {}
    real = httpx.AsyncClient

    def cliente(**kw):
        vistos.update(kw)
        return real(**kw)

    monkeypatch.setattr(modulo_app.httpx, "AsyncClient", cliente)
    modulo_app.crear_app(config, motor, None)
    limites, esperas = vistos["limits"], vistos["timeout"]
    assert (limites.max_connections, limites.max_keepalive_connections, limites.keepalive_expiry) == (200, 20, 5.0)
    assert (esperas.connect, esperas.read, esperas.write, esperas.pool) == (10.0, 600.0, 600.0, 10.0)
    assert vistos["transport"] is None                         # en producción httpx arma el transporte con ellos
    assert (modulo_app.CONEXIONES_MAX, modulo_app.CONEXIONES_EN_ESPERA, modulo_app.VIDA_EN_ESPERA_S,
            modulo_app.ESPERA_CONEXION_S) == (200, 20, 5.0, 10.0)


def test_el_pool_de_verdad_queda_con_esos_limites(config, motor):
    estado = modulo_app.crear_app(config, motor, None).state.pia
    pool = estado.cliente._transport._pool                     # httpcore: confirma que los límites sí llegaron
    assert (pool._max_connections, pool._max_keepalive_connections, pool._keepalive_expiry) == (200, 20, 5.0)


def test_sin_conexion_libre_es_503_y_no_502(cliente, llaves, falso, motor):
    def ocupadas(req):
        raise httpx.PoolTimeout("x")
    falso.respuestas["*"] = ocupadas
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": llaves["prod"]})
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
    assert "conexiones hacia Anthropic están ocupadas" in r.json()["error"]["message"]
    falso.respuestas["*"] = responde(200, OK)
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": llaves["prod"]}).status_code == 200
