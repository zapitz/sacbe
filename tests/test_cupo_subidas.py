"""Tope de las subidas de archivos y cupo de memoria para las simultáneas."""
import anyio
import httpx
import pytest
from datos import MSG, OK, estatus, responde, subir_asgi

from pasarela import proxy

SUBIR = "/anthropic/v1/files"
MULTIPARTE = {"content-type": "multipart/form-data; boundary=x"}


def test_los_topes_son_los_del_diseno():
    assert proxy.TOPE_JSON == 32 * 1024 * 1024 and proxy.TOPE_ARCHIVO == 100 * 1024 * 1024
    assert proxy.TOPE_ARCHIVO <= proxy.CUPO_SUBIDAS == 300 * 1024 * 1024     # una subida del tope siempre cabe sola


def test_la_subida_aparta_su_tamano_y_lo_suelta(cliente, llaves, falso):
    estado = cliente.app.state.pia
    vistos = []

    def anthropic(req):
        vistos.append(estado.subiendo)
        return httpx.Response(200, json={"id": "file_1", "type": "file"})

    falso.respuestas["*"] = anthropic
    h = {"x-api-key": llaves["prod"], **MULTIPARTE}
    assert cliente.post(SUBIR, content=b"x" * 2000, headers=h).status_code == 200
    assert vistos == [2000] and estado.subiendo == 0


def test_sin_content_length_aparta_el_tope_entero(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_ARCHIVO", 5000)
    estado = cliente.app.state.pia
    vistos = []

    def anthropic(req):
        vistos.append(estado.subiendo)
        return httpx.Response(200, json={"id": "file_1", "type": "file"})

    falso.respuestas["*"] = anthropic
    h = {"x-api-key": llaves["prod"], **MULTIPARTE}
    assert cliente.post(SUBIR, content=iter([b"x" * 600, b"x" * 600]), headers=h).status_code == 200
    assert vistos == [5000] and estado.subiendo == 0


def test_con_el_cupo_lleno_429_sin_leer_ni_reenviar(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "CUPO_SUBIDAS", 10_000)
    estado = cliente.app.state.pia
    estado.subiendo = 9_000                                    # otras subidas en curso
    falso.respuestas["*"] = responde(200, {"id": "file_1", "type": "file"})
    h = {"x-api-key": llaves["prod"], **MULTIPARTE}
    r = cliente.post(SUBIR, content=b"x" * 2000, headers=h)
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
    assert "subidas de archivos a la vez" in r.json()["error"]["message"] and r.headers["retry-after"] == "2"
    assert falso.recibidas == [] and estado.subiendo == 9_000
    assert cliente.post(SUBIR, content=b"x" * 1000, headers=h).status_code == 200      # la que sí cabe, pasa
    assert estado.subiendo == 9_000


def test_el_cupo_se_suelta_aunque_la_subida_falle(cliente, llaves, falso, monkeypatch):
    estado = cliente.app.state.pia
    h = {"x-api-key": llaves["prod"], **MULTIPARTE}

    def revienta(req):
        raise httpx.ConnectError("x")
    falso.respuestas["*"] = revienta
    assert cliente.post(SUBIR, content=b"x" * 2000, headers=h).status_code == 502
    monkeypatch.setattr(proxy, "TOPE_ARCHIVO", 1000)
    assert cliente.post(SUBIR, content=b"x" * 2000, headers=h).status_code == 413
    assert estado.subiendo == 0


def test_el_cupo_solo_es_de_las_subidas(cliente, llaves, falso):
    cliente.app.state.pia.subiendo = proxy.CUPO_SUBIDAS        # lleno
    falso.respuestas["*"] = responde(200, OK)
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers={"x-api-key": llaves["prod"]}).status_code == 200
    falso.respuestas["*"] = responde(200, {"id": "file_1", "type": "file"})
    assert cliente.get(f"{SUBIR}/file_1", headers={"x-api-key": llaves["prod"]}).status_code == 200


