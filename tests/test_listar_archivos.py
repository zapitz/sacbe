"""GET /anthropic/v1/files: una sola lista paginada con los archivos del proyecto, de todas las cuentas."""
import threading
import time
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
RUTA = "/anthropic/v1/files"


def meta(i):
    return {"id": i, "type": "file", "filename": f"{i}.txt", "mime_type": "text/plain", "size_bytes": 4,
            "created_at": "2026-11-14T12:00:00Z", "downloadable": False, "expires_at": None}


def sembrar(cliente, filas):
    """filas: (id, cuenta, proyecto, segundos después de T0)."""
    with cliente.app.state.pia.motor.begin() as c:
        for i, fuente, proyecto, s in filas:
            c.execute(insert(objeto).values(id=i, tipo="archivo", fuente=fuente, proyecto=proyecto,
                                            creado=T0 + timedelta(seconds=s), costo_registrado=False))


def anthropic_con(falso, tiene: dict[str, set[str]], de_mas=()):
    """Anthropic de mentira: a GET v1/files?ids[]=… contesta los archivos que esa cuenta sí tiene."""
    def responder(req):
        suyos = tiene.get(req.headers["x-api-key"], set())
        datos = [meta(i) for i in req.url.params.get_list("ids[]") if i in suyos] + [meta(i) for i in de_mas]
        return httpx.Response(200, json={"data": datos, "next_page": None}, headers={"request-id": "req_1"})
    falso.respuestas["*"] = responder


def cinco(cliente, falso):
    """crm tiene cinco archivos en dos cuentas (e es el más nuevo, a el más viejo) y lotes tiene uno."""
    sembrar(cliente, [("file_a", "org1", "crm", 1), ("file_b", "org2", "crm", 2), ("file_c", "org1", "crm", 3),
                      ("file_d", "org2", "crm", 4), ("file_e", "org1", "crm", 5), ("file_x", "org1", "lotes", 6)])
    anthropic_con(falso, {ORG1: {"file_a", "file_c", "file_e", "file_x"}, ORG2: {"file_b", "file_d"}})


def prod(llaves, **extra):
    return {"x-api-key": llaves["prod"], "anthropic-version": "2023-06-01", **extra}


def ids(r):
    return [o["id"] for o in r.json()["data"]]


