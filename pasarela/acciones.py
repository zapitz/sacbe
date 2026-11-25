"""Acciones del panel: lo que el dueño y el responsable cambian desde un formulario.

Cada acción llega como POST /panel/accion con un formulario urlencoded y pasa, en este orden: quién es (Cloudflare
Access; el panel interno es solo de lectura y no tiene esta ruta), que el Origin sea el del panel, el tope del
cuerpo, la ficha del día y el papel. Lo que cambia algo lo hacen las funciones de admin.py, que escriben su fila en
`cambio`.
Aquí no se imprime nada de lo que llega: ni el cuerpo, ni la ficha, ni la llave que regresan crear y rotar.
`ejecutar` y `datos_panel` son sincrónicas (leen o escriben la base): el endpoint las llama por proxy.en_base."""
import hashlib
import hmac
import unicodedata
from datetime import date, timedelta
from typing import Mapping
from urllib.parse import parse_qs

from . import admin

TOPE_CUERPO = 16 * 1024               # bytes; un formulario del panel cabe de sobra
CAMPOS_MAX = 40

NO_ORIGEN = "La acción no salió de esta página del panel. Ábrelo de nuevo y repítela."
SIN_FICHA = "La página del panel ya venció o no deja hacer cambios. Ábrela de nuevo y repite la acción."
SIN_PERMISO = "Tu papel no permite esa acción."
GRANDE = "El formulario es demasiado grande."
DESCONOCIDA = "Acción desconocida."
CONTROL = ("El formulario trae caracteres de control (saltos de línea, tabuladores u otros invisibles). "
           "Escríbelo en una sola línea.")
SIN_CONFIRMO = "Falta marcar la casilla «confirmo»."
APLICA = "aplica en el gestor en menos de un minuto"
UNA_VEZ = "Esta llave aparece una sola vez; cópiala ahora, no se vuelve a mostrar."

# Los campos de cada acción, en el orden en que quedan en la cita de la bitácora (nunca hay secretos en ellos).
CAMPOS = {
    "proyecto-alta": ("nombre", "clase"),
    "proyecto-rotar": ("proyecto",),
    "proyecto-baja": ("proyecto",),
    "proyecto-clase": ("proyecto", "clase"),
    "tope-poner": ("proyecto", "usd", "modo"),
    "tope-quitar": ("proyecto",),
    "usuario-alta": ("correo", "nombre", "papel"),
    "usuario-baja": ("correo",),
    "usuario-papel": ("correo", "papel"),
    "usuario-asignar": ("correo", "proyecto"),
    "usuario-quitar": ("correo", "proyecto"),
}
CON_CONFIRMO = frozenset({"proyecto-rotar", "proyecto-baja", "tope-quitar", "usuario-baja", "usuario-quitar"})
DE_USUARIO = frozenset(a for a in CAMPOS if a.startswith("usuario-"))
DE_PROYECTO = frozenset({"proyecto-alta", "proyecto-rotar", "proyecto-baja", "proyecto-clase", "tope-poner",
                         "tope-quitar"})


