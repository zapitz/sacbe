"""Identidad: quién entra. Sin red: la llave RSA se genera aquí y los certs de Cloudflare son un JWKS falso."""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from pasarela import admin, identidad
from pasarela.config import cargar
from pasarela.identidad import BaseNoResponde, Quien, VerificadorAccess, quien_de

EQUIPO = "tu-equipo.cloudflareaccess.com"
AUD = "aud-de-la-app"
ISS = f"https://{EQUIPO}"
POR = "admin"
CITA = "«prueba»"
BASE = {"PIA_DB_URL": "sqlite://", "PIA_FUENTES": "org1:gratis", "PIA_LLAVE_ORG1": "sk-falsa"}


class Par:
    """Una llave RSA con su kid, que firma tokens y publica su parte pública como JWK."""

    def __init__(self, kid):
        self.kid = kid
        self.privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self):
        d = json.loads(RSAAlgorithm.to_jwk(self.privada.public_key()))
        return {**d, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def token(self, kid=None, **claims):
        base = {"email": "Rosa@Example.com", "aud": [AUD], "iss": ISS, "exp": int(time.time()) + 600,
                "iat": int(time.time())}
        base.update(claims)
        base = {k: v for k, v in base.items() if v is not None}
        return jwt.encode(base, self.privada, algorithm="RS256", headers={"kid": kid or self.kid})


class Certs:
    """traer_certs de mentira: cuenta cuántas veces se llamó y regresa el JWKS que toque."""

    def __init__(self, *pares):
        self.pares = list(pares)
        self.llamadas = 0

    def __call__(self):
        self.llamadas += 1
        return {"keys": [p.jwk() for p in self.pares], "public_cert": {}}


@pytest.fixture(scope="module")
def par():
    return Par("kid-1")


@pytest.fixture(scope="module")
def ajeno():
    return Par("kid-1")          # mismo kid, otra llave: firma ajena


class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ver(certs, reloj=None):
    return VerificadorAccess(EQUIPO, AUD, traer_certs=certs, reloj=reloj or Reloj())


# Verificador

def test_token_bueno_da_el_correo_en_minusculas(par):
    assert _ver(Certs(par)).verificar(par.token()) == "rosa@example.com"


def test_firma_ajena(par, ajeno):
    assert _ver(Certs(par)).verificar(ajeno.token()) is None


def test_aud_equivocado(par):
    v = _ver(Certs(par))
    assert v.verificar(par.token(aud=["otra-app"])) is None
    assert v.verificar(par.token(aud=None)) is None


def test_aud_como_texto_tambien_vale(par):
    assert _ver(Certs(par)).verificar(par.token(aud=AUD)) == "rosa@example.com"


def test_iss_equivocado(par):
    v = _ver(Certs(par))
    assert v.verificar(par.token(iss="https://otro.cloudflareaccess.com")) is None
    assert v.verificar(par.token(iss=None)) is None


def test_vencido(par):
    assert _ver(Certs(par)).verificar(par.token(exp=int(time.time()) - 120)) is None


def test_vencido_hace_poco_pasa_por_el_margen_de_60_s(par):
    assert _ver(Certs(par)).verificar(par.token(exp=int(time.time()) - 30)) == "rosa@example.com"


def test_sin_exp(par):
    assert _ver(Certs(par)).verificar(par.token(exp=None)) is None


@pytest.mark.parametrize("email", [None, "", "   ", 7, "sin-arroba"])
def test_sin_email_valido(par, email):
    assert _ver(Certs(par)).verificar(par.token(email=email)) is None


def test_algoritmo_none_o_hs256_no_pasan(par):
    sin_firma = jwt.encode({"email": "a@b.mx", "aud": AUD, "iss": ISS, "exp": int(time.time()) + 60}, None,
                           algorithm="none", headers={"kid": "kid-1"})
    hs = jwt.encode({"email": "a@b.mx", "aud": AUD, "iss": ISS, "exp": int(time.time()) + 60}, "x" * 40,
                    algorithm="HS256", headers={"kid": "kid-1"})
    v = _ver(Certs(par))
    assert v.verificar(sin_firma) is None and v.verificar(hs) is None


@pytest.mark.parametrize("basura", ["", None, "no-es-jwt", "a.b.c", "a" * 5000])
def test_basura_da_none_sin_excepcion(par, basura):
    assert _ver(Certs(par)).verificar(basura) is None


def test_kid_desconocido_recarga_una_vez(par):
    otra = Par("kid-2")
    certs = Certs(par)
    v = _ver(certs)
    assert v.verificar(par.token()) == "rosa@example.com"
    assert certs.llamadas == 1
    certs.pares.append(otra)                      # Cloudflare rotó: ahora publica las dos
    assert v.verificar(otra.token()) == "rosa@example.com"
    assert certs.llamadas == 2


def test_kid_que_sigue_desconocido_no_recarga_en_bucle(par):
    otra = Par("kid-9")
    certs = Certs(par)
    reloj = Reloj()
    v = _ver(certs, reloj)
    assert v.verificar(otra.token()) is None
    assert certs.llamadas == 2                             # carga inicial + una recarga por el kid desconocido
    antes = certs.llamadas
    for _ in range(5):
        assert v.verificar(otra.token()) is None
    assert certs.llamadas == antes                         # un kid inventado no puede forzar una consulta por petición
    reloj.t += identidad.RECARGA_MINIMA_S + 1
    assert v.verificar(otra.token()) is None
    assert certs.llamadas == antes + 1


def test_los_certs_duran_una_hora(par):
    certs = Certs(par)
    reloj = Reloj()
    v = _ver(certs, reloj)
    v.verificar(par.token())
    v.verificar(par.token())
    assert certs.llamadas == 1
    reloj.t += 3599
    v.verificar(par.token())
    assert certs.llamadas == 1
    reloj.t += 2
    v.verificar(par.token())
    assert certs.llamadas == 2


def test_si_traer_certs_falla_da_none_y_reintenta(par):
    intentos = []

    def traer():
        intentos.append(1)
        if len(intentos) == 1:
            raise RuntimeError("sin red")
        return {"keys": [par.jwk()]}

    v = _ver(traer)
    assert v.verificar(par.token()) is None
    assert v.verificar(par.token()) == "rosa@example.com"


def test_si_cloudflare_cae_con_llaves_ya_cargadas_se_siguen_usando(par):
    reloj = Reloj()
    estado = {"cae": False, "n": 0}

    def traer():
        estado["n"] += 1
        if estado["cae"]:
            raise RuntimeError("sin red")
        return {"keys": [par.jwk()]}

    v = _ver(traer, reloj)
    assert v.verificar(par.token()) == "rosa@example.com"
    estado["cae"] = True
    reloj.t += 3601                                         # caducó la caché y Cloudflare no contesta
    assert v.verificar(par.token()) == "rosa@example.com"
    n = estado["n"]
    assert v.verificar(par.token()) == "rosa@example.com"
    assert estado["n"] == n                                 # no reintenta en cada petición
    reloj.t += identidad.RECARGA_MINIMA_S + 1
    v.verificar(par.token())
    assert estado["n"] == n + 1


def test_certs_con_formato_raro_dan_none(par):
    for raro in ({}, {"keys": "x"}, {"keys": [None, 3]}, [], None):
        assert _ver(lambda raro=raro: raro).verificar(par.token()) is None


def test_el_equipo_se_normaliza():
    v = VerificadorAccess(f"https://{EQUIPO}/", AUD, traer_certs=lambda: {"keys": []})
    assert v.equipo == EQUIPO


def test_los_certs_por_omision_piden_la_url_del_equipo(monkeypatch):
    pedidos = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"keys": []}

    def get(url, timeout):
        pedidos.append((url, timeout))
        return R()

    monkeypatch.setattr(identidad.httpx, "get", get)
    VerificadorAccess(EQUIPO, AUD).traer_certs()
    assert pedidos == [(f"https://{EQUIPO}/cdn-cgi/access/certs", 5.0)]


