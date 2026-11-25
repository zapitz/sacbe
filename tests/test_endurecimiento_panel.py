"""Endurecimiento del panel: cada prueba demuestra que un hueco conocido está cerrado."""
import json
import re
import threading
import time
from datetime import datetime

import pytest
import httpx
from conftest import ENV
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from test_acciones import (DUENO, FIN, LECTOR, LLAVE, PAR, RESP, app_sacbe, app_interno, base,  # noqa: F401
                           cambios_de, jwt_de, postear, proyecto_fila, ver_llave)
from test_identidad import AUD, CITA, EQUIPO, POR, Certs, Par, Reloj

from pasarela import admin, identidad
from pasarela.app import crear_app
from pasarela.config import cargar
from pasarela.esquema import proyecto, usuario, usuario_proyecto
from pasarela.identidad import VerificadorAccess
from pasarela.tiempo import ahora

NOTA = "Para hacer cambios entra a https://panel.example.com"
REFRESCO = 'http-equiv="refresh"'
MALO = {"accion": "usuario-alta", "correo": "malo@evil.example", "nombre": "x", "papel": "dueno"}


# el panel interno es de solo lectura

def test_dns_rebinding_por_interno_ya_no_escribe(motor, falso, base):
    """DNS rebinding: una página con Host ajeno lee /panel y postea. Ya no hay ficha que leer ni acción que postear."""
    antes = len(admin.cambios(motor, 1000))
    with TestClient(app_interno(motor, falso), base_url="http://evil.example:8090") as c:
        html = c.get("/panel").text
        assert re.search(r'name="ficha"', html) is None
        r = postear(c, {**MALO, "ficha": "0" * 64}, None, origen="http://evil.example:8090")
        assert r.status_code == 404
        r = postear(c, MALO, None, origen="http://evil.example:8090", ficha_de="interno")
        assert r.status_code == 404
    assert admin.usuario_de(motor, "malo@evil.example") is None
    assert len(admin.cambios(motor, 1000)) == antes


