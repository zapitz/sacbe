import json

from datos import BLOQUE_ARCHIVO, OK_CON_ARCHIVO, SSE, SSE_CON_ARCHIVO

from pasarela.uso import LectorJSONL, LectorSSE, archivos_de_bloque, mensaje_de_json, uso_de_json


def test_sse_completo():
    lector = LectorSSE()
    lector.alimentar(SSE)
    assert lector.modelo == "claude-opus-5-5"
    assert lector.uso["input_tokens"] == 120 and lector.uso["output_tokens"] == 42


def test_sse_de_byte_en_byte():
    lector = LectorSSE()
    for i in range(len(SSE)):
        lector.alimentar(SSE[i:i + 1])
    assert lector.uso["output_tokens"] == 42


def test_json():
    assert uso_de_json(b'{"model":"m","usage":{"input_tokens":5}}') == ("m", {"input_tokens": 5})
    assert uso_de_json(b"no es json") == (None, {})
    assert uso_de_json(b"[1,2]") == (None, {})


def test_jsonl_de_lote():
    linea_ok = (b'{"custom_id":"a","result":{"type":"succeeded","message":{"model":"claude-opus-5-5",'
                b'"usage":{"input_tokens":1000,"output_tokens":500}}}}')
    linea_error = b'{"custom_id":"b","result":{"type":"errored","error":{}}}'
    todo = linea_ok + b"\n" + linea_error + b"\n" + linea_ok      # la última sin salto de línea
    lector = LectorJSONL()
    lector.alimentar(todo[:50])
    lector.alimentar(todo[50:])
    lector.terminar()
    assert lector.n == 2
    assert lector.micro == 14_000          # 7,000 por petición con descuento de lote
    assert lector.tokens["entrada"] == 2000 and lector.tokens["salida"] == 1000
    assert lector.modelos == {"claude-opus-5-5"}


def test_resultados_de_lote_con_modelo_sin_precio_avisan_una_vez(capsys):
    modelo = "claude-sin-precio-prueba-jsonl-" + "x" * 100
    linea = (b'{"custom_id":"a","result":{"type":"succeeded","message":{"model":"' + modelo.encode()
             + b'","usage":{"input_tokens":1}}}}\n')
    lector = LectorJSONL()
    lector.alimentar(linea * 2)
    lector.terminar()
    assert capsys.readouterr().err.splitlines() == [f"pasarela: modelo sin precio {modelo[:80]}, se estimó como Fable 5.1"]


# file_id de archivos que nacen en la respuesta.

def test_archivos_de_bloque_solo_resultados_de_herramientas_de_servidor():
    assert archivos_de_bloque(BLOQUE_ARCHIVO) == {"file_gen1"}
    viejo = {"type": "code_execution_tool_result", "content": {"type": "code_execution_result",
             "content": [{"type": "code_execution_output", "file_id": "file_v"}, {"file_id": "file_w"}]}}
    assert archivos_de_bloque(viejo) == {"file_v", "file_w"}
    assert archivos_de_bloque({"type": "tool_use", "input": {"file_id": "file_m"}}) == set()
    assert archivos_de_bloque({"type": "server_tool_use", "input": {"file_id": "file_m"}}) == set()
    assert archivos_de_bloque({"type": "mcp_tool_result", "content": [{"file_id": "file_t"}]}) == set()
    assert archivos_de_bloque({"type": "text", "text": '{"file_id": "file_x"}'}) == set()
    assert archivos_de_bloque({"type": "tool_result", "content": {"file_id": "file_c"}}) == set()
    assert archivos_de_bloque("raro") == set() and archivos_de_bloque({"type": 5, "file_id": "f"}) == set()


def test_archivos_de_bloque_ignora_ids_raros_y_no_usa_recursion():
    raro = {"type": "x_tool_result", "content": [{"file_id": 7}, {"file_id": ""}, {"file_id": "f" * 121},
                                                  {"file_id": {"file_id": "file_dentro"}}, {"file_id": "f" * 120}]}
    assert archivos_de_bloque(raro) == {"file_dentro", "f" * 120}
    hondo = {"file_id": "file_fondo"}
    for _ in range(50_000):
        hondo = {"c": [hondo]}
    assert archivos_de_bloque({"type": "x_tool_result", "content": hondo}) == {"file_fondo"}


def test_mensaje_de_json_trae_los_archivos():
    assert mensaje_de_json(json.dumps(OK_CON_ARCHIVO).encode()) == (
        "claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 500}, {"file_gen1"})
    assert mensaje_de_json(b'{"model":"m","usage":{"input_tokens":5}}') == ("m", {"input_tokens": 5}, set())
    assert mensaje_de_json(b'{"model":"m","content":"texto"}') == ("m", {}, set())
    assert mensaje_de_json(b"no es json") == (None, {}, set()) and mensaje_de_json(b"[1]") == (None, {}, set())
    assert uso_de_json(json.dumps(OK_CON_ARCHIVO).encode()) == ("claude-opus-5-5", {"input_tokens": 1000,
                                                                                    "output_tokens": 500})


def test_sse_junta_los_archivos_y_los_entrega_una_vez():
    lector = LectorSSE()
    corte = SSE_CON_ARCHIVO.index(b"file_gen1") + 3            # el evento llega partido en dos trozos
    lector.alimentar(SSE_CON_ARCHIVO[:corte])
    assert lector.archivos == set()
    lector.alimentar(SSE_CON_ARCHIVO[corte:])
    assert lector.sacar_archivos() == {"file_gen1"} and lector.sacar_archivos() == set()
    assert (lector.modelo, lector.uso["output_tokens"]) == ("claude-opus-5-5", 42)      # el uso se sigue leyendo


def test_jsonl_de_lote_junta_los_archivos():
    def linea(custom_id, contenido):
        return json.dumps({"custom_id": custom_id, "result": {"type": "succeeded", "message": {
            "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 500},
            "content": contenido}}}).encode()
    otro = {**BLOQUE_ARCHIVO, "content": {**BLOQUE_ARCHIVO["content"],
                                          "content": [{"type": "bash_code_execution_output", "file_id": "file_gen2"}]}}
    lector = LectorJSONL()
    lector.alimentar(linea("a", [BLOQUE_ARCHIVO]) + b"\n" + b'{"custom_id":"b","result":{"type":"errored"}}\n')
    assert lector.sacar_archivos() == {"file_gen1"}
    lector.alimentar(linea("c", [otro]))                       # la última sin salto de línea
    assert lector.sacar_archivos() == set()
    lector.terminar()
    assert lector.sacar_archivos() == {"file_gen2"} and lector.n == 2 and lector.micro == 14_000


def test_sse_con_crlf_partido_entre_trozos_se_lee_igual():
    crlf = SSE_CON_ARCHIVO.replace(b"\n", b"\r\n")
    lector = LectorSSE()
    for i in range(len(crlf)):                                  # byte por byte: cada \r\n queda partido
        lector.alimentar(crlf[i:i + 1])
    assert lector.sacar_archivos() == {"file_gen1"}
    assert (lector.modelo, lector.uso["output_tokens"]) == ("claude-opus-5-5", 42)
    for corte in range(1, len(crlf)):                           # y en dos trozos, en todos los cortes posibles
        lector = LectorSSE()
        lector.alimentar(crlf[:corte])
        lector.alimentar(crlf[corte:])
        assert lector.sacar_archivos() == {"file_gen1"} and lector.uso["output_tokens"] == 42, corte
