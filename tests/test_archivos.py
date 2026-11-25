"""Archivos por la pasarela: los que nacen en respuestas quedan fijados a su cuenta y a su proyecto."""
import json
import threading
import time

import anyio
import httpx
import pytest
from datos import (BLOQUE_ARCHIVO, LOTE, MSG, OK, OK_CON_ARCHIVO, SSE, SSE_CON_ARCHIVO, Trozos, esperar_escrituras, filas,
                   llamar_asgi, responde)
from sqlalchemy import create_engine

from pasarela import proxy
from pasarela.almacen import objeto_de

CREAR = {"requests": [{"custom_id": "a", "params": MSG}]}
CON_GENERADO = {**MSG, "messages": [{"role": "user", "content": [
    {"type": "container_upload", "file_id": "file_gen1"}, {"type": "text", "text": "sigue"}]}]}


def prod(llaves):
    return {"x-api-key": llaves["prod"]}


def dueno(motor, id_):
    o = objeto_de(motor, id_)
    return (o["tipo"], o["fuente"], o["proyecto"]) if o else None


def test_archivo_de_una_respuesta_queda_fijado_a_su_cuenta_y_proyecto(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, OK_CON_ARCHIVO)
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.json() == OK_CON_ARCHIVO           # la respuesta pasa sin tocar
    assert dueno(motor, "file_gen1") == ("archivo", "org1", "crm")
    assert dueno(motor, "file_del_modelo") is None                       # lo que escribe el modelo no se registra
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")    # el orden ya daría org2
    falso.respuestas["*"] = responde(200, OK)
    falso.recibidas.clear()
    assert cliente.post("/anthropic/v1/messages", json=CON_GENERADO, headers=prod(llaves)).status_code == 200
    assert cliente.get("/anthropic/v1/files/file_gen1", headers=prod(llaves)).status_code == 200
    assert [q.headers["x-api-key"] for q in falso.recibidas] == ["sk-falsa-org1"] * 2
    n = len(falso.recibidas)
    ajeno = {"x-api-key": llaves["extra"]}
    assert cliente.post("/anthropic/v1/messages", json=CON_GENERADO, headers=ajeno).status_code == 404
    assert cliente.get("/anthropic/v1/files/file_gen1/content", headers=ajeno).status_code == 404
    assert len(falso.recibidas) == n


def test_respuesta_sin_archivos_no_escribe_objetos(cliente, llaves, falso, monkeypatch):
    llamadas = []
    monkeypatch.setattr(proxy, "guardar_objetos", lambda *a: llamadas.append(a))
    falso.respuestas["*"] = responde(200, OK)
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 200
    falso.respuestas["*"] = responde(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": json.dumps(BLOQUE_ARCHIVO)}})
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 400
    assert llamadas == []


def test_archivo_ya_registrado_a_otro_no_cambia_de_dueno(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, OK_CON_ARCHIVO)
    cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": llaves["extra"]})
    assert dueno(motor, "file_gen1") == ("archivo", "org1", "lotes")
    cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert dueno(motor, "file_gen1") == ("archivo", "org1", "lotes")


def test_archivo_de_un_stream_se_registra_antes_de_entregar_su_trozo(app, llaves, falso, motor, monkeypatch):
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE_CON_ARCHIVO,
                                                       headers={"content-type": "text/event-stream"})
    real, hecho = proxy.guardar_objetos, []

    def lenta(*a):
        time.sleep(0.3)
        n = real(*a)
        hecho.append(time.monotonic())
        return n

    monkeypatch.setattr(proxy, "guardar_objetos", lenta)
    enviados = anyio.run(llamar_asgi, app, "/anthropic/v1/messages", {**MSG, "stream": True},
                         {"x-api-key": llaves["prod"]})
    cuerpo = [(t, m["body"]) for t, m in enviados if m["type"] == "http.response.body" and m.get("body")]
    assert b"".join(b for _, b in cuerpo) == SSE_CON_ARCHIVO
    assert len(hecho) == 1 and next(t for t, b in cuerpo if b"file_gen1" in b) >= hecho[0]
    assert dueno(motor, "file_gen1") == ("archivo", "org1", "crm")
    f = filas(motor)
    assert len(f) == 1 and (f[0]["entrada"], f[0]["salida"]) == (120, 42)       # el uso se sigue midiendo


