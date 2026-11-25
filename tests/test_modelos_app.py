"""El catálogo de modelos dentro de la app: carga al arrancar, refresco desde /v1/models y su ciclo."""
import time
from datetime import datetime

import anyio
import httpx
from conftest import ENV
from fastapi.testclient import TestClient
from sqlalchemy import select

from pasarela import app as modulo_app
from pasarela import modelos
from pasarela.app import crear_app
from pasarela.config import cargar
from pasarela.esquema import modelo


def pagina(*ids, has_more=False):
    data = [{"type": "model", "id": i, "display_name": i, "created_at": "2026-10-25T00:00:00Z"} for i in ids]
    return {"data": data, "has_more": has_more, "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None}


def contesta(cuerpo, status=200):
    return lambda req: httpx.Response(status, json=cuerpo)


def test_config_del_catalogo():
    assert cargar({k: v for k, v in ENV.items() if k != "PIA_CATALOGO_S"}).catalogo_s == 21600
    assert cargar(ENV).catalogo_s == 0


def test_refrescar_lee_v1_models_y_guarda(app, falso, motor):
    falso.respuestas["*"] = contesta(pagina("claude-haiku-5-5"))
    estado = app.state.pia
    assert anyio.run(modulo_app.refrescar_modelos, estado) is True
    req = falso.recibidas[0]
    assert (req.method, req.url.path, req.url.params["limit"]) == ("GET", "/v1/models", "1000")
    assert req.headers["anthropic-version"] == "2023-06-01" and req.headers["x-api-key"] == "sk-falsa-org1"
    assert estado.modelos.alias == {"claude-ultimo-haiku": "claude-haiku-5-5"}
    with motor.connect() as c:
        assert list(c.execute(select(modelo.c.id)).scalars()) == ["claude-haiku-5-5"]


def test_lee_todas_las_paginas(app, falso):
    paginas = iter([pagina("claude-opus-5-5", has_more=True), pagina("claude-haiku-5-5")])
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=next(paginas))
    estado = app.state.pia
    assert anyio.run(modulo_app.refrescar_modelos, estado) is True
    assert falso.recibidas[1].url.params["after_id"] == "claude-opus-5-5"
    assert set(estado.modelos.alias) == {"claude-ultimo-opus", "claude-ultimo-haiku"}


def test_si_una_cuenta_falla_prueba_la_siguiente(app, falso, capsys):
    falso.respuestas["sk-falsa-org1"] = contesta({"type": "error"}, 500)
    falso.respuestas["sk-falsa-org2"] = contesta(pagina("claude-haiku-5-5"))
    assert anyio.run(modulo_app.refrescar_modelos, app.state.pia) is True
    assert [r.headers["x-api-key"] for r in falso.recibidas] == ["sk-falsa-org1", "sk-falsa-org2"]
    err = capsys.readouterr().err
    assert "org1" in err and "500" in err and "sk-falsa" not in err


def test_si_ninguna_contesta_se_queda_lo_que_habia(app, falso, motor):
    estado = app.state.pia
    previos = modelos.de_anthropic(pagina("claude-opus-5-5")["data"])
    antes = modelos.armar(previos, datetime(2026, 11, 12))
    modelos.guardar_modelos(motor, previos, antes, datetime(2026, 11, 12))
    estado.modelos.poner(antes)
    # error de Anthropic, lista vacía, cuerpo sin forma, paginación rota y una lista sin ningún modelo con precio
    #
    malas = [({"type": "error"}, 500), (pagina(), 200), ({"data": "x"}, 200),
             (pagina("claude-opus-5", has_more=True) | {"last_id": None}, 200),
             (pagina("claude-modelo-sin-precio-9"), 200)]
    for cuerpo, status in malas:
        falso.respuestas["*"] = contesta(cuerpo, status)
        assert anyio.run(modulo_app.refrescar_modelos, estado) is False
        assert estado.modelos.lista is antes
        with motor.connect() as c:
            assert list(c.execute(select(modelo.c.id)).scalars()) == ["claude-opus-5-5"]


