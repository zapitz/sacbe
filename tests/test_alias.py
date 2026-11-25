"""Los alias claude-ultimo-* se cambian por el modelo real antes de llegar a Anthropic."""
import json
from datetime import datetime

import httpx
from datos import LOTE, MSG, OK, filas, responde

from pasarela import modelos

LISTA = [modelos.Modelo("claude-opus-5-5", "Claude Opus 5.5", "opus", datetime(2026, 10, 20)),
         modelos.Modelo("claude-haiku-5-5", "Claude Haiku 5.5", "haiku", datetime(2026, 10, 25))]
ALIAS = {"claude-ultimo-opus": "claude-opus-5-5", "claude-ultimo-haiku": "claude-haiku-5-5"}


def con_catalogo(cliente):
    cliente.app.state.pia.modelos.poner(modelos.armar(LISTA, datetime(2026, 11, 12)))


def prod(llaves):
    return {"x-api-key": llaves["prod"], "content-type": "application/json"}


def enviado(falso):
    return json.loads(falso.recibidas[-1].content)


def repite_el_modelo(req: httpx.Request) -> httpx.Response:
    """Anthropic de mentira que contesta con el modelo que recibió."""
    return httpx.Response(200, json={**OK, "model": json.loads(req.content)["model"]},
                          headers={"request-id": "req_1"})


def test_alias_en_messages(cliente, llaves, falso, motor):
    con_catalogo(cliente)
    falso.respuestas["*"] = repite_el_modelo
    r = cliente.post("/anthropic/v1/messages", json={**MSG, "model": "claude-ultimo-opus"}, headers=prod(llaves))
    assert r.status_code == 200 and r.json()["model"] == "claude-opus-5-5"     # lo recibido arriba ya era el real
    assert enviado(falso) == {**MSG, "model": "claude-opus-5-5"}
    assert filas(motor)[0]["modelo"] == "claude-opus-5-5"


def test_alias_en_count_tokens(cliente, llaves, falso):
    con_catalogo(cliente)
    falso.respuestas["*"] = responde(200, {"input_tokens": 5})
    r = cliente.post("/anthropic/v1/messages/count_tokens", json={**MSG, "model": "claude-ultimo-haiku"},
                     headers=prod(llaves))
    assert r.status_code == 200 and enviado(falso)["model"] == "claude-haiku-5-5"


def test_alias_en_cada_peticion_del_lote(cliente, llaves, falso):
    con_catalogo(cliente)
    falso.respuestas["*"] = responde(200, LOTE)
    cuerpo = {"requests": [{"custom_id": "a", "params": {**MSG, "model": "claude-ultimo-haiku"}},
                           {"custom_id": "b", "params": dict(MSG)}]}
    assert cliente.post("/anthropic/v1/messages/batches", json=cuerpo, headers=prod(llaves)).status_code == 200
    assert [p["params"]["model"] for p in enviado(falso)["requests"]] == ["claude-haiku-5-5", "claude-opus-5-5"]


def test_modelo_fijo_llega_identico(cliente, llaves, falso):
    con_catalogo(cliente)
    falso.respuestas["*"] = responde(200, OK)
    crudo = json.dumps({**MSG, "system": "año ñ"}, ensure_ascii=True).encode()     # con escapes \u
    cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert falso.recibidas[0].content == crudo


def test_alias_sin_catalogo_503(cliente, llaves, falso):
    r = cliente.post("/anthropic/v1/messages", json={**MSG, "model": "claude-ultimo-opus"}, headers=prod(llaves))
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error" and falso.recibidas == []


def test_modelo_fijo_sin_catalogo_pasa(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 200


def test_alias_sin_modelo_con_precio_400(cliente, llaves, falso):
    con_catalogo(cliente)
    r = cliente.post("/anthropic/v1/messages", json={**MSG, "model": "claude-ultimo-sonnet"}, headers=prod(llaves))
    assert r.status_code == 400 and "claude-ultimo-sonnet" in r.json()["error"]["message"]
    assert falso.recibidas == []


def test_alias_que_no_existe_pasa_tal_cual(cliente, llaves, falso):
    con_catalogo(cliente)
    falso.respuestas["*"] = responde(404, {"type": "error", "error": {"type": "not_found_error", "message": "x"}})
    cliente.post("/anthropic/v1/messages", json={**MSG, "model": "claude-ultimo-fable"}, headers=prod(llaves))
    assert enviado(falso)["model"] == "claude-ultimo-fable"


def test_llave_model_repetida_con_alias_400(cliente, llaves, falso):
    con_catalogo(cliente)
    crudo = b'{"model":"claude-opus-5","model":"claude-ultimo-opus","max_tokens":1,"messages":[]}'
    r = cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert r.status_code == 400 and "repite" in r.json()["error"]["message"] and falso.recibidas == []


def test_anidamiento_enorme_400(cliente, llaves, falso):
    con_catalogo(cliente)
    crudo = b'{"model":"claude-ultimo-opus","x":' + b"[" * 50_000 + b"]" * 50_000 + b"}"
    r = cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert r.status_code == 400 and "anidación" in r.json()["error"]["message"] and falso.recibidas == []


def test_alias_con_surrogado_suelto_400(cliente, llaves, falso):
    con_catalogo(cliente)
    crudo = b'{"model":"claude-ultimo-opus","max_tokens":1,"messages":[],"s":"\\ud800"}'
    r = cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error" and falso.recibidas == []


def test_alias_con_numero_fuera_de_rango_400(cliente, llaves, falso):
    con_catalogo(cliente)
    crudo = b'{"model":"claude-ultimo-opus","max_tokens":1,"messages":[],"x":1e400}'
    r = cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert r.status_code == 400 and falso.recibidas == []


def test_llave_model_repetida_con_fijo_al_final_pasa_tal_cual():
    crudo = b'{"model":"claude-ultimo-opus","model":"claude-opus-5-5"}'
    assert modelos.reescribir(crudo, "v1/messages", ALIAS) == crudo


def test_cuerpo_que_no_es_json_pasa_tal_cual(cliente, llaves, falso):
    con_catalogo(cliente)
    falso.respuestas["*"] = responde(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})
    crudo = b"claude-ultimo-opus y nada de JSON"
    cliente.post("/anthropic/v1/messages", content=crudo, headers=prod(llaves))
    assert falso.recibidas[0].content == crudo


def test_alias_con_escapes_no_se_ve():
    """sin el texto claude-ultimo- en los bytes no se lee; Anthropic contestará 404."""
    crudo = b'{"model":"\\u0063laude-ultimo-opus"}'
    assert modelos.reescribir(crudo, "v1/messages", ALIAS) == crudo


def test_alias_a_medias_con_escape_si_se_ve():
    assert json.loads(modelos.reescribir(b'{"model":"claude-ultimo-\\u006fpus"}', "v1/messages", ALIAS)) == \
           {"model": "claude-opus-5-5"}


def test_otras_rutas_no_se_tocan():
    crudo = b'{"model":"claude-ultimo-opus"}'
    assert modelos.reescribir(crudo, "v1/files", ALIAS) == crudo
