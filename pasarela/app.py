"""Arma la app. En producción: uvicorn pasarela.app:crear_app --factory"""
import secrets
import sys
import time
from contextlib import asynccontextmanager
from html import escape

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import acciones, costo, modelos, panel, proxy
from .almacen import probar_base
from .bolsa import Bolsa, al_dia
from .catalogo import Catalogo
from .config import Config, cargar
from .esquema import HILOS_ESCRITURA, HILOS_LECTURA, crear_motor
from .identidad import BaseNoResponde, VerificadorAccess, olvidar_usuarios, quien_de
from .orden import SinCredito
from .puerta import Puerta
from .tiempo import NOMBRE_ZONA, ahora, en_local


class Estado:
    def __init__(self, config: Config, motor, cliente: httpx.AsyncClient):
        self.config = config
        self.motor = motor
        self.cliente = cliente
        self.llaves = {f.nombre: f.llave for f in config.fuentes}
        self.catalogo = Catalogo(motor, config.agotada_s)
        # saldos, topes y orden; se recalcula cada config.orden_s. En Sacbé calcula la foto pero no escribe avisos.
        self.bolsa = Bolsa(motor, config, escribe_avisos=not config.sacbe)
        self.modelos = modelos.Modelos()            # catálogo y alias; se relee cada config.catalogo_s
        self.fallas_registro = 0
        self.hilos_lectura = anyio.CapacityLimiter(HILOS_LECTURA)       # lo que una petición o el refresco esperan
        self.hilos_escritura = anyio.CapacityLimiter(HILOS_ESCRITURA)   # filas escritas ya enviada la respuesta
        self.tareas = None                # grupo de tareas de la vida de la app: ahí se escriben los streams
        self.pendientes = 0               # escrituras de fondo, de las dos vías, que todavía no terminan
        self.subiendo = 0                 # bytes de subidas (POST v1/files) apartados del cupo en este momento
        self.aviso_descarte = None        # hora (monotonic) del último aviso de fila descartada por el tope
        self.reloj = time.monotonic       # la puerta pública cuenta fallas con este reloj (las pruebas lo cambian)
        # Cloudflare Access: solo en el contenedor de Sacbé; se crea una vez y trae los certs la primera vez que se usa
        self.verificador = VerificadorAccess(config.access_equipo, config.access_aud) if config.sacbe else None
        # Llaves recién creadas o rotadas que esperan su GET /panel/llave/<token>: {token: Pendiente}. Solo en
        # memoria de este proceso (uvicorn corre con un solo worker), de un solo uso y por LLAVE_VIDA_S.
        self.llaves_por_ver: dict[str, "Pendiente"] = {}


class Pendiente:
    """Una llave por mostrar: a quién, de qué proyecto, y hasta cuándo (en el reloj de `Estado.reloj`)."""
    __slots__ = ("vence", "correo", "proyecto", "llave", "accion")

    def __init__(self, vence: float, correo: str, proyecto: str, llave: str, accion: str):
        self.vence, self.correo, self.proyecto, self.llave, self.accion = vence, correo, proyecto, llave, accion


CONEXIONES_MAX = 200                  # peticiones hacia Anthropic en curso a la vez, entre todos los programas
CONEXIONES_EN_ESPERA = 20             # conexiones abiertas sin uso que se guardan para la siguiente petición
VIDA_EN_ESPERA_S = 5.0                # una conexión sin uso se cierra a los 5 s
ESPERA_CONEXION_S = 10.0              # con las CONEXIONES_MAX ocupadas, una petición espera esto y luego recibe 503
ESPERA_SALUD_S = 3                    # tope del SELECT 1 de /salud
REINTENTO_MODELOS_S = 300             # si no se pudo leer /v1/models, se reintenta a los 5 minutos
ESPERA_QUIEN_S = 8                    # tope de saber quién entra (certs de Cloudflare, hasta 5 s, más la base)
ESPERA_SONDA_S = 4                    # tope total de la sonda del panel; queda arriba de ESPERA_SALUD_S
LLAVE_VIDA_S = 300.0                  # la llave nueva se puede ver una vez, en los 5 minutos que siguen al cambio
CABECERAS_PANEL = {
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}
SIN_ACCESO = "Tu correo no tiene acceso. Pídeselo al administrador."
LLAVE_YA_NO = "Esa llave ya se mostró o venció. Si no la copiaste, rota la llave otra vez."
BASE_CAIDA = "La base no responde, reintenta en un minuto."