def test_si_la_base_falla_el_catalogo_en_memoria_igual_cambia(app, falso, monkeypatch):
    def truena(*_):
        raise RuntimeError("base")
    monkeypatch.setattr(modelos, "guardar_modelos", truena)
    falso.respuestas["*"] = contesta(pagina("claude-haiku-5-5"))
    estado = app.state.pia
    assert anyio.run(modulo_app.refrescar_modelos, estado) is True
    assert estado.modelos.alias == {"claude-ultimo-haiku": "claude-haiku-5-5"}


def test_al_arrancar_lee_lo_guardado(config, motor, falso):
    vistos = modelos.de_anthropic(pagina("claude-opus-5-5")["data"])
    modelos.guardar_modelos(motor, vistos, modelos.armar(vistos, datetime(2026, 11, 12)), datetime(2026, 11, 12))
    app = crear_app(config, motor, httpx.MockTransport(falso))
    assert app.state.pia.modelos.alias == {"claude-ultimo-opus": "claude-opus-5-5"}
    assert falso.recibidas == []


def test_la_vida_lee_los_modelos_al_arrancar(motor, falso):
    falso.respuestas["*"] = contesta(pagina("claude-haiku-5-5"))
    app = crear_app(cargar({**ENV, "PIA_CATALOGO_S": "3600"}), motor, httpx.MockTransport(falso))
    with TestClient(app):
        fin = time.monotonic() + 2
        while not app.state.pia.modelos.cargado and time.monotonic() < fin:
            time.sleep(0.01)
    assert app.state.pia.modelos.alias == {"claude-ultimo-haiku": "claude-haiku-5-5"}


def test_el_ciclo_reintenta_pronto_si_falla(monkeypatch):
    llamadas = []

    async def refresco_que_falla(estado):
        llamadas.append(1)
        return False

    monkeypatch.setattr(modulo_app, "refrescar_modelos", refresco_que_falla)
    monkeypatch.setattr(modulo_app, "REINTENTO_MODELOS_S", 0.01)

    async def probar():
        alcance = anyio.CancelScope()
        async with anyio.create_task_group() as tg:
            tg.start_soon(modulo_app.modelos_siempre, None, 3600, alcance)
            await anyio.sleep(0.2)
            alcance.cancel()

    anyio.run(probar)
    assert len(llamadas) >= 3


def test_refresco_que_truena_no_lanza(app, monkeypatch):
    async def truena(*_):
        raise RuntimeError("raro")
    monkeypatch.setattr(modelos, "traer", truena)
    assert anyio.run(modulo_app.refrescar_modelos, app.state.pia) is False


def test_la_llave_nunca_sale_en_stderr(app, falso, capsys):
    """una llave con fin de línea hace que h11 lance un error con la llave en el texto. Solo se imprime
    el tipo; el texto de ErrorModelos sí sale porque lo arma la pasarela."""
    def truena(req):
        raise httpx.LocalProtocolError(f"Illegal header value b'{req.headers['x-api-key']}\\n'")
    falso.respuestas["sk-falsa-org1"] = contesta({"type": "error"}, 500)
    falso.respuestas["sk-falsa-org2"] = truena
    assert anyio.run(modulo_app.refrescar_modelos, app.state.pia) is False
    err = capsys.readouterr().err
    assert "sk-falsa" not in err and "LocalProtocolError" in err
    assert "ErrorModelos: Anthropic contestó 500" in err


def test_fila_rara_en_la_base_no_impide_arrancar(config, motor, falso, monkeypatch, capsys):
    """si armar() truena con lo guardado, la app arranca igual, sin catálogo."""
    vistos = modelos.de_anthropic(pagina("claude-opus-5-5")["data"])
    modelos.guardar_modelos(motor, vistos, modelos.armar(vistos, datetime(2026, 11, 12)), datetime(2026, 11, 12))

    def truena(*_):
        raise TypeError("fila rara")
    monkeypatch.setattr(modelos, "armar", truena)
    app = crear_app(config, motor, httpx.MockTransport(falso))
    assert app.state.pia.modelos.cargado is False
    assert "no pude leer el catálogo guardado (TypeError)" in capsys.readouterr().err