def test_archivos_de_resultados_de_un_lote_quedan_en_la_cuenta_del_lote(cliente, llaves, falso, motor):
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")           # el lote nace en org2
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    linea = json.dumps({"custom_id": "a", "result": {"type": "succeeded", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 500},
        "content": [BLOQUE_ARCHIVO]}}}).encode()
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=linea,          # una sola línea, sin salto final
                                                       headers={"content-type": "application/binary"})
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
    assert r.content == linea
    esperar_escrituras(cliente)
    assert dueno(motor, "file_gen1") == ("archivo", "org2", "crm")
    assert [f["costo_micro"] for f in filas(motor) if f["lote"]] == [7_000]


def test_resultados_con_la_base_caida_bajan_completos_y_no_insisten(cliente, llaves, falso, monkeypatch, capsys):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    lineas = [json.dumps({"custom_id": c, "result": {"type": "succeeded", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 1}, "content": [BLOQUE_ARCHIVO]}}}).encode() + b"\n"
        for c in "abc"]
    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=Trozos(lineas),
                                                       headers={"content-type": "application/binary"})
    llamadas = []

    def caida(*a):
        llamadas.append(a)
        raise OSError("sin base")

    monkeypatch.setattr(proxy, "guardar_objetos", caida)
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
    assert r.status_code == 200 and r.content == b"".join(lineas)
    esperar_escrituras(cliente)
    assert len(llamadas) == 1 and capsys.readouterr().err.count("no guardé a qué cuenta pertenecen") == 1


def test_si_la_base_falla_al_registrar_la_respuesta_sale_y_avisa_con_el_id(cliente, llaves, falso, capsys):
    falso.respuestas["*"] = responde(200, OK_CON_ARCHIVO)
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.json() == OK_CON_ARCHIVO
    err = capsys.readouterr().err
    assert "no guardé a qué cuenta pertenecen 1 archivos" in err and "ids=file_gen1" in err
    assert "fuente=org1 proyecto=crm" in err and "listo" not in err


def test_base_congelada_no_detiene_la_respuesta_mas_que_el_tope(cliente, llaves, falso, monkeypatch, capsys):
    monkeypatch.setattr(proxy, "ESPERA_FIJADA_S", 0.2)
    soltar = threading.Event()
    monkeypatch.setattr(proxy, "guardar_objetos", lambda *a: soltar.wait(3))
    falso.respuestas["*"] = responde(200, OK_CON_ARCHIVO)
    try:
        t0 = time.monotonic()
        r = cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 200 and tardo < 1.5
    assert "TimeoutError" in capsys.readouterr().err


# Caminos de archivos que no tenían prueba y el borrado.

def subir(cliente, llaves, falso, id_="file_abc", llave="prod"):
    falso.respuestas["*"] = responde(200, {"id": id_, "type": "file"})
    r = cliente.post("/anthropic/v1/files", files={"file": ("a.txt", b"hola", "text/plain")},
                     headers={"x-api-key": llaves[llave]})
    assert r.status_code == 200


def test_contenido_de_un_archivo_sale_de_su_cuenta_y_pasa_tal_cual(cliente, llaves, falso, motor):
    subir(cliente, llaves, falso)
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    binario = bytes(range(256)) * 40
    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=Trozos([binario[:5000], binario[5000:]]),
                                                       headers={"content-type": "application/pdf",
                                                                "content-disposition": 'attachment; filename="a.pdf"'})
    falso.recibidas.clear()
    r = cliente.get("/anthropic/v1/files/file_abc/content", headers=prod(llaves))
    assert r.status_code == 200 and r.content == binario
    assert r.headers["content-type"] == "application/pdf" and r.headers["x-pia-fuente"] == "org1"
    assert r.headers["content-disposition"] == 'attachment; filename="a.pdf"'
    assert falso.recibidas[0].headers["x-api-key"] == "sk-falsa-org1"
    assert str(falso.recibidas[0].url) == "https://api.anthropic.com/v1/files/file_abc/content"
    assert filas(motor) == []                                            # bajar un archivo no es gasto


def test_descarga_que_se_corta_a_medias_cierra_la_respuesta_de_anthropic(cliente, llaves, falso):
    subir(cliente, llaves, falso)
    cerradas = []

    class Cortada(Trozos):
        async def aclose(self):
            cerradas.append(True)

    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=Cortada([b"medio"], falla=True),
                                                       headers={"content-type": "application/pdf"})
    with pytest.raises(httpx.ReadError):              # el corte llega al cliente como conexión rota
        cliente.get("/anthropic/v1/files/file_abc/content", headers=prod(llaves))
    assert cerradas == [True]
    falso.respuestas["*"] = responde(200, OK)         # la pasarela sigue atendiendo
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves)).status_code == 200


