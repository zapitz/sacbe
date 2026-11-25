"""Paso de peticiones a Anthropic: llave interna por llave real, en el orden que da la bolsa (pasarela/bolsa.py).
No guarda el contenido de peticiones ni respuestas: solo modelo, tokens y costo.
Lotes y archivos se quedan en la cuenta donde se crearon."""
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import timezone
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .almacen import (guardar_objeto, guardar_objetos, guardar_registro, marcar_borrado, marcar_caducos, objeto_de,
                      objetos_entre, pagina_de_objetos, registrar_costo_de_lote, uso_de, usos_de)
from .llaves import llave_de_peticion
from .modelos import AliasInvalido, reescribir
from .orden import Candidata, SinCredito, es_falta_de_saldo
from .precios import avisar_sin_precio, costo_micro, tokens
from .tiempo import ahora
from .uso import LectorJSONL, LectorSSE, mensaje_de_json

QUITAR_PETICION = {"host", "content-length", "x-api-key", "authorization", "connection", "keep-alive",
                   "accept-encoding", "transfer-encoding", "te", "upgrade", "proxy-authorization",
                   "x-real-ip", "forwarded", "via"}       # las x-pia-* y las x-forwarded-* se quitan por prefijo
QUITAR_RESPUESTA = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}
RUTAS = re.compile(r"^v1/(messages|messages/count_tokens|models|models/[^/]+"
                   r"|messages/batches|messages/batches/[^/]+|messages/batches/[^/]+/(results|cancel)"
                   r"|files|files/[^/]+|files/[^/]+/content)$")
CON_ID = re.compile(r"^v1/(messages/batches|files)/([^/]+)")
USAN_ARCHIVOS = {"v1/messages", "v1/messages/count_tokens", "v1/messages/batches"}
PARAMS_LOTES = {"limit", "after_id", "before_id", "beta"}   # beta: lo mandan client.beta.* de los SDK viejos
TOPE_PAGINA_LOTES = 20                               # cada lote se le pregunta a Anthropic por separado
LOTES_A_LA_VEZ = 8                                   # consultas de lotes en curso a la vez al armar una página
PARAMS_LISTA = {"limit", "page", "after_id", "before_id", "ids[]", "ids", "beta"}
TOPE_PAGINA = 100                                    # ids[] de Anthropic admite 100 por llamada
RONDAS_LISTA = 5                                     # vueltas al registro para llenar una página con huecos
TOPE_LISTA_S = 10                                    # tiempo total para armar una página de lista
ID_FORMA = re.compile(r"[A-Za-z0-9_-]{1,120}")       # forma de un id y de un cursor de lista
BETA_ARCHIVOS = "files-api-2025-04-14"               # con ella Anthropic lista en la forma vieja, sin ids[]
CREAN = {"v1/messages/batches": "lote", "v1/files": "archivo"}
REGISTRAR = {"v1/messages"}
ESPERA_FIJADA_S = 5                                  # tope para saber a qué cuenta va un objeto; después, 503
ESPERA_USO_S = 5                                     # tope para leer el uso del mes (GET /v1/uso); después, 503
GASTAN = {"v1/messages", "v1/messages/batches"}      # los POST que cobran tokens (no count_tokens ni cancel)
SIN_PAGADA = {"type": "error", "error": {"type": "billing_error",
                                         "message": "Este proyecto no puede gastar en la cuenta pagada."}}
TOPE_PENDIENTES = 2000                               # escrituras de fondo esperando a la base; más, y se descartan
AVISO_DESCARTE_S = 60                                # un aviso de fila descartada por minuto, a lo más
TOPE_JSON = 32 * 1024 * 1024                         # cuerpo de cualquier ruta salvo subir archivos
TOPE_ARCHIVO = 100 * 1024 * 1024                     # POST v1/files (multipart), en esta fase
CUPO_SUBIDAS = 300 * 1024 * 1024                     # bytes de subidas en memoria a la vez, entre todos
ESPERA_TROZO_S = 30                                  # una subida que pasa esto sin mandar datos se corta (408)

router = APIRouter()


@dataclass
class Contexto:
    proyecto: str
    fuente: str
    ruta: str
    t0: float
    reintentos: int


async def en_base(limite: anyio.CapacityLimiter, fn, *args, abandonar: bool = False):
    """Toda llamada a la base corre con un limitador propio y no con el de anyio, que usa todo lo demás. Cada llamada
    elige el suyo: estado.hilos_escritura para las filas que se escriben ya enviada la respuesta, estado.hilos_lectura
    para todo lo que una petición o el refresco esperan. Así una cola de escrituras no le quita turno a una lectura.
    abandonar=True deja que un fail_after suelte la petición aunque el hilo siga colgado."""
    return await anyio.to_thread.run_sync(fn, *args, limiter=limite, abandon_on_cancel=abandonar)


def error(status: int, tipo: str, mensaje: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": tipo, "message": f"pasarela-ia: {mensaje}"}},
                        status_code=status)


class CuerpoGrande(Exception):
    pass


class CuerpoCallado(Exception):
    pass


async def leer_cuerpo(request: Request, tope: int, espera: float | None = None) -> bytes:
    """Lee el cuerpo contando bytes: si pasa del tope se corta ahí, sin juntarlo entero en memoria. Con
    Content-Length mayor al tope ni se empieza a leer; sin él (chunked) se corta al pasar el tope. Con `espera`
    (las subidas), si pasan esos segundos sin que llegue un trozo se corta con CuerpoCallado."""
    largo = request.headers.get("content-length")
    if largo and largo.isdigit() and int(largo) > tope:
        raise CuerpoGrande
    partes, total = [], 0
    if espera is None:
        async for trozo in request.stream():
            total += len(trozo)
            if total > tope:
                raise CuerpoGrande
            partes.append(trozo)
        return b"".join(partes)
    flujo = request.stream().__aiter__()
    while True:
        try:
            with anyio.fail_after(espera):
                trozo = await flujo.__anext__()
        except StopAsyncIteration:
            return b"".join(partes)
        except TimeoutError:
            raise CuerpoCallado from None
        total += len(trozo)
        if total > tope:
            raise CuerpoGrande
        partes.append(trozo)


