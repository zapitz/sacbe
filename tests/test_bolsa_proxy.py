"""El proxy con cuentas y concesiones en la base. Las fechas son relativas a hoy: las pruebas no caducan."""
from datetime import timedelta

from datos import MSG, OK, SALDO, responde
from sqlalchemy import insert, select

from pasarela.esquema import aviso, concesion, conciliacion, cuenta, fuente_estado, objeto, registro, tope
from pasarela.tiempo import ahora

M = 1_000_000
CON_ARCHIVO = {**MSG, "messages": [{"role": "user", "content": [
    {"type": "document", "source": {"type": "file", "file_id": "file_1"}}, {"type": "text", "text": "resume"}]}]}


def con_cuentas(cliente, org2_libre=True, conciliada=False):
    estado = cliente.app.state.pia
    t = ahora()
    hoy = t.replace(hour=0, minute=0, second=0, microsecond=0)
    with estado.motor.begin() as c:
        c.execute(insert(cuenta).values(nombre="org1", correo_plan="u@ejemplo.mx", tiene_tarjeta=False,
                                        activa=True, creada=t))
        c.execute(insert(cuenta).values(nombre="org2", correo_plan="r@ejemplo.mx", tiene_tarjeta=True,
                                        activa=True, creada=t))
        if org2_libre:
            c.execute(insert(concesion).values(cuenta="org2", monto_micro=150 * M, otorgada=hoy - timedelta(days=3),
                                               vence=hoy + timedelta(days=20), origen="consola", confirmada=True,
                                               creada=t))
        if conciliada:
            c.execute(insert(conciliacion).values(cuenta="org2", dia=hoy - timedelta(days=1), micro=0,
                                                  recibido=t - timedelta(hours=1)))
    assert estado.bolsa.recalcular(estado.catalogo.agotadas())


def gastado(cliente, proyecto_, usd):
    base = {"ruta": "v1/messages", "modelo": "m", "status": 200, "lote": False, "entrada": 1, "salida": 1,
            "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0, "latencia_ms": 1,
            "request_id": None, "reintentos": 0}
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(registro).values(ts=ahora(), proyecto=proyecto_, fuente="org1",
                                          costo_micro=usd * M, **base))


def poner_tope(cliente, proyecto_, usd, modo):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(tope).values(proyecto=proyecto_, tope_mensual_micro=usd * M, modo=modo,
                                      actualizado=ahora()))


def post(cliente, llave, cuerpo=MSG, ruta="/anthropic/v1/messages"):
    return cliente.post(ruta, json=cuerpo, headers={"x-api-key": llave})


def test_produccion_va_primero_a_la_que_tiene_saldo(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente)                                          # org1 sin concesión: estimado cero
    assert post(cliente, llaves["prod"]).headers["x-pia-fuente"] == "org2"


def test_extra_no_toca_la_tarjeta(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente)
    assert post(cliente, llaves["extra"]).headers["x-pia-fuente"] == "org1"


def test_extra_no_usa_la_tarjeta_aunque_este_conciliada(cliente, llaves, falso):
    """aunque la cuenta con tarjeta esté conciliada y tenga saldo gratis, extra no la usa."""
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente, conciliada=True)
    assert post(cliente, llaves["extra"]).headers["x-pia-fuente"] == "org1"


def test_sin_foto_el_orden_es_el_del_env(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente)
    cliente.app.state.pia.bolsa.foto = None
    assert post(cliente, llaves["prod"]).headers["x-pia-fuente"] == "org1"


def test_tope_en_corte_da_429_sin_llamar(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, {"input_tokens": 3})
    poner_tope(cliente, "lotes", 1, "corte")
    gastado(cliente, "lotes", 2)
    estado = cliente.app.state.pia
    estado.bolsa.recalcular(estado.catalogo.agotadas())
    r = post(cliente, llaves["extra"])
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
    assert "tope mensual del proyecto alcanzado" in r.json()["error"]["message"]
    assert falso.recibidas == []
    assert post(cliente, llaves["extra"], ruta="/anthropic/v1/messages/count_tokens").status_code == 200
    assert post(cliente, llaves["prod"]).status_code == 200      # otro proyecto sigue