def test_error_al_bajar_pasa_tal_cual(cliente, llaves, falso):
    subir(cliente, llaves, falso)
    no = {"type": "error", "error": {"type": "invalid_request_error", "message": "File is not downloadable."}}
    falso.respuestas["*"] = responde(400, no)
    r = cliente.get("/anthropic/v1/files/file_abc/content", headers=prod(llaves))
    assert r.status_code == 400 and r.json() == no


def test_mensaje_con_archivos_de_dos_cuentas_es_400(cliente, llaves, falso):
    subir(cliente, llaves, falso, "file_u")
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    subir(cliente, llaves, falso, "file_r")
    assert falso.recibidas[-1].headers["x-api-key"] == "sk-falsa-org2"
    n = len(falso.recibidas)
    cuerpo = {**MSG, "messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "file", "file_id": "file_u"}},
        {"type": "document", "source": {"type": "file", "file_id": "file_r"}}]}]}
    r = cliente.post("/anthropic/v1/messages", json=cuerpo, headers=prod(llaves))
    assert r.status_code == 400 and "dos cuentas" in r.json()["error"]["message"]
    assert len(falso.recibidas) == n


def test_borrar_va_a_su_cuenta_y_lo_marca_borrado(cliente, llaves, falso, motor):
    subir(cliente, llaves, falso)
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    falso.respuestas["*"] = responde(200, {"id": "file_abc", "type": "file_deleted"})
    falso.recibidas.clear()
    r = cliente.delete("/anthropic/v1/files/file_abc", headers=prod(llaves))
    assert r.status_code == 200 and r.json() == {"id": "file_abc", "type": "file_deleted"}
    assert (falso.recibidas[0].method, falso.recibidas[0].headers["x-api-key"]) == ("DELETE", "sk-falsa-org1")
    assert dueno(motor, "file_abc") == ("borrado", "org1", "crm")
    falso.respuestas["*"] = responde(404, {"type": "error", "error": {"type": "not_found_error", "message": "x"}})
    assert cliente.get("/anthropic/v1/files/file_abc", headers=prod(llaves)).status_code == 404
    assert falso.recibidas[-1].headers["x-api-key"] == "sk-falsa-org1"     # sigue yendo a su cuenta
    n = len(falso.recibidas)
    assert cliente.get("/anthropic/v1/files/file_abc", headers={"x-api-key": llaves["extra"]}).status_code == 404
    assert len(falso.recibidas) == n                                         # y sigue sin ser de otro proyecto


def test_borrado_que_anthropic_rechaza_no_lo_marca(cliente, llaves, falso, motor):
    subir(cliente, llaves, falso)
    falso.respuestas["*"] = responde(500, {"type": "error", "error": {"type": "api_error", "message": "x"}})
    assert cliente.delete("/anthropic/v1/files/file_abc", headers=prod(llaves)).status_code == 500
    assert dueno(motor, "file_abc") == ("archivo", "org1", "crm")


def test_borrar_un_lote_no_toca_su_registro(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    falso.respuestas["*"] = responde(200, {"id": "msgbatch_1", "type": "message_batch_deleted"})
    assert cliente.delete("/anthropic/v1/messages/batches/msgbatch_1", headers=prod(llaves)).status_code == 200
    assert dueno(motor, "msgbatch_1") == ("lote", "org1", "crm")


# La ruta decide, y tras una falla ya no se insiste en el stream.

SSE_DE_OTRO = (b'data: {"type":"message_start","message":{"model":"claude-opus-5-5","usage":{"input_tokens":900}}}\n\n'
               b'data: ' + json.dumps({"type": "content_block_start", "content_block": {
                   "type": "x_tool_result", "file_id": "file_de_otro_programa"}}).encode() + b"\n\n")


def test_el_contenido_de_un_archivo_servido_como_sse_pasa_tal_cual_sin_registrar_ni_cobrar(cliente, llaves, falso,
                                                                                           motor):
    subir(cliente, llaves, falso)
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE_DE_OTRO,
                                                       headers={"content-type": "text/event-stream"})
    r = cliente.get("/anthropic/v1/files/file_abc/content", headers=prod(llaves))
    assert r.status_code == 200 and r.content == SSE_DE_OTRO
    esperar_escrituras(cliente)
    assert dueno(motor, "file_de_otro_programa") is None and filas(motor) == []