def test_interno_no_tiene_rutas_de_escritura(motor, falso, base):
    app = app_interno(motor, falso)
    rutas = {getattr(r, "path", None) for r in app.routes}
    assert "/panel/accion" not in rutas and "/panel/llave/{token}" not in rutas
    with TestClient(app) as c:
        for datos in ({"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"},
                      {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"}):
            assert postear(c, datos, None, ficha_de="interno").status_code == 404
        assert c.get("/panel/llave/" + "a" * 43).status_code == 404
    assert proyecto_fila(motor, "crm").activo is True


def test_interno_ve_todo_con_usuarios_pero_sin_botones(motor, falso, base):
    huella = proyecto_fila(motor, "lotes").huella
    with TestClient(app_interno(motor, falso)) as c:
        r = c.get("/panel")
    t = r.text
    assert r.status_code == 200 and "Panel interno · dueño" in t
    assert NOTA in t and t.index(NOTA) < t.index("Concesiones vigentes")         # arriba
    assert "<form" not in t and 'name="ficha"' not in t and "<button" not in t and "<details" not in t
    assert "Usuarios" in t and RESP in t and "Rosa" in t and LECTOR in t and f"····{huella}" in t
    assert "Nuevo proyecto" not in t
    assert REFRESCO in t                                         # sin formularios, sigue el meta refresh


def test_por_sacbe_no_sale_la_nota_del_panel_interno(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        assert NOTA not in c.get("/panel", headers=jwt_de(DUENO)).text


# el panel con login solo contesta a su Host

def test_sacbe_exige_su_host(motor, falso, base):
    app = app_sacbe(motor, falso, PIA_SACBE_HOST="panel.example.com")
    with TestClient(app) as c:                                                  # Host: testserver
        assert c.get("/panel", headers=jwt_de(DUENO)).status_code == 404
        assert postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"}).status_code == 404
        assert c.get("/panel/llave/" + "a" * 43, headers=jwt_de(DUENO)).status_code == 404
        assert c.get("/salud").status_code in (200, 503)                        # el healthcheck de Docker
    for host in ("http://192.0.2.10:8090", "http://pasarela-ia-sacbe:8080", "https://panel.example.com.evil.mx"):
        with TestClient(app, base_url=host) as c:
            assert c.get("/panel", headers=jwt_de(DUENO)).status_code == 404, host
    assert proyecto_fila(motor, "crm").activo is True
    with TestClient(app, base_url="https://panel.example.com") as c:
        assert c.get("/panel", headers=jwt_de(DUENO)).status_code == 200
        r = postear(c, {"accion": "proyecto-baja", "proyecto": "crm", "confirmo": "si"},
                    origen="https://panel.example.com")
        assert r.status_code == 303
    assert proyecto_fila(motor, "crm").activo is False


def test_sin_host_el_panel_con_login_no_arranca(motor, falso):
    with pytest.raises(ValueError, match="PIA_SACBE_HOST"):
        crear_app(cargar({**ENV, "PIA_SACBE": "1", "PIA_ACCESS_AUD": AUD, "PIA_ACCESS_EQUIPO": EQUIPO}), motor,
                  httpx.MockTransport(falso))


def test_la_nota_del_panel_interno_usa_el_host_configurado(motor, falso, base):
    with TestClient(app_interno(motor, falso, PIA_SACBE_HOST="login.example.org")) as c:
        assert "Para hacer cambios entra a https://login.example.org" in c.get("/panel").text
    falso.respuestas.clear()
    with TestClient(app_interno(motor, falso, PIA_SACBE_HOST="")) as c:
        t = c.get("/panel").text
    assert "Para hacer cambios entra al panel con login." in t and "https://" not in t.split("<main>")[1][:400]


# sin meta refresh cuando la página tiene formularios

@pytest.mark.parametrize("correo,refresco", [(DUENO, False), (RESP, False), (FIN, True), (LECTOR, True)])
def test_meta_refresh_solo_sin_formularios(motor, falso, base, correo, refresco):
    with TestClient(app_sacbe(motor, falso)) as c:
        t = c.get("/panel", headers=jwt_de(correo)).text
    assert ("<form" in t) is not refresco
    assert (REFRESCO in t) is refresco


def test_sin_secreto_no_hay_formularios_y_vuelve_el_refresco(motor, falso, base):
    with TestClient(app_sacbe(motor, falso, secreto="")) as c:
        t = c.get("/panel", headers=jwt_de(DUENO)).text
    assert "<form" not in t and REFRESCO in t


# nombres con fullmatch

def test_nombre_con_salto_de_linea_no_pasa(motor, falso, base):
    """«crm\\n» creaba un proyecto fantasma."""
    with pytest.raises(SystemExit, match="Nombre inválido"):
        admin.alta(motor, "nuevo\n", "extra", POR, CITA)
    with pytest.raises(SystemExit, match="Nombre inválido"):
        admin.cuenta_agregar(motor, "otra\n", "plan@example.com", False, None, POR, CITA)
    with pytest.raises(SystemExit, match="Cuenta inválida"):
        admin.conciliacion_cargar(motor, {"cuentas": {"org1\n": []}}, POR, CITA)
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-alta", "nombre": "crm\n", "clase": "extra"})
    assert r.status_code == 400
    with motor.connect() as k:
        nombres = {f.nombre for f in k.execute(select(proyecto.c.nombre))}
    assert nombres == {"crm", "lotes"}


# nada de caracteres de control hacia la base

def test_nul_y_control_en_el_nombre_desde_el_panel_es_400(motor, falso, base):
    """«a\\x00b\\nc» daba 503 en Postgres. Ahora es un 400 claro y no se guarda nada."""
    antes = len(admin.cambios(motor, 1000))
    with TestClient(app_sacbe(motor, falso)) as c:
        for nombre in ("a\x00b\nc", "a\x1bb", "a\x7fb", "a\u0085b"):
            r = postear(c, {"accion": "usuario-alta", "correo": "a@b.co", "nombre": nombre, "papel": "lector"})
            assert r.status_code == 400 and "caracteres de control" in r.text, repr(nombre)
        r = postear(c, {"accion": "proyecto-rotar", "proyecto": "crm\x00", "confirmo": "si"})
        assert r.status_code == 400
    assert admin.usuario_de(motor, "a@b.co") is None
    assert len(admin.cambios(motor, 1000)) == antes


@pytest.mark.parametrize("malo", ["a\x00b", "a\nb", "a\tb", "a\x1fb", "a\x9bb"])
def test_admin_rechaza_control_en_todo_campo_de_texto(motor, malo):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    casos = [
        lambda: admin.usuario_alta(motor, "a@b.co", malo, "lector", POR, CITA),
        lambda: admin.usuario_alta(motor, f"{malo}@b.co", "Nombre", "lector", POR, CITA),
        lambda: admin.usuario_alta(motor, "a@b.co", "Nombre", "lector", malo, CITA),
        lambda: admin.usuario_alta(motor, "a@b.co", "Nombre", "lector", POR, malo),
        lambda: admin.clase(motor, "crm", "extra", POR, f"«{malo}»"),
        lambda: admin.cuenta_agregar(motor, "otra", f"{malo}@b.co", False, None, POR, CITA),
        lambda: admin.credito_agregar(motor, "org1", 1_000_000, datetime(2026, 11, 1), datetime(2026, 12, 1),
                                      False, malo, POR, CITA),
    ]
    admin.cuenta_agregar(motor, "org1", "plan@example.com", False, None, POR, CITA)
    antes = len(admin.cambios(motor, 1000))
    for caso in casos:
        with pytest.raises(SystemExit, match="caracteres de control|Correo inválido"):
            caso()
    assert len(admin.cambios(motor, 1000)) == antes
    assert admin.usuario_de(motor, "a@b.co") is None


# la baja borra las asignaciones

def test_reactivar_ya_no_devuelve_los_proyectos(motor, base):
    """tras la baja y una nueva alta, el usuario recuperaba «crm» sin que nadie lo asignara."""
    admin.usuario_baja(motor, RESP, POR, CITA)
    with motor.connect() as k:
        assert k.execute(select(usuario_proyecto).where(usuario_proyecto.c.correo == RESP)).all() == []
    (f,) = cambios_de(motor, "usuario-baja")
    assert json.loads(f.detalle) == {"correo": RESP, "proyectos_quitados": ["crm"]}
    admin.usuario_alta(motor, RESP, "Rosa", "lector", POR, CITA)
    assert admin.usuario_de(motor, RESP)["proyectos"] == []


def test_baja_sin_proyectos_lo_anota_vacio(motor, base):
    admin.usuario_baja(motor, FIN, POR, CITA)
    (f,) = cambios_de(motor, "usuario-baja")
    assert json.loads(f.detalle) == {"correo": FIN, "proyectos_quitados": []}


# la carrera de dos altas da un SystemExit claro, no 503

def _colar(monkeypatch, accion_, fila):
    """Inserta la fila que chocará justo después de la consulta previa, como haría otra alta simultánea."""
    real = admin._cambio

    def colado(c, por, cita, accion, detalle):
        real(c, por, cita, accion, detalle)
        if accion == accion_:
            c.execute(fila)
    monkeypatch.setattr(admin, "_cambio", colado)


def test_alta_de_proyecto_en_carrera(motor, monkeypatch):
    _colar(monkeypatch, "proyecto-alta", insert(proyecto).values(
        nombre="crm", clase="extra", llave_hash="h" * 64, huella="hhhh", activo=True, dueno="admin", creado=ahora()))
    with pytest.raises(SystemExit, match="Ya existe el proyecto crm."):
        admin.alta(motor, "crm", "produccion", POR, CITA)
    assert proyecto_fila(motor, "crm") is None and cambios_de(motor, "proyecto-alta") == []


def test_alta_de_usuario_en_carrera(motor, monkeypatch):
    _colar(monkeypatch, "usuario-alta", insert(usuario).values(
        correo="a@b.co", nombre="Otro", papel="lector", activo=True, creado=ahora(), por="x"))
    with pytest.raises(SystemExit, match="El usuario a@b.co ya existe."):
        admin.usuario_alta(motor, "a@b.co", "A", "dueno", POR, CITA)
    assert admin.usuario_de(motor, "a@b.co") is None and cambios_de(motor, "usuario-alta") == []


def test_asignar_en_carrera(motor, monkeypatch):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.usuario_alta(motor, "a@b.co", "A", "lector", POR, CITA)
    _colar(monkeypatch, "usuario-asignar", insert(usuario_proyecto).values(correo="a@b.co", proyecto="crm"))
    with pytest.raises(SystemExit, match="a@b.co ya tiene el proyecto crm."):
        admin.usuario_asignar(motor, "a@b.co", "crm", POR, CITA)
    assert cambios_de(motor, "usuario-asignar") == []


def test_la_carrera_en_el_panel_es_400(motor, falso, base, monkeypatch):
    _colar(monkeypatch, "proyecto-alta", insert(proyecto).values(
        nombre="nuevo", clase="extra", llave_hash="h" * 64, huella="hhhh", activo=True, dueno="admin",
        creado=ahora()))
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-alta", "nombre": "nuevo", "clase": "extra"})
    assert r.status_code == 400 and "Ya existe el proyecto nuevo." in r.text


# la llave se ve tras un 303 y una sola vez

def test_la_llave_vence_a_los_5_minutos(motor, falso, base):
    app = app_sacbe(motor, falso)
    reloj = Reloj()
    app.state.pia.reloj = reloj
    with TestClient(app) as c:
        r = postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"})
        assert r.status_code == 303
        reloj.t += 301
        tarde = c.get(r.headers["location"], headers=jwt_de(DUENO))
        assert tarde.status_code == 404 and not LLAVE.search(tarde.text)
        r = postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"})
        reloj.t += 299
        assert ver_llave(c, r, DUENO).status_code == 200


def test_la_llave_solo_la_ve_quien_la_pidio(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"}, RESP)
        ruta = r.headers["location"]
        for cab in ({}, jwt_de(DUENO), jwt_de(LECTOR)):
            otro = c.get(ruta, headers=cab)
            assert otro.status_code in (403, 404) and not LLAVE.search(otro.text)
        bien = c.get(ruta, headers=jwt_de(RESP))                    # el intento ajeno no la gastó
        assert bien.status_code == 200 and LLAVE.search(bien.text)
        assert bien.headers["cache-control"] == "no-store" and bien.headers["referrer-policy"] == "no-referrer"
        assert c.get(ruta, headers=jwt_de(RESP)).status_code == 404


def test_las_llaves_por_mostrar_no_se_acumulan(motor, falso, base):
    app = app_sacbe(motor, falso)
    reloj = Reloj()
    app.state.pia.reloj = reloj
    with TestClient(app) as c:
        for _ in range(3):
            postear(c, {"accion": "proyecto-rotar", "proyecto": "crm", "confirmo": "si"})
        reloj.t += 301
        postear(c, {"accion": "proyecto-rotar", "proyecto": "lotes", "confirmo": "si"})
    assert len(app.state.pia.llaves_por_ver) == 1


# pedir los certs no deja tomado el cerrojo

def test_una_recarga_lenta_no_frena_a_los_demas():
    par, otra = Par("kid-1"), Par("kid-2")
    entro, soltar = threading.Event(), threading.Event()
    llamadas = []

    def traer():
        llamadas.append(1)
        if len(llamadas) > 1:                       # la recarga por el kid desconocido tarda (Cloudflare lenta)
            entro.set()
            soltar.wait(3)
        return {"keys": [par.jwk(), otra.jwk()]}

    v = VerificadorAccess(EQUIPO, AUD, traer_certs=traer, reloj=Reloj())
    assert v.verificar(par.token()) == "rosa@example.com"
    hilo_a = threading.Thread(target=v.verificar, args=(Par("kid-9").token(),))
    hilo_a.start()
    try:
        assert entro.wait(2)
        resultado = []
        hilo_b = threading.Thread(target=lambda: resultado.append(v.verificar(par.token())))
        t0 = time.monotonic()
        hilo_b.start()
        hilo_b.join(1.0)
        assert resultado == ["rosa@example.com"] and time.monotonic() - t0 < 1.0
    finally:
        soltar.set()
        hilo_a.join(3)
    assert len(llamadas) == 2                       # la petición B no pidió otra recarga mientras A esperaba


# la caché de usuarios tiene tope

def test_la_cache_de_usuarios_tiene_tope(motor, monkeypatch):
    monkeypatch.setattr(identidad, "USUARIOS_MAX", 5)
    identidad.olvidar_usuarios()
    for i in range(40):
        identidad._usuario(motor, f"x{i}@b.co")
    assert len(identidad._usuarios[motor]) <= 5


# /salud de Sacbé no cuenta nada

def test_salud_de_sacbe_solo_dice_ok(motor, falso, base):
    with TestClient(app_sacbe(motor, falso)) as c:
        r = c.get("/salud")
    assert set(r.json()) == {"ok"}


def test_salud_del_gestor_sigue_completa(motor, falso, base):
    with TestClient(app_interno(motor, falso)) as c:
        assert {"ok", "base", "agotadas", "fallas_registro"} <= set(c.get("/salud").json())


# --por no pasa de 80

def test_por_de_mas_de_80_es_systemexit(motor):
    with pytest.raises(SystemExit, match="80"):
        admin.alta(motor, "crm", "produccion", "x" * 81, CITA)
    admin.alta(motor, "crm", "produccion", "x" * 80, CITA)
