"""Pruebas del cliente `sacbe` con el SDK oficial de anthropic y una red falsa (sin internet)."""

import asyncio
import json
import sys

import anthropic
import pytest

import sacbe
from sacbe import _transporte

GESTOR = "http://gestor.prueba/anthropic"
LIB = sacbe._libreria(anthropic.DefaultHttpxClient)   # httpx2 con el SDK 1.x y 2.x, httpx con el 0.x

MENSAJE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
           "content": [{"type": "text", "text": "hola"}], "stop_reason": "end_turn", "stop_sequence": None,
           "usage": {"input_tokens": 3, "output_tokens": 1}}


class Red:
    """Decide qué contesta el gestor y anota cada petición: (host, ruta, llave)."""

    def __init__(self, gestor):
        self.gestor = gestor   # (estado, cuerpo) o el nombre de una excepción de la librería
        self.pedidas = []

    def __call__(self, request):
        llave = request.headers.get("x-api-key") or request.headers.get("authorization")
        self.pedidas.append((request.url.host, request.url.path, llave))
        if request.url.host == "api.anthropic.com":
            if request.url.path.endswith("count_tokens"):
                return LIB.Response(200, json={"input_tokens": 7})
            return LIB.Response(200, json=dict(MENSAJE, id="msg_directo"))
        if isinstance(self.gestor, str):
            raise getattr(LIB, self.gestor)("falla de prueba", request=request)
        estado, cuerpo = self.gestor
        return LIB.Response(estado, json=cuerpo)

    def hosts(self):
        return [p[0] for p in self.pedidas]


@pytest.fixture(autouse=True)
def limpio(monkeypatch):
    for v in ("SACBE_URL", "SACBE_LLAVE", "PASARELA_IA_URL", "PASARELA_IA_LLAVE", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-directa")
    _transporte._estados.clear()


def cliente(red, **kw):
    return sacbe.Anthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba", transporte=LIB.MockTransport(red),
                           max_retries=0, **kw)


def crear(c):
    return c.messages.create(model="claude-haiku-4-5", max_tokens=5, messages=[{"role": "user", "content": "hola"}])


def test_va_por_el_gestor_con_la_llave_pia():
    red = Red((200, MENSAJE))
    r = crear(cliente(red))
    assert r.id == "msg_1"
    host, ruta, llave = red.pedidas[0]
    assert (host, ruta) == ("gestor.prueba", "/anthropic/v1/messages")
    assert "pia_prueba" in llave and "sk-directa" not in llave
    assert sacbe.estado(GESTOR)["enrutadas"] == 1


def test_count_tokens_tambien_va_por_el_gestor():
    red = Red((200, {"input_tokens": 9}))
    assert cliente(red).messages.count_tokens(model="claude-haiku-4-5",
                                              messages=[{"role": "user", "content": "x"}]).input_tokens == 9
    assert red.pedidas[0][1] == "/anthropic/v1/messages/count_tokens"


@pytest.mark.parametrize("gestor", [
    "ConnectError", "ConnectTimeout", "RemoteProtocolError",
    (502, {"x": 1}), (503, {"x": 1}), (504, {"x": 1}),
    (429, {"type": "error", "error": {"type": "rate_limit_error",
                                      "message": "pasarela-ia: tope mensual del proyecto alcanzado"}}),
    (401, {"type": "error", "error": {"type": "authentication_error", "message": "pasarela-ia: llave inválida"}}),
])
def test_regresa_directo_con_la_llave_de_anthropic_y_se_queda(gestor):
    red = Red(gestor)
    c = cliente(red)
    assert crear(c).id == "msg_directo"
    assert red.pedidas[-1][:2] == ("api.anthropic.com", "/v1/messages") and "sk-directa" in red.pedidas[-1][2]
    crear(c)   # enfriamiento: ya no prueba el gestor
    assert red.hosts().count("gestor.prueba") == 1
    assert sacbe.estado(GESTOR)["regresos"] == 1 and sacbe.estado(GESTOR)["en_directo"]