def test_los_resultados_de_un_lote_servidos_como_sse_van_por_su_lector(cliente, llaves, falso, motor):
    falso.respuestas["*"] = responde(200, LOTE)
    cliente.post("/anthropic/v1/messages/batches", json=CREAR, headers=prod(llaves))
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE_DE_OTRO,
                                                       headers={"content-type": "text/event-stream"})
    r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1/results", headers=prod(llaves))
    assert r.status_code == 200 and r.content == SSE_DE_OTRO
    esperar_escrituras(cliente)
    assert dueno(motor, "file_de_otro_programa") is None and [f for f in filas(motor) if not f["lote"]] == []


def test_un_sse_que_no_es_de_mensajes_no_se_mide(cliente, llaves, falso, motor):
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE_DE_OTRO,
                                                       headers={"content-type": "text/event-stream"})
    r = cliente.post("/anthropic/v1/messages/count_tokens", json=MSG, headers=prod(llaves))
    assert r.status_code == 200 and r.content == SSE_DE_OTRO
    esperar_escrituras(cliente)
    assert dueno(motor, "file_de_otro_programa") is None and filas(motor) == []


def test_stream_con_la_base_congelada_no_insiste_tras_la_primera_falla(app, llaves, falso, monkeypatch, capsys):
    monkeypatch.setattr(proxy, "ESPERA_FIJADA_S", 0.3)
    soltar = threading.Event()
    llamadas = []
    monkeypatch.setattr(proxy, "guardar_objetos", lambda *a: llamadas.append(a) or soltar.wait(5))
    inicio = SSE[:SSE.index(b"event: message_delta")]
    fin = SSE[SSE.index(b"event: message_delta"):]
    eventos = []
    for k in range(6):
        b = json.loads(json.dumps(BLOQUE_ARCHIVO))
        b["content"]["content"][0]["file_id"] = f"file_gen{k}"
        eventos.append(b"event: content_block_start\ndata: " + json.dumps(
            {"type": "content_block_start", "index": k + 1, "content_block": b}).encode() + b"\n\n")
    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=Trozos([inicio, *eventos, fin]),
                                                       headers={"content-type": "text/event-stream"})
    try:
        t0 = time.monotonic()
        enviados = anyio.run(llamar_asgi, app, "/anthropic/v1/messages", {**MSG, "stream": True},
                             {"x-api-key": llaves["prod"]})
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    cuerpo = b"".join(m.get("body", b"") for _, m in enviados if m["type"] == "http.response.body")
    assert cuerpo == b"".join([inicio, *eventos, fin])                                # el stream sale completo
    assert len(llamadas) == 1 and tardo < 1.0
    err = capsys.readouterr().err
    assert err.count("no guardé a qué cuenta pertenecen") == 1
    assert "sin registrar" in err and all(f"file_gen{k}" in err for k in range(6))    # ningún id se pierde (N1)


def test_un_stream_normal_no_toca_la_base_en_ningun_trozo(app, llaves, falso, monkeypatch):
    llamadas = []
    monkeypatch.setattr(proxy, "guardar_objetos", lambda *a: llamadas.append(a))
    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=Trozos([SSE[i:i + 7] for i in range(0, len(SSE), 7)]),
                                                       headers={"content-type": "text/event-stream"})
    enviados = anyio.run(llamar_asgi, app, "/anthropic/v1/messages", {**MSG, "stream": True},
                         {"x-api-key": llaves["prod"]})
    assert b"".join(m.get("body", b"") for _, m in enviados if m["type"] == "http.response.body") == SSE
    assert llamadas == []


@pytest.mark.parametrize("metodo,ruta", [("post", "/anthropic/v1/messages/count_tokens"), ("get", "/anthropic/v1/models")],
                         ids=["count_tokens", "models"])
def test_solo_v1_messages_registra_archivos_de_una_respuesta(cliente, llaves, falso, motor, monkeypatch, metodo, ruta):
    llamadas = []
    monkeypatch.setattr(proxy, "guardar_objetos", lambda *a: llamadas.append(a))
    falso.respuestas["*"] = responde(200, OK_CON_ARCHIVO)                    # trae un bloque con file_gen1
    pedido = cliente.post(ruta, json=MSG, headers=prod(llaves)) if metodo == "post" else cliente.get(
        ruta, headers=prod(llaves))
    assert pedido.status_code == 200 and pedido.json() == OK_CON_ARCHIVO
    esperar_escrituras(cliente)
    assert llamadas == [] and dueno(motor, "file_gen1") is None
    cliente.post("/anthropic/v1/messages", json=MSG, headers=prod(llaves))   # y por v1/messages sí lo registra
    assert len(llamadas) == 1
