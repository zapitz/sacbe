"""Una base lenta o congelada nunca frena ni bloquea las respuestas."""
import ast
import inspect
import json
import threading
import time
from pathlib import Path

import anyio
import httpx
import pytest
from datos import (LOTE, MSG, OK, SSE, Lento, Trozos, en_trozos, esperar_escrituras, estatus, filas, llamar_asgi,
                   responde, ultimo_envio)
from sqlalchemy import create_engine, insert

from pasarela import app as modulo_app
from pasarela import proxy
from pasarela.esquema import DESBORDE, POOL, objeto
from pasarela.tiempo import ahora


def congelada(soltar: threading.Event, hasta: float = 1.5):
    """Una consulta a la base que no contesta hasta que la prueba la suelta (o pasa `hasta`); luego da None."""
    def consulta(*_):
        soltar.wait(hasta)
    return consulta


def test_la_base_usa_hilos_propios(app):
    """Llamadas colgadas a la base llenan sus dos limitadores y dejan libre el de anyio que usa todo lo demás."""
    estado = app.state.pia
    assert (estado.hilos_lectura.total_tokens, estado.hilos_escritura.total_tokens) == (4, 6)
    assert estado.hilos_lectura.total_tokens + estado.hilos_escritura.total_tokens == POOL + DESBORDE
    soltar = threading.Event()

    async def probar():
        async with anyio.create_task_group() as tg:
            for limite in (estado.hilos_lectura, estado.hilos_escritura):
                for _ in range(int(limite.total_tokens)):
                    tg.start_soon(proxy.en_base, limite, soltar.wait, 5)
            with anyio.fail_after(2):
                while estado.hilos_lectura.borrowed_tokens + estado.hilos_escritura.borrowed_tokens < 10:
                    await anyio.sleep(0.01)
            ocupados = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
            soltar.set()
        return ocupados

    assert anyio.run(probar) == 0


ESCRITURAS = {"guardar_registro", "registrar_costo_de_lote"}     # las filas que se escriben ya enviada la respuesta


def test_toda_llamada_a_un_hilo_pasa_por_en_base():
    """Cada to_thread.run_sync de la pasarela toca la base, así que todas van por en_base; y cada en_base elige a la
    vista su limitador: escritura para las filas de uso, lectura para todo lo que una petición o el refresco esperan."""
    vistas = set()
    for f in sorted(Path(proxy.__file__).parent.glob("*.py")):
        texto = f.read_text()
        assert texto.count("to_thread.run_sync") == (1 if f.name == "proxy.py" else 0), f.name
        for nodo in ast.walk(ast.parse(texto)):
            if isinstance(nodo, ast.Call) and ast.unparse(nodo.func) in ("en_base", "proxy.en_base"):
                limite, fn = ast.unparse(nodo.args[0]), ast.unparse(nodo.args[1]).rsplit(".", 1)[-1]
                esperado = "estado.hilos_escritura" if fn in ESCRITURAS else "estado.hilos_lectura"
                assert limite == esperado, f"{f.name}:{nodo.lineno} {fn} va por {limite}"
                vistas.add(fn)
    assert vistas == ESCRITURAS | {"objeto_de", "probar_base", "refrescar", "marcar_agotada", "guardar_objeto",
                                   "recalcular", "guardar_modelos", "uso_de", "usos_de",
                                   "guardar_objetos", "marcar_borrado", "pagina_de_objetos", "objetos_entre", "marcar_caducos",
                                   "quien_de", "ejecutar", "datos_panel", "cargar_modelos_guardados", "leer"}


def test_fijada_con_base_congelada_es_503(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "ESPERA_FIJADA_S", 0.2, raising=False)
    soltar = threading.Event()
    monkeypatch.setattr(proxy, "objeto_de", congelada(soltar))
    try:
        t0 = time.monotonic()
        r = cliente.get("/anthropic/v1/messages/batches/msgbatch_1", headers={"x-api-key": llaves["prod"]})
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
    assert tardo < 1 and falso.recibidas == []
    monkeypatch.undo()
    assert proxy.ESPERA_FIJADA_S == 5


def test_salud_es_async(cliente):
    salud = next(r.endpoint for r in cliente.app.routes if getattr(r, "path", None) == "/salud")
    assert inspect.iscoroutinefunction(salud)