def test_subida_que_se_calla_da_408_y_suelta_el_cupo(app, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "ESPERA_TROZO_S", 0.2)
    estado = app.state.pia
    falso.respuestas["*"] = responde(200, {"id": "file_1", "type": "file"})
    cab = {"x-api-key": llaves["prod"], **MULTIPARTE, "transfer-encoding": "chunked"}
    enviados = anyio.run(subir_asgi, app, cab, [b"x" * 100], True)
    assert estatus(enviados) == 408 and estado.subiendo == 0 and falso.recibidas == []
    cuerpo = b"".join(m.get("body", b"") for _, m in enviados if m["type"] == "http.response.body")
    assert b"timeout_error" in cuerpo and "la subida se quedó sin mandar datos".encode() in cuerpo
    enviados = anyio.run(subir_asgi, app, cab, [b"x" * 100])                       # la que sí termina, pasa
    assert estatus(enviados) == 200 and estado.subiendo == 0


def test_el_tope_entre_trozos_es_solo_de_las_subidas(cliente, llaves, falso, monkeypatch):
    vistos = []
    real = proxy.leer_cuerpo

    async def espia(request, tope, espera=None):
        vistos.append((request.url.path, espera))
        return await real(request, tope, espera)

    monkeypatch.setattr(proxy, "leer_cuerpo", espia)
    falso.respuestas["*"] = responde(200, OK)
    h = {"x-api-key": llaves["prod"]}
    assert cliente.post("/anthropic/v1/messages", json=MSG, headers=h).status_code == 200
    falso.respuestas["*"] = responde(200, {"id": "file_1", "type": "file"})
    assert cliente.post(SUBIR, content=b"x" * 10, headers={**h, **MULTIPARTE}).status_code == 200
    assert vistos == [("/anthropic/v1/messages", None), (SUBIR, proxy.ESPERA_TROZO_S)]
    assert proxy.ESPERA_TROZO_S == 30


def test_el_rechazo_por_cupo_deja_una_linea_con_lo_apartado(cliente, llaves, falso, monkeypatch, capsys):
    monkeypatch.setattr(proxy, "CUPO_SUBIDAS", 10_000)
    cliente.app.state.pia.subiendo = 9_000
    h = {"x-api-key": llaves["prod"], **MULTIPARTE}
    assert cliente.post(SUBIR, content=b"x" * 2000, headers=h).status_code == 429
    err = capsys.readouterr().err
    assert "subiendo=9000" in err and "cupo" in err and llaves["prod"] not in err


def test_con_transfer_encoding_se_aparta_el_tope_entero_diga_lo_que_diga_content_length(app, llaves, falso,
                                                                                         monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_ARCHIVO", 5000)
    estado = app.state.pia
    vistos = []

    def anthropic(req):
        vistos.append(estado.subiendo)
        return httpx.Response(200, json={"id": "file_1", "type": "file"})
    falso.respuestas["*"] = anthropic
    cab = {"x-api-key": llaves["prod"], **MULTIPARTE, "content-length": "1", "transfer-encoding": "chunked"}
    assert estatus(anyio.run(subir_asgi, app, cab, [b"x" * 2000, b"x" * 1000])) == 200
    assert vistos == [5000] and estado.subiendo == 0


def test_el_cupo_se_suelta_si_la_atencion_revienta_con_una_excepcion(app, llaves, monkeypatch):
    estado = app.state.pia

    async def revienta(*a, **k):
        assert estado.subiendo > 0                                    # en pleno vuelo el cupo está apartado
        raise RuntimeError("falla inesperada")
    monkeypatch.setattr(proxy, "atender", revienta)
    cab = {"x-api-key": llaves["prod"], **MULTIPARTE, "content-length": "2000"}
    with pytest.raises(RuntimeError, match="falla inesperada"):
        anyio.run(subir_asgi, app, cab, [b"x" * 2000])
    assert estado.subiendo == 0


def test_el_cupo_se_suelta_si_la_subida_se_cancela(app, llaves, falso):
    estado = app.state.pia
    falso.respuestas["*"] = responde(200, {"id": "file_1", "type": "file"})
    cab = {"x-api-key": llaves["prod"], **MULTIPARTE, "content-length": "2000"}
    visto = []

    async def cancelada():
        with anyio.move_on_after(0.3) as alcance:                      # el cliente se cuelga y el servidor lo cancela
            await subir_asgi(app, cab, [b"x" * 100], colgar=True)
        visto.append((alcance.cancelled_caught, estado.subiendo))

    anyio.run(cancelada)
    assert visto == [(True, 0)] and falso.recibidas == []
