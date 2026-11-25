"""GET /anthropic/v1/messages/batches: los lotes del proyecto, de todas las cuentas, paginados."""
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, insert

from pasarela import proxy
from pasarela.almacen import objeto_de
from pasarela.esquema import objeto
from pasarela.tiempo import ahora

T0 = datetime(2026, 11, 14, 12, 0, 0)
ORG1, ORG2 = "sk-falsa-org1", "sk-falsa-org2"
RUTA = "/anthropic/v1/messages/batches"
NO_ESTA = {"type": "error", "error": {"type": "not_found_error", "message": "no existe"}}


def lote(i):
    return {"id": i, "type": "message_batch", "processing_status": "ended", "created_at": "2026-11-14T12:00:00Z",
            "results_url": f"https://api.anthropic.com/v1/messages/batches/{i}/results"}


def sembrar(cliente, filas):
    """filas: (id, cuenta, proyecto, segundos después de T0)."""
    with cliente.app.state.pia.motor.begin() as c:
        for i, fuente, proyecto, s in filas:
            c.execute(insert(objeto).values(id=i, tipo="lote", fuente=fuente, proyecto=proyecto,
                                            creado=T0 + timedelta(seconds=s), costo_registrado=False))


def anthropic_con(falso, tiene: dict[str, set[str]]):
    """Anthropic de mentira: GET v1/messages/batches/{id} da el lote si esa cuenta lo tiene y 404 si no."""
    def responder(req):
        i = req.url.path.rsplit("/", 1)[-1]
        if i in tiene.get(req.headers["x-api-key"], set()):
            return httpx.Response(200, json=lote(i), headers={"request-id": "req_1"})
        return httpx.Response(404, json=NO_ESTA)
    falso.respuestas["*"] = responder


def cuatro(cliente, falso):
    sembrar(cliente, [("msgbatch_a", "org1", "crm", 1), ("msgbatch_b", "org2", "crm", 2),
                      ("msgbatch_c", "org1", "crm", 3), ("msgbatch_d", "org2", "crm", 4),
                      ("msgbatch_x", "org1", "lotes", 5)])
    anthropic_con(falso, {ORG1: {"msgbatch_a", "msgbatch_c", "msgbatch_x"}, ORG2: {"msgbatch_b", "msgbatch_d"}})


def prod(llaves):
    return {"x-api-key": llaves["prod"], "anthropic-version": "2023-06-01"}


def ids(r):
    return [o["id"] for o in r.json()["data"]]


def test_lista_los_lotes_propios_de_todas_las_cuentas_con_su_url_hacia_la_pasarela(cliente, llaves, falso):
    cuatro(cliente, falso)
    r = cliente.get(RUTA, headers=prod(llaves))
    d = r.json()
    assert r.status_code == 200 and ids(r) == ["msgbatch_d", "msgbatch_c", "msgbatch_b", "msgbatch_a"]
    assert set(d) == {"data", "has_more", "first_id", "last_id"}                   # la forma de Anthropic para lotes
    assert (d["has_more"], d["first_id"], d["last_id"]) == (False, "msgbatch_d", "msgbatch_a")
    assert d["data"][0]["results_url"] == "http://testserver/anthropic/v1/messages/batches/msgbatch_d/results"
    pedidos = {(q.url.path.rsplit("/", 1)[-1], q.headers["x-api-key"]) for q in falso.recibidas}
    assert pedidos == {("msgbatch_a", ORG1), ("msgbatch_c", ORG1), ("msgbatch_b", ORG2), ("msgbatch_d", ORG2)}
    assert ids(cliente.get(RUTA, headers={"x-api-key": llaves["extra"]})) == ["msgbatch_x"]


def test_pagina_con_after_id_y_before_id(cliente, llaves, falso):
    cuatro(cliente, falso)
    r = cliente.get(RUTA, params={"limit": 2}, headers=prod(llaves))
    assert ids(r) == ["msgbatch_d", "msgbatch_c"] and r.json()["has_more"] is True
    r = cliente.get(RUTA, params={"limit": 2, "after_id": r.json()["last_id"]}, headers=prod(llaves))
    assert ids(r) == ["msgbatch_b", "msgbatch_a"] and r.json()["has_more"] is False
    r = cliente.get(RUTA, params={"limit": 1, "before_id": "msgbatch_b"}, headers=prod(llaves))
    assert ids(r) == ["msgbatch_c"] and r.json()["has_more"] is True


def test_lote_que_anthropic_ya_no_tiene_no_sale_y_la_pagina_se_rellena(cliente, llaves, falso):
    cuatro(cliente, falso)
    anthropic_con(falso, {ORG1: {"msgbatch_a"}, ORG2: {"msgbatch_b"}})
    r = cliente.get(RUTA, params={"limit": 1}, headers=prod(llaves))
    assert ids(r) == ["msgbatch_b"] and (r.json()["first_id"], r.json()["last_id"]) == ("msgbatch_d", "msgbatch_b")