def test_salud_con_base_congelada_dice_que_la_base_no_responde(cliente, monkeypatch):
    monkeypatch.setattr(modulo_app, "ESPERA_SALUD_S", 0.2, raising=False)
    soltar = threading.Event()
    monkeypatch.setattr(modulo_app, "probar_base", congelada(soltar), raising=False)
    try:
        t0 = time.monotonic()
        r = cliente.get("/salud")
        tardo = time.monotonic() - t0
    finally:
        soltar.set()
    assert r.status_code == 200 and tardo < 1
    assert r.json() == {"ok": True, "base": False, "proyectos": 2, "agotadas": [], "fallas_registro": 0}
    monkeypatch.undo()
    assert modulo_app.ESPERA_SALUD_S == 3


def test_salud_con_base_caida_dice_que_la_base_no_responde(cliente):
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    r = cliente.get("/salud")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["base"] is False


def lenta(real):
    """La escritura del registro tarda 2 s, como una base lenta."""
    def guardar(motor, fila):
        time.sleep(2)
        return real(motor, fila)
    return guardar


LIMITE = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}


@pytest.mark.parametrize("status,cuerpo", [(200, OK), (429, LIMITE)], ids=["json", "error"])
def test_la_respuesta_sale_antes_de_escribir_el_registro(app, llaves, falso, motor, monkeypatch, status, cuerpo):
    falso.respuestas["*"] = responde(status, cuerpo)
    monkeypatch.setattr(proxy, "guardar_registro", lenta(proxy.guardar_registro))
    t0 = time.monotonic()
    enviados = anyio.run(llamar_asgi, app, "/anthropic/v1/messages", MSG, {"x-api-key": llaves["prod"]})
    fin = time.monotonic()
    assert ultimo_envio(enviados) - t0 < 1        # el cliente ya tiene la respuesta completa
    assert fin - t0 >= 2                          # la escritura vino después, con la respuesta ya enviada
    f = filas(motor)
    assert len(f) == 1 and f[0]["status"] == status


def test_el_ultimo_trozo_del_stream_no_espera_a_la_base(app, llaves, falso, motor, monkeypatch):
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(proxy, "guardar_registro", lenta(proxy.guardar_registro))

    async def con_vida():
        async with app.router.lifespan_context(app):
            enviados = await llamar_asgi(app, "/anthropic/v1/messages", {**MSG, "stream": True},
                                         {"x-api-key": llaves["prod"]})
            return enviados, time.monotonic()

    t0 = time.monotonic()
    enviados, regreso = anyio.run(con_vida)
    fin = time.monotonic()
    assert b"".join(m.get("body", b"") for _, m in enviados if m["type"] == "http.response.body") == SSE
    assert ultimo_envio(enviados) - t0 < 1 and regreso - t0 < 1     # ni el último trozo ni la app esperaron
    assert fin - t0 >= 2                          # al cerrar, la vida de la app sí esperó la escritura
    f = filas(motor)
    assert len(f) == 1 and (f[0]["entrada"], f[0]["salida"]) == (120, 42)


@pytest.mark.parametrize("cuerpo", [
    lambda: Lento(en_trozos(SSE)[0]),             # el cliente se va mientras el modelo sigue pensando
    lambda: Trozos(en_trozos(SSE, 4)),            # el cliente se va con trozos listos para mandar
], ids=["modelo-pensando", "trozos-listos"])
def test_stream_cortado_por_el_cliente_igual_se_registra(cliente, llaves, falso, motor, cuerpo):
    falso.respuestas["*"] = lambda req: httpx.Response(200, stream=cuerpo(),
                                                       headers={"content-type": "text/event-stream"})
    t0 = time.monotonic()
    with cliente.stream("POST", "/anthropic/v1/messages", json={**MSG, "stream": True},
                        headers={"x-api-key": llaves["prod"]}) as r:
        assert next(r.iter_bytes()) == en_trozos(SSE)[0]
    assert time.monotonic() - t0 < 2              # al cerrar, la app se cancela: no espera al modelo
    esperar_escrituras(cliente)
    f = filas(motor)
    assert len(f) == 1 and f[0]["entrada"] == 120 and f[0]["modelo"] == "claude-opus-5-5"


def test_stream_sin_la_vida_de_la_app_igual_se_registra(app, llaves, falso, motor):
    """Sin lifespan no hay grupo de tareas: la fila se escribe en el finally mismo (uvicorn siempre la arranca)."""
    falso.respuestas["*"] = lambda req: httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
    anyio.run(llamar_asgi, app, "/anthropic/v1/messages", {**MSG, "stream": True}, {"x-api-key": llaves["prod"]})
    assert app.state.pia.tareas is None and len(filas(motor)) == 1