# Quién

@pytest.mark.parametrize("papel,accion,proyecto,esperado", [
    ("dueno", "proyecto-alta", None, True), ("dueno", "proyecto-rotar", "otro", True),
    ("dueno", "usuario-papel", None, True), ("dueno", "lo-que-sea", "x", True),
    ("finanzas", "proyecto-alta", None, False), ("finanzas", "proyecto-rotar", "crm", False),
    ("finanzas", "tope-poner", "crm", False), ("finanzas", "usuario-alta", None, False),
    ("lector", "proyecto-rotar", "crm", False), ("lector", "tope-quitar", "crm", False),
    ("lector", "usuario-baja", None, False),
    ("responsable", "proyecto-rotar", "crm", True), ("responsable", "proyecto-rotar", "ajeno", False),
    ("responsable", "proyecto-rotar", None, False), ("responsable", "proyecto-baja", "crm", False),
    ("responsable", "proyecto-alta", None, False), ("responsable", "proyecto-clase", "crm", False),
    ("responsable", "tope-poner", "crm", False), ("responsable", "tope-quitar", "crm", False),
    ("responsable", "usuario-asignar", None, False),
])
def test_matriz_de_puede(papel, accion, proyecto, esperado):
    q = Quien("x@y.mx", papel, frozenset({"crm"}), "sacbe")
    assert q.puede(accion, proyecto) is esperado


