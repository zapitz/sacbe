import gzip
import json

import httpx
from datos import (MSG, OK, SALDO, SSE, SSE_MODELO_NUM, SSE_RARO, Trozos, en_trozos, esperar_escrituras, filas,
                   responde)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from pasarela.app import crear_app
from pasarela.esquema import fuente_estado


def prod(llaves, **extra):
    return {"x-api-key": llaves["prod"], **extra}


def test_sin_llave_401(cliente):
    r = cliente.post("/anthropic/v1/messages", json=MSG)
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"


def test_llave_desconocida_401(cliente):
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": "pia_nadie_xxxx"})
    assert r.status_code == 401


def test_ruta_no_permitida(cliente, llaves):
    r = cliente.get("/anthropic/v1/organizations/usage_report", headers=prod(llaves))
    assert r.status_code == 404


def test_reenvia_con_llave_real_y_registra(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, OK)
    r = cliente.post("/anthropic/v1/messages", json=MSG,
                     headers=prod(llaves, **{"anthropic-version": "2023-06-01", "anthropic-beta": "algo-2026"}))
    assert r.status_code == 200 and r.json()["id"] == "msg_1"
    assert r.headers["x-pia-fuente"] == "org1" and r.headers["request-id"] == "req_1"
    enviada = falso.recibidas[0]
    assert enviada.headers["x-api-key"] == "sk-falsa-org1"
    assert enviada.headers["anthropic-version"] == "2023-06-01"
    assert enviada.headers["anthropic-beta"] == "algo-2026"
    assert "authorization" not in enviada.headers
    assert str(enviada.url) == "https://api.anthropic.com/v1/messages"
    assert json.loads(enviada.content) == MSG
    f = filas(motor)[0]
    assert (f["proyecto"], f["fuente"], f["entrada"], f["salida"], f["costo_micro"], f["status"]) == \
           ("crm", "org1", 1000, 500, 14_000, 200)
    assert f["request_id"] == "req_1" and f["reintentos"] == 0


def test_request_id_largo_se_recorta(cliente, llaves, falso, motor):
    largo = "req_" + "x" * 196
    falso.respuestas["*"] = responde(200, OK, {"request-id": largo})
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 200
    assert filas(motor)[0]["request_id"] == largo[:80]


def test_bearer_tambien_sirve_y_no_se_reenvia(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers={"authorization": f"Bearer {llaves['prod']}"})
    assert r.status_code == 200
    assert "authorization" not in falso.recibidas[0].headers
    assert falso.recibidas[0].headers["x-api-key"] == "sk-falsa-org1"


def test_sin_saldo_pasa_a_la_siguiente_y_la_marca(cliente, llaves, falso, motor):
    falso.respuestas["sk-falsa-org1"] = responde(400, SALDO)
    falso.respuestas["sk-falsa-org2"] = responde(200, OK)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.headers["x-pia-fuente"] == "org2"
    assert cliente.app.state.pia.catalogo.agotadas() == {"org1"}
    assert filas(motor)[0]["reintentos"] == 1 and filas(motor)[0]["fuente"] == "org2"
    falso.recibidas.clear()
    cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert [q.headers["x-api-key"] for q in falso.recibidas] == ["sk-falsa-org2"]


def test_extra_nunca_toca_la_que_cobra(cliente, llaves, falso):
    falso.respuestas["sk-falsa-org1"] = responde(400, SALDO)
    falso.respuestas["sk-falsa-org2"] = responde(200, OK)
    extra = {"x-api-key": llaves["extra"]}
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=extra)
    assert r.status_code == 400                       # el error de saldo de la gratis, tal cual
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=extra)
    assert r.status_code == 402 and "gratis" in r.json()["error"]["message"]
    assert all(q.headers["x-api-key"] != "sk-falsa-org2" for q in falso.recibidas)


def test_produccion_con_las_dos_marcadas_sale_por_la_pagada(cliente, llaves, falso):
    cat = cliente.app.state.pia.catalogo
    cat.marcar_agotada("org1", "prueba")
    cat.marcar_agotada("org2", "prueba")
    falso.respuestas["*"] = responde(200, OK)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.headers["x-pia-fuente"] == "org2"
    assert [q.headers["x-api-key"] for q in falso.recibidas] == ["sk-falsa-org2"]


def test_pagada_marcada_que_vuelve_a_fallar_se_marca_otra_vez(cliente, llaves, falso, motor):
    cat = cliente.app.state.pia.catalogo
    cat.marcar_agotada("org1", "prueba")
    cat.marcar_agotada("org2", "prueba")
    falso.respuestas["*"] = responde(400, SALDO)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 400 and r.json() == SALDO
    assert [q.headers["x-api-key"] for q in falso.recibidas] == ["sk-falsa-org2"]
    with motor.connect() as c:
        motivo = c.execute(select(fuente_estado.c.motivo).where(fuente_estado.c.nombre == "org2")).scalar()
    assert motivo == SALDO["error"]["message"]


def test_extra_con_las_dos_marcadas_402_sin_llamar(cliente, llaves, falso):
    cat = cliente.app.state.pia.catalogo
    cat.marcar_agotada("org1", "prueba")
    cat.marcar_agotada("org2", "prueba")
    falso.respuestas["*"] = responde(200, OK)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": llaves["extra"]})
    assert r.status_code == 402 and r.json()["error"]["type"] == "billing_error"
    assert falso.recibidas == []


def test_otros_errores_pasan_tal_cual(cliente, llaves, falso, motor):
    limite = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}
    falso.respuestas["*"] = responde(429, limite, {"retry-after": "7"})
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert cliente.app.state.pia.catalogo.agotadas() == set()
    assert len(falso.recibidas) == 1 and filas(motor)[0]["status"] == 429