def colgada(soltar: threading.Event, llamadas: list):
    """Una escritura del registro que no termina hasta que la prueba la suelta (o pasan 10 s)."""
    def guardar(motor, fila):
        llamadas.append(fila)
        soltar.wait(10)
        return True
    return guardar


def test_escrituras_colgadas_no_frenan_las_lecturas(app, llaves, falso, motor, monkeypatch):
    """Veinte filas de uso esperando a una base que no escribe: consultar un lote sigue saliendo al momento."""
    with motor.begin() as c:
        c.execute(insert(objeto).values(id="msgbatch_1", tipo="lote", fuente="org1", proyecto="crm",
                                        creado=ahora(), costo_registrado=False))
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=LOTE if "batches" in req.url.path else OK)
    soltar, llamadas = threading.Event(), []
    monkeypatch.setattr(proxy, "guardar_registro", colgada(soltar, llamadas))
    h = {"x-api-key": llaves["prod"]}

    async def probar():
        async with app.router.lifespan_context(app), anyio.create_task_group() as tg:
            try:
                for _ in range(20):
                    tg.start_soon(llamar_asgi, app, "/anthropic/v1/messages", MSG, h)
                with anyio.fail_after(2):
                    while len(llamadas) < 6:
                        await anyio.sleep(0.01)
                await anyio.sleep(0.1)                # las demás ya esperan su turno detrás
                t0 = time.monotonic()
                enviados = await llamar_asgi(app, "/anthropic/v1/messages/batches/msgbatch_1", None, h, "GET")
                return enviados, time.monotonic() - t0
            finally:
                soltar.set()

    enviados, tardo = anyio.run(probar)
    assert estatus(enviados) == 200 and tardo < 1


def test_con_el_tope_de_escrituras_lleno_la_fila_se_descarta_y_el_cliente_ni_lo_nota(app, llaves, falso, monkeypatch,
                                                                                    capsys):
    """Una sola cuenta junta las dos vías de fondo (BackgroundTask y despues). Llena, la fila nueva se pierde, se
    cuenta en fallas_registro y se avisa una vez por minuto; la respuesta sale igual."""
    falso.respuestas["*"] = lambda req: (
        httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        if json.loads(req.content).get("stream") else httpx.Response(200, json=OK))
    monkeypatch.setattr(proxy, "TOPE_PENDIENTES", 3, raising=False)
    soltar, llamadas = threading.Event(), []
    monkeypatch.setattr(proxy, "guardar_registro", colgada(soltar, llamadas))
    estado = app.state.pia
    h = {"x-api-key": llaves["prod"]}

    async def probar():
        async with app.router.lifespan_context(app), anyio.create_task_group() as tg:
            try:
                for _ in range(3):
                    tg.start_soon(llamar_asgi, app, "/anthropic/v1/messages", MSG, h)
                with anyio.fail_after(2):
                    while estado.pendientes < 3:
                        await anyio.sleep(0.01)
                with anyio.fail_after(2):         # ya no esperan a nadie: ni la respuesta ni la escritura
                    json_ = await llamar_asgi(app, "/anthropic/v1/messages", MSG, h)
                    stream = await llamar_asgi(app, "/anthropic/v1/messages", {**MSG, "stream": True}, h)
                return json_, stream, estado.fallas_registro, estado.pendientes
            finally:
                soltar.set()

    json_, stream, fallas, pendientes = anyio.run(probar)
    assert estatus(json_) == 200 and estatus(stream) == 200
    assert b"".join(m.get("body", b"") for _, m in stream if m["type"] == "http.response.body") == SSE
    assert (fallas, pendientes, len(llamadas)) == (2, 3, 3)
    err = capsys.readouterr().err
    assert err.count("registro descartado") == 1
    assert "pasarela: registro descartado, 3 escrituras pendientes\n" in err
    assert (estado.fallas_registro, estado.pendientes) == (2, 0)
    monkeypatch.undo()
    assert proxy.TOPE_PENDIENTES == 2000


def test_el_aviso_de_descarte_vuelve_a_salir_pasado_un_minuto(app, monkeypatch, capsys):
    estado = app.state.pia
    monkeypatch.setattr(proxy, "TOPE_PENDIENTES", 0)
    assert not proxy.reservar(estado) and not proxy.reservar(estado)
    estado.aviso_descarte -= 61
    assert not proxy.reservar(estado)
    assert capsys.readouterr().err.count("registro descartado, 0 escrituras pendientes") == 2
    assert (estado.fallas_registro, estado.pendientes) == (3, 0)