def test_papel_desconocido_no_puede_nada_ni_ve_nada():
    q = Quien("x@y.mx", "admin", frozenset({"crm"}), "sacbe")
    assert q.puede("proyecto-rotar", "crm") is False and q.puede("proyecto-alta") is False
    assert q.ve_todo() is False and q.ve_proyecto("ajeno") is False


@pytest.mark.parametrize("papel,todo", [("dueno", True), ("finanzas", True), ("responsable", False), ("lector", False)])
def test_ve_todo_y_ve_proyecto(papel, todo):
    q = Quien("x@y.mx", papel, frozenset({"crm"}), "sacbe")
    assert q.ve_todo() is todo
    assert q.ve_proyecto("crm") is True
    assert q.ve_proyecto("ajeno") is todo


def test_etiquetas_de_la_bitacora():
    assert Quien("rosa@example.com", "finanzas", frozenset(), "sacbe").etiqueta() == "rosa@example.com (Sacbé)"
    assert Quien("interno", "dueno", frozenset(), "interno").etiqueta() == "panel interno (dueño)"


def test_quien_es_inmutable():
    with pytest.raises(Exception):
        Quien("a@b.mx", "dueno", frozenset(), "sacbe").papel = "lector"


# quien_de

@pytest.fixture
def entorno(motor, par):
    cfg = cargar({**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": AUD, "PIA_ACCESS_EQUIPO": EQUIPO,
                  "PIA_SACBE_HOST": "panel.example.com"})
    admin.usuario_alta(motor, "dueno@example.com", "Admin", "dueno", POR, CITA)
    admin.usuario_alta(motor, "rosa@example.com", "Rosa", "responsable", POR, CITA)
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.usuario_asignar(motor, "rosa@example.com", "crm", POR, CITA)
    identidad.olvidar_usuarios()
    return cfg, VerificadorAccess(EQUIPO, AUD, traer_certs=Certs(par))


def _cab(token, nombre="Cf-Access-Jwt-Assertion"):
    return {nombre: token}


def test_interno_es_dueno_sin_mirar_nada(motor, config):
    assert config.sacbe is False
    q = quien_de({}, motor, config, None)
    assert q == Quien("interno", "dueno", frozenset(), "interno")


def test_sacbe_arma_quien_con_sus_proyectos(motor, entorno, par):
    cfg, ver = entorno
    q = quien_de(_cab(par.token()), motor, cfg, ver)
    assert q == Quien("rosa@example.com", "responsable", frozenset({"crm"}), "sacbe")
    assert q.puede("proyecto-rotar", "crm") and not q.puede("proyecto-rotar", "otro")


