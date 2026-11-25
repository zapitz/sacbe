"""Acciones desde el panel: ficha, origen, papeles, efecto en la base y la llave que se ve una vez."""
import json
import logging
import re
from datetime import date, timedelta

import anyio
import httpx
import pytest
from conftest import ENV
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from test_identidad import AUD, CITA, EQUIPO, POR, Certs, Par

from pasarela import acciones, admin, bolsa, identidad
from pasarela import app as app_modulo
from pasarela.app import crear_app
from pasarela.config import cargar
from pasarela.esquema import cambio, proyecto, registro, tope, usuario, usuario_proyecto
from pasarela.identidad import VerificadorAccess
from pasarela.llaves import hash_llave
from pasarela.tiempo import ahora

SECRETO = "s" * 64
PAR = Par("kid-1")
CAB = "cf-access-jwt-assertion"
ORIGEN = "http://testserver"
SALUD = {"ok": True, "base": True, "proyectos": 2, "agotadas": [], "fallas_registro": 0}
DUENO, RESP, FIN, LECTOR = ("dueno@example.com", "rosa@example.com", "fina@example.com",
                            "lucia@example.com")
LLAVE = re.compile(r"pia_[a-z0-9-]+_[A-Za-z0-9_-]{32}")


def hoy() -> date:
    return ahora().date()


def app_interno(motor, falso, secreto=SECRETO, **env):
    falso.respuestas.setdefault("*", lambda req: httpx.Response(200, json=SALUD))
    extra = {"PIA_PANEL_SECRETO": secreto} if secreto else {}
    env = {"PIA_SACBE_HOST": "panel.example.com", **env}   # la nota del panel interno dice a dónde ir
    return crear_app(cargar({**ENV, "PIA_PANEL": "1", **extra, **env}), motor, httpx.MockTransport(falso))


def app_sacbe(motor, falso, secreto=SECRETO, **env):
    falso.respuestas.setdefault("*", lambda req: httpx.Response(200, json=SALUD))
    extra = {"PIA_PANEL_SECRETO": secreto} if secreto else {}
    env = {"PIA_SACBE_HOST": "testserver", **env}         # el Host que manda TestClient
    app = crear_app(cargar({**ENV, "PIA_SACBE": "1", "PIA_ACCESS_AUD": AUD, "PIA_ACCESS_EQUIPO": EQUIPO, **extra,
                            **env}), motor, httpx.MockTransport(falso))
    app.state.pia.verificador = VerificadorAccess(EQUIPO, AUD, traer_certs=Certs(PAR))
    return app


@pytest.fixture
def base(motor):
    """Un dueño, un responsable de crm, finanzas y un lector de crm; proyectos crm (produccion) y lotes (extra)."""
    admin.usuario_alta(motor, DUENO, "Admin", "dueno", POR, CITA)
    admin.usuario_alta(motor, RESP, "Rosa", "responsable", POR, CITA)
    admin.usuario_alta(motor, FIN, "Fina", "finanzas", POR, CITA)
    admin.usuario_alta(motor, LECTOR, "Lucia", "lector", POR, CITA)
    for n, clase in (("crm", "produccion"), ("lotes", "extra")):
        admin.alta(motor, n, clase, POR, CITA)
    admin.usuario_asignar(motor, RESP, "crm", POR, CITA)
    admin.usuario_asignar(motor, LECTOR, "crm", POR, CITA)
    identidad.olvidar_usuarios()
    return motor


def jwt_de(correo):
    return {CAB: PAR.token(email=correo)}


COMO_CORREO = object()


