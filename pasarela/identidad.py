"""Quién está entrando al panel: el panel interno de solo lectura (dueño) o el panel con login (Cloudflare Access +
tabla `usuario`).

Sincrónico a propósito: traer los certs de Cloudflare y leer la base bloquean. El endpoint de FastAPI (async) debe
llamar a `quien_de` desde un hilo aparte, por `proxy.en_base` (ver tests/test_base_lenta.py), no directo.
Nunca se imprime ni se registra un token."""
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Callable, Mapping

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from . import admin

CABECERA = "cf-access-jwt-assertion"
CERTS_VIDA_S = 3600.0          # los certs de Cloudflare se guardan 1 h
RECARGA_MINIMA_S = 10.0        # un kid desconocido recarga una vez, y no más de una vez cada tanto (anti inundación)
USUARIO_VIDA_S = 30.0          # la búsqueda del usuario en la base se guarda 30 s por correo
USUARIOS_MAX = 1000            # tope de correos en esa caché: al llenarse se podan los vencidos, o se vacía
MARGEN_S = 60                  # tolerancia de reloj para exp

_reloj: Callable[[], float] = time.monotonic        # las pruebas lo cambian para no esperar

ACCIONES_RESPONSABLE = frozenset({"proyecto-rotar"})


class BaseNoResponde(Exception):
    """La base falló al buscar al usuario: no se sabe quién es. El panel da 503, no 403."""


@dataclass(frozen=True)
class Quien:
    correo: str
    papel: str                      # dueno · finanzas · responsable · lector
    proyectos: frozenset[str]
    puerta: str                     # sacbe (panel con login) · interno (panel de solo lectura)

    def ve_todo(self) -> bool:
        return self.papel in ("dueno", "finanzas")

    def ve_proyecto(self, nombre: str) -> bool:
        return self.ve_todo() or (self.papel in ("responsable", "lector") and nombre in self.proyectos)

    def puede(self, accion: str, proyecto: str | None = None) -> bool:
        """El dueño todo; el responsable solo rotar la llave de sus proyectos; finanzas y lector, nada."""
        if self.papel == "dueno":
            return True
        if self.papel == "responsable":
            return accion in ACCIONES_RESPONSABLE and proyecto is not None and proyecto in self.proyectos
        return False

    def etiqueta(self) -> str:
        """Lo que queda en la bitácora como «ordenado por»."""
        if self.puerta == "interno":
            return "panel interno (dueño)"
        return f"{self.correo} (Sacbé)"


def _certs_por_omision(equipo: str) -> Callable[[], dict]:
    def traer() -> dict:
        r = httpx.get(f"https://{equipo}/cdn-cgi/access/certs", timeout=5.0)
        r.raise_for_status()
        return r.json()
    return traer