def test_la_cabecera_no_distingue_mayusculas(motor, entorno, par):
    cfg, ver = entorno
    assert quien_de(_cab(par.token(), "cf-access-jwt-assertion"), motor, cfg, ver) is not None
    assert quien_de(_cab(par.token(), "CF-ACCESS-JWT-ASSERTION"), motor, cfg, ver) is not None


def test_acepta_las_cabeceras_de_starlette(motor, entorno, par):
    from starlette.datastructures import Headers
    cfg, ver = entorno
    assert quien_de(Headers({"cf-access-jwt-assertion": par.token()}), motor, cfg, ver) is not None


def test_sin_cabecera_o_con_jwt_malo_da_none(motor, entorno, par, ajeno):
    cfg, ver = entorno
    assert quien_de({}, motor, cfg, ver) is None
    assert quien_de(_cab(""), motor, cfg, ver) is None
    assert quien_de(_cab("basura"), motor, cfg, ver) is None
    assert quien_de(_cab(ajeno.token()), motor, cfg, ver) is None


def test_un_jwt_valido_de_correo_sin_alta_da_none(motor, entorno, par):
    cfg, ver = entorno
    assert quien_de(_cab(par.token(email="intruso@example.net")), motor, cfg, ver) is None


def test_usuario_inactivo_da_none(motor, entorno, par):
    cfg, ver = entorno
    admin.usuario_baja(motor, "rosa@example.com", POR, CITA)
    assert quien_de(_cab(par.token()), motor, cfg, ver) is None


def test_sacbe_sin_verificador_da_none(motor, entorno, par):
    cfg, _ = entorno
    assert quien_de(_cab(par.token()), motor, cfg, None) is None


def test_un_verificador_que_truena_da_none(motor, entorno, par):
    cfg, _ = entorno

    class Roto:
        def verificar(self, token):
            raise RuntimeError("falla")

    assert quien_de(_cab(par.token()), motor, cfg, Roto()) is None


def test_la_busqueda_en_la_base_dura_30_s(motor, entorno, par, monkeypatch):
    cfg, ver = entorno
    reloj = Reloj()
    monkeypatch.setattr(identidad, "_reloj", reloj)
    llamadas = []
    real = admin.usuario_de
    monkeypatch.setattr(admin, "usuario_de", lambda m, c: (llamadas.append(c), real(m, c))[1])
    cab = _cab(par.token())
    assert quien_de(cab, motor, cfg, ver) is not None
    assert quien_de(cab, motor, cfg, ver) is not None
    assert len(llamadas) == 1
    admin.usuario_baja(motor, "rosa@example.com", POR, CITA)
    reloj.t += 29
    assert quien_de(cab, motor, cfg, ver) is not None          # aún en caché
    reloj.t += 2
    assert quien_de(cab, motor, cfg, ver) is None              # ya se enteró de la baja
    assert len(llamadas) == 2


def test_olvidar_usuarios_deja_ver_el_cambio_al_instante(motor, entorno, par):
    cfg, ver = entorno
    cab = _cab(par.token())
    assert quien_de(cab, motor, cfg, ver) is not None
    admin.usuario_baja(motor, "rosa@example.com", POR, CITA)
    identidad.olvidar_usuarios(motor)
    assert quien_de(cab, motor, cfg, ver) is None


def test_si_la_base_falla_no_es_un_no_se_quien_eres(motor, entorno, par, monkeypatch):
    """Una base caída no se confunde con un correo sin acceso: sube BaseNoResponde. Sin JWT o con JWT malo, sigue None
    (ni se toca la base)."""
    cfg, ver = entorno

    def cae(m, c):
        raise RuntimeError("base caída")
    monkeypatch.setattr(admin, "usuario_de", cae)
    with pytest.raises(BaseNoResponde):
        quien_de(_cab(par.token()), motor, cfg, ver)
    assert quien_de({}, motor, cfg, ver) is None
    assert quien_de(_cab("basura"), motor, cfg, ver) is None