def pagina_corta(texto: str, status: int, volver: bool = False) -> HTMLResponse:
    """Una página mínima para el 403, el 503 y los rechazos de las acciones. `texto` va tal cual: quien lo arma con
    algo que vino de afuera lo pasa por escape() antes."""
    enlace = '<p><a href="/panel">Volver al panel</a></p>\n' if volver else ""
    cuerpo = (f'<!doctype html>\n<html lang="es-MX">\n<head>\n<meta charset="utf-8">\n'
              f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
              f'<meta name="robots" content="noindex">\n<title>{panel.TITULO}</title>\n'
              f'<style>:root{{color-scheme:light dark}}body{{font:16px/1.45 -apple-system,BlinkMacSystemFont,'
              f'"Segoe UI",Roboto,sans-serif;margin:0;padding:48px 16px;text-align:center}}</style>\n</head>\n<body>\n'
              f'<h1>{panel.TITULO}</h1>\n<p>{texto}</p>\n{enlace}</body>\n</html>\n')
    return HTMLResponse(cuerpo, status_code=status, headers=CABECERAS_PANEL)


async def base_responde(estado: Estado) -> bool:
    """False si la base tarda más de ESPERA_SALUD_S o falla: /salud lo dice en el cuerpo y no truena."""
    try:
        with anyio.fail_after(ESPERA_SALUD_S):
            await proxy.en_base(estado.hilos_lectura, probar_base, estado.motor, abandonar=True)
        return True
    except Exception:  # noqa: BLE001
        return False


async def refrescar_siempre(estado: Estado, cada: float, alcance: anyio.CancelScope):
    with alcance:
        while True:
            await anyio.sleep(cada)
            await proxy.en_base(estado.hilos_lectura, estado.catalogo.refrescar)


async def recalcular_siempre(estado: Estado, cada: float, alcance: anyio.CancelScope):
    with alcance:
        while True:
            await anyio.sleep(cada)
            await proxy.en_base(estado.hilos_lectura, estado.bolsa.recalcular, estado.catalogo.agotadas())


async def sondear_api(estado: Estado) -> dict | None:
    """El /salud de pasarela-ia-api visto desde el panel, con tope total de ESPERA_SONDA_S (no los 600 s del cliente;
    el timeout de httpx es por fase, así que el tope total lo pone fail_after)."""
    try:
        with anyio.fail_after(ESPERA_SONDA_S):
            r = await estado.cliente.get(estado.config.salud_api_url)
            d = r.json()
    except Exception:  # noqa: BLE001  cualquier falla, también pasarse del tope, es «no contesta»
        return None
    return d if isinstance(d, dict) else None


def cargar_modelos_guardados(estado: Estado) -> None:
    """Al arrancar: la última lista guardada sirve aunque Anthropic no conteste (los alias no dan 503)."""
    try:
        vistos, ts = modelos.cargar_modelos(estado.motor)
        if vistos:
            estado.modelos.poner(modelos.armar(vistos, ts))
    except Exception as e:  # noqa: BLE001  sin base o con una fila rara, el catálogo llega con el primer refresco
        print(f"pasarela: no pude leer el catálogo guardado ({type(e).__name__})", file=sys.stderr)