def cabeceras_salida(request: Request, llave: str) -> dict:
    h = {k: v for k, v in request.headers.items()
         if k.lower() not in QUITAR_PETICION and not k.lower().startswith(("x-pia-", "x-forwarded-"))}
    h["x-api-key"] = llave
    h["accept-encoding"] = "identity"
    return h


def cabeceras_regreso(resp: httpx.Response, fuente: str) -> dict:
    h = {k: v for k, v in resp.headers.items() if k.lower() not in QUITAR_RESPUESTA}
    h["x-pia-fuente"] = fuente
    return h


def motivo(datos: bytes) -> str:
    try:
        return str(json.loads(datos)["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return datos[:200].decode("utf-8", "replace")


def _primero(valor: str | None) -> str | None:
    """Detrás de varios proxies estas cabeceras llegan como lista separada por comas: vale la primera."""
    return (valor.split(",")[0].strip() or None) if valor else None


def base_publica(request: Request) -> str:
    proto = _primero(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = _primero(request.headers.get("x-forwarded-host")) or request.headers.get("host")
    return f"{proto}://{host}/anthropic"


def reescribir_lote(datos: bytes, base: str, anthropic_url: str) -> bytes:
    """El SDK baja resultados desde results_url (absoluta). Si apuntara a otro host, la llave interna viajaría allá:
    toda results_url absoluta se reescribe hacia la pasarela, sin importar su esquema ni su host."""
    try:
        d = json.loads(datos)
    except ValueError:
        return datos
    if not isinstance(d, dict):
        return datos
    lista = d.get("data")
    for o in [d, *(lista if isinstance(lista, list) else [])]:
        u = o.get("results_url") if isinstance(o, dict) else None
        if isinstance(u, str):
            partes = urlsplit(u)
            if partes.scheme and partes.netloc:
                o["results_url"] = base + partes.path + (f"?{partes.query}" if partes.query else "")
    return json.dumps(d).encode()


def _entero(v) -> int:
    """Solo enteros sanos llegan a la base; cualquier tipo raro de Anthropic cuenta como 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) and abs(v) < 2**62 else 0


def fila_de(ctx: Contexto, status: int, request_id, modelo, t: dict, micro: int, lote: bool = False) -> dict:
    modelo = modelo[:80] if isinstance(modelo, str) and modelo else None
    t = {k: _entero(v) for k, v in t.items()}
    return {"proyecto": ctx.proyecto, "fuente": ctx.fuente, "ruta": ctx.ruta[:80],
            "modelo": modelo, "status": status, "lote": lote, **t,
            "costo_micro": _entero(micro), "latencia_ms": int((time.monotonic() - ctx.t0) * 1000),
            "request_id": request_id[:80] if isinstance(request_id, str) else None, "reintentos": ctx.reintentos}


def fila_de_uso(ctx: Contexto, status: int, request_id, modelo, uso: dict, lote: bool = False) -> dict:
    try:
        micro, conocido = costo_micro(modelo or "", uso, lote) if uso else (0, True)
        if not conocido:
            avisar_sin_precio(modelo or "")
        t = tokens(uso)
    except Exception as e:  # noqa: BLE001  medir nunca debe romper el tráfico
        print(f"pasarela: no pude medir el uso ({type(e).__name__})", file=sys.stderr)
        micro, t = 0, tokens({})
    return fila_de(ctx, status, request_id, modelo, t, micro, lote)


async def escribir_fila(estado, fila: dict) -> None:
    if not await en_base(estado.hilos_escritura, guardar_registro, estado.motor, fila):
        estado.fallas_registro += 1


async def contar_lote(estado, lote_id: str, fila: dict) -> None:
    if await en_base(estado.hilos_escritura, registrar_costo_de_lote, estado.motor, lote_id, fila) is None:
        estado.fallas_registro += 1


def reservar(estado) -> bool:
    """Aparta un lugar en la cola de escrituras de fondo, una sola cuenta para las dos vías (en_fondo y despues).
    Con TOPE_PENDIENTES esperando, la fila se pierde: se cuenta en fallas_registro y se avisa una vez por minuto.
    Una base lenta no puede juntar tareas sin fin detrás de su limitador."""
    if estado.pendientes >= TOPE_PENDIENTES:
        estado.fallas_registro += 1
        t = time.monotonic()
        if estado.aviso_descarte is None or t - estado.aviso_descarte >= AVISO_DESCARTE_S:
            estado.aviso_descarte = t
            print(f"pasarela: registro descartado, {estado.pendientes} escrituras pendientes", file=sys.stderr)
        return False
    estado.pendientes += 1
    return True


async def en_fondo(estado, escribir, *args) -> None:
    """Lo que corre el BackgroundTask de una respuesta JSON, ya enviada. Reserva aquí y no al armar la Response:
    si Starlette no llega a correr la tarea (send truena), no queda un lugar apartado para siempre."""
    if reservar(estado):
        await _contada(estado, escribir, args)


async def despues(estado, escribir, *args) -> None:
    """Lanza la escritura sin esperarla, en el grupo de tareas de la vida de la app: el último trozo de un stream
    no espera a la base y la escritura se hace aunque el cliente ya se haya ido."""
    if estado.tareas is None:                 # la app corre sin su vida (uvicorn siempre la arranca): aquí mismo
        with anyio.CancelScope(shield=True):
            await escribir(estado, *args)
        return
    if reservar(estado):                      # antes de start_soon: la tarea ya cuenta aunque no haya arrancado
        estado.tareas.start_soon(_contada, estado, escribir, args)


async def _contada(estado, escribir, args) -> None:
    """Escribe con un lugar ya reservado y lo suelta al terminar, salga como salga."""
    try:
        with anyio.CancelScope(shield=True):
            await escribir(estado, *args)
    except Exception as e:  # noqa: BLE001  una excepción aquí cancelaría el grupo de tareas de toda la app
        print(f"pasarela: no pude escribir el registro ({type(e).__name__})", file=sys.stderr)
    finally:
        estado.pendientes -= 1


class Transmision(StreamingResponse):
    """Cierra el generador al terminar, también si el cliente se fue a media respuesta: así su finally cierra la
    respuesta de Anthropic y lanza la escritura en ese momento, y no cuando pase el recolector de basura."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def ids_de_archivos(cuerpo: bytes) -> set[str]:
    """Todo valor de texto bajo la llave "file_id", a cualquier profundidad, también si la llave se repite en el
    mismo objeto. Se lee el JSON de verdad: una búsqueda en los bytes se evade con escapes como \\u0066ile_id.
    Sin "file_id", ni un escape \\u, ni un byte nulo (json.loads también lee UTF-16 y UTF-32, donde b"file_id" no
    aparece) no puede haber ninguno y no se lee. Si no es JSON, Anthropic lo rechazará. Un anidamiento enorme sale
    como RecursionError."""
    if b"\x00" not in cuerpo and b"file_id" not in cuerpo and b"\\u" not in cuerpo:
        return set()
    ids = set()

    def juntar(pares):                 # ve todos los pares de cada objeto, repetidos incluidos; el objeto no se usa
        ids.update(v for k, v in pares if k == "file_id" and isinstance(v, str))

    try:
        json.loads(cuerpo, object_pairs_hook=juntar)
    except ValueError:
        return set()
    return ids


async def fuente_fijada(estado, ruta: str, cuerpo: bytes, proyecto: str):
    """Devuelve None (no hay fijación), el nombre de la fuente, o una respuesta de error.
    Un lote desconocido es 404. Un archivo desconocido se ignora (nació fuera de la pasarela); uno registrado a otro
    proyecto es 404. Uno borrado sigue fijando su cuenta."""
    m = CON_ID.match(ruta)
    if m:
        ids = [(m.group(2), m.group(1) == "messages/batches")]
    elif ruta in USAN_ARCHIVOS:
        try:
            ids = [(i, False) for i in sorted(ids_de_archivos(cuerpo))]
        except RecursionError:
            return error(400, "invalid_request_error", "el JSON tiene demasiados niveles de anidación")
    else:
        ids = []
    fuentes = set()
    i = None
    try:
        with anyio.fail_after(ESPERA_FIJADA_S):     # una base congelada no detiene la petición más que esto
            for i, estricto in ids:
                obj = await en_base(estado.hilos_lectura, objeto_de, estado.motor, i, abandonar=True)
                if obj is None and not estricto:
                    continue
                if obj is None or obj["proyecto"] != proyecto:
                    return error(404, "not_found_error", f"{i} no es de este proyecto o no pasó por la pasarela")
                fuentes.add(obj["fuente"])
    except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
        print(f"pasarela: no pude consultar el objeto {i} ({type(e).__name__})", file=sys.stderr)
        return error(503, "api_error", "no pude consultar la base; reintenta")
    if len(fuentes) > 1:
        return error(400, "invalid_request_error", "la petición mezcla archivos de dos cuentas")
    return fuentes.pop() if fuentes else None


@router.get("/v1/catalogo")
async def ver_catalogo(request: Request):
    """Marcas, modelos con precio y alias, para los menús de los programas. Pide la llave del
    programa, de cualquier clase, por las dos puertas. Sale de la memoria: no lee la base."""
    estado = request.app.state.pia
    if not estado.catalogo.cargado:
        return error(503, "api_error", "todavía no cargo los proyectos; reintenta")
    llave = llave_de_peticion(request.headers)
    if not llave or estado.catalogo.proyecto_por_llave(llave) is None:
        return error(401, "authentication_error", "llave interna inválida")
    if not estado.modelos.cargado:
        return error(503, "api_error", "todavía no cargo el catálogo de modelos; reintenta")
    return JSONResponse(estado.modelos.catalogo(), headers={"cache-control": "no-store"})


def _uso_json(estado, t, nombre: str, clase: str, n: int, micro: int, ultima) -> dict:
    return {"proyecto": nombre, "mes": f"{t:%Y-%m}", "peticiones": n, "usd": round(micro / 1e6, 4),
            "ultima": ultima.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ultima else None,
            "cuenta_en_uso": estado.bolsa.primera(clase, estado.catalogo.agotadas())}


@router.get("/v1/uso")
async def ver_uso(request: Request):
    """Uso del mes (UTC) del programa dueño de la llave, solo el suyo, por las dos puertas: peticiones y costo
    estimado del registro, su última petición de cualquier mes y la cuenta que la bolsa le daría ahora.
    Con ?proyectos=todos: {"proyectos": [el suyo, y luego los activos que la tabla lectura le deja ver, por nombre]}."""
    estado = request.app.state.pia
    if not estado.catalogo.cargado:
        return error(503, "api_error", "todavía no cargo los proyectos; reintenta")
    llave = llave_de_peticion(request.headers)
    proyecto = estado.catalogo.proyecto_por_llave(llave) if llave else None
    if proyecto is None:
        return error(401, "authentication_error", "llave interna inválida")
    pedidos = request.query_params.getlist("proyectos")
    if pedidos and pedidos != ["todos"]:
        return error(400, "invalid_request_error", "proyectos solo acepta el valor todos")
    t = ahora()
    mes = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        with anyio.fail_after(ESPERA_USO_S):        # una base congelada no detiene la petición más que esto
            if pedidos:
                filas = await en_base(estado.hilos_lectura, usos_de, estado.motor, proyecto.nombre, mes,
                                      abandonar=True)
            else:
                n, micro, ultima = await en_base(estado.hilos_lectura, uso_de, estado.motor, proyecto.nombre, mes,
                                                 abandonar=True)
    except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
        print(f"pasarela: no pude leer el uso de {proyecto.nombre} ({type(e).__name__})", file=sys.stderr)
        return error(503, "api_error", "no pude consultar la base; reintenta")
    if pedidos:
        cuerpo = {"proyectos": [_uso_json(estado, t, *f) for f in filas]}
    else:
        cuerpo = _uso_json(estado, t, proyecto.nombre, proyecto.clase, n, micro, ultima)
    return JSONResponse(cuerpo, headers={"cache-control": "no-store"})


@router.api_route("/anthropic/{ruta:path}", methods=["GET", "POST", "DELETE"])
async def pasar(ruta: str, request: Request):
    estado = request.app.state.pia
    if not estado.catalogo.cargado:
        return error(503, "api_error", "todavía no cargo los proyectos; reintenta")
    llave = llave_de_peticion(request.headers)
    proyecto = estado.catalogo.proyecto_por_llave(llave) if llave else None
    if proyecto is None:
        return error(401, "authentication_error", "llave interna inválida")
    if not RUTAS.match(ruta):
        return error(404, "not_found_error", f"la ruta /{ruta} no pasa por la pasarela")
    if request.method == "POST" and ruta in GASTAN and estado.bolsa.cortado(proyecto.nombre):
        return error(429, "rate_limit_error", "tope mensual del proyecto alcanzado")   # de la foto, sin leer la base
    if request.method == "POST" and ruta == "v1/files":
        n = tamano_de_subida(request)
        if estado.subiendo + n > CUPO_SUBIDAS:                # la subida no se lee: no ocupa memoria
            print(f"pasarela: subida rechazada por cupo lleno (subiendo={estado.subiendo} pide={n} "
                  f"cupo={CUPO_SUBIDAS})", file=sys.stderr)
            r = error(429, "rate_limit_error", "hay demasiadas subidas de archivos a la vez; reintenta")
            r.headers["retry-after"] = "2"
            return r
        estado.subiendo += n
        try:
            return await atender(request, estado, proyecto, ruta)
        finally:
            estado.subiendo -= n
    return await atender(request, estado, proyecto, ruta)


def tamano_de_subida(request: Request) -> int:
    """Lo que una subida aparta del cupo: su Content-Length, o el tope entero si no lo declara o si viene por trozos
    (Transfer-Encoding manda sobre Content-Length). Nunca más que TOPE_ARCHIVO: leer_cuerpo corta ahí."""
    largo = request.headers.get("content-length")
    if "transfer-encoding" in request.headers or not (largo and largo.isdigit()):
        return TOPE_ARCHIVO
    return min(int(largo), TOPE_ARCHIVO)


async def atender(request: Request, estado, proyecto, ruta: str):
    """Lee el cuerpo, decide la cuenta y reenvía. `proyecto` es el Proyecto del catálogo, ya autenticado."""
    tope = TOPE_ARCHIVO if ruta == "v1/files" else TOPE_JSON
    espera = ESPERA_TROZO_S if request.method == "POST" and ruta == "v1/files" else None
    try:
        cuerpo = await leer_cuerpo(request, tope, espera)
    except CuerpoGrande:
        return error(413, "request_too_large", f"el cuerpo pasa de {tope // (1024 * 1024)} MB")
    except CuerpoCallado:
        return error(408, "timeout_error", "la subida se quedó sin mandar datos")
    if request.method == "POST":
        try:
            cuerpo = reescribir(cuerpo, ruta, estado.modelos.alias)       # alias «lo más nuevo»
        except AliasInvalido as e:
            return error(e.status, e.tipo, e.mensaje)
    if request.method == "GET" and ruta == "v1/files":
        return await listar_archivos(request, proyecto.nombre)
    if request.method == "GET" and ruta == "v1/messages/batches":
        return await listar_lotes(request, proyecto.nombre)
    fijada = await fuente_fijada(estado, ruta, cuerpo, proyecto.nombre)
    if isinstance(fijada, Response):
        return fijada
    if fijada:
        tipo = estado.bolsa.tipo_de(fijada)                  # pagado si PIA_FUENTES o la tabla cuenta dicen tarjeta
        if tipo is None:
            return error(404, "not_found_error", f"la cuenta {fijada} ya no está configurada")
        if tipo == "pagado" and proyecto.clase != "produccion" and request.method == "POST" and ruta in GASTAN:
            return JSONResponse(SIN_PAGADA, status_code=402)     # un objeto fijado no abre la tarjeta
        candidatas = [Candidata(fijada, tipo)]
    else:
        try:
            candidatas = estado.bolsa.ordenar(proyecto.clase, estado.catalogo.agotadas())
        except SinCredito as e:
            return error(402, "billing_error", str(e))
    return await reenviar(request, ruta, cuerpo, proyecto.nombre, candidatas)


async def reenviar(request: Request, ruta: str, cuerpo: bytes, proyecto: str, candidatas: list[Candidata]):
    estado = request.app.state.pia
    t0 = time.monotonic()
    for intento, cand in enumerate(candidatas):
        ctx = Contexto(proyecto, cand.fuente, ruta, t0, intento)
        salida = estado.cliente.build_request(
            request.method, f"{estado.config.anthropic_url}/{ruta}",
            params=list(request.query_params.multi_items()),
            headers=cabeceras_salida(request, estado.llaves[cand.fuente]), content=cuerpo)
        try:
            resp = await estado.cliente.send(salida, stream=True)
        except httpx.PoolTimeout:                      # las CONEXIONES_MAX de app.py siguen ocupadas
            return sin_conexion()
        except httpx.HTTPError as e:
            return error(502, "api_error", f"no pude hablar con Anthropic ({type(e).__name__})")
        if resp.status_code >= 400:
            try:
                datos = await resp.aread()
            except httpx.HTTPError as e:
                return error(502, "api_error", f"no pude leer la respuesta de Anthropic ({type(e).__name__})")
            finally:
                await resp.aclose()
            if es_falta_de_saldo(resp.status_code, datos):
                await en_base(estado.hilos_lectura, estado.catalogo.marcar_agotada, cand.fuente, motivo(datos),
                               cand.tipo)
                if intento + 1 < len(candidatas):
                    continue
            fondo = None
            if ruta in REGISTRAR:                      # la fila se escribe ya enviada la respuesta
                fila = fila_de_uso(ctx, resp.status_code, resp.headers.get("request-id"), None, {})
                fondo = BackgroundTask(en_fondo, estado, escribir_fila, fila)
            return Response(datos, status_code=resp.status_code, headers=cabeceras_regreso(resp, cand.fuente),
                            background=fondo)
        return await exito(request, estado, ctx, resp)
    return error(502, "api_error", "sin fuentes")     # no se llega: ordenar() nunca regresa vacío


async def exito(request: Request, estado, ctx: Contexto, resp: httpx.Response):
    cab = cabeceras_regreso(resp, ctx.fuente)
    m = CON_ID.match(ctx.ruta)
    if m and m.group(1) == "messages/batches" and ctx.ruta.endswith("/results"):
        return Transmision(transmitir_resultados(estado, ctx, resp, m.group(2)),
                           status_code=resp.status_code, headers=cab)
    if m and m.group(1) == "files" and ctx.ruta.endswith("/content"):
        return Transmision(pasar_tal_cual(resp), status_code=resp.status_code, headers=cab)
    if ctx.ruta in REGISTRAR and resp.headers.get("content-type", "").startswith("text/event-stream"):
        return Transmision(transmitir(estado, ctx, resp), status_code=resp.status_code, headers=cab)
    try:
        datos = await resp.aread()
    except httpx.HTTPError as e:
        return error(502, "api_error", f"no pude leer la respuesta de Anthropic ({type(e).__name__})")
    finally:
        await resp.aclose()
    fondo = None
    if ctx.ruta in REGISTRAR:                          # la fila se escribe ya enviada la respuesta
        modelo, uso, archivos = mensaje_de_json(datos)
        fondo = BackgroundTask(en_fondo, estado, escribir_fila,
                               fila_de_uso(ctx, resp.status_code, resp.headers.get("request-id"), modelo, uso))
        if archivos:                                   # solo las respuestas con archivos de herramientas tocan la base
            await guardar_generados(estado, ctx, archivos)
    if request.method == "POST" and ctx.ruta in CREAN:
        await guardar_creado(estado, ctx, datos, CREAN[ctx.ruta], resp.headers.get("request-id"))
    if (request.method == "DELETE" and resp.status_code < 300 and m and m.group(1) == "files"
            and ctx.ruta == f"v1/files/{m.group(2)}"):
        await quitar_de_la_lista(estado, ctx, m.group(2))
    if ctx.ruta.startswith("v1/messages/batches"):
        datos = reescribir_lote(datos, base_publica(request), estado.config.anthropic_url)
    return Response(datos, status_code=resp.status_code, headers=cab, background=fondo)


async def guardar_creado(estado, ctx: Contexto, datos: bytes, tipo: str, request_id=None):
    id_ = None
    try:
        id_ = json.loads(datos)["id"]
        await en_base(estado.hilos_lectura, guardar_objeto, estado.motor, id_, tipo, ctx.fuente, ctx.proyecto)
    except Exception as e:  # noqa: BLE001 (se avisa con el id para poder dar de alta el objeto a mano)
        id_ = id_[:120] if isinstance(id_, str) else None          # un id no es contenido; otra cosa no se imprime
        print(f"pasarela: no guardé a qué cuenta pertenece un {tipo} ({type(e).__name__}) id={id_} "
              f"fuente={ctx.fuente} proyecto={ctx.proyecto} request-id={request_id} bytes={len(datos)}",
              file=sys.stderr)


async def quitar_de_la_lista(estado, ctx: Contexto, id_: str) -> None:
    """Anthropic ya borró el archivo: en el registro queda como «borrado» y deja de salir en la lista del proyecto.
    Si la base falla, la fila sigue como archivo y la lista simplemente no lo encuentra en Anthropic."""
    try:
        with anyio.fail_after(ESPERA_FIJADA_S):
            await en_base(estado.hilos_lectura, marcar_borrado, estado.motor, id_, ctx.proyecto, abandonar=True)
    except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
        print(f"pasarela: no pude marcar como borrado el archivo {id_[:120]} ({type(e).__name__})", file=sys.stderr)


async def guardar_generados(estado, ctx: Contexto, ids: set[str]) -> bool:
    """Fija a esta cuenta y a este proyecto los archivos que nacieron dentro de una respuesta (herramientas de
    ejecución de código). Se espera antes de entregar la respuesta, o el trozo, que los trae: así la siguiente
    petición del programa ya los encuentra. Con la base congelada se suelta a los ESPERA_FIJADA_S, avisa
    con los ids, que no son contenido, para poder darlos de alta a mano (ops/pia objeto registrar) y regresa False."""
    try:
        with anyio.fail_after(ESPERA_FIJADA_S):
            await en_base(estado.hilos_lectura, guardar_objetos, estado.motor, ids, "archivo", ctx.fuente,
                          ctx.proyecto, abandonar=True)
        return True
    except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
        print(f"pasarela: no guardé a qué cuenta pertenecen {len(ids)} archivos de una respuesta "
              f"({type(e).__name__}) ids={','.join(sorted(ids))[:600]} fuente={ctx.fuente} proyecto={ctx.proyecto}",
              file=sys.stderr)
        return False


def avisar_sin_registro(ctx: Contexto, ids: set[str]) -> None:
    """Tras una falla de la base ya no se insiste en ese stream, pero los ids siguen saliendo en stderr para poder
    darlos de alta a mano (ops/pia objeto registrar)."""
    print(f"pasarela: sin registrar {len(ids)} archivos de una respuesta (la base ya falló en este stream) "
          f"ids={','.join(sorted(ids))[:600]} fuente={ctx.fuente} proyecto={ctx.proyecto}", file=sys.stderr)


async def registrar_o_avisar(estado, ctx: Contexto, ids: set[str], registrando: bool) -> bool:
    """Registra los archivos de un trozo, o solo los avisa si la base ya falló antes en este stream. Regresa si
    se sigue registrando."""
    if registrando:
        return await guardar_generados(estado, ctx, ids)
    avisar_sin_registro(ctx, ids)
    return False


def limite_de(q) -> int | None:
    """El limit de una lista, como lo define Anthropic: de 1 a 1000, 20 si no viene. None si no es eso."""
    try:
        n = int(q.get("limit", "20"))
    except ValueError:
        return None
    return n if 1 <= n <= 1000 else None


def cabeceras_lista(request: Request, llave: str) -> dict:
    """Las de cabeceras_salida, sin la beta vieja de archivos: la pasarela pregunta siempre en la forma actual."""
    h = cabeceras_salida(request, llave)
    betas = [b.strip() for b in h.pop("anthropic-beta", "").split(",") if b.strip() and b.strip() != BETA_ARCHIVOS]
    if betas:
        h["anthropic-beta"] = ",".join(betas)
    return h


def sin_conexion() -> JSONResponse:
    """El 503 cuando las CONEXIONES_MAX hacia Anthropic siguen ocupadas."""
    return error(503, "api_error", "todas las conexiones hacia Anthropic están ocupadas; reintenta")


def tipo_de_error(datos: bytes) -> str | None:
    """El `error.type` del sobre de error de Anthropic, o None si no tiene esa forma."""
    try:
        t = json.loads(datos)["error"]["type"]
    except (ValueError, KeyError, TypeError):
        return None
    return t if isinstance(t, str) else None


def es_error_de_ids(resp: httpx.Response) -> bool:
    """Un 400 por un id mal formado de ids[]: su mensaje empieza con «ids[]: invalid id». Cualquier otro
    400 es otra cosa (cabecera, versión, «ids[]: unknown field») y no se disfraza de «Anthropic ya no lo tiene»."""
    return resp.status_code == 400 and motivo(resp.content).startswith("ids[]: invalid id")


async def preguntar_archivos(request: Request, estado, fuente: str, ids: list[str]):
    """Una llamada GET v1/files?ids[]=… a la cuenta. Regresa la respuesta de Anthropic (con cualquier estado) o, si no
    se pudo hablar con ella, un 502 (o el 503 de conexiones ocupadas) ya armado."""
    salida = estado.cliente.build_request(
        "GET", f"{estado.config.anthropic_url}/v1/files", params=[("ids[]", i) for i in ids],
        headers=cabeceras_lista(request, estado.llaves[fuente]))
    try:
        return await estado.cliente.send(salida)
    except httpx.PoolTimeout:
        return sin_conexion()
    except httpx.HTTPError as e:
        return error(502, "api_error", f"no pude hablar con Anthropic ({type(e).__name__})")


async def traer_archivos(request: Request, estado, filas: list[tuple[str, str]]):
    """(id → objeto de Anthropic, ids que Anthropic omitió en una respuesta 200), pedidos a su cuenta con ids[], o
    una respuesta de error si algo falla. Lo que Anthropic ya no tiene (borrado por fuera, vencido hace más de 30
    días) no viene y se avisa en el segundo conjunto, para marcarlo.
    Un id mal formado hace que Anthropic conteste 400 a toda la llamada, con un mensaje que empieza con «ids[]»: solo
    entonces el tramo se repite id por id y el que siga con ese 400 se omite sin marcarlo. Cualquier otro
    error, también en las llamadas id por id, se regresa al cliente."""
    por_fuente: dict[str, list[str]] = {}
    for i, fuente in filas:
        por_fuente.setdefault(fuente, []).append(i)
    encontrados: dict[str, dict] = {}
    muertos: set[str] = set()
    for fuente in sorted(por_fuente):
        if fuente not in estado.llaves:               # la cuenta ya no está configurada: sus archivos no se ven
            continue
        for k in range(0, len(por_fuente[fuente]), TOPE_PAGINA):
            ids = por_fuente[fuente][k:k + TOPE_PAGINA]
            resp = await preguntar_archivos(request, estado, fuente, ids)
            if isinstance(resp, Response):
                return resp
            respuestas = [(resp, ids)]
            if es_error_de_ids(resp):
                respuestas = []
                for i in ids if len(ids) > 1 else ():
                    uno = await preguntar_archivos(request, estado, fuente, [i])
                    if isinstance(uno, Response):
                        return uno
                    if not es_error_de_ids(uno):      # con ese 400 el id se omite; otro 400 se regresa abajo
                        respuestas.append((uno, [i]))
            for resp, pedidos in respuestas:
                if resp.status_code >= 400:
                    return Response(resp.content, status_code=resp.status_code,
                                    headers=cabeceras_regreso(resp, fuente))
                try:
                    pagina = resp.json()
                except ValueError:
                    pagina = None
                if not isinstance(pagina, dict) or not isinstance(pagina.get("data"), list):
                    return error(502, "api_error", "Anthropic contestó algo que no es una lista")
                pedidos = set(pedidos)                # solo lo que se pidió: nada de otro proyecto puede colarse
                vistos = {o["id"]: o for o in pagina["data"]
                          if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"] in pedidos}
                encontrados.update(vistos)
                if not pagina.get("next_page") and not pagina.get("has_more"):   # página incompleta: no prueba nada
                    muertos |= pedidos - set(vistos)
    return encontrados, muertos


async def marcar_muertos(estado, proyecto: str, tipo: str, ids: set[str]) -> None:
    """Lo que Anthropic ya no tiene y lleva más de una hora registrado pasa a «caduco»: deja de preguntarse en cada
    lista. Si la base falla o tarda, la lista sigue sin marcar."""
    if not ids:
        return
    try:
        with anyio.fail_after(ESPERA_FIJADA_S):
            n = await en_base(estado.hilos_lectura, marcar_caducos, estado.motor, proyecto, tipo, sorted(ids),
                              abandonar=True)
        if n:
            print(f"pasarela: {n} objetos de tipo {tipo} del proyecto {proyecto} pasaron a caduco "
                  f"(Anthropic ya no los tiene)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
        print(f"pasarela: no pude marcar como caducos {len(ids)} objetos ({type(e).__name__})", file=sys.stderr)


async def reunir(request: Request, estado, proyecto: str, tipo: str, n: int, despues: str | None,
                 antes: str | None, traer):
    """Arma una página desde el registro propio: toma del registro los ids que siguen al cursor, le pide sus datos a
    Anthropic con `traer` y, si Anthropic ya no tiene algunos, da vueltas para llenar la página: hasta RONDAS_LISTA, y
    si aún no hay nada que mostrar, las que hagan falta con tope de TOPE_LISTA_S en total. Nunca entrega una página
    vacía con has_more verdadero (los SDK dejan de paginar ahí): si se acaba el tiempo sin encontrar nada y
    aún quedan filas, contesta 503.
    Regresa (ids del registro que cubre la página, del más nuevo al más viejo; id → objeto de Anthropic; si quedan
    más en esa dirección) o una respuesta de error."""
    hacia_atras = antes is not None
    vistas: list[str] = []
    encontrados: dict[str, dict] = {}
    hay_mas = True                        # mientras no se vea el final del registro, se supone que hay más
    rondas = 0
    with anyio.move_on_after(TOPE_LISTA_S) as espera:
        while True:
            try:
                with anyio.fail_after(ESPERA_FIJADA_S):
                    pagina = await en_base(estado.hilos_lectura, pagina_de_objetos, estado.motor, proyecto, tipo,
                                           n - len(encontrados), despues, antes, abandonar=True)
            except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
                print(f"pasarela: no pude listar los objetos ({type(e).__name__})", file=sys.stderr)
                return error(503, "api_error", "no pude consultar la base; reintenta")
            if pagina is None:
                return error(400, "invalid_request_error", "el cursor no es de este proyecto")
            filas, mas = pagina
            if not filas:
                hay_mas = False
                break
            r = await traer(request, estado, filas)
            if isinstance(r, Response):
                return r
            nuevos, muertos = r
            await marcar_muertos(estado, proyecto, tipo, muertos)
            encontrados.update(nuevos)
            ids = [i for i, _ in filas]
            if hacia_atras:
                vistas, antes = ids + vistas, ids[0]
            else:
                vistas, despues = vistas + ids, ids[-1]
            hay_mas, rondas = mas, rondas + 1
            if not hay_mas or len(encontrados) >= n or (encontrados and rondas >= RONDAS_LISTA):
                break
    if espera.cancelled_caught and not encontrados and hay_mas:
        return error(503, "api_error", "la lista está saltando archivos que Anthropic ya no tiene; reintenta")
    return vistas, encontrados, hay_mas


def pagina_json(datos: list, vistas: list[str], hay_mas: bool, siguiente: str | None) -> JSONResponse:
    """La forma que entiende cualquier SDK: next_page de la forma actual y has_more, first_id y last_id de
    la vieja. first_id y last_id son los extremos del tramo del registro que cubre la página, estén o no en data."""
    return JSONResponse({"data": datos, "has_more": hay_mas, "first_id": vistas[0] if vistas else None,
                         "last_id": vistas[-1] if vistas else None, "next_page": siguiente},
                        headers={"cache-control": "no-store"})


def cursor_valido(texto: str) -> bool:
    """Un id o un cursor de la pasarela: solo letras, números, guion y guion bajo, de 1 a 120 caracteres. Se valida
    antes de ir a la base (en Postgres un byte nulo da DataError y el SDK reintentaría un 503 para siempre)."""
    return ID_FORMA.fullmatch(texto) is not None


MAL_CURSOR = "el cursor o el id no tiene forma de id (letras, números, guion y guion bajo, hasta 120 caracteres)"


async def listar_archivos(request: Request, proyecto: str):
    """GET v1/files: una sola lista con los archivos que el proyecto tiene registrados en la pasarela, de todas las
    cuentas, del más nuevo al más viejo. La página sale del registro propio (tabla objeto) y los datos de cada
    archivo, de su cuenta. Acepta page (el next_page anterior), after_id, before_id, limit e ids[], y
    ignora beta (lo mandan los SDK viejos)."""
    estado = request.app.state.pia
    q = request.query_params
    raros = sorted(set(q.keys()) - PARAMS_LISTA)
    if raros:
        return error(400, "invalid_request_error", f"al listar archivos la pasarela no admite: {', '.join(raros)}")
    cursores = [k for k in ("page", "after_id", "before_id") if k in q]
    if len(cursores) > 1:
        return error(400, "invalid_request_error", "usa solo uno de page, after_id o before_id")
    pedidos = list(dict.fromkeys(q.getlist("ids[]") + q.getlist("ids")))
    if pedidos:
        if cursores or "limit" in q:
            return error(400, "invalid_request_error", "ids[] no se combina con page, limit, after_id ni before_id")
        if len(pedidos) > TOPE_PAGINA:
            return error(400, "invalid_request_error", f"ids[] admite hasta {TOPE_PAGINA} archivos")
        if not all(cursor_valido(i) for i in pedidos):
            return error(400, "invalid_request_error", MAL_CURSOR)
        try:
            with anyio.fail_after(ESPERA_FIJADA_S):
                propios = await en_base(estado.hilos_lectura, objetos_entre, estado.motor, proyecto, "archivo",
                                        pedidos, abandonar=True)
        except Exception as e:  # noqa: BLE001 (incluye TimeoutError de fail_after)
            print(f"pasarela: no pude listar los objetos ({type(e).__name__})", file=sys.stderr)
            return error(503, "api_error", "no pude consultar la base; reintenta")
        vistas = [i for i in pedidos if i in propios]
        r = await traer_archivos(request, estado, [(i, propios[i]) for i in vistas])
        if isinstance(r, Response):
            return r
        encontrados, muertos = r
        await marcar_muertos(estado, proyecto, "archivo", muertos)
        return pagina_json([encontrados[i] for i in vistas if i in encontrados], vistas, False, None)
    n = limite_de(q)
    if n is None:
        return error(400, "invalid_request_error", "limit va de 1 a 1000")
    n = min(n, TOPE_PAGINA)
    despues = antes = None
    if "page" in q:
        if not q["page"].startswith("page_"):
            return error(400, "invalid_request_error", "page no es un cursor de la pasarela")
        despues = q["page"][5:]
    elif "after_id" in q:
        despues = q["after_id"]
    elif "before_id" in q:
        antes = q["before_id"]
    if not all(cursor_valido(c) for c in (despues, antes) if c is not None):
        return error(400, "invalid_request_error", MAL_CURSOR)
    r = await reunir(request, estado, proyecto, "archivo", n, despues, antes, traer_archivos)
    if isinstance(r, Response):
        return r
    vistas, encontrados, hay_mas = r
    siguiente = f"page_{vistas[-1]}" if vistas and (hay_mas or antes is not None) else None
    return pagina_json([encontrados[i] for i in vistas if i in encontrados], vistas, hay_mas, siguiente)


async def traer_lotes(request: Request, estado, filas: list[tuple[str, str]]):
    """(id → objeto de Anthropic, ids a los que Anthropic contestó 404 not_found_error), preguntando lote por lote a su
    cuenta, hasta LOTES_A_LA_VEZ a la vez: la lista de lotes de Anthropic no filtra por id. Un 404 es un lote que
    Anthropic ya no tiene: no sale y, si es not_found_error, se avisa para marcarlo. Regresa una respuesta
    de error si algo más falla."""
    encontrados: dict[str, dict] = {}
    muertos: set[str] = set()
    fallas: list[Response] = []

    async def uno(i: str, fuente: str) -> None:
        salida = estado.cliente.build_request("GET", f"{estado.config.anthropic_url}/v1/messages/batches/{i}",
                                              headers=cabeceras_salida(request, estado.llaves[fuente]))
        try:
            resp = await estado.cliente.send(salida)
        except httpx.PoolTimeout:
            fallas.append(sin_conexion())
            return
        except httpx.HTTPError as e:
            fallas.append(error(502, "api_error", f"no pude hablar con Anthropic ({type(e).__name__})"))
            return
        if resp.status_code == 404:
            if tipo_de_error(resp.content) == "not_found_error":
                muertos.add(i)
            return
        if resp.status_code >= 400:
            fallas.append(Response(resp.content, status_code=resp.status_code,
                                   headers=cabeceras_regreso(resp, fuente)))
            return
        try:
            o = resp.json()
        except ValueError:
            o = None
        if not isinstance(o, dict) or o.get("id") != i:
            fallas.append(error(502, "api_error", "Anthropic contestó algo que no es ese lote"))
            return
        encontrados[i] = o

    pendientes = [(i, fuente) for i, fuente in filas if fuente in estado.llaves]
    for k in range(0, len(pendientes), LOTES_A_LA_VEZ):
        async with anyio.create_task_group() as tg:
            for i, fuente in pendientes[k:k + LOTES_A_LA_VEZ]:
                tg.start_soon(uno, i, fuente)
        if fallas:
            return fallas[0]
    return encontrados, muertos


async def listar_lotes(request: Request, proyecto: str):
    """GET v1/messages/batches: los lotes que el proyecto creó por la pasarela, de todas las cuentas, del más nuevo
    al más viejo, con after_id, before_id y limit como en Anthropic. Da hasta TOPE_PAGINA_LOTES por
    página. Ignora beta (lo mandan los SDK viejos)."""
    estado = request.app.state.pia
    q = request.query_params
    raros = sorted(set(q.keys()) - PARAMS_LOTES)
    if raros:
        return error(400, "invalid_request_error", f"al listar lotes la pasarela no admite: {', '.join(raros)}")
    if "after_id" in q and "before_id" in q:
        return error(400, "invalid_request_error", "usa solo uno de after_id o before_id")
    n = limite_de(q)
    if n is None:
        return error(400, "invalid_request_error", "limit va de 1 a 1000")
    if not all(cursor_valido(q[k]) for k in ("after_id", "before_id") if k in q):
        return error(400, "invalid_request_error", MAL_CURSOR)
    r = await reunir(request, estado, proyecto, "lote", min(n, TOPE_PAGINA_LOTES), q.get("after_id"),
                     q.get("before_id"), traer_lotes)
    if isinstance(r, Response):
        return r
    vistas, encontrados, hay_mas = r
    cuerpo = {"data": [encontrados[i] for i in vistas if i in encontrados], "has_more": hay_mas,
              "first_id": vistas[0] if vistas else None, "last_id": vistas[-1] if vistas else None}
    crudo = reescribir_lote(json.dumps(cuerpo).encode(), base_publica(request), estado.config.anthropic_url)
    return Response(crudo, media_type="application/json", headers={"cache-control": "no-store"})


async def transmitir(estado, ctx: Contexto, resp: httpx.Response):
    lector = LectorSSE()
    registrando = True                    # tras una falla de la base no se insiste en este stream
    try:
        async for trozo in resp.aiter_bytes():
            if lector is not None:
                try:
                    lector.alimentar(trozo)
                except Exception as e:  # noqa: BLE001  medir nunca debe cortar el stream
                    print(f"pasarela: no pude medir el uso ({type(e).__name__})", file=sys.stderr)
                    lector = None
            if lector is not None and lector.archivos:                       # antes de entregar el trozo que los trae
                registrando = await registrar_o_avisar(estado, ctx, lector.sacar_archivos(), registrando)
            yield trozo
    finally:
        modelo, uso = (lector.modelo, lector.uso) if lector is not None else (None, {})
        with anyio.CancelScope(shield=True):          # aunque el cliente se desconecte, se cierra y se anota
            await resp.aclose()
            await despues(estado, escribir_fila,
                          fila_de_uso(ctx, resp.status_code, resp.headers.get("request-id"), modelo, uso))


async def transmitir_resultados(estado, ctx: Contexto, resp: httpx.Response, lote_id: str):
    lector = LectorJSONL()
    midiendo = True
    registrando = True                    # tras una falla de la base no se insiste: bajar otra vez los registra
    completo = False
    try:
        async for trozo in resp.aiter_bytes():
            if midiendo:
                try:
                    lector.alimentar(trozo)
                except Exception as e:  # noqa: BLE001  medir nunca debe cortar la descarga
                    print(f"pasarela: no pude medir los resultados del lote {lote_id} ({type(e).__name__})",
                          file=sys.stderr)
                    midiendo = False
            if lector.archivos:                                 # los de las líneas ya leídas
                registrando = await registrar_o_avisar(estado, ctx, lector.sacar_archivos(), registrando)
            yield trozo
        if midiendo:
            try:
                lector.terminar()
            except Exception as e:  # noqa: BLE001
                print(f"pasarela: no pude medir los resultados del lote {lote_id} ({type(e).__name__})",
                      file=sys.stderr)
            if lector.archivos:                                 # la última línea, si llegó sin salto
                await registrar_o_avisar(estado, ctx, lector.sacar_archivos(), registrando)
        completo = resp.status_code == 200
    finally:
        with anyio.CancelScope(shield=True):
            await resp.aclose()
            if completo:
                modelo = ",".join(sorted(m for m in lector.modelos if isinstance(m, str)))[:80] or None
                fila = fila_de(ctx, resp.status_code, resp.headers.get("request-id"), modelo,
                               lector.tokens, lector.micro, lote=True)
                await despues(estado, contar_lote, lote_id, fila)


async def pasar_tal_cual(resp: httpx.Response):
    try:
        async for trozo in resp.aiter_bytes():
            yield trozo
    finally:
        with anyio.CancelScope(shield=True):
            await resp.aclose()