class Rechazo(Exception):
    """Una validación que no pasó: el mensaje es para la persona (400), ya en español."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def ficha(secreto: str, correo: str, dia: date) -> str:
    """HMAC-SHA256 del correo y el día (UTC) con PIA_PANEL_SECRETO, en hex. En el panel interno el correo es «interno»."""
    return hmac.new(secreto.encode(), f"{correo}|{dia.isoformat()}".encode(), hashlib.sha256).hexdigest()


def ficha_valida(secreto: str, correo: str, valor, hoy: date) -> bool:
    """Vale la de hoy y la de ayer (UTC). Sin secreto no vale ninguna: el panel solo muestra."""
    if not secreto or not isinstance(valor, str) or not valor.isascii() or not valor:
        return False
    return any(hmac.compare_digest(ficha(secreto, correo, d), valor) for d in (hoy, hoy - timedelta(days=1)))


def origen_valido(cabeceras: Mapping[str, str], host: str) -> bool:
    """Origin igual al host que vio la app (https o http). Sin Origin, Sec-Fetch-Site: same-origin. Sin ninguno, no."""
    origen = cabeceras.get("origin")
    if origen is not None:
        return origen in (f"https://{host}", f"http://{host}")
    return cabeceras.get("sec-fetch-site") == "same-origin"


def leer_campos(cuerpo: bytes) -> dict[str, str]:
    """El formulario urlencoded; de un campo repetido vale el primero. Ningún campo trae caracteres de control."""
    try:
        datos = parse_qs(cuerpo.decode("utf-8"), keep_blank_values=True, max_num_fields=CAMPOS_MAX,
                         errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise Rechazo("El formulario llegó mal.") from None
    campos = {k: v[0] for k, v in datos.items()}
    # Nada de caracteres de control hacia la base ni hacia la cita: el NUL tumba a psycopg.
    if any(unicodedata.category(ch) == "Cc" for k, v in campos.items() for ch in k + v):
        raise Rechazo(CONTROL)
    return campos


def proyecto_de(accion: str, campos: Mapping[str, str]) -> str | None:
    """El proyecto sobre el que se actúa, para `Quien.puede` (el mismo que recibe admin)."""
    return campos.get("proyecto") if "proyecto" in CAMPOS.get(accion, ()) else None


def _limpio(v: str) -> str:
    return " ".join(str(v).split())[:80]


def cita_de(accion: str, campos: Mapping[str, str]) -> str:
    partes = [f"{k}={_limpio(campos.get(k, ''))}" for k in CAMPOS[accion]]
    return f"acción en el panel: {accion} {' '.join(partes)}"


def ejecutar(motor, por: str, accion: str, campos: Mapping[str, str]) -> str | None:
    """Hace la acción con admin.py. Regresa la llave nueva (alta y rotar) o None. Un SystemExit de admin sale como
    Rechazo, que es una excepción normal: no cruza el hilo como BaseException."""
    g = lambda k: campos.get(k, "")          # noqa: E731
    cita = cita_de(accion, campos)
    try:
        if accion == "proyecto-alta":
            return admin.alta(motor, g("nombre"), g("clase"), por, cita)
        if accion == "proyecto-rotar":
            return admin.rotar(motor, g("proyecto"), por, cita)
        if accion == "proyecto-baja":
            admin.baja(motor, g("proyecto"), por, cita)
        elif accion == "proyecto-clase":
            admin.clase(motor, g("proyecto"), g("clase"), por, cita)
        elif accion == "tope-poner":
            limite = admin._micro(g("usd"), "Tope en US$")
            admin.tope_poner(motor, g("proyecto"), limite, g("modo") or None, por, cita)
        elif accion == "tope-quitar":
            admin.tope_quitar(motor, g("proyecto"), por, cita)
        elif accion == "usuario-alta":
            admin.usuario_alta(motor, g("correo"), g("nombre"), g("papel"), por, cita)
        elif accion == "usuario-baja":
            admin.usuario_baja(motor, g("correo"), por, cita)
        elif accion == "usuario-papel":
            admin.usuario_papel(motor, g("correo"), g("papel"), por, cita)
        elif accion == "usuario-asignar":
            admin.usuario_asignar(motor, g("correo"), g("proyecto"), por, cita)
        elif accion == "usuario-quitar":
            admin.usuario_quitar(motor, g("correo"), g("proyecto"), por, cita)
        else:
            raise Rechazo(DESCONOCIDA)
    except SystemExit as e:
        raise Rechazo(str(e.code)) from None
    return None


def datos_panel(motor, con_usuarios: bool) -> tuple[list[dict], list[dict] | None]:
    """Proyectos (con su huella, nunca la llave) y, para el dueño, los usuarios. Lectura."""
    return admin.proyectos(motor), (admin.usuarios(motor) if con_usuarios else None)