def test_el_tope_de_pagina_de_lotes(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_PAGINA_LOTES", 3)
    monkeypatch.setattr(proxy, "LOTES_A_LA_VEZ", 2)
    cuatro(cliente, falso)
    r = cliente.get(RUTA, params={"limit": 1000}, headers=prod(llaves))
    assert ids(r) == ["msgbatch_d", "msgbatch_c", "msgbatch_b"] and r.json()["has_more"] is True
    assert len(falso.recibidas) == 3


@pytest.mark.parametrize("params,texto", [
    ({"page": "page_x"}, "no admite: page"), ({"limit": "0"}, "limit va de 1 a 1000"),
    ({"after_id": "msgbatch_x"}, "el cursor no es de este proyecto"),
    ({"after_id": "msgbatch_a", "before_id": "msgbatch_b"}, "usa solo uno"),
], ids=["raro", "limit-0", "cursor-ajeno", "dos-cursores"])
def test_parametros_malos_400(cliente, llaves, falso, params, texto):
    cuatro(cliente, falso)
    r = cliente.get(RUTA, params=params, headers=prod(llaves))
    assert r.status_code == 400 and texto in r.json()["error"]["message"] and falso.recibidas == []


def test_errores_de_anthropic_y_de_la_base(cliente, llaves, falso):
    cuatro(cliente, falso)
    limite = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}
    falso.respuestas["*"] = lambda req: httpx.Response(429, json=limite)
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 429 and r.json() == limite
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=["no", "es", "objeto"])
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 502
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=lote("msgbatch_otro"))    # no es el que se pidió
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 502

    def revienta(req):
        raise httpx.ConnectError("x")
    falso.respuestas["*"] = revienta
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 502
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 503


def tipo_de(cliente, id_):
    return objeto_de(cliente.app.state.pia.motor, id_)["tipo"]


def test_beta_true_se_acepta_y_se_ignora(cliente, llaves, falso):
    cuatro(cliente, falso)
    r = cliente.get(RUTA, params={"beta": "true", "limit": 2},
                    headers={**prod(llaves), "anthropic-beta": "files-api-2025-04-14"})
    assert r.status_code == 200 and ids(r) == ["msgbatch_d", "msgbatch_c"]


def test_sin_conexion_libre_da_503(cliente, llaves, falso):
    cuatro(cliente, falso)

    def ocupadas(req):
        raise httpx.PoolTimeout("x")
    falso.respuestas["*"] = ocupadas
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 503 and "conexiones hacia Anthropic están ocupadas" in r.json()["error"]["message"]


@pytest.mark.parametrize("params", [{"after_id": "a\x00b"}, {"before_id": "con espacio"}, {"after_id": "x" * 121}],
                         ids=["nulo", "espacio", "largo"])
def test_cursores_con_forma_rara_son_400_sin_ir_a_la_base(cliente, llaves, falso, params, monkeypatch):
    def a_la_base(*a):
        raise AssertionError("no debe ir a la base")
    monkeypatch.setattr(proxy, "pagina_de_objetos", a_la_base)
    r = cliente.get(RUTA, params=params, headers=prod(llaves))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


# un lote viejo al que Anthropic contesta 404 not_found_error deja de preguntarse.

def test_lote_viejo_con_404_se_marca_caduco_y_ya_no_se_pregunta(cliente, llaves, falso):
    cuatro(cliente, falso)
    anthropic_con(falso, {ORG1: {"msgbatch_a"}, ORG2: {"msgbatch_b"}})            # c y d ya no existen allá
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["msgbatch_b", "msgbatch_a"]
    assert (tipo_de(cliente, "msgbatch_c"), tipo_de(cliente, "msgbatch_d")) == ("caduco", "caduco")
    assert tipo_de(cliente, "msgbatch_a") == "lote"
    falso.recibidas.clear()
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["msgbatch_b", "msgbatch_a"]
    assert {q.url.path.rsplit("/", 1)[-1] for q in falso.recibidas} == {"msgbatch_a", "msgbatch_b"}
    r = cliente.get(RUTA, params={"after_id": "msgbatch_d"}, headers=prod(llaves))     # un caduco sirve de cursor
    assert ids(r) == ["msgbatch_b", "msgbatch_a"]


def test_lote_de_menos_de_una_hora_o_con_otro_404_no_se_marca(cliente, llaves, falso):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(objeto).values(id="msgbatch_nuevo", tipo="lote", fuente="org1", proyecto="crm",
                                        creado=ahora() - timedelta(minutes=10), costo_registrado=False))
    sembrar(cliente, [("msgbatch_raro", "org1", "crm", 1)])
    otro = {"type": "error", "error": {"type": "permission_error", "message": "x"}}
    falso.respuestas["*"] = lambda req: httpx.Response(404, json=NO_ESTA if "nuevo" in req.url.path else otro)
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == []
    assert (tipo_de(cliente, "msgbatch_nuevo"), tipo_de(cliente, "msgbatch_raro")) == ("lote", "lote")


def test_lote_de_otra_cuenta_sin_configurar_no_se_marca(cliente, llaves, falso):
    cuatro(cliente, falso)
    sembrar(cliente, [("msgbatch_v", "vieja", "crm", 9)])
    cliente.get(RUTA, headers=prod(llaves))
    assert tipo_de(cliente, "msgbatch_v") == "lote"