def postear(c, datos, correo=DUENO, origen=ORIGEN, ficha_de=COMO_CORREO, **cab):
    """POST /panel/accion como lo manda el navegador, por Sacbé y por omisión como el dueño.
    ficha_de: el correo para la ficha (por omisión el mismo de `correo`; None: sin ficha)."""
    datos = dict(datos)
    if ficha_de is COMO_CORREO:
        ficha_de = correo
    if ficha_de is not None and "ficha" not in datos:
        datos["ficha"] = acciones.ficha(SECRETO, ficha_de, hoy())
    h = {"content-type": "application/x-www-form-urlencoded", **cab}
    if origen is not None:
        h["origin"] = origen
    if correo is not None:
        h.update(jwt_de(correo))
    return c.post("/panel/accion", content=httpx.QueryParams(datos).__str__(), headers=h, follow_redirects=False)


LLAVE_RUTA = re.compile(r"/panel/llave/[A-Za-z0-9_-]{40,}")


def ver_llave(c, r, correo):
    """Crear o rotar responde 303 a GET /panel/llave/<token>; se sigue a mano con el JWT de quien la pidió."""
    assert r.status_code == 303, (r.status_code, r.text[:300])
    assert LLAVE_RUTA.fullmatch(r.headers["location"]), r.headers["location"]
    assert not LLAVE.search(r.text) and not LLAVE.search(r.headers["location"])
    return c.get(r.headers["location"], headers=jwt_de(correo))


def cambios_de(motor, accion):
    with motor.connect() as c:
        return c.execute(select(cambio).where(cambio.c.accion == accion).order_by(cambio.c.id)).all()


def proyecto_fila(motor, nombre):
    with motor.connect() as c:
        return c.execute(select(proyecto).where(proyecto.c.nombre == nombre)).first()


# La ficha

def test_ficha_es_hmac_sha256_en_hex():
    import hashlib
    import hmac
    f = acciones.ficha(SECRETO, "a@b.mx", date(2026, 11, 20))
    assert re.fullmatch(r"[0-9a-f]{64}", f)
    assert f == hmac.new(SECRETO.encode(), b"a@b.mx|2026-11-20", hashlib.sha256).hexdigest()
    assert f != acciones.ficha(SECRETO, "a@b.mx", date(2026, 11, 21))
    assert f != acciones.ficha(SECRETO, "otro@b.mx", date(2026, 11, 20))
    assert f != acciones.ficha("otro-secreto", "a@b.mx", date(2026, 11, 20))


def test_ficha_vale_hoy_y_ayer_y_nada_mas():
    d = date(2026, 11, 20)
    for dia, vale in ((d, True), (d - timedelta(days=1), True), (d - timedelta(days=2), False),
                      (d + timedelta(days=1), False)):
        assert acciones.ficha_valida(SECRETO, "a@b.mx", acciones.ficha(SECRETO, "a@b.mx", dia), d) is vale
    assert not acciones.ficha_valida(SECRETO, "otro@b.mx", acciones.ficha(SECRETO, "a@b.mx", d), d)
    for basura in ("", None, "zz", "é" * 64):
        assert not acciones.ficha_valida(SECRETO, "a@b.mx", basura, d)
    assert not acciones.ficha_valida("", "a@b.mx", acciones.ficha("", "a@b.mx", d), d)   # sin secreto, nada vale


# Los rechazos, en orden

def test_sin_quien_es_403(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        for cab in ({}, {CAB: "basura"}, jwt_de("intruso@example.net")):
            r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}, None, ficha_de=DUENO,
                        **cab)
            assert r.status_code == 403 and acciones.NO_ORIGEN not in r.text
    assert proyecto_fila(motor, "crm").activo is True and cambios_de(motor, "proyecto-baja") == []


@pytest.mark.parametrize("origen,sitio", [
    ("https://otro.example", None), ("null", None), ("http://testserver.otro.mx", None), ("ftp://testserver", None),
    (None, "cross-site"), (None, "same-site"), (None, "none"), (None, None)])
def test_origen_ajeno_es_403(motor, falso, base, origen, sitio):
    cab = {"sec-fetch-site": sitio} if sitio else {}
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}, origen=origen, **cab)
    assert r.status_code == 403 and acciones.NO_ORIGEN in r.text
    assert proyecto_fila(motor, "crm").activo is True