def test_respuesta_comprimida_llega_limpia(cliente, llaves, falso):
    crudo = gzip.compress(json.dumps(OK).encode())
    falso.respuestas["*"] = lambda req: httpx.Response(
        200, content=crudo, headers={"content-encoding": "gzip", "content-type": "application/json"})
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.json()["id"] == "msg_1" and "content-encoding" not in r.headers
    assert falso.recibidas[0].headers["accept-encoding"] == "identity"


def test_stream_pasa_igual_y_registra(cliente, llaves, falso, motor):
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
    with cliente.stream("POST", "/anthropic/v1/messages", json={**MSG, "stream": True}, headers=prod(llaves)) as r:
        cuerpo = b"".join(r.iter_bytes())
    assert cuerpo == SSE
    esperar_escrituras(cliente)
    f = filas(motor)[0]
    assert (f["entrada"], f["salida"], f["modelo"]) == (120, 42, "claude-opus-5-5")


def test_modelo_sin_precio_avisa_una_vez(cliente, llaves, falso, capsys):
    modelo = "claude-sin-precio-prueba-json"
    falso.respuestas["*"] = responde(200, {**OK, "model": modelo})
    for _ in range(2):
        assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 200
    sse = SSE.replace(b"claude-opus-5-5", modelo.encode())
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
    with cliente.stream("POST", "/anthropic/v1/messages", json={**MSG, "stream": True}, headers=prod(llaves)) as r:
        assert b"".join(r.iter_bytes()) == sse
    esperar_escrituras(cliente)
    linea = f"pasarela: modelo sin precio {modelo}, se estimó como Fable 5.1"
    assert capsys.readouterr().err.splitlines().count(linea) == 1


def test_uso_raro_no_rompe_la_respuesta(cliente, llaves, falso, motor):
    raro = {"id": "msg_1", "model": "claude-opus-5-5", "usage": "raro"}
    falso.respuestas["*"] = responde(200, raro)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.json() == raro
    filas_ = filas(motor)
    assert len(filas_) == 1 and filas_[0]["costo_micro"] == 0


def test_stream_raro_no_se_corta(cliente, llaves, falso, motor):
    trozos = en_trozos(SSE_RARO)
    assert len(trozos) >= 2 and b"raro" in trozos[0]
    falso.respuestas["*"] = lambda req: httpx.Response(
        200, stream=Trozos(trozos), headers={"content-type": "text/event-stream"})
    with cliente.stream("POST", "/anthropic/v1/messages", json={**MSG, "stream": True}, headers=prod(llaves)) as r:
        cuerpo = b"".join(r.iter_bytes())
    assert cuerpo == SSE_RARO
    esperar_escrituras(cliente)
    f = filas(motor)
    assert len(f) == 1 and f[0]["modelo"] is None and f[0]["entrada"] == 0


def test_modelo_no_texto_no_rompe_la_respuesta(cliente, llaves, falso, motor):
    raro = {**OK, "model": 123}
    falso.respuestas["*"] = responde(200, raro)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.json() == raro
    f = filas(motor)
    assert len(f) == 1 and f[0]["modelo"] is None


def test_stream_con_modelo_no_texto_no_se_corta(cliente, llaves, falso, motor):
    trozos = en_trozos(SSE_MODELO_NUM)
    falso.respuestas["*"] = lambda req: httpx.Response(
        200, stream=Trozos(trozos), headers={"content-type": "text/event-stream"})
    with cliente.stream("POST", "/anthropic/v1/messages", json={**MSG, "stream": True}, headers=prod(llaves)) as r:
        assert r.status_code == 200
        cuerpo = b"".join(r.iter_bytes())
    assert cuerpo == SSE_MODELO_NUM
    esperar_escrituras(cliente)
    assert len(filas(motor)) == 1


def test_error_al_leer_el_cuerpo_es_502(cliente, llaves, falso):
    falso.respuestas["*"] = lambda req: httpx.Response(
        200, stream=Trozos([b"{"], falla=True), headers={"content-type": "application/json"})
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 502 and r.json()["error"]["type"] == "api_error"


def test_error_al_leer_cuerpo_de_error_es_502(cliente, llaves, falso):
    falso.respuestas["*"] = lambda req: httpx.Response(
        500, stream=Trozos([b"{"], falla=True), headers={"content-type": "application/json"})
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 502 and r.json()["error"]["type"] == "api_error"


def test_si_no_conecta_es_502(cliente, llaves, falso):
    def revienta(req):
        raise httpx.ConnectError("x")
    falso.respuestas["*"] = revienta
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 502 and r.json()["error"]["type"] == "api_error"


def test_count_tokens_y_models_no_se_registran(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, {"input_tokens": 9})
    assert cliente.post("/anthropic/v1/messages/count_tokens", json=MSG, headers=prod(llaves)).json() == {"input_tokens": 9}
    falso.respuestas["*"] = responde(200, {"data": []})
    assert cliente.get("/anthropic/v1/models?limit=5", headers=prod(llaves)).status_code == 200
    assert str(falso.recibidas[-1].url) == "https://api.anthropic.com/v1/models?limit=5"
    assert filas(motor) == []


def test_si_la_base_falla_igual_responde(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    estado = cliente.app.state.pia
    estado.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and estado.fallas_registro == 1


def test_sin_catalogo_503(config, falso, llaves):
    roto = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    app = crear_app(config, roto, httpx.MockTransport(falso))
    with TestClient(app) as c:
        assert c.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 503
        assert c.get("/salud").status_code == 503


def test_salud(cliente):
    r = cliente.get("/salud")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "base": True, "proyectos": 2, "agotadas": [], "fallas_registro": 0}