def test_tope_en_aviso_no_corta(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    poner_tope(cliente, "lotes", 1, "aviso")
    gastado(cliente, "lotes", 2)
    estado = cliente.app.state.pia
    estado.bolsa.recalcular(estado.catalogo.agotadas())
    assert post(cliente, llaves["extra"]).status_code == 200


def fijar_archivo_a_org2(cliente):
    with cliente.app.state.pia.motor.begin() as c:
        c.execute(insert(objeto).values(id="file_1", tipo="archivo", fuente="org2", proyecto="lotes",
                                        creado=ahora(), costo_registrado=False))


def test_extra_fijado_a_tarjeta_sin_cuadrar_402(cliente, llaves, falso):
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente)
    fijar_archivo_a_org2(cliente)
    r = post(cliente, llaves["extra"], CON_ARCHIVO)
    assert r.status_code == 402 and falso.recibidas == []


def test_extra_fijado_a_tarjeta_cuadrada_tambien_402(cliente, llaves, falso):
    """Conciliada o no, un extra fijado a la tarjeta recibe 402."""
    falso.respuestas["*"] = responde(200, OK)
    con_cuentas(cliente, conciliada=True)
    fijar_archivo_a_org2(cliente)
    r = post(cliente, llaves["extra"], CON_ARCHIVO)
    assert r.status_code == 402 and falso.recibidas == []


def test_cuenta_agotada_deja_aviso(cliente, llaves, falso):
    falso.respuestas["sk-falsa-org1"] = responde(400, SALDO)
    falso.respuestas["sk-falsa-org2"] = responde(200, OK)
    assert post(cliente, llaves["prod"]).headers["x-pia-fuente"] == "org2"
    with cliente.app.state.pia.motor.connect() as c:
        claves = list(c.execute(select(aviso.c.clave_unica)).scalars())
    assert claves == [f"agotada:org1:{ahora():%Y-%m-%d}"]


def textos_de_agotada(cliente):
    with cliente.app.state.pia.motor.connect() as c:
        filas = c.execute(select(aviso.c.clave_unica, aviso.c.texto).where(aviso.c.clave_unica.like("agotada:%")))
        return {k: t for k, t in filas}


def test_aviso_de_agotada_sin_tarjeta_dice_que_sigue_la_siguiente(cliente, llaves, falso):
    """la cuenta sin tarjeta se agota y producción sigue por otra con saldo."""
    falso.respuestas["sk-falsa-org1"] = responde(400, SALDO)
    falso.respuestas["sk-falsa-org2"] = responde(200, OK)
    post(cliente, llaves["prod"])
    texto = textos_de_agotada(cliente)[f"agotada:org1:{ahora():%Y-%m-%d}"]
    assert "Se sigue con la siguiente cuenta con saldo." in texto
    assert "extra" not in texto and "se detiene" not in texto


def test_aviso_de_agotada_con_tarjeta_dice_que_produccion_la_reintenta(cliente, llaves, falso):
    """con la cuenta con tarjeta, producción la reintenta al final y extra nunca la usa."""
    falso.respuestas["*"] = responde(400, SALDO)
    assert post(cliente, llaves["prod"]).status_code == 400
    textos = textos_de_agotada(cliente)
    hoy = f"{ahora():%Y-%m-%d}"
    assert "Producción la reintenta al final; extra nunca la usa." in textos[f"agotada:org2:{hoy}"]
    assert "siguiente cuenta" not in textos[f"agotada:org2:{hoy}"]
    assert "Se sigue con la siguiente cuenta con saldo." in textos[f"agotada:org1:{hoy}"]


def test_marcar_agotada_dos_veces_el_mismo_dia(cliente):
    """la segunda marca del día choca con la clave del aviso y aun así guarda la marca de fuente_estado."""
    cat = cliente.app.state.pia.catalogo
    cat.marcar_agotada("org1", "uno")
    cat.marcar_agotada("org1", "dos")
    with cliente.app.state.pia.motor.connect() as c:
        claves = list(c.execute(select(aviso.c.clave_unica)).scalars())
        motivo = c.execute(select(fuente_estado.c.motivo).where(fuente_estado.c.nombre == "org1")).scalar()
    assert claves == [f"agotada:org1:{ahora():%Y-%m-%d}"]
    assert motivo == "dos" and "org1" in cat.agotadas()


def test_la_bolsa_se_calcula_al_arrancar(cliente):
    assert cliente.app.state.pia.bolsa.foto is not None
    assert cliente.app.state.pia.bolsa.en_uso == "org1"