def test_una_sola_lista_con_los_archivos_propios_de_todas_las_cuentas(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert ids(r) == ["file_e", "file_d", "file_c", "file_b", "file_a"]            # del más nuevo al más viejo
    assert r.json()["data"][0] == meta("file_e")                                   # el objeto de Anthropic, entero
    d = r.json()
    assert (d["has_more"], d["next_page"], d["first_id"], d["last_id"]) == (False, None, "file_e", "file_a")
    pedidas = {q.headers["x-api-key"]: q.url.params.get_list("ids[]") for q in falso.recibidas}
    assert pedidas == {ORG2: ["file_d", "file_b"], ORG1: ["file_e", "file_c", "file_a"]}   # una llamada por cuenta
    for q in falso.recibidas:
        assert q.url.path == "/v1/files" and set(q.url.params.keys()) == {"ids[]"}
        assert q.headers["anthropic-version"] == "2023-06-01"
    assert ids(cliente.get(RUTA, headers={"x-api-key": llaves["extra"]})) == ["file_x"]


def test_pagina_por_page_hasta_el_final(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params={"limit": 2}, headers=prod(llaves))
    d = r.json()
    assert ids(r) == ["file_e", "file_d"]
    assert (d["has_more"], d["next_page"], d["first_id"], d["last_id"]) == (True, "page_file_d", "file_e", "file_d")
    todos, pagina = ids(r), d["next_page"]
    while pagina:
        r = cliente.get(RUTA, params={"limit": 2, "page": pagina}, headers=prod(llaves))
        todos += ids(r)
        pagina = r.json()["next_page"]
    assert todos == ["file_e", "file_d", "file_c", "file_b", "file_a"] and r.json()["has_more"] is False


def test_pagina_por_after_id_y_before_id(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params={"limit": 2, "after_id": "file_d"}, headers=prod(llaves))
    assert ids(r) == ["file_c", "file_b"] and r.json()["has_more"] is True and r.json()["last_id"] == "file_b"
    r = cliente.get(RUTA, params={"limit": 2, "before_id": "file_b"}, headers=prod(llaves))
    d = r.json()
    assert ids(r) == ["file_d", "file_c"]                                          # los dos inmediatamente anteriores
    assert (d["has_more"], d["first_id"], d["last_id"], d["next_page"]) == (True, "file_d", "file_c", "page_file_c")
    r = cliente.get(RUTA, params={"before_id": "file_e"}, headers=prod(llaves))
    assert ids(r) == [] and r.json() == {"data": [], "has_more": False, "first_id": None, "last_id": None,
                                         "next_page": None}


def test_lo_que_anthropic_ya_no_tiene_no_sale_y_la_pagina_se_rellena(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a", "file_c"}, ORG2: {"file_b"}})          # e y d ya no existen allá
    r = cliente.get(RUTA, params={"limit": 2}, headers=prod(llaves))
    d = r.json()
    assert ids(r) == ["file_c", "file_b"]
    assert (d["has_more"], d["first_id"], d["last_id"], d["next_page"]) == (True, "file_e", "file_b", "page_file_b")
    assert ids(cliente.get(RUTA, params={"limit": 2, "page": d["next_page"]}, headers=prod(llaves))) == ["file_a"]


def test_el_relleno_sigue_hasta_encontrar_uno_aunque_pase_de_las_rondas(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "RONDAS_LISTA", 2)                                  # nunca página vacía con has_more
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a"}})                                     # solo el más viejo existe
    r = cliente.get(RUTA, params={"limit": 1}, headers=prod(llaves))
    assert r.json() == {"data": [meta("file_a")], "has_more": False, "first_id": "file_e", "last_id": "file_a",
                        "next_page": None}
    assert len(falso.recibidas) == 5


def test_130_archivos_con_los_100_mas_nuevos_muertos_entrega_los_vivos(cliente, llaves, falso):
    sembrar(cliente, [(f"file_{n:04d}", "org1", "crm", n) for n in range(130)])
    anthropic_con(falso, {ORG1: {f"file_{n:04d}" for n in range(30)}})
    r = cliente.get(RUTA, headers=prod(llaves))
    d = r.json()
    assert ids(r) == [f"file_{n:04d}" for n in range(29, 9, -1)] and d["has_more"] is True
    assert (d["first_id"], d["last_id"], d["next_page"]) == ("file_0129", "file_0010", "page_file_0010")
    falso.recibidas.clear()                                                        # los muertos ya están marcados
    r = cliente.get(RUTA, params={"limit": 100}, headers=prod(llaves))
    assert len(ids(r)) == 30 and r.json()["has_more"] is False and len(falso.recibidas) == 1


def test_si_el_tope_de_tiempo_vence_sin_encontrar_nada_es_503(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_LISTA_S", 0)
    cinco(cliente, falso)
    anthropic_con(falso, {})
    r = cliente.get(RUTA, params={"limit": 1}, headers=prod(llaves))
    assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
    assert "saltando archivos que Anthropic ya no tiene" in r.json()["error"]["message"]


def test_registro_vacio_o_acabado_sale_200_aunque_nada_exista(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {})
    r = cliente.get(RUTA, params={"limit": 1}, headers=prod(llaves))
    assert r.status_code == 200 and r.json()["data"] == [] and r.json()["has_more"] is False
    assert ids(cliente.get(RUTA, headers={"x-api-key": llaves["extra"]})) == []


def test_ids_solo_los_propios_en_el_orden_pedido(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params=[("ids[]", i) for i in ("file_b", "file_x", "file_nada", "file_e", "file_b")],
                    headers=prod(llaves))
    assert ids(r) == ["file_b", "file_e"] and r.json()["next_page"] is None and r.json()["has_more"] is False
    assert sorted(i for q in falso.recibidas for i in q.url.params.get_list("ids[]")) == ["file_b", "file_e"]
    falso.recibidas.clear()
    r = cliente.get(RUTA, params={"ids[]": "file_x"}, headers=prod(llaves))        # solo ajenos: ni se pregunta
    assert ids(r) == [] and falso.recibidas == []


@pytest.mark.parametrize("params,texto", [
    ({"scope_id": "x"}, "no admite: scope_id"),
    ({"limit": "0"}, "limit va de 1 a 1000"), ({"limit": "1001"}, "limit va de 1 a 1000"),
    ({"limit": "dos"}, "limit va de 1 a 1000"),
    ({"page": "file_a"}, "page no es un cursor"),
    ({"page": "page_file_x"}, "el cursor no es de este proyecto"),      # es de lotes
    ({"after_id": "file_nada"}, "el cursor no es de este proyecto"),
    ({"after_id": "file_a", "before_id": "file_b"}, "usa solo uno"),
    ([("ids[]", "file_a"), ("limit", "5")], "ids[] no se combina"),
    ([("ids[]", f"file_{n}") for n in range(101)], "hasta 100"),
], ids=["raro", "limit-0", "limit-1001", "limit-texto", "page-sin-prefijo", "page-ajeno", "after-desconocido",
        "dos-cursores", "ids-con-limit", "ids-de-mas"])
def test_parametros_malos_400_sin_preguntar_a_anthropic(cliente, llaves, falso, params, texto):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params=params, headers=prod(llaves))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert texto in r.json()["error"]["message"] and falso.recibidas == []


def test_la_beta_vieja_de_archivos_no_llega_a_anthropic(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, headers=prod(llaves, **{"anthropic-beta": "files-api-2025-04-14, otra-beta-2026-01-01"}))
    assert ids(r)[0] == "file_e" and {"has_more", "first_id", "last_id", "next_page"} <= set(r.json())
    assert {q.headers["anthropic-beta"] for q in falso.recibidas} == {"otra-beta-2026-01-01"}
    falso.recibidas.clear()
    cliente.get(RUTA, headers=prod(llaves, **{"anthropic-beta": "files-api-2025-04-14"}))
    assert all("anthropic-beta" not in q.headers for q in falso.recibidas) and falso.recibidas


def test_lo_que_anthropic_mande_de_mas_no_se_cuela(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a", "file_c", "file_e"}, ORG2: {"file_b", "file_d"}}, de_mas=["file_x"])
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_e", "file_d", "file_c", "file_b", "file_a"]


def test_un_archivo_borrado_por_la_pasarela_ya_no_sale(cliente, llaves, falso):
    cinco(cliente, falso)
    falso.respuestas["*"] = lambda req: httpx.Response(200, json={"id": "file_d", "type": "file_deleted"})
    assert cliente.delete(f"{RUTA}/file_d", headers=prod(llaves)).status_code == 200
    anthropic_con(falso, {ORG1: {"file_a", "file_c", "file_e"}, ORG2: {"file_b"}})
    falso.recibidas.clear()
    r = cliente.get(RUTA, params={"limit": 2, "after_id": "file_d"}, headers=prod(llaves))   # sigue sirviendo de cursor
    assert ids(r) == ["file_c", "file_b"]
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_e", "file_c", "file_b", "file_a"]
    assert "file_d" not in [i for q in falso.recibidas for i in q.url.params.get_list("ids[]")]


def test_cuenta_que_ya_no_esta_configurada_se_salta(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_v", "vieja", "crm", 9)])
    r = cliente.get(RUTA, headers=prod(llaves))
    assert ids(r) == ["file_e", "file_d", "file_c", "file_b", "file_a"] and r.json()["first_id"] == "file_v"


def test_errores_de_anthropic_y_de_la_base(cliente, llaves, falso):
    cinco(cliente, falso)
    limite = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}
    falso.respuestas["*"] = lambda req: httpx.Response(429, json=limite, headers={"retry-after": "3"})
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 429 and r.json() == limite and r.headers["retry-after"] == "3"
    falso.respuestas["*"] = lambda req: httpx.Response(200, json=["no", "es", "objeto"])
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 502

    def revienta(req):
        raise httpx.ConnectError("x")
    falso.respuestas["*"] = revienta
    assert cliente.get(RUTA, headers=prod(llaves)).status_code == 502
    cliente.app.state.pia.motor = create_engine("postgresql+psycopg://nadie@127.0.0.1:1/x")
    n = len(falso.recibidas)
    for params in ({}, {"ids[]": "file_a"}):
        r = cliente.get(RUTA, params=params, headers=prod(llaves))
        assert r.status_code == 503 and r.json()["error"]["type"] == "api_error"
    assert len(falso.recibidas) == n


def test_por_la_puerta_publica_tambien_lista(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params={"limit": 1}, headers=prod(llaves, **{"x-pia-puerta": "publica",
                                                                       "x-real-ip": "203.0.113.7"}))
    assert r.status_code == 200 and ids(r) == ["file_e"] and "x-pia-fuente" not in r.headers


# un id mal formado en ids[] hace que Anthropic conteste 400 a la llamada entera. No tumba la lista.

MAL_ID = {"type": "error", "error": {"type": "invalid_request_error", "message": "ids[]: invalid id `file_malo`"}}


def anthropic_con_id_malo(falso, tiene: dict[str, set[str]], malos=("file_malo",)):
    """Como Anthropic de verdad: si algún id de la llamada está mal formado, 400 a toda la llamada."""
    def responder(req):
        pedidos = req.url.params.get_list("ids[]")
        if any(i in malos for i in pedidos):
            return httpx.Response(400, json=MAL_ID, headers={"request-id": "req_400"})
        suyos = tiene.get(req.headers["x-api-key"], set())
        return httpx.Response(200, json={"data": [meta(i) for i in pedidos if i in suyos], "next_page": None})
    falso.respuestas["*"] = responder


def test_id_mal_formado_se_omite_y_la_lista_sale_con_los_demas(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_malo", "org1", "crm", 6)])                          # el más nuevo, dado de alta a mano
    anthropic_con_id_malo(falso, {ORG1: {"file_a", "file_c", "file_e", "file_x"}, ORG2: {"file_b", "file_d"}})
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 200 and ids(r) == ["file_e", "file_d", "file_c", "file_b", "file_a"]
    d = r.json()
    assert (d["has_more"], d["first_id"], d["last_id"]) == (False, "file_malo", "file_a")   # el cursor lo sigue contando
    org1 = [q.url.params.get_list("ids[]") for q in falso.recibidas if q.headers["x-api-key"] == ORG1]
    assert org1[0] == ["file_malo", "file_e", "file_c", "file_a"]                # primero el tramo entero
    assert sorted(org1[1:]) == [["file_a"], ["file_c"], ["file_e"], ["file_malo"]]   # y luego id por id
    assert len(falso.recibidas) == 1 + 5                                           # org2: una sola llamada
    for q in falso.recibidas:
        assert q.url.path == "/v1/files" and q.headers["anthropic-version"] == "2023-06-01"
    falso.recibidas.clear()
    r = cliente.get(RUTA, params={"limit": 2}, headers=prod(llaves))               # el malo no deja la página corta
    assert ids(r) == ["file_e", "file_d"] and r.json()["has_more"] is True


def test_un_solo_id_mal_formado_se_omite_sin_repetir_la_llamada(cliente, llaves, falso):
    sembrar(cliente, [("file_malo", "org1", "crm", 1)])
    anthropic_con_id_malo(falso, {})
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 200 and r.json()["data"] == [] and r.json()["first_id"] == "file_malo"
    assert len(falso.recibidas) == 1


def test_un_429_en_el_tramo_se_regresa_tal_cual(cliente, llaves, falso):
    cinco(cliente, falso)
    limite = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}
    falso.respuestas["*"] = lambda req: httpx.Response(429, json=limite, headers={"retry-after": "3"})
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 429 and r.json() == limite and r.headers["retry-after"] == "3"
    assert len(falso.recibidas) == 1                                               # no se repite id por id


def test_un_429_al_repetir_id_por_id_tambien_se_regresa(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_malo", "org1", "crm", 6)])
    limite = {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit"}}

    def responder(req):
        if len(req.url.params.get_list("ids[]")) > 1:
            return httpx.Response(400, json=MAL_ID)
        return httpx.Response(429, json=limite, headers={"retry-after": "3"})
    falso.respuestas["*"] = responder
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 429 and r.json() == limite and r.headers["retry-after"] == "3"


# solo un 400 que habla de ids[] manda al camino id por id.

def test_otro_400_se_regresa_tal_cual_con_una_sola_llamada(cliente, llaves, falso):
    cinco(cliente, falso)
    malo = {"type": "error", "error": {"type": "invalid_request_error", "message": "anthropic-version: header is required"}}
    falso.respuestas["*"] = lambda req: httpx.Response(400, json=malo)
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 400 and r.json() == malo and len(falso.recibidas) == 1
    falso.recibidas.clear()
    r = cliente.get(RUTA, params={"ids[]": "file_a"}, headers=prod(llaves))        # un solo id: igual
    assert r.status_code == 400 and r.json() == malo and len(falso.recibidas) == 1


def test_otro_400_al_repetir_id_por_id_tambien_se_regresa(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_malo", "org1", "crm", 6)])
    otro = {"type": "error", "error": {"type": "invalid_request_error", "message": "otra cosa"}}

    def responder(req):
        pedidos = req.url.params.get_list("ids[]")
        if len(pedidos) > 1:
            return httpx.Response(400, json=MAL_ID)
        return httpx.Response(400, json=MAL_ID if pedidos == ["file_malo"] else otro)
    falso.respuestas["*"] = responder
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 400 and r.json() == otro


def test_lo_omitido_por_un_400_de_id_no_se_marca_caduco(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_malo", "org1", "crm", 6)])
    anthropic_con_id_malo(falso, {ORG1: {"file_a", "file_c", "file_e"}, ORG2: {"file_b", "file_d"}})
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_e", "file_d", "file_c", "file_b", "file_a"]
    assert tipo_de(cliente, "file_malo") == "archivo"


# el parámetro beta que mandan los SDK viejos se acepta y se ignora.

def test_beta_true_se_acepta_y_se_ignora(cliente, llaves, falso):
    cinco(cliente, falso)
    r = cliente.get(RUTA, params={"beta": "true", "limit": 2},
                    headers=prod(llaves, **{"anthropic-beta": "files-api-2025-04-14"}))
    assert r.status_code == 200 and ids(r) == ["file_e", "file_d"]
    assert all("beta" not in q.url.params and "anthropic-beta" not in q.headers for q in falso.recibidas)


# lo que Anthropic omite y tiene más de una hora deja de preguntarse.

def tipo_de(cliente, id_):
    return objeto_de(cliente.app.state.pia.motor, id_)["tipo"]


def sembrar_reciente(cliente, filas, minutos):
    with cliente.app.state.pia.motor.begin() as c:
        for i, fuente, proyecto in filas:
            c.execute(insert(objeto).values(id=i, tipo="archivo", fuente=fuente, proyecto=proyecto,
                                            creado=ahora() - timedelta(minutes=minutos), costo_registrado=False))


def test_lo_viejo_que_anthropic_omite_se_marca_caduco_y_ya_no_se_pregunta(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a", "file_c"}, ORG2: {"file_b"}})          # e y d ya no existen allá
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_c", "file_b", "file_a"]
    assert (tipo_de(cliente, "file_e"), tipo_de(cliente, "file_d")) == ("caduco", "caduco")
    assert tipo_de(cliente, "file_c") == "archivo"
    falso.recibidas.clear()
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_c", "file_b", "file_a"]
    preguntados = {i for q in falso.recibidas for i in q.url.params.get_list("ids[]")}
    assert preguntados == {"file_a", "file_b", "file_c"}                           # lo marcado ya no se pregunta


def test_lo_de_menos_de_una_hora_no_se_marca(cliente, llaves, falso):
    sembrar_reciente(cliente, [("file_nuevo", "org1", "crm")], minutos=10)
    sembrar_reciente(cliente, [("file_viejo", "org1", "crm")], minutos=120)
    anthropic_con(falso, {})
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == []
    assert (tipo_de(cliente, "file_nuevo"), tipo_de(cliente, "file_viejo")) == ("archivo", "caduco")


def test_marcar_filtra_por_proyecto_y_no_toca_cuentas_sin_configurar(cliente, llaves, falso):
    cinco(cliente, falso)
    sembrar(cliente, [("file_v", "vieja", "crm", 9)])
    anthropic_con(falso, {})
    cliente.get(RUTA, headers=prod(llaves))
    assert tipo_de(cliente, "file_v") == "archivo" and tipo_de(cliente, "file_x") == "archivo"   # x es de lotes
    assert tipo_de(cliente, "file_a") == "caduco"


def test_un_archivo_caduco_se_porta_como_borrado(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a", "file_c", "file_e"}, ORG2: {"file_b"}})          # d ya no existe
    cliente.get(RUTA, headers=prod(llaves))
    assert tipo_de(cliente, "file_d") == "caduco"
    r = cliente.get(RUTA, params={"limit": 2, "after_id": "file_d"}, headers=prod(llaves))   # sirve de cursor
    assert ids(r) == ["file_c", "file_b"]
    falso.recibidas.clear()
    falso.respuestas["*"] = lambda req: httpx.Response(404, json={"type": "error", "error": {
        "type": "not_found_error", "message": "x"}})
    cliente.app.state.pia.catalogo.marcar_agotada("org1", "prueba")
    assert cliente.get(f"{RUTA}/file_d", headers=prod(llaves)).status_code == 404
    assert falso.recibidas[-1].headers["x-api-key"] == "sk-falsa-org2"                        # d es de org2: sigue fijando
    n = len(falso.recibidas)
    assert cliente.get(f"{RUTA}/file_d", headers={"x-api-key": llaves["extra"]}).status_code == 404
    assert len(falso.recibidas) == n                                                         # y de su proyecto


def test_si_la_base_falla_al_marcar_la_lista_sigue(cliente, llaves, falso, monkeypatch, capsys):
    def revienta(*a):
        raise OSError("sin base")
    monkeypatch.setattr(proxy, "marcar_caducos", revienta)
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a"}})
    assert ids(cliente.get(RUTA, headers=prod(llaves))) == ["file_a"]
    assert tipo_de(cliente, "file_e") == "archivo" and "caduco" in capsys.readouterr().err


def test_ids_tambien_marca_lo_omitido(cliente, llaves, falso):
    cinco(cliente, falso)
    anthropic_con(falso, {ORG1: {"file_a"}})
    assert ids(cliente.get(RUTA, params=[("ids[]", "file_a"), ("ids[]", "file_e")], headers=prod(llaves))) == ["file_a"]
    assert tipo_de(cliente, "file_e") == "caduco"


# sin conexión libre hacia Anthropic, las listas dan el mismo 503 que el resto.

def test_sin_conexion_libre_las_listas_dan_503(cliente, llaves, falso):
    cinco(cliente, falso)

    def ocupadas(req):
        raise httpx.PoolTimeout("x")
    falso.respuestas["*"] = ocupadas
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 503 and "conexiones hacia Anthropic están ocupadas" in r.json()["error"]["message"]


# las lecturas y escrituras a la base de la lista llevan el tope de ESPERA_FIJADA_S.

def test_base_congelada_no_cuelga_la_lista_ni_el_borrado(cliente, llaves, falso, monkeypatch):
    cinco(cliente, falso)
    monkeypatch.setattr(proxy, "ESPERA_FIJADA_S", 0.2)
    soltar = threading.Event()
    for nombre in ("pagina_de_objetos", "objetos_entre", "marcar_borrado"):
        monkeypatch.setattr(proxy, nombre, lambda *a: soltar.wait(3))
    try:
        t0 = time.monotonic()
        assert cliente.get(RUTA, headers=prod(llaves)).status_code == 503
        assert cliente.get(RUTA, params={"ids[]": "file_a"}, headers=prod(llaves)).status_code == 503
        falso.respuestas["*"] = lambda req: httpx.Response(200, json={"id": "file_a", "type": "file_deleted"})
        assert cliente.delete(f"{RUTA}/file_a", headers=prod(llaves)).status_code == 200
        assert time.monotonic() - t0 < 2
    finally:
        soltar.set()


# la forma de los cursores y de ids[] se valida antes de ir a la base.

@pytest.mark.parametrize("params", [
    {"after_id": "a\x00b"}, {"before_id": "con espacio"}, {"after_id": "x" * 121}, {"page": "page_"},
    {"page": "page_a\x00b"}, {"page": "page_" + "x" * 121}, {"ids[]": "a\x00b"}, {"ids[]": ""},
], ids=["after-nulo", "before-espacio", "after-largo", "page-vacio", "page-nulo", "page-largo", "ids-nulo", "ids-vacio"])
def test_cursores_e_ids_con_forma_rara_son_400(cliente, llaves, falso, params, monkeypatch):
    def a_la_base(*a):
        raise AssertionError("no debe ir a la base")
    monkeypatch.setattr(proxy, "pagina_de_objetos", a_la_base)       # en Postgres un byte nulo da DataError
    monkeypatch.setattr(proxy, "objetos_entre", a_la_base)
    cinco(cliente, falso)
    r = cliente.get(RUTA, params=params, headers=prod(llaves))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error" and falso.recibidas == []


def test_before_id_con_huecos_da_varias_vueltas_hacia_los_mas_nuevos(cliente, llaves, falso, monkeypatch):
    """Del más nuevo al más viejo: h g f e d c b a. Anthropic solo conserva a, b y g. Con before_id=b y limit=2 se
    recorre hacia los más nuevos en tres vueltas ([d,c], [f,e], [h,g]) hasta encontrar a g. Las filas son recientes:
    no se marcan caducas, así que lo que avanza es el cursor y no el marcado."""
    monkeypatch.setattr(proxy, "TOPE_LISTA_S", 1)                  # si el cursor no avanza, falla pronto
    sembrar_reciente(cliente, [(f"file_{x}", "org1", "crm") for x in "abcdefgh"], 5)
    anthropic_con(falso, {ORG1: {"file_a", "file_b", "file_g"}})
    r = cliente.get(RUTA, params={"limit": 2, "before_id": "file_b"}, headers=prod(llaves))
    assert r.status_code == 200 and ids(r) == ["file_g"]
    assert [sorted(q.url.params.get_list("ids[]")) for q in falso.recibidas] == [
        ["file_c", "file_d"], ["file_e", "file_f"], ["file_g", "file_h"]]
    d = r.json()
    assert (d["first_id"], d["last_id"], d["has_more"]) == ("file_h", "file_c", False)
    assert tipo_de(cliente, "file_d") == "archivo"                 # recientes: sin marcar
    r = cliente.get(RUTA, params={"limit": 2, "before_id": "file_b"}, headers=prod(llaves))   # otra vez, igual
    assert ids(r) == ["file_g"] and len(falso.recibidas) == 6


def test_un_400_de_ids_que_no_es_por_id_mal_formado_se_regresa(cliente, llaves, falso):
    """N3: «ids[]: unknown field» (la beta vieja) también empieza con ids[], pero no es un id malo."""
    cinco(cliente, falso)
    otro = {"type": "error", "error": {"type": "invalid_request_error", "message": "ids[]: unknown field"}}
    falso.respuestas["*"] = lambda req: httpx.Response(400, json=otro)
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 400 and r.json() == otro and len(falso.recibidas) == 1


@pytest.mark.parametrize("resto", [{"next_page": "page_x"}, {"next_page": None, "has_more": True}])
def test_una_respuesta_incompleta_de_anthropic_no_marca_caducos(cliente, llaves, falso, resto):
    """N7: si la respuesta a ids[] dice que hay más páginas, que falte un id no prueba que ya no exista."""
    sembrar(cliente, [("file_a", "org1", "crm", 1), ("file_b", "org1", "crm", 2)])
    falso.respuestas["*"] = lambda req: httpx.Response(200, json={"data": [meta("file_b")], **resto})
    r = cliente.get(RUTA, headers=prod(llaves))
    assert r.status_code == 200 and ids(r) == ["file_b"]
    assert tipo_de(cliente, "file_a") == "archivo"
