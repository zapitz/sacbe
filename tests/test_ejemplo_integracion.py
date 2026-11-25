"""El ejemplo de docs/conectar-programas.md (docs/ejemplos/cliente_con_respaldo.py) con transportes falsos, sin red."""
import importlib.util
import json
from pathlib import Path

import pytest

anthropic = pytest.importorskip("anthropic")
from anthropic import _base_client  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
EJEMPLO = RAIZ / "docs/ejemplos/cliente_con_respaldo.py"
# La librería HTTP que usa el SDK instalado: httpx2 en el 1.x y el 2.x, httpx en el 0.x.
http = getattr(_base_client, "httpx2", None) or _base_client.httpx

GESTOR = "http://pasarela-ia-api:8080/anthropic"
LLAVE_PIA = "pia_crm_" + "p" * 32
LLAVE_DIRECTA = "sk-ant-falsa-directa"
MENSAJE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-prueba",
           "content": [{"type": "text", "text": "hola"}], "stop_reason": "end_turn", "stop_sequence": None,
           "usage": {"input_tokens": 3, "output_tokens": 1}}


def cargar():
    spec = importlib.util.spec_from_file_location("cliente_con_respaldo", EJEMPLO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Lado:
    """Un transporte falso: contesta con `responder(request)` y guarda lo que recibe."""

    def __init__(self, responder):
        self.responder = responder
        self.recibidas = []

    def __call__(self, request):
        self.recibidas.append(request)
        return self.responder(request)


def llave(request) -> str:
    """El SDK 0.x manda x-api-key; el 1.x y el 2.x, Authorization: Bearer. El gestor acepta las dos."""
    if "x-api-key" in request.headers:
        return request.headers["x-api-key"]
    return request.headers["authorization"].removeprefix("Bearer ")


def ok(request):
    return http.Response(200, json=MENSAJE)


def armar(gestor, directo=None):
    mod = cargar()
    reloj = Reloj()
    directo = directo or Lado(ok)
    cliente = mod.ClienteConRespaldo(GESTOR, LLAVE_PIA, LLAVE_DIRECTA, reloj=reloj,
                                     http_gestor=http.Client(transport=http.MockTransport(gestor)),
                                     http_directo=http.Client(transport=http.MockTransport(directo)))
    return mod, cliente, reloj, directo


def pedir(cliente):
    return cliente.llamar(lambda c: c.messages.create(model="claude-prueba", max_tokens=16,
                                                      messages=[{"role": "user", "content": "hola"}]))


def error_json(status, tipo, mensaje):
    return lambda request: http.Response(status, json={"type": "error", "error": {"type": tipo, "message": mensaje}})


def test_por_el_gestor_con_la_llave_pia():
    gestor = Lado(ok)
    _, cliente, _, directo = armar(gestor)
    assert pedir(cliente).content[0].text == "hola"
    assert len(gestor.recibidas) == 1 and directo.recibidas == []
    r = gestor.recibidas[0]
    assert str(r.url) == GESTOR + "/v1/messages"
    assert llave(r) == LLAVE_PIA


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gestor_caido_pasa_a_directo_y_se_queda_60_s(status):
    gestor = Lado(lambda request: http.Response(status, text="caído"))
    mod, cliente, reloj, directo = armar(gestor)
    assert pedir(cliente).id == "msg_1"
    assert len(gestor.recibidas) == 1 and len(directo.recibidas) == 1
    assert llave(directo.recibidas[0]) == LLAVE_DIRECTA
    assert str(directo.recibidas[0].url) == "https://api.anthropic.com/v1/messages"
    reloj.t += mod.ENFRIAMIENTO_S - 1
    pedir(cliente)
    assert len(gestor.recibidas) == 1 and len(directo.recibidas) == 2        # sigue en directo
    reloj.t += 2
    gestor.responder = ok
    pedir(cliente)
    assert len(gestor.recibidas) == 2 and len(directo.recibidas) == 2        # vuelve al gestor


def test_error_propio_del_gestor_pasa_a_directo():
    gestor = Lado(error_json(401, "authentication_error", "pasarela-ia: llave interna inválida"))
    _, cliente, _, directo = armar(gestor)
    assert pedir(cliente).id == "msg_1" and len(directo.recibidas) == 1


def test_error_de_anthropic_se_devuelve_tal_cual():
    gestor = Lado(error_json(400, "invalid_request_error", "max_tokens: Field required"))
    _, cliente, _, directo = armar(gestor)
    with pytest.raises(anthropic.BadRequestError) as e:
        pedir(cliente)
    assert "max_tokens" in str(e.value) and directo.recibidas == [] and len(gestor.recibidas) == 1


def test_un_429_de_anthropic_no_cambia_de_camino():
    gestor = Lado(error_json(429, "rate_limit_error", "Number of request tokens has exceeded your rate limit"))
    _, cliente, _, directo = armar(gestor)
    with pytest.raises(anthropic.RateLimitError):
        pedir(cliente)
    assert directo.recibidas == [] and len(gestor.recibidas) == 1            # max_retries=0: un solo intento


@pytest.mark.parametrize("falla", ["ConnectError", "ConnectTimeout"])
def test_sin_conexion_pasa_a_directo(falla):
    def no_conecta(request):
        raise getattr(http, falla)("sin conexión", request=request)
    _, cliente, _, directo = armar(Lado(no_conecta))
    assert pedir(cliente).id == "msg_1" and len(directo.recibidas) == 1


def test_timeout_de_lectura_no_se_repite_y_tres_abren_el_enfriamiento():
    def lento(request):
        raise http.ReadTimeout("lento", request=request)
    gestor = Lado(lento)
    _, cliente, _, directo = armar(gestor)
    for _ in range(3):
        with pytest.raises(anthropic.APITimeoutError):
            pedir(cliente)
    assert len(gestor.recibidas) == 3 and directo.recibidas == []             # ninguno se repitió en directo
    assert cliente.en_directo()
    pedir(cliente)
    assert len(gestor.recibidas) == 3 and len(directo.recibidas) == 1


def test_un_exito_reinicia_la_cuenta_de_timeouts():
    respuestas = iter(["lento", "lento", "ok", "lento", "lento"])

    def a_veces(request):
        if next(respuestas) == "lento":
            raise http.ReadTimeout("lento", request=request)
        return ok(request)
    _, cliente, _, _ = armar(Lado(a_veces))
    for esperado in ("lento", "lento", "ok", "lento", "lento"):
        if esperado == "ok":
            pedir(cliente)
        else:
            with pytest.raises(anthropic.APITimeoutError):
                pedir(cliente)
    assert not cliente.en_directo()


def test_el_cliente_del_gestor_no_reintenta_y_conecta_en_3_s():
    mod, cliente, _, _ = armar(Lado(ok))
    assert cliente.gestor.max_retries == 0
    assert cliente.gestor.timeout.connect == mod.CONEXION_S == 3.0


def test_el_documento_trae_el_ejemplo_tal_cual():
    doc = (RAIZ / "docs/conectar-programas.md").read_text()
    assert EJEMPLO.read_text().strip() in doc


def test_desde_entorno(monkeypatch):
    mod = cargar()
    monkeypatch.setenv("PASARELA_IA_URL", GESTOR)
    monkeypatch.setenv("PASARELA_IA_LLAVE", LLAVE_PIA)
    monkeypatch.setenv("ANTHROPIC_API_KEY", LLAVE_DIRECTA)
    c = mod.ClienteConRespaldo.desde_entorno()
    assert str(c.gestor.base_url).rstrip("/") == GESTOR and c.gestor.api_key == LLAVE_PIA
    assert c.directo.api_key == LLAVE_DIRECTA and json.dumps(str(c.directo.base_url)).count("api.anthropic.com") == 1
