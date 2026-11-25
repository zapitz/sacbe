import json

import httpx
import pytest
from sqlalchemy import create_engine, text

from datos import LOTE, MSG, OK, esperar_escrituras, filas, responde
from pasarela import admin
from pasarela.proxy import ids_de_archivos

LINEA = (b'{"custom_id":"a","result":{"type":"succeeded","message":{"model":"claude-opus-5-5",'
         b'"usage":{"input_tokens":1000,"output_tokens":500}}}}\n')
CREAR = {"requests": [{"custom_id": "a", "params": MSG}]}


def prod(llaves):
    return {"x-api-key": llaves["prod"]}


def test_lote_se_queda_en_su_cuenta_y_su_url_apunta_a_la_pasarela(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, LOTE)
    r = cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    assert r.json()["results_url"] == "http://testserver/anthropic/v1/messages/batches/msgbatch_1/results"
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    falso.recibidas.clear()
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1", headers=prod(llaves))
    assert r.status_code == 200 and r.json()["results_url"].startswith("http://testserver/anthropic/")
    assert falso.recibidas[0].headers["x-api-key"] == "sk-falsa-org1"


def test_url_publica_respeta_cabeceras_del_proxy(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, LOTE)
    r = cliente.post("/anthropic/v1/messages/batches", json=CREAR,
                     headers={**prod(llaves), "x-forwarded-proto": "https", "x-forwarded-host": "ia.ejemplo.mx"})
    assert r.json()["results_url"] == "https://ia.ejemplo.mx/anthropic/v1/messages/batches/msgbatch_1/results"


def test_lote_ajeno_o_desconocido_404(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    n = len(falso.recibidas)
    assert cliente.get("/anthropic/v1/messages/batches/msgbatch_1", headers={"x-api-key": llaves["extra"]}).status_code == 404
    assert cliente.get("/anthropic/v1/messages/batches/msgbatch_otro", headers=prod(llaves)).status_code == 404
    assert len(falso.recibidas) == n                  # ni siquiera se le preguntó a Anthropic


def test_resultados_pasan_tal_cual_y_cuentan_una_vez(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=LINEA * 2,
                                                       headers={"content-type": "application/binary"})
    for _ in range(2):
        r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
        assert r.content == LINEA * 2
        esperar_escrituras(cliente)
    lotes = [f for f in filas(motor) if f["lote"]]
    assert len(lotes) == 1
    assert (lotes[0]["costo_micro"], lotes[0]["entrada"], lotes[0]["modelo"]) == (14_000, 2000, "claude-opus-5-5")


def test_marca_y_fila_del_lote_van_en_una_transaccion(cliente, llaves, falso, motor):
    """Si la fila del costo no entra, la marca tampoco: la siguiente descarga lo cuenta (y solo una vez)."""
    if motor.dialect.name != "sqlite":
        pytest.skip("el disparador que hace fallar la inserción es de SQLite")
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=LINEA, headers={"content-type": "application/binary"})
    with motor.begin() as c:
        c.execute(text("CREATE TRIGGER sin_registro BEFORE INSERT ON registro BEGIN SELECT RAISE(ABORT, 'x'); END"))
    assert cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves)).content == LINEA
    esperar_escrituras(cliente)
    assert cliente.app.state.pia.fallas_registro == 1
    with motor.begin() as c:
        c.execute(text("DROP TRIGGER sin_registro"))
    for _ in range(2):
        cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
        esperar_escrituras(cliente)
    lotes = [f for f in filas(motor) if f["lote"]]
    assert len(lotes) == 1 and lotes[0]["costo_micro"] == 7_000