@pytest.mark.parametrize("origen,sitio", [("http://testserver", None), ("https://testserver", None),
                                          (None, "same-origin")])
def test_origen_propio_pasa(motor, falso, base, origen, sitio):
    cab = {"sec-fetch-site": sitio} if sitio else {}
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}, origen=origen, **cab)
    assert r.status_code == 303 and r.headers["location"] == "/panel"
    assert proyecto_fila(motor, "crm").activo is False


def test_sin_secreto_no_hay_acciones(motor, falso, base):
    with TestClient(app_sacbe(motor, falso, secreto="")) as c:
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si",
                        "ficha": acciones.ficha("", DUENO, hoy())})
        assert r.status_code == 403 and acciones.SIN_FICHA in r.text
        assert "<form" not in c.get("/panel", headers=jwt_de(DUENO)).text
    assert proyecto_fila(motor, "crm").activo is True


def test_ficha_mala_vieja_o_de_otro_es_403(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        for ficha in ("", "0" * 64, acciones.ficha(SECRETO, DUENO, hoy() - timedelta(days=2)),
                      acciones.ficha(SECRETO, RESP, hoy()), acciones.ficha(SECRETO, "interno", hoy())):
            r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si", "ficha": ficha}, DUENO)
            assert r.status_code == 403 and acciones.SIN_FICHA in r.text
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}, DUENO, ficha_de=None)
        assert r.status_code == 403
        ayer = acciones.ficha(SECRETO, DUENO, hoy() - timedelta(days=1))
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si", "ficha": ayer}, DUENO)
        assert r.status_code == 303