def test_vuelve_a_probar_el_gestor_al_terminar_el_enfriamiento(monkeypatch):
    ahora = [1000.0]
    monkeypatch.setattr(_transporte.time, "monotonic", lambda: ahora[0])
    red = Red((503, {}))
    c = cliente(red)
    crear(c)
    ahora[0] += sacbe.ENFRIAMIENTO_S + 1
    red.gestor = (200, MENSAJE)
    assert crear(c).id == "msg_1"


def test_error_de_anthropic_se_devuelve_tal_cual():
    red = Red((429, {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}}))
    with pytest.raises(anthropic.RateLimitError):
        crear(cliente(red))
    assert red.hosts() == ["gestor.prueba"] and sacbe.estado(GESTOR)["regresos"] == 0


def test_timeout_de_lectura_no_se_repite_y_tres_abren_el_enfriamiento():
    red = Red("ReadTimeout")
    c = cliente(red)
    for _ in range(3):
        with pytest.raises(anthropic.APITimeoutError):
            crear(c)
    assert red.hosts() == ["gestor.prueba"] * 3
    assert crear(c).id == "msg_directo"


def test_otras_rutas_salen_directo():
    red = Red((200, {}))
    c = cliente(red)
    c._client.post("https://api.anthropic.com/v1/messages/batches", content=b"{}")
    c._client.get("https://api.anthropic.com/v1/messages")
    assert "gestor.prueba" not in red.hosts()


def test_streaming_pasa_por_el_gestor():
    eventos = [
        ("message_start", {"type": "message_start", "message": dict(MENSAJE, content=[])}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "hola gestor"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    cuerpo = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in eventos).encode()

    def red(request):
        assert request.url.host == "gestor.prueba"
        return LIB.Response(200, headers={"content-type": "text/event-stream"}, content=cuerpo)

    c = sacbe.Anthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba", transporte=LIB.MockTransport(red))
    with c.messages.stream(model="claude-haiku-4-5", max_tokens=5,
                           messages=[{"role": "user", "content": "hola"}]) as s:
        assert "".join(s.text_stream) == "hola gestor"


def test_with_options_conserva_la_regla():
    red = Red((503, {}))
    c = cliente(red).with_options(timeout=30)
    assert crear(c).id == "msg_directo" and red.hosts() == ["gestor.prueba", "api.anthropic.com"]


def test_asincrono():
    red = Red((502, {}))

    async def go():
        c = sacbe.AsyncAnthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba", transporte=LIB.MockTransport(red),
                                 max_retries=0)
        return await c.messages.create(model="claude-haiku-4-5", max_tokens=5,
                                       messages=[{"role": "user", "content": "hola"}])

    assert asyncio.run(go()).id == "msg_directo"
    assert red.hosts() == ["gestor.prueba", "api.anthropic.com"]


def test_lee_el_entorno_y_los_nombres_viejos(monkeypatch):
    monkeypatch.setenv("PASARELA_IA_URL", GESTOR + "/")
    monkeypatch.setenv("PASARELA_IA_LLAVE", "pia_vieja")
    assert sacbe.configuracion() == (GESTOR, "pia_vieja")
    monkeypatch.setenv("SACBE_URL", "http://otro/anthropic")
    monkeypatch.setenv("SACBE_LLAVE", "pia_nueva")
    assert sacbe.configuracion() == ("http://otro/anthropic", "pia_nueva")


def test_sin_configuracion_es_el_cliente_normal():
    c = sacbe.Anthropic()
    assert not sacbe._es_envoltura(getattr(c._client, "_transport", None))
    assert str(c.base_url).startswith("https://api.anthropic.com")


def test_sin_llave_directa_todo_va_al_gestor(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    c = sacbe.Anthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba")
    assert str(c.base_url).rstrip("/") == GESTOR and c.api_key == "pia_prueba"


def test_otra_base_url_no_se_toca():
    c = sacbe.Anthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba", base_url="http://mi-proxy:9000")
    assert not sacbe._es_envoltura(getattr(c._client, "_transport", None))


def test_http_client_propio_se_rechaza():
    with pytest.raises(TypeError, match="http_client"):
        sacbe.Anthropic(sacbe_url=GESTOR, sacbe_llave="pia_prueba", http_client=anthropic.DefaultHttpxClient())


def test_la_libreria_es_la_del_sdk():
    assert LIB.__name__ in ("httpx", "httpx2") and LIB is sys.modules[LIB.__name__]