async def refrescar_modelos(estado: Estado) -> bool:
    """Lee /v1/models con la primera cuenta que conteste, en el orden de producción y luego las demás. Una lista
    vacía o rara (también la que no trae ningún modelo con precio) no reemplaza la anterior.
    Nunca lanza: el ciclo de la app no debe morir por esto."""
    try:
        try:
            orden = [c.fuente for c in estado.bolsa.ordenar("produccion", estado.catalogo.agotadas())]
        except SinCredito:
            orden = []
        for fuente in orden + [n for n in estado.llaves if n not in orden]:
            try:
                vistos = await modelos.traer(estado.cliente, estado.config.anthropic_url, estado.llaves[fuente])
            except Exception as e:  # noqa: BLE001  se prueba con la siguiente cuenta
                # Solo el texto de ErrorModelos, que arma la pasarela sin la llave. El de httpx o h11 puede traer
                # la llave entera (una llave con \r o \n da «Illegal header value b'sk-...'»): solo el tipo.
                detalle = f"{type(e).__name__}: {e}" if isinstance(e, modelos.ErrorModelos) else type(e).__name__
                print(f"pasarela: no pude leer los modelos con la cuenta {fuente} ({detalle})"[:300], file=sys.stderr)
                continue
            t = ahora()
            lista = modelos.armar(vistos, t)
            if not lista.modelos:
                print(f"pasarela: /v1/models de la cuenta {fuente} no trajo ningún modelo con precio; "
                      f"se queda el catálogo anterior", file=sys.stderr)
                continue
            try:
                await proxy.en_base(estado.hilos_lectura, modelos.guardar_modelos, estado.motor, vistos, lista, t)
            except Exception as e:  # noqa: BLE001  el catálogo en memoria sirve igual; se guarda en la siguiente
                print(f"pasarela: no pude guardar el catálogo de modelos ({type(e).__name__})", file=sys.stderr)
            estado.modelos.poner(lista)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        print(f"pasarela: falló el refresco de modelos ({type(e).__name__})", file=sys.stderr)
        return False


async def modelos_guardados_siempre(estado: Estado, cada: float, alcance: anyio.CancelScope):
    """Sacbé no llama a Anthropic: relee de la base el catálogo que guardó pasarela-ia-api (solo lectura)."""
    with alcance:
        while True:
            await anyio.sleep(cada)
            await proxy.en_base(estado.hilos_lectura, cargar_modelos_guardados, estado)


async def modelos_siempre(estado: Estado, cada: float, alcance: anyio.CancelScope):
    """Lee los modelos al arrancar y luego cada `cada` segundos; si falla, reintenta pronto."""
    with alcance:
        while True:
            bien = await refrescar_modelos(estado)
            await anyio.sleep(cada if bien else min(cada, REINTENTO_MODELOS_S))