def test_archivo_fija_la_cuenta_del_mensaje_que_lo_usa(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file", "created_at": "2026-11-07T00:00:00Z"})
    r = cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    assert r.status_code == 200
    assert b"hola" in falso.recibidas[0].content     # el multipart llegó completo
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    falso.respuestas["*"] = responde(200, OK)
    falso.recibidas.clear()
    cuerpo = {**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_abc"}}]}]}
    assert cliente.post("/anthropic/v1/messages", json=cuerpo, headers=prod(llaves)).status_code == 200
    assert falso.recibidas[0].headers["x-api-key"] == "sk-falsa-org1"


def test_extra_no_gasta_en_la_pagada_por_un_archivo_fijado(cliente, llaves, falso, motor):
    """crm sube file_abc con org1 marcada (queda en org2) y luego pasa a extra."""
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    assert falso.recibidas[-1].headers["x-api-key"] == "sk-falsa-org2"
    admin.clase(motor, "crm", "extra", "admin", "«prueba»")
    cliente.app.state.pia.catalogo.refrescar()
    falso.respuestas["*"] = responde(200, OK)
    n = len(falso.recibidas)
    cuerpo = {**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_abc"}}]}]}
    esperado = {"type": "error", "error": {"type": "billing_error",
                                           "message": "Este proyecto no puede gastar en la cuenta pagada."}}
    for ruta, datos in (("v1/messages", cuerpo), ("v1/messages/batches", {"requests": [{"custom_id": "a", "params": cuerpo}]})):
        r = cliente.post(f"/anthropic/{ruta}", json=datos, headers=prod(llaves))
        assert r.status_code == 402 and r.json() == esperado
    assert len(falso.recibidas) == n
    falso.respuestas["*"] = responde(200, {"input_tokens": 9})       # contar tokens no gasta: sigue pasando
    assert cliente.post("/anthropic/v1/messages/count_tokens", json=cuerpo, headers=prod(llaves)).status_code == 200
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    assert cliente.get("/anthropic/v1/files/file_abc", headers=prod(llaves)).status_code == 200
    assert [q.headers["x-api-key"] for q in falso.recibidas[n:]] == ["sk-falsa-org2", "sk-falsa-org2"]


def test_archivo_desconocido_pasa_sin_fijar(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    cuerpo = {**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_zzz"}}]}]}
    r = cliente.post("/anthropic/v1/messages", json=cuerpo, headers=prod(llaves))
    assert r.status_code == 200
    assert falso.recibidas[0].headers["x-api-key"] == "sk-falsa-org1"


def test_archivo_de_otro_proyecto_404(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    falso.respuestas["*"] = responde(200, OK)
    n = len(falso.recibidas)
    cuerpo = {**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_abc"}}]}]}
    r = cliente.post("/anthropic/v1/messages", json=cuerpo, headers={"x-api-key": llaves["extra"]})
    assert r.status_code == 404 and len(falso.recibidas) == n


def test_file_id_escapado_en_json_tampoco_se_cuela(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    falso.respuestas["*"] = responde(200, OK)
    n = len(falso.recibidas)
    crudo = (b'{"model":"claude-opus-5-5","max_tokens":10,"messages":[{"role":"user","content":[{"type":"document",'
             b'"source":{"type":"file","\\u0066ile_id": "file_abc"}}]}]}')
    h = {"x-api-key": llaves["extra"], "content-type": "application/json"}
    assert cliente.post("/anthropic/v1/messages", content=crudo, headers=h).status_code == 404
    crudo2 = crudo.replace(b'"\\u0066ile_id": "file_abc"', b'"file_id": "file_abc"')
    assert cliente.post("/anthropic/v1/messages", content=crudo2, headers=h).status_code == 404
    assert len(falso.recibidas) == n


def test_file_id_repetido_en_el_mismo_objeto_tampoco_se_cuela(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    falso.respuestas["*"] = responde(200, OK)
    n = len(falso.recibidas)
    crudo = (b'{"model":"claude-opus-5-5","max_tokens":10,"messages":[{"role":"user","content":[{"type":"document",'
             b'"source":{"type":"file","file_id":"file_abc","file_id":"file_desconocido"}}]}]}')
    h = {"x-api-key": llaves["extra"], "content-type": "application/json"}
    assert cliente.post("/anthropic/v1/messages", content=crudo, headers=h).status_code == 404
    assert len(falso.recibidas) == n


def test_sin_file_id_ni_escapes_no_se_lee_el_json():
    assert ids_de_archivos(b"[" * 100000) == set()         # no puede traer un file_id: ni se intenta leer
    assert ids_de_archivos(b'{"file_id":"file_a"}') == {"file_a"}
    assert ids_de_archivos(b'{"\\u0066ile_id":"file_b"}') == {"file_b"}
    assert ids_de_archivos('{"file_id":"file_c"}'.encode("utf-16")) == {"file_c"}    # json.loads lee UTF-16 y 32


def test_file_id_en_utf16_tampoco_se_cuela(cliente, llaves, falso):
    """En UTF-16 no aparece el texto b"file_id" pero json.loads lo lee igual: un cuerpo así también se revisa."""
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    falso.respuestas["*"] = responde(200, OK)
    n = len(falso.recibidas)
    crudo = json.dumps({**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_abc"}}]}]}).encode("utf-16")
    assert b"file_id" not in crudo
    h = {"x-api-key": llaves["extra"], "content-type": "application/json"}
    assert cliente.post("/anthropic/v1/messages", content=crudo, headers=h).status_code == 404
    assert len(falso.recibidas) == n


def test_json_anidado_de_mas_es_400(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    crudo = b'{"file_id":' + b"[" * 100000
    r = cliente.post("/anthropic/v1/messages", content=crudo, headers={**prod(llaves), "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert falso.recibidas == []


def test_results_url_de_otro_host_se_reescribe(cliente, llaves, falso):
    ajeno = {**LOTE, "results_url": "https://otro.host/v1/messages/batches/msgbatch_1/results"}
    falso.respuestas["*"] = responde(200, ajeno)
    r = cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    assert r.json()["results_url"] == "http://testserver/anthropic/v1/messages/batches/msgbatch_1/results"


def test_cabeceras_reenviadas_con_lista_toman_el_primer_valor(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, LOTE)
    r = cliente.post("/anthropic/v1/messages/batches", json=CREAR,
                     headers={**prod(llaves), "x-forwarded-proto": "https, http",
                              "x-forwarded-host": " ia.ejemplo.mx , interno"})
    assert r.json()["results_url"] == "https://ia.ejemplo.mx/anthropic/v1/messages/batches/msgbatch_1/results"


def test_resultados_con_error_de_estado_no_se_cuentan(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    falso.respuestas["*"] = lambda req: httpx.Response(206, content=LINEA, headers={"content-type": "application/binary"})
    cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
    esperar_escrituras(cliente)
    assert [f for f in filas(motor) if f["lote"]] == []


def test_resultados_raros_no_se_cortan(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    cuerpo = LINEA + b'{"custom_id":"x","result":{"type":"succeeded","message":"raro"}}\n' + LINEA
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=cuerpo,
                                                       headers={"content-type": "application/binary"})
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
    assert r.status_code == 200 and r.content == cuerpo
    esperar_escrituras(cliente)
    lotes = [f for f in filas(motor) if f["lote"]]
    assert len(lotes) == 1
    assert (lotes[0]["costo_micro"], lotes[0]["entrada"]) == (7_000, 1000)    # solo la primera línea


def test_lote_con_base_caida_es_503(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    n = len(falso.recibidas)
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1", headers=prod(llaves))
    assert r.status_code == 503
    assert len(falso.recibidas) == n


def test_objeto_sin_guardar_avisa_con_su_id(cliente, llaves, falso, capsys):
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file"})
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    r = cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")}, headers=prod(llaves))
    assert r.status_code == 200
    err = capsys.readouterr().err
    assert "no guardé a qué cuenta pertenece un archivo" in err and "id=file_abc" in err and "hola" not in err