ACCIONES_TODAS = [
    {"accion": "proyecto-alta", "nombre": "nuevo", "clase": "extra"},
    {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"},
    {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"},
    {"accion": "proyecto-clase", "proyecto": "crm", "clase": "extra"},
    {"accion": "tope-poner", "proyecto": "crm", "usd": "50", "modo": "corte"},
    {"accion": "tope-quitar", "proyecto": "crm", "confirmo": "si"},
    {"accion": "usuario-alta", "correo": "nuevo@example.com", "nombre": "Nuevo", "papel": "lector"},
    {"accion": "usuario-baja", "correo": LECTOR, "confirmo": "si"},
    {"accion": "usuario-papel", "correo": LECTOR, "papel": "dueno"},
    {"accion": "usuario-asignar", "correo": LECTOR, "proyecto": "lotes"},
    {"accion": "usuario-quitar", "correo": LECTOR, "proyecto": "crm", "confirmo": "si"},
]


@pytest.mark.parametrize("correo", [FIN, LECTOR])
def test_finanzas_y_lector_no_pueden_nada(motor, falso, base, correo):
    admin.tope_poner(motor, "crm", 10_000_000, "aviso", POR, CITA)
    antes = len(admin.cambios(motor, 1000))
    with TestClient(app_sacbe(motor, falso)) as c:
        for datos in ACCIONES_TODAS:
            r = postear(c, datos, correo, ficha_de=correo)
            assert r.status_code == 403 and acciones.SIN_PERMISO in r.text, datos["accion"]
    assert len(admin.cambios(motor, 1000)) == antes


def test_responsable_solo_rota_lo_suyo(motor, falso, base):
    huella = proyecto_fila(motor, "lotes").huella
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-rotar", "proyecto": "lotes", "confirmo": "si"}, RESP, ficha_de=RESP)
        assert r.status_code == 403 and acciones.SIN_PERMISO in r.text
        assert proyecto_fila(motor, "lotes").huella == huella
        for datos in ACCIONES_TODAS:
            if datos["accion"] != "proyecto-rotar":
                assert postear(c, datos, RESP, ficha_de=RESP).status_code == 403, datos["accion"]
        r = ver_llave(c, postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"}, RESP), RESP)
    assert r.status_code == 200
    llave = LLAVE.search(r.text).group(0)
    assert proyecto_fila(motor, "crm").llave_hash == hash_llave(llave)
    (fila,) = cambios_de(motor, "proyecto-rotar")
    assert fila.ordenado_por == f"{RESP} (Sacbé)"


def test_validacion_de_admin_es_400_escapado_con_enlace(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "tope-poner", "proyecto": "crm", "usd": "<script>x</script>", "modo": ""})
        assert r.status_code == 400
        assert "<script>x" not in r.text and "&lt;script&gt;x" in r.text
        assert 'href="/panel"' in r.text and "volver al panel" in r.text.lower()
        assert r.headers["cache-control"] == "no-store"
        r = postear(c, {"accion": "proyecto-alta", "nombre": "crm", "clase": "extra"})
        assert r.status_code == 400 and "Ya existe el proyecto crm." in r.text
        r = postear(c, {"accion": "usuario-baja", "correo": DUENO, "confirmo": "si"})
        assert r.status_code == 400 and "No se puede dar de baja al último dueño activo." in r.text
        r = postear(c, {"accion": "inventada"})
        assert r.status_code == 400 and "Acción desconocida" in r.text


def test_baja_rotar_y_quitar_piden_confirmo(motor, falso, base):
    admin.tope_poner(motor, "crm", 10_000_000, "aviso", POR, CITA)
    with TestClient(app_sacbe(motor, falso)) as c:
        for datos in ACCIONES_TODAS:
            if "confirmo" in datos:
                sin = {k: v for k, v in datos.items() if k != "confirmo"}
                r = postear(c, sin)
                assert r.status_code == 400 and "confirmo" in r.text, datos["accion"]
    assert proyecto_fila(motor, "crm").activo is True


def test_cuerpo_grande_es_413(motor, falso, base):
    grande = {"accion": "proyecto-alta", "nombre": "x" * 17_000, "clase": "extra"}
    with TestClient(app_sacbe(motor, falso)) as c:
        assert postear(c, grande).status_code == 413

        def trozos():
            yield b"accion=proyecto-alta&nombre="
            for _ in range(20):
                yield b"x" * 1000
        r = c.post("/panel/accion", content=trozos(), headers={"origin": ORIGEN, **jwt_de(DUENO),
                                                               "content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 413
    assert [f for f in cambios_de(motor, "proyecto-alta") if "xxx" in f.cita] == []


def test_base_caida_en_la_accion_es_503(motor, falso, base, monkeypatch):
    def cae(*a, **kw):
        raise RuntimeError("se cayó con secreto-en-el-mensaje")
    monkeypatch.setattr(admin, "baja", cae)
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"})
    assert r.status_code == 503 and "secreto-en-el-mensaje" not in r.text


def test_cuerpo_que_no_es_utf8_es_400(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = c.post("/panel/accion", content=b"accion=\xff\xfe", headers={"origin": ORIGEN, **jwt_de(DUENO)})
    assert r.status_code == 400


# Cada acción hace su efecto y deja su fila en cambio

def test_cada_accion_del_dueno_hace_su_efecto(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        def ok(datos, llave=False):
            r = postear(c, datos, DUENO, ficha_de=DUENO)
            if llave:
                r = ver_llave(c, r, DUENO)
                assert r.status_code == 200, (datos, r.status_code)
                return r
            assert r.status_code == 303 and r.headers["location"] == "/panel", (datos, r.status_code, r.text[:300])
            return r

        r = ok({"accion": "proyecto-alta", "nombre": "nuevo", "clase": "extra"}, llave=True)
        llave = LLAVE.search(r.text).group(0)
        assert proyecto_fila(motor, "nuevo").llave_hash == hash_llave(llave)
        ok({"accion": "proyecto-clase", "proyecto": "nuevo", "clase": "produccion"})
        assert proyecto_fila(motor, "nuevo").clase == "produccion"
        ok({"accion": "tope-poner", "proyecto": "nuevo", "usd": "12.5", "modo": "corte"})
        with motor.connect() as k:
            assert k.execute(select(tope.c.tope_mensual_micro, tope.c.modo)
                             .where(tope.c.proyecto == "nuevo")).first() == (12_500_000, "corte")
        ok({"accion": "tope-poner", "proyecto": "nuevo", "usd": "20", "modo": ""})     # sin modo: el de su clase
        ok({"accion": "tope-quitar", "proyecto": "nuevo", "confirmo": "si"})
        with motor.connect() as k:
            assert k.execute(select(tope).where(tope.c.proyecto == "nuevo")).first() is None
        r = ok({"accion": "proyecto-rotar", "proyecto": "nuevo", "confirmo": "si"}, llave=True)
        otra = LLAVE.search(r.text).group(0)
        assert otra != llave and proyecto_fila(motor, "nuevo").llave_hash == hash_llave(otra)
        ok({"accion": "proyecto-baja", "proyecto": "nuevo", "confirmo": "si"})
        assert proyecto_fila(motor, "nuevo").activo is False
        ok({"accion": "usuario-alta", "correo": "Nuevo@Example.com", "nombre": "Nuevo", "papel": "lector"})
        ok({"accion": "usuario-papel", "correo": "nuevo@example.com", "papel": "responsable"})
        ok({"accion": "usuario-asignar", "correo": "nuevo@example.com", "proyecto": "lotes"})
        assert admin.usuario_de(motor, "nuevo@example.com")["proyectos"] == ["lotes"]
        ok({"accion": "usuario-quitar", "correo": "nuevo@example.com", "proyecto": "lotes", "confirmo": "si"})
        u = admin.usuario_de(motor, "nuevo@example.com")
        assert (u["papel"], u["proyectos"]) == ("responsable", [])
        ok({"accion": "usuario-baja", "correo": "nuevo@example.com", "confirmo": "si"})
        assert admin.usuario_de(motor, "nuevo@example.com") is None
    hechas = {"proyecto-alta", "proyecto-clase", "tope", "tope-quitar", "proyecto-rotar", "proyecto-baja",
              "usuario-alta", "usuario-papel", "usuario-asignar", "usuario-quitar", "usuario-baja"}
    for a in hechas:
        filas = [f for f in cambios_de(motor, a) if f.ordenado_por == f"{DUENO} (Sacbé)"]
        assert filas, a
        for f in filas:
            assert f.cita.startswith("acción en el panel: "), f.cita
            assert "ficha" not in f.cita and "confirmo" not in f.cita
    (alta,) = [f for f in cambios_de(motor, "proyecto-alta") if f.ordenado_por.endswith("(Sacbé)")]
    assert alta.cita == "acción en el panel: proyecto-alta nombre=nuevo clase=extra"


def test_la_llave_se_ve_una_vez_y_no_queda_en_ningun_log(motor, falso, base, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    with TestClient(app_sacbe(motor, falso)) as c:
        r1 = postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"})
        r = ver_llave(c, r1, DUENO)
        otra_vez = c.get(r1.headers["location"], headers=jwt_de(DUENO))       # recargar no la muestra ni rota otra
        alta = ver_llave(c, postear(c, {"accion": "proyecto-alta", "nombre": "nuevo", "clase": "extra"}), DUENO)
        despues = c.get("/panel", headers=jwt_de(DUENO))
    assert otra_vez.status_code == 404 and not LLAVE.search(otra_vez.text)
    assert otra_vez.headers["cache-control"] == "no-store"
    assert len(cambios_de(motor, "proyecto-rotar")) == 1
    llaves = []
    for resp in (r, alta):
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert "aparece una sola vez; cópiala ahora" in resp.text
        assert "aplica en el gestor en menos de un minuto" in resp.text
        assert 'http-equiv="refresh"' not in resp.text and "location" not in resp.headers
        assert 'href="/panel"' in resp.text
        (llave,) = set(LLAVE.findall(resp.text))
        llaves.append(llave)
    salida = capfd.readouterr()
    for llave in llaves:
        assert llave not in caplog.text and llave not in salida.out and llave not in salida.err
        assert llave not in despues.text                                  # el panel solo muestra los últimos 4
        assert llave[-4:] in despues.text
        for f in admin.cambios(motor, 1000):
            assert llave not in f["cita"] and llave not in f["detalle"]


def test_baja_de_usuario_surte_efecto_al_instante(motor, falso, base):
    """La caché de 30 s de usuarios se vacía tras una acción de usuario: la baja no tarda en verse."""
    with TestClient(app_sacbe(motor, falso)) as c:
        assert c.get("/panel", headers=jwt_de(RESP)).status_code == 200
        r = postear(c, {"accion": "usuario-baja", "correo": RESP, "confirmo": "si"}, DUENO, ficha_de=DUENO)
        assert r.status_code == 303
        assert c.get("/panel", headers=jwt_de(RESP)).status_code == 403


# Los formularios en el panel

def test_el_dueno_ve_formularios_huellas_y_usuarios(motor, falso, base):
    huella = proyecto_fila(motor, "lotes").huella
    with TestClient(app_sacbe(motor, falso)) as c:
        r = c.get("/panel", headers=jwt_de(DUENO))
    assert r.status_code == 200
    assert "form-action 'self'" in r.headers["content-security-policy"]
    t = r.text
    assert f'name="ficha" value="{acciones.ficha(SECRETO, DUENO, hoy())}"' in t
    assert 'action="/panel/accion"' in t and 'method="post"' in t
    for accion in ("proyecto-alta", "proyecto-rotar", "proyecto-baja", "proyecto-clase", "tope-poner",
                   "usuario-alta", "usuario-baja", "usuario-papel", "usuario-asignar", "usuario-quitar"):
        assert f'value="{accion}"' in t, accion
    assert f"····{huella}" in t
    assert "Usuarios" in t and RESP in t and "Rosa" in t and LECTOR in t
    assert "aplica en el gestor en menos de un minuto" in t
    assert '<select name="modo">' in t            # con la opción vacía, `required` no dejaría enviar «según su clase»
    # Las peligrosas van en <details> con la casilla obligatoria
    for bloque in re.findall(r"<details.*?</details>", t, re.S):
        if 'value="proyecto-baja"' in bloque or 'value="proyecto-rotar"' in bloque:
            assert 'name="confirmo"' in bloque and "required" in bloque
    assert t.count('name="confirmo"') >= 3 and t.count("<details") >= 3
    assert "<script" not in t


def test_el_responsable_solo_ve_rotar_lo_suyo(motor, falso, base):
    crm = proyecto_fila(motor, "crm").huella
    with TestClient(app_sacbe(motor, falso)) as c:
        t = c.get("/panel", headers=jwt_de(RESP)).text
    assert f"····{crm}" in t and 'value="proyecto-rotar"' in t and 'name="proyecto" value="crm"' in t
    for oculto in ("proyecto-alta", "proyecto-baja", "proyecto-clase", "tope-poner", "usuario-alta", "Usuarios",
                   "lotes", LECTOR):
        assert oculto not in t, oculto


@pytest.mark.parametrize("correo", [FIN, LECTOR])
def test_finanzas_y_lector_no_ven_huellas_ni_formularios(motor, falso, base, correo):
    with TestClient(app_sacbe(motor, falso)) as c:
        t = c.get("/panel", headers=jwt_de(correo)).text
    assert "<form" not in t and "····" not in t


def test_sin_secreto_el_dueno_ve_huellas_pero_no_formularios(motor, falso, base):
    with TestClient(app_sacbe(motor, falso, secreto="")) as c:
        t = c.get("/panel", headers=jwt_de(DUENO)).text
    assert "<form" not in t and f"····{proyecto_fila(motor, 'crm').huella}" in t


def test_si_la_base_no_contesta_los_datos_del_panel_sale_sin_formularios(motor, falso, base, monkeypatch):
    def cae(*a, **kw):
        raise RuntimeError("no")
    monkeypatch.setattr(admin, "proyectos", cae)
    with TestClient(app_sacbe(motor, falso)) as c:
        r = c.get("/panel", headers=jwt_de(DUENO))
    assert r.status_code == 200 and "<form" not in r.text


# El contenedor sacbe: ciclos de fondo

def test_sacbe_calcula_la_foto_pero_no_escribe_avisos_ni_llama_a_anthropic(motor, falso, base, monkeypatch):
    """Con sacbe arranca el ciclo de la foto de la bolsa (lectura) y el refresco de proyectos (lectura), pero no el
    de /v1/models (llama a Anthropic) ni se escriben avisos. El control sin sacbe sí los escribe."""
    escritos = []

    def avisar(m, lista):
        escritos.extend(a.clave for a in lista)
        return {a.clave for a in lista}
    monkeypatch.setattr(bolsa, "avisar", avisar)
    # Un tope rebasado al 100% genera aviso en cada recálculo
    admin.tope_poner(motor, "crm", 1_000_000, "aviso", POR, CITA)
    with motor.begin() as k:
        k.execute(insert(registro).values(
            ts=ahora(), proyecto="crm", fuente="org1", ruta="/v1/messages", modelo="m", status=200, lote=False,
            entrada=1, salida=1, cache_5m=0, cache_1h=0, cache_lectura=0, busquedas=0, costo_micro=2_000_000,
            latencia_ms=1, reintentos=0))
    ciclos = {"PIA_ORDEN_S": "0.05", "PIA_CATALOGO_S": "0.05", "PIA_REFRESCO_S": "0.05"}
    falso.respuestas["*"] = lambda req: httpx.Response(200, json={"data": [], "has_more": False})

    app = app_sacbe(motor, falso, **ciclos)
    assert app.state.pia.bolsa.foto is not None and escritos == []
    with TestClient(app):
        t0 = app.state.pia.bolsa.foto.ts
        anyio.run(anyio.sleep, 0.4)
        assert app.state.pia.bolsa.foto.ts > t0
    assert escritos == [] and falso.recibidas == []

    app = app_interno(motor, falso, **ciclos)
    with TestClient(app):
        anyio.run(anyio.sleep, 0.3)
    assert any(k.startswith("tope:crm") for k in escritos)
    assert any("/v1/models" in str(x.url) for x in falso.recibidas)


def test_bolsa_sin_escribir_avisos(motor, monkeypatch):
    llamadas = []
    monkeypatch.setattr(bolsa, "avisar", lambda m, lista: llamadas.append(lista) or set())
    config = cargar(ENV)
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.tope_poner(motor, "crm", 1_000_000, "aviso", POR, CITA)
    with motor.begin() as k:
        k.execute(insert(registro).values(
            ts=ahora(), proyecto="crm", fuente="org1", ruta="/v1/messages", modelo="m", status=200, lote=False,
            entrada=1, salida=1, cache_5m=0, cache_1h=0, cache_lectura=0, busquedas=0, costo_micro=2_000_000,
            latencia_ms=1, reintentos=0))
    b = bolsa.Bolsa(motor, config, escribe_avisos=False)
    assert b.recalcular(set()) and b.foto is not None and llamadas == []
    b = bolsa.Bolsa(motor, config)
    assert b.recalcular(set()) and llamadas


def test_alta_de_un_proyecto_que_ya_existe_es_systemexit(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    with pytest.raises(SystemExit, match="Ya existe el proyecto crm."):
        admin.alta(motor, "crm", "extra", POR, CITA)
    assert len(cambios_de(motor, "proyecto-alta")) == 1


def test_la_puerta_publica_no_deja_postear_acciones(motor, falso, base):
    """En el contenedor con proxy, lo que llega por la puerta pública (x-pia-puerta: publica) no alcanza el panel."""
    antes = len(admin.cambios(motor, 1000))
    with TestClient(app_interno(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}, **{"x-pia-puerta": "publica"})
    assert r.status_code == 404 and falso.recibidas == []
    assert len(admin.cambios(motor, 1000)) == antes and proyecto_fila(motor, "crm").activo is True