class VerificadorAccess:
    """Verifica el JWT de Cloudflare Access (RS256, aud, iss, exp con 60 s de margen) y devuelve el correo, o None.
    `traer_certs` regresa el JSON de certs (con "keys"); por omisión lo pide por HTTPS con timeout de 5 s."""

    def __init__(self, equipo: str, aud: str, traer_certs: Callable[[], dict] | None = None,
                 reloj: Callable[[], float] | None = None):
        self.equipo = equipo.strip().lower().removeprefix("https://").strip("/")
        self.aud = aud
        self.iss = f"https://{self.equipo}"
        self.traer_certs = traer_certs or _certs_por_omision(self.equipo)
        self._reloj = reloj or time.monotonic
        self._llaves: dict[str, object] = {}
        self._cargadas: float | None = None
        self._recargada: float | None = None          # última recarga forzada por un kid desconocido
        self._cargando = False                         # hay una petición de certs en vuelo (solo una a la vez)
        self._cerrojo = threading.Lock()               # protege el estado; nunca se tiene tomado durante la petición

    def _cargar(self) -> None:
        """Pide los certs SIN el cerrojo (un Cloudflare lento no frena a las demás peticiones) y luego los instala
        con él. Si falla y ya había llaves, se siguen usando y se reintenta en
        RECARGA_MINIMA_S (sin amontonar peticiones de 5 s contra una Cloudflare caída). Sin llaves previas, la falla
        sube y la verificación da None. Quien llama ya marcó `_cargando`; aquí se desmarca siempre."""
        try:
            crudo = self.traer_certs()
        except Exception:
            with self._cerrojo:
                self._cargando = False
                if not self._llaves:
                    raise
                self._cargadas = self._reloj() - CERTS_VIDA_S + RECARGA_MINIMA_S
            return
        llaves = {}
        try:
            for jwk in (crudo.get("keys") if isinstance(crudo, dict) else None) or []:
                try:
                    llaves[jwk["kid"]] = RSAAlgorithm.from_jwk(jwk)
                except Exception:
                    continue
        finally:
            with self._cerrojo:
                self._cargando = False
                self._llaves, self._cargadas = llaves, self._reloj()

    def _llave(self, kid: str):
        """Dos vueltas como máximo: la carga por caducidad y la recarga por un kid desconocido. Si otra petición ya
        está pidiendo los certs, no se espera: se usa lo que haya (las llaves de antes, o ninguna)."""
        for _ in range(2):
            with self._cerrojo:
                ahora = self._reloj()
                if self._cargadas is None or ahora - self._cargadas >= CERTS_VIDA_S:
                    por_kid = False
                else:
                    llave = self._llaves.get(kid)
                    if llave is not None or (self._recargada is not None
                                             and ahora - self._recargada < RECARGA_MINIMA_S):
                        return llave
                    por_kid = True
                if self._cargando:
                    return self._llaves.get(kid)
                self._cargando = True
                if por_kid:
                    self._recargada = ahora
            self._cargar()
            if por_kid:
                with self._cerrojo:
                    return self._llaves.get(kid)
        with self._cerrojo:
            return self._llaves.get(kid)

    def verificar(self, token: str | None) -> str | None:
        try:
            if not token or not isinstance(token, str):
                return None
            cab = jwt.get_unverified_header(token)
            kid = cab.get("kid")
            if cab.get("alg") != "RS256" or not isinstance(kid, str):
                return None
            llave = self._llave(kid)
            if llave is None:
                return None
            datos = jwt.decode(token, llave, algorithms=["RS256"], audience=self.aud, issuer=self.iss,
                               leeway=MARGEN_S, options={"require": ["exp", "iss", "aud"]})
            correo = datos.get("email")
            if not isinstance(correo, str):
                return None
            correo = correo.strip().lower()
            return correo if "@" in correo[1:-1] else None
        except Exception:
            return None


# Caché de 30 s de la búsqueda del usuario: {motor: {correo: (momento, usuario o None)}}
_usuarios: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_cerrojo_usuarios = threading.Lock()


def olvidar_usuarios(motor=None) -> None:
    """Vacía la caché de usuarios (de un motor, o de todos). Se llama tras cambiar usuarios para verlo al instante."""
    with _cerrojo_usuarios:
        if motor is None:
            _usuarios.clear()
        else:
            _usuarios.pop(motor, None)


def _usuario(motor, correo: str) -> dict | None:
    ahora = _reloj()
    with _cerrojo_usuarios:
        hit = _usuarios.get(motor, {}).get(correo)
    if hit is not None and ahora - hit[0] < USUARIO_VIDA_S:
        return hit[1]
    u = admin.usuario_de(motor, correo)
    with _cerrojo_usuarios:
        cache = _usuarios.setdefault(motor, {})
        if correo not in cache and len(cache) >= USUARIOS_MAX:      # cualquiera saca un JWT por cada correo suyo
            for k in [k for k, (t, _) in cache.items() if ahora - t >= USUARIO_VIDA_S]:
                del cache[k]
            if len(cache) >= USUARIOS_MAX:
                cache.clear()
        cache[correo] = (ahora, u)
    return u


def _cabecera(cabeceras, nombre: str) -> str | None:
    try:
        v = cabeceras.get(nombre)
        if v is not None:
            return v
    except Exception:
        pass
    for k, v in cabeceras.items():
        if k.lower() == nombre:
            return v
    return None


def quien_de(cabeceras: Mapping[str, str], motor, config, verificador: VerificadorAccess | None) -> Quien | None:
    """Panel interno (`not config.sacbe`): dueño. Panel con login: JWT de Access + usuario activo en la base; si
    falta algo, None.
    Si la base falla al buscar al usuario sube BaseNoResponde (el panel da 503, no 403).
    Sincrónica: desde un endpoint async, llamarla desde un hilo aparte."""
    if not config.sacbe:
        return Quien("interno", "dueno", frozenset(), "interno")
    if verificador is None:
        return None
    try:
        token = _cabecera(cabeceras, CABECERA)
        correo = verificador.verificar(token) if token else None
    except Exception:
        return None
    if not correo:
        return None
    try:
        u = _usuario(motor, correo)
    except Exception as e:
        raise BaseNoResponde(type(e).__name__) from None      # solo el tipo: nada de la base ni del correo
    if u is None:
        return None
    return Quien(u["correo"], u["papel"], frozenset(u["proyectos"]), "sacbe")
