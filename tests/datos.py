"""Datos de prueba compartidos. Se importa como `import datos` (pytest pone tests/ en sys.path)."""
import json
import time

import anyio
import httpx
from sqlalchemy import select

from pasarela.esquema import registro

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-5-5",'
       b'"usage":{"input_tokens":120,"output_tokens":1,"cache_read_input_tokens":0}}}\n\n'
       b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
       b'"delta":{"type":"text_delta","text":"hola"}}\n\n'
       b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
       b'"usage":{"output_tokens":42}}\n\n'
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')

SSE_MODELO_NUM = SSE.replace(b'"model":"claude-opus-5-5"', b'"model":123')

SSE_RARO = b'event: message_start\ndata: {"type":"message_start","message":"raro"}\n\n' + SSE

MSG = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hola"}]}
OK = {"id": "msg_1", "type": "message", "model": "claude-opus-5-5",
      "content": [{"type": "text", "text": "hola"}], "usage": {"input_tokens": 1000, "output_tokens": 500}}
SALDO = {"type": "error", "error": {"type": "invalid_request_error",
                                    "message": "Your credit balance is too low to access the Anthropic API."}}
# un archivo que nace en la respuesta (ejecución de código). La forma es la de la documentación oficial:
# el resultado llega en un bloque bash_code_execution_tool_result y, en streaming, entero en un content_block_start.
BLOQUE_ARCHIVO = {"type": "bash_code_execution_tool_result", "tool_use_id": "srvtoolu_1",
                  "content": {"type": "bash_code_execution_result", "stdout": "", "stderr": "", "return_code": 0,
                              "content": [{"type": "bash_code_execution_output", "file_id": "file_gen1"}]}}
OK_CON_ARCHIVO = {**OK, "content": [
    {"type": "server_tool_use", "id": "srvtoolu_1", "name": "bash_code_execution",
     "input": {"command": "cp /tmp/r.pdf $OUTPUT_DIR/", "file_id": "file_del_modelo"}},
    BLOQUE_ARCHIVO, {"type": "text", "text": "listo"}]}
SSE_CON_ARCHIVO = (SSE[:SSE.index(b"event: message_delta")]
                   + b"event: content_block_start\ndata: "
                   + json.dumps({"type": "content_block_start", "index": 2, "content_block": BLOQUE_ARCHIVO}).encode()
                   + b"\n\nevent: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":2}\n\n"
                   + SSE[SSE.index(b"event: message_delta"):])
LOTE = {"id": "msgbatch_1", "type": "message_batch", "processing_status": "ended",
        "created_at": "2026-11-07T00:00:00Z",
        "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"}


class Trozos(httpx.AsyncByteStream):
    """Cuerpo que sale en varios trozos; si `falla`, revienta con ReadError al llegar a el."""

    def __init__(self, trozos, falla=False):
        self.trozos = trozos
        self.falla = falla

    async def __aiter__(self):
        for t in self.trozos:
            yield t
        if self.falla:
            raise httpx.ReadError("x")


class Lento(httpx.AsyncByteStream):
    """Manda el primer trozo y luego tarda, como un modelo que sigue pensando."""

    def __init__(self, primero: bytes, espera: float = 5):
        self.primero = primero
        self.espera = espera

    async def __aiter__(self):
        yield self.primero
        await anyio.sleep(self.espera)


def en_trozos(cuerpo: bytes, n: int = 3) -> list[bytes]:
    """Parte el cuerpo en n trozos; el primero siempre trae el primer evento completo."""
    corte = cuerpo.index(b"\n\n") + 2
    resto = cuerpo[corte:]
    paso = max(1, len(resto) // (n - 1))
    return [cuerpo[:corte]] + [resto[i:i + paso] for i in range(0, len(resto), paso)]


def responde(status, cuerpo, cabeceras=None):
    return lambda req: httpx.Response(status, json=cuerpo, headers={"request-id": "req_1", **(cabeceras or {})})


def filas(motor):
    with motor.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(registro).order_by(registro.c.id))]


async def llamar_asgi(app, ruta: str, cuerpo: dict | None, cabeceras: dict,
                     metodo: str = "POST") -> list[tuple[float, dict]]:
    """Petición a la app como la hace uvicorn (ASGI 2.3), sin TestClient: TestClient espera las tareas de fondo antes
    de entregar la respuesta y aquí se quiere ver a qué hora sale cada mensaje.
    Regresa (hora, mensaje) de cada envío."""
    crudo = json.dumps(cuerpo).encode() if cuerpo is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": metodo, "scheme": "http", "path": ruta, "raw_path": ruta.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                         *[(k.encode(), v.encode()) for k, v in cabeceras.items()]]}
    pedido = False
    enviados = []

    async def receive():
        nonlocal pedido
        if not pedido:
            pedido = True
            return {"type": "http.request", "body": crudo, "more_body": False}
        await anyio.sleep_forever()               # el cliente nunca se va

    async def send(mensaje):
        enviados.append((time.monotonic(), mensaje))

    await app(scope, receive, send)
    return enviados


async def subir_asgi(app, cabeceras: dict, partes: list[bytes], colgar: bool = False,
                     ruta: str = "/anthropic/v1/files", metodo: str = "POST") -> list[tuple[float, dict]]:
    """POST a la app como la hace uvicorn, con el cuerpo en `partes`. Con colgar=True el cliente deja de mandar datos
    sin cerrar el cuerpo ni irse. Regresa (hora, mensaje) de cada envío."""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": metodo, "scheme": "http", "path": ruta, "raw_path": ruta.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), *[(k.encode(), v.encode()) for k, v in cabeceras.items()]]}
    entregadas = 0
    enviados = []

    async def receive():
        nonlocal entregadas
        if entregadas < len(partes):
            entregadas += 1
            return {"type": "http.request", "body": partes[entregadas - 1],
                    "more_body": entregadas < len(partes) or colgar}
        await anyio.sleep_forever()

    async def send(mensaje):
        enviados.append((time.monotonic(), mensaje))

    await app(scope, receive, send)
    return enviados


def estatus(enviados) -> int:
    return next(m["status"] for _, m in enviados if m["type"] == "http.response.start")


def ultimo_envio(enviados) -> float:
    """Hora del mensaje que cierra el cuerpo: desde ahí el cliente ya tiene su respuesta completa."""
    return next(t for t, m in enviados if m["type"] == "http.response.body" and not m.get("more_body"))


def esperar_escrituras(cliente, hasta: float = 5.0) -> None:
    """Las filas de los streams se escriben después del último trozo, en el grupo de tareas de la app. La prueba
    espera a que terminen antes de leer o de mandar otra petición: SQLite en memoria es una sola conexión."""
    estado = cliente.app.state.pia
    fin = time.monotonic() + hasta
    while estado.pendientes and time.monotonic() < fin:
        time.sleep(0.01)
    assert estado.pendientes == 0, "quedaron escrituras sin terminar"
