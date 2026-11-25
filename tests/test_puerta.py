from datos import MSG, OK, responde

from pasarela import proxy

PUB = {"x-pia-puerta": "publica", "x-real-ip": "203.0.113.7"}
MALA = "pia_nadie_" + "x" * 32


def post(cliente, llave=None, extra=None, json=MSG):
    h = {**(extra if extra is not None else PUB)}
    if llave:
        h["x-api-key"] = llave
    return cliente.post("/anthropic/v1/messages", json=json, headers=h)


def reloj_falso(cliente, inicio=1000.0):
    hora = [inicio]
    cliente.app.state.pia.reloj = lambda: hora[0]
    return hora


def test_salud_publica_solo_dice_ok(cliente):
    r = cliente.get("/salud", headers=PUB)
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_salud_interna_igual_que_antes(cliente):
    assert set(cliente.get("/salud").json()) == {"ok", "base", "proyectos", "agotadas", "fallas_registro"}


def test_rutas_cerradas_por_la_puerta(cliente, llaves):
    for ruta in ("/docs", "/openapi.json", "/otra", "/anthropic/v2/messages", "/v1/catalogo/x", "/anthropic"):
        r = cliente.get(ruta, headers={**PUB, "x-api-key": llaves["prod"]})
        assert r.status_code == 404, ruta


def test_ruta_abierta_con_llave(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    assert post(cliente, llaves["prod"]).status_code == 200


def test_castigo_por_ip(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    hora = reloj_falso(cliente)
    for _ in range(19):
        assert post(cliente, MALA).status_code == 401
    assert post(cliente, llaves["prod"]).status_code == 200            # 19 fallas todavía no castigan
    assert post(cliente, MALA).status_code == 401                      # la 20
    r = post(cliente, llaves["prod"])
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
    assert post(cliente, llaves["prod"], {**PUB, "x-real-ip": "198.51.100.9"}).status_code == 200
    assert post(cliente, llaves["prod"], {}).status_code == 200         # la red interna no se castiga
    hora[0] += 901
    assert post(cliente, llaves["prod"]).status_code == 200


def test_la_ip_es_el_ultimo_salto(cliente, llaves, falso):
    """Lo que el cliente pone al principio de X-Forwarded-For no lo salva del castigo."""
    falso.respuestas["*"] = responde(200, OK)
    reloj_falso(cliente)
    for i in range(20):
        post(cliente, MALA, {**PUB, "x-forwarded-for": f"192.0.2.{i}, 203.0.113.50"})
    assert post(cliente, llaves["prod"], {**PUB, "x-forwarded-for": "198.51.100.1, 203.0.113.50"}).status_code == 429
    assert post(cliente, llaves["prod"], {**PUB, "x-forwarded-for": "203.0.113.51"}).status_code == 200


def test_fallas_viejas_no_cuentan(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    hora = reloj_falso(cliente)
    for _ in range(19):
        post(cliente, MALA)
    hora[0] += 601
    post(cliente, MALA)
    assert post(cliente, llaves["prod"]).status_code == 200


def test_sin_llave_cuenta_como_falla(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    reloj_falso(cliente)
    for _ in range(20):
        assert post(cliente).status_code == 401
    assert post(cliente, llaves["prod"]).status_code == 429


def test_interna_nunca_se_castiga(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    for _ in range(25):
        post(cliente, MALA, {})
    assert post(cliente, llaves["prod"], {}).status_code == 200


def test_cabeceras_de_la_puerta_no_llegan_a_anthropic(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    extra = {**PUB, "x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https",
             "x-forwarded-host": "ia.example.com", "x-forwarded-port": "443", "x-forwarded-server": "t",
             "forwarded": "for=203.0.113.7", "via": "1.1 traefik"}
    assert post(cliente, llaves["prod"], extra).status_code == 200
    enviadas = {k.lower() for k in falso.recibidas[0].headers}
    assert not enviadas & {"x-pia-puerta", "x-real-ip", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
                           "x-forwarded-port", "x-forwarded-server", "forwarded", "via"}


def test_cuerpo_grande_413(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_JSON", 1000)
    r = cliente.post("/anthropic/v1/messages", content=b"x" * 2000,
                     headers={"x-api-key": llaves["prod"], "content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["type"] == "request_too_large"
    assert falso.recibidas == []


def test_cuerpo_grande_sin_content_length(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_JSON", 1000)
    r = cliente.post("/anthropic/v1/messages", content=iter([b"x" * 600, b"x" * 600]),
                     headers={"x-api-key": llaves["prod"], "content-type": "application/json"})
    assert r.status_code == 413 and falso.recibidas == []


def test_archivos_tienen_su_propio_tope(cliente, llaves, falso, monkeypatch):
    monkeypatch.setattr(proxy, "TOPE_JSON", 1000)
    monkeypatch.setattr(proxy, "TOPE_ARCHIVO", 5000)
    falso.respuestas["*"] = responde(200, {"id": "file_9", "type": "file"})
    h = {"x-api-key": llaves["prod"], "content-type": "multipart/form-data; boundary=x"}
    assert cliente.post("/anthropic/v1/files", content=b"x" * 2000, headers=h).status_code == 200
    assert cliente.post("/anthropic/v1/files", content=b"x" * 6000, headers=h).status_code == 413


def test_401_de_anthropic_no_castigan(cliente, llaves, falso):
    """un 401 que contesta Anthropic (trae x-pia-fuente por dentro) no es una llave interna inválida.
    Por la puerta pública esa cabecera ya no sale; el cuerpo de Anthropic sí."""
    falso.respuestas["*"] = responde(401, {"type": "error",
                                           "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    reloj_falso(cliente)
    for _ in range(25):
        r = post(cliente, llaves["prod"])
        assert r.status_code == 401 and r.json()["error"]["message"] == "invalid x-api-key"
    falso.respuestas["*"] = responde(200, OK)
    assert post(cliente, llaves["prod"]).status_code == 200


def test_por_la_puerta_publica_no_salen_la_cuenta_ni_la_organizacion(cliente, llaves, falso):
    """x-pia-fuente y anthropic-organization-id se quedan dentro; por la puerta interna siguen."""
    falso.respuestas["*"] = responde(200, OK, {"anthropic-organization-id": "org-falsa"})
    r = post(cliente, llaves["prod"])
    assert r.status_code == 200
    assert "x-pia-fuente" not in r.headers and "anthropic-organization-id" not in r.headers
    r = post(cliente, llaves["prod"], {})
    assert r.headers["x-pia-fuente"] == "org1" and r.headers["anthropic-organization-id"] == "org-falsa"


def test_401_de_anthropic_por_la_puerta_sale_sin_cabeceras_y_no_castiga(cliente, llaves, falso):
    """Se decide con las cabeceras originales y luego se quitan."""
    falso.respuestas["*"] = responde(
        401, {"type": "error", "error": {"type": "authentication_error", "message": "x"}},
        {"anthropic-organization-id": "org-falsa"})
    reloj_falso(cliente)
    for _ in range(25):
        r = post(cliente, llaves["prod"])
        assert r.status_code == 401 and "x-pia-fuente" not in r.headers
        assert "anthropic-organization-id" not in r.headers
    falso.respuestas["*"] = responde(200, OK)
    assert post(cliente, llaves["prod"]).status_code == 200


def test_x_forwarded_prefix_no_llega_a_anthropic(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    extra = {**PUB, "x-forwarded-prefix": "/interno", "x-forwarded-ssl": "on"}
    assert post(cliente, llaves["prod"], extra).status_code == 200
    assert not [k for k in falso.recibidas[0].headers if k.lower().startswith("x-forwarded-")]
