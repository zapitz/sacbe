"""Reglas de la puerta pública (la API que se publica en internet, por ejemplo ia.example.com).
El proxy inverso pone X-Pia-Puerta: publica (pisa la que mande el cliente). La IP del cliente es el último valor
de X-Forwarded-For, que agrega el proxy; si no viene, X-Real-Ip. Sin X-Pia-Puerta la petición viene de la red
interna y pasa sin tocar.
Por la puerta: solo /anthropic/v1/*, /v1/catalogo, /v1/uso y /salud (que solo dice ok); 20 llaves inválidas desde una IP
en 10 minutos castigan a esa IP 15 minutos con 429. Solo cuenta el 401 que da la propia pasarela (llave pia_
ausente o inválida): ese sale sin x-pia-fuente. Todo lo que contesta Anthropic sale con x-pia-fuente
(proxy.cabeceras_regreso), así que su 401 nunca castiga. La decisión se toma con las cabeceras originales
y solo después, por esta puerta, se quitan x-pia-fuente y anthropic-organization-id de lo que ve el cliente; por la
puerta interna siguen saliendo."""
import json
import sys

from starlette.datastructures import Headers

OCULTAS = {b"x-pia-fuente", b"anthropic-organization-id"}      # no salen por la puerta pública
ABIERTAS = ("/anthropic/v1/",)
EXACTAS = {"/v1/catalogo", "/v1/uso"}   # catálogo de modelos y uso propio del mes (proxy.ver_catalogo, ver_uso)
FALLAS_MAX = 20
VENTANA_S = 600
CASTIGO_S = 900
IPS_MAX = 10_000                      # tope de IPs recordadas: un barrido enorme no llena la memoria


def falla(tipo: str, mensaje: str) -> dict:
    """El mismo sobre que proxy.error(): se repite aquí porque el middleware ASGI no importa proxy (FastAPI y
    Starlette arman ahí respuestas que la puerta no necesita, y así no hay import circular con app)."""
    return {"type": "error", "error": {"type": tipo, "message": f"pasarela-ia: {mensaje}"}}


async def responder(send, status: int, cuerpo: dict) -> None:
    datos = json.dumps(cuerpo).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(datos)).encode())]})
    await send({"type": "http.response.body", "body": datos})


def ip_cliente(h: Headers) -> str:
    """El último valor de X-Forwarded-For lo agrega el proxy inverso con la IP que de verdad se conectó; los anteriores los
    pudo mandar el cliente para evadir el castigo."""
    xff = h.get("x-forwarded-for") or ""
    ip = xff.split(",")[-1] if xff.strip() else (h.get("x-real-ip") or "")
    return ip.strip()[:45] or "sin-ip"


def es_de_la_pasarela(cabeceras) -> bool:
    """True si la respuesta la armó la pasarela y no viene de Anthropic (no trae x-pia-fuente)."""
    return not any(k.lower() == b"x-pia-fuente" for k, _ in cabeceras)


class Puerta:
    def __init__(self, app, estado):
        self.app = app
        self.estado = estado
        self.fallas: dict[str, list[float]] = {}
        self.castigadas: dict[str, float] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = Headers(scope=scope)
        if h.get("x-pia-puerta") != "publica":
            return await self.app(scope, receive, send)
        ip = ip_cliente(h)
        t = self.estado.reloj()
        hasta = self.castigadas.get(ip)
        if hasta is not None:
            if t < hasta:
                aviso = "demasiadas llaves inválidas desde esta IP; espera 15 minutos"
                return await responder(send, 429, falla("rate_limit_error", aviso))
            del self.castigadas[ip]
        ruta = scope["path"]
        if ruta == "/salud":
            ok = bool(self.estado.catalogo.cargado)
            return await responder(send, 200 if ok else 503, {"ok": ok})
        if not (ruta.startswith(ABIERTAS) or ruta in EXACTAS):
            return await responder(send, 404, falla("not_found_error", "ruta no disponible"))
        propia_401 = False

        async def mirar(mensaje):
            nonlocal propia_401
            if mensaje["type"] == "http.response.start":
                cabeceras = mensaje.get("headers") or ()
                propia_401 = mensaje["status"] == 401 and es_de_la_pasarela(cabeceras)
                mensaje = {**mensaje, "headers": [(k, v) for k, v in cabeceras if k.lower() not in OCULTAS]}
            await send(mensaje)

        await self.app(scope, receive, mirar)
        if propia_401:
            self.anotar(ip, t)

    def anotar(self, ip: str, t: float) -> None:
        lista = [x for x in self.fallas.get(ip, ()) if t - x < VENTANA_S] + [t]
        if len(lista) >= FALLAS_MAX:
            self.fallas.pop(ip, None)
            if len(self.castigadas) >= IPS_MAX:
                self.castigadas = {k: v for k, v in self.castigadas.items() if v > t}
            if len(self.castigadas) < IPS_MAX:
                self.castigadas[ip] = t + CASTIGO_S
            print(f"pasarela: IP {ip} castigada 15 minutos por llaves inválidas", file=sys.stderr)
            return
        if ip not in self.fallas and len(self.fallas) >= IPS_MAX:
            self.fallas = {k: v for k, v in self.fallas.items() if t - v[-1] < VENTANA_S}
            if len(self.fallas) >= IPS_MAX:
                self.fallas.clear()       # barrido enorme de IPs: se empieza de cero antes que crecer sin fin
        self.fallas[ip] = lista