def crear_app(config: Config | None = None, motor=None,
              transporte: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    config = config or cargar()
    motor = motor if motor is not None else crear_motor(config.db_url)
    cliente = httpx.AsyncClient(
        transport=transporte,
        limits=httpx.Limits(max_connections=CONEXIONES_MAX, max_keepalive_connections=CONEXIONES_EN_ESPERA,
                            keepalive_expiry=VIDA_EN_ESPERA_S),
        timeout=httpx.Timeout(600.0, connect=10.0, pool=ESPERA_CONEXION_S))
    estado = Estado(config, motor, cliente)
    estado.catalogo.refrescar()
    estado.bolsa.recalcular(estado.catalogo.agotadas())
    cargar_modelos_guardados(estado)

    @asynccontextmanager
    async def vida(app: FastAPI):
        refresco, recalculo, catalogo = anyio.CancelScope(), anyio.CancelScope(), anyio.CancelScope()  # uno por ciclo
        async with anyio.create_task_group() as tg:
            if config.refresco_s > 0:
                tg.start_soon(refrescar_siempre, estado, config.refresco_s, refresco)
            if config.orden_s > 0:
                tg.start_soon(recalcular_siempre, estado, config.orden_s, recalculo)
            if config.catalogo_s > 0:     # Sacbé no lleva llaves a Anthropic: solo relee lo guardado
                tg.start_soon(modelos_guardados_siempre if config.sacbe else modelos_siempre, estado,
                              config.catalogo_s, catalogo)
            estado.tareas = tg
            yield
            estado.tareas = None
            refresco.cancel()             # solo los ciclos: las escrituras que quedan terminan antes de cerrar
            recalculo.cancel()
            catalogo.cancel()
        await cliente.aclose()

    app = FastAPI(lifespan=vida, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pia = estado
    if not config.sacbe:                      # el contenedor de Sacbé no monta el proxy ni la puerta pública: 404
        app.include_router(proxy.router)
        app.add_middleware(Puerta, estado=estado)
    else:
        @app.middleware("http")
        async def solo_su_host(request: Request, call_next):
            """Sacbé solo contesta a PIA_SACBE_HOST; /salud queda para el healthcheck."""
            if request.url.path != "/salud" and request.headers.get("host", "").lower() != config.sacbe_host:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return await call_next(request)

    @app.get("/salud")
    async def salud():
        cat = estado.catalogo
        if config.sacbe:                      # desde la red del proxy no se cuenta qué cuentas están agotadas
            return JSONResponse({"ok": cat.cargado}, status_code=200 if cat.cargado else 503)
        edad = (ahora() - cat.ultimo_refresco).total_seconds() if cat.ultimo_refresco else None
        fresca = edad is not None and al_dia(edad, config.refresco_s)
        viva = await base_responde(estado)
        cuerpo = {"ok": cat.cargado, "base": fresca and viva, "proyectos": cat.cuantos(),
                  "agotadas": sorted(cat.agotadas()), "fallas_registro": estado.fallas_registro}
        return JSONResponse(cuerpo, status_code=200 if cat.cargado else 503)

    if config.panel or config.sacbe:          # pasarela-ia-panel (PIA_PANEL=1) y pasarela-ia-sacbe (PIA_SACBE=1)
        async def quien_entra(request: Request):
            """El Quien, o la página corta del 403 o del 503."""
            try:
                with anyio.fail_after(ESPERA_QUIEN_S):
                    quien = await proxy.en_base(estado.hilos_lectura, quien_de, request.headers, estado.motor,
                                                config, estado.verificador, abandonar=True)
            except (BaseNoResponde, TimeoutError):
                return None, pagina_corta(BASE_CAIDA, 503)
            if quien is None:
                return None, pagina_corta(SIN_ACCESO, 403)
            return quien, None

        @app.get("/panel")
        async def ver_panel(request: Request):
            quien, rechazo = await quien_entra(request)
            if rechazo is not None:
                return rechazo
            t = ahora()
            proyectos = usuarios = None
            if quien.papel in ("dueno", "responsable"):      # huellas y formularios; si la base no contesta, sin ellos
                try:
                    with anyio.fail_after(ESPERA_QUIEN_S):
                        proyectos, usuarios = await proxy.en_base(estado.hilos_lectura, acciones.datos_panel,
                                                                  estado.motor, quien.papel == "dueno",
                                                                  abandonar=True)
                except Exception as e:  # noqa: BLE001
                    print(f"pasarela: el panel no pudo leer proyectos y usuarios ({type(e).__name__})",
                          file=sys.stderr)
            # Solo el panel con login tiene formularios: el panel interno es de solo lectura.
            ficha = (acciones.ficha(config.panel_secreto, quien.correo, t.date())
                     if config.sacbe and config.panel_secreto and proyectos is not None else None)
            if quien.ve_todo():               # solo dueño y finanzas ven la salud del gestor
                api = await sondear_api(estado)
                base = await base_responde(estado)
                lista = estado.modelos.lista
                salud_ = panel.salud_de(api, base, estado.bolsa.foto, config.orden_s, lista.ts if lista else None,
                                        len(lista.modelos) if lista else 0,
                                        tuple(m.id for m in lista.sin_precio) if lista else (), config.catalogo_s, t)
            else:
                salud_ = ()
            vista = panel.armar_vista(estado.bolsa.foto, estado.bolsa.en_uso, salud_, config.tipo_cambio, t,
                                      config.tipo_cambio_supuesto, quien=quien, proyectos=proyectos,
                                      usuarios=usuarios, ficha=ficha, host=config.sacbe_host)
            return HTMLResponse(panel.pagina(vista), headers=CABECERAS_PANEL)

        @app.get("/panel/costo")
        async def ver_costo(request: Request):
            quien, rechazo = await quien_entra(request)
            if rechazo is not None:
                return rechazo
            t = ahora()
            visibles = None if quien.ve_todo() else set(quien.proyectos)
            f = costo.leer_filtros(request.query_params, visibles)
            try:
                with anyio.fail_after(10):
                    datos = await proxy.en_base(estado.hilos_lectura, costo.leer, estado.motor, f, quien, t,
                                                abandonar=True)
            except Exception as e:  # noqa: BLE001
                print(f"pasarela: la página de costos no pudo leer la base ({type(e).__name__})", file=sys.stderr)
                return HTMLResponse("<p>No pude leer la base. Reintenta en un momento.</p>", status_code=503,
                                    headers=CABECERAS_PANEL)
            hora = f"Página generada a las {en_local(t):%H:%M} ({NOMBRE_ZONA})"
            html = costo.pagina(f, datos, panel.quien_texto(quien), hora, panel.TITULO, panel.ESTILO, panel.ICONOS)
            return HTMLResponse(html, headers=CABECERAS_PANEL)

    if config.sacbe:                          # las acciones existen solo detrás de Cloudflare Access
        @app.post("/panel/accion")
        async def accion_panel(request: Request):
            """Rechazos en orden: quién (403), origen (403), tamaño (413), ficha (403), acción desconocida (400),
            papel (403), «confirmo» (400), validación de admin (400). Si la base falla, 503."""
            quien, rechazo = await quien_entra(request)
            if rechazo is not None:
                return rechazo
            if not acciones.origen_valido(request.headers, request.url.netloc):
                return pagina_corta(acciones.NO_ORIGEN, 403, volver=True)
            try:
                cuerpo = await proxy.leer_cuerpo(request, acciones.TOPE_CUERPO)
            except proxy.CuerpoGrande:
                return pagina_corta(acciones.GRANDE, 413, volver=True)
            try:
                campos = acciones.leer_campos(cuerpo)
            except acciones.Rechazo as e:
                return pagina_corta(escape(e.mensaje), 400, volver=True)
            if not acciones.ficha_valida(config.panel_secreto, quien.correo, campos.get("ficha"), ahora().date()):
                return pagina_corta(acciones.SIN_FICHA, 403, volver=True)
            accion = campos.get("accion", "")
            if accion not in acciones.CAMPOS:
                return pagina_corta(acciones.DESCONOCIDA, 400, volver=True)
            if not quien.puede(accion, acciones.proyecto_de(accion, campos)):
                return pagina_corta(acciones.SIN_PERMISO, 403, volver=True)
            if accion in acciones.CON_CONFIRMO and campos.get("confirmo") != "si":
                return pagina_corta(acciones.SIN_CONFIRMO, 400, volver=True)
            # Escritura que la petición espera: va por el limitador de lectura, como guardar_objeto o marcar_agotada.
            # Sin fail_after ni abandonar: una rotación abandonada podría guardarse sin que nadie vea la llave nueva.
            # El tope lo ponen statement_timeout y pool_timeout de la base.
            try:
                llave = await proxy.en_base(estado.hilos_lectura, acciones.ejecutar, estado.motor,
                                            quien.etiqueta()[:80], accion, campos)
            except acciones.Rechazo as e:
                return pagina_corta(escape(e.mensaje), 400, volver=True)
            except Exception as e:  # noqa: BLE001  solo el tipo: el texto de la base puede traer los parámetros
                print(f"pasarela: falló la acción {accion} del panel ({type(e).__name__})", file=sys.stderr)
                return pagina_corta(BASE_CAIDA, 503, volver=True)
            if accion in acciones.DE_USUARIO:
                olvidar_usuarios(estado.motor)          # sin esperar los 30 s de la caché
            if llave is not None:
                # la llave se ve tras un 303 a GET /panel/llave/<token>; recargar esa página no repite el POST.
                nombre = campos.get("nombre") if accion == "proyecto-alta" else campos.get("proyecto")
                ahora_ = estado.reloj()
                for k in [k for k, p in estado.llaves_por_ver.items() if p.vence <= ahora_]:
                    del estado.llaves_por_ver[k]
                token = secrets.token_urlsafe(32)
                estado.llaves_por_ver[token] = Pendiente(ahora_ + LLAVE_VIDA_S, quien.correo, nombre or "", llave,
                                                         accion)
                return RedirectResponse(f"/panel/llave/{token}", status_code=303, headers=CABECERAS_PANEL)
            return RedirectResponse("/panel", status_code=303, headers=CABECERAS_PANEL)

        @app.get("/panel/llave/{token}")
        async def ver_llave(request: Request, token: str):
            """Una sola vez, solo para quien hizo el cambio y antes de LLAVE_VIDA_S; luego 404."""
            quien, rechazo = await quien_entra(request)
            if rechazo is not None:
                return rechazo
            p = estado.llaves_por_ver.get(token)
            if p is not None and p.vence <= estado.reloj():
                estado.llaves_por_ver.pop(token, None)
                p = None
            if p is None or p.correo != quien.correo:           # la de otro no se gasta: sigue esperando a su dueño
                return pagina_corta(LLAVE_YA_NO, 404, volver=True)
            del estado.llaves_por_ver[token]
            return HTMLResponse(panel.pagina_llave(p.proyecto, p.llave, p.accion), headers=CABECERAS_PANEL)

    return app
