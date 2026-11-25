"""Catálogo de modelos y alias «lo más nuevo».
La lista sale de GET /v1/models de una cuenta activa cada PIA_CATALOGO_S segundos (6 h) y se cruza con
pasarela/precios.py: un modelo sin precio no aparece, para que el gasto siempre cuadre. La última lista buena vive
en la tabla modelo y se lee al arrancar; alias_modelo guarda a qué apunta cada alias para avisar cuando cambia.
Este módulo no importa proxy: proxy lo importa a él."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from .avisos import Aviso, escribir
from .esquema import alias_modelo, modelo
from .precios import PRECIOS, normalizar

ALIAS = {"claude-ultimo-opus": "opus", "claude-ultimo-sonnet": "sonnet", "claude-ultimo-haiku": "haiku"}
PREFIJO_ALIAS = b"claude-ultimo-"
RUTAS_ALIAS = {"v1/messages", "v1/messages/count_tokens", "v1/messages/batches"}
FAMILIA = re.compile(r"^claude-([a-z]{1,20})-\d")      # 20 letras: cabe en modelo.familia
VERSION_API = "2023-06-01"
PAGINAS_MAX = 10                      # 1,000 modelos por página: diez páginas es más de lo que existe
ESPERA_S = 30.0                       # tope de cada GET /v1/models
MUY_VIEJO = datetime(2000, 1, 1)      # created_at que no se pudo leer


@dataclass(frozen=True)
class Modelo:
    id: str
    nombre: str
    familia: str | None
    creado: datetime                  # UTC sin zona


@dataclass(frozen=True)
class Lista:
    modelos: tuple[Modelo, ...]       # solo los que tienen precio, el más nuevo primero
    alias: dict[str, str]             # alias -> id real; falta el alias si su familia no tiene modelo con precio
    sin_precio: tuple[Modelo, ...]    # sin precio y más nuevos que todo lo de su familia que sí tiene
    ts: datetime                      # cuándo se leyó de Anthropic


class ErrorModelos(Exception):
    pass


class AliasInvalido(Exception):
    def __init__(self, status: int, tipo: str, mensaje: str):
        super().__init__(mensaje)
        self.status, self.tipo, self.mensaje = status, tipo, mensaje


def familia_de(id_: str) -> str | None:
    m = FAMILIA.match(id_)
    return m.group(1) if m else None


def con_precio(m: Modelo) -> bool:
    return normalizar(m.id) in PRECIOS


def _fecha(texto) -> datetime:
    try:
        d = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return MUY_VIEJO
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def de_anthropic(data: list) -> list[Modelo]:
    """Los modelos de una página de /v1/models. Lo que no tiene forma de modelo se ignora; un id repetido cuenta
    una vez."""
    vistos: dict[str, Modelo] = {}
    for o in data:
        if not isinstance(o, dict):
            continue
        i = o.get("id")
        if not isinstance(i, str) or not i or len(i) > 80:
            continue
        nombre = o.get("display_name")
        nombre = nombre.strip()[:120] if isinstance(nombre, str) and nombre.strip() else i
        vistos[i] = Modelo(i, nombre, familia_de(i), _fecha(o.get("created_at")))
    return list(vistos.values())


def armar(vistos: list[Modelo], ts: datetime) -> Lista:
    """Cruza con PRECIOS, ordena del más nuevo al más viejo y elige el destino de cada alias."""
    con = sorted((m for m in vistos if con_precio(m)), key=lambda m: (m.creado, m.id), reverse=True)
    alias = {}
    for a, familia in ALIAS.items():
        m = next((m for m in con if m.familia == familia), None)
        if m is not None:
            alias[a] = m.id
    mas_nuevo: dict[str, datetime] = {}
    for m in con:
        if m.familia and m.familia not in mas_nuevo:
            mas_nuevo[m.familia] = m.creado
    sin = sorted((m for m in vistos if not con_precio(m) and m.familia
                  and (m.familia not in mas_nuevo or m.creado > mas_nuevo[m.familia])), key=lambda m: m.id)
    return Lista(tuple(con), alias, tuple(sin), ts)


async def traer(cliente, url_base: str, llave: str) -> list[Modelo]:
    """Todas las páginas de GET /v1/models con la llave de una cuenta. Una respuesta rara es ErrorModelos, cuyo
    mensaje nunca lleva la llave; las fallas de red o de cabeceras pasan como excepciones de httpx, y su texto sí
    puede traerla, así que quien llama imprime solo su tipo."""
    vistos: list[Modelo] = []
    despues = None
    for _ in range(PAGINAS_MAX):
        params = {"limit": 1000, **({"after_id": despues} if despues else {})}
        r = await cliente.get(f"{url_base}/v1/models", params=params, timeout=ESPERA_S,
                              headers={"x-api-key": llave, "anthropic-version": VERSION_API})
        if r.status_code != 200:
            raise ErrorModelos(f"Anthropic contestó {r.status_code}")
        try:
            d = r.json()
        except ValueError:
            raise ErrorModelos("la respuesta no es JSON") from None
        if not isinstance(d, dict) or not isinstance(d.get("data"), list):
            raise ErrorModelos("la respuesta no trae data")
        vistos += de_anthropic(d["data"])
        if not d.get("has_more"):
            return vistos
        despues = d.get("last_id")
        if not isinstance(despues, str) or not despues:
            raise ErrorModelos("has_more sin last_id")
    raise ErrorModelos(f"más de {PAGINAS_MAX} páginas")


def cargar_modelos(motor) -> tuple[list[Modelo], datetime | None]:
    """La última lista guardada y cuándo se leyó. Al arrancar sirve aunque Anthropic no conteste."""
    with motor.connect() as c:
        filas = c.execute(select(modelo)).all()
    return ([Modelo(f.id, f.nombre, f.familia, f.creado) for f in filas],
            max((f.visto for f in filas), default=None))


def _texto_sin_precio(m: Modelo) -> str:
    alias = f"claude-ultimo-{m.familia}"
    ni = f" ni en {alias}" if alias in ALIAS else ""
    return (f"pasarela-ia: Anthropic ya tiene el modelo {m.id} y la pasarela no sabe su precio. No sale en el "
            f"catálogo{ni} hasta agregarlo a pasarela/precios.py.")


def _mover_alias(c, a: str, previo: str, destino: str, t: datetime) -> bool:
    """Cambia el alias de `previo` a `destino` solo si en la base sigue apuntando a `previo`. Si otro contenedor
    ya lo movió, no toca nada y no avisa. Quien lo mueve deja el aviso y su fila en cambio."""
    n = c.execute(update(alias_modelo)
                  .where(alias_modelo.c.alias == a, alias_modelo.c.apunta_a == previo)
                  .values(apunta_a=destino, desde=t)).rowcount
    if not n:
        return False
    escribir(c, Aviso(
        f"alias:{a}:{previo}:{destino}:{t:%Y-%m-%d}",
        f"pasarela-ia: {a} ahora apunta a {destino} (antes {previo}). Los programas que lo usan ya "
        f"reciben el modelo nuevo.",
        {"accion": "alias-cambio", "alias": a, "antes": previo, "ahora": destino}))
    return True


def guardar_modelos(motor, vistos: list[Modelo], lista: Lista, t: datetime) -> list[str]:
    """En una transacción: la tabla modelo queda igual a `vistos`, alias_modelo se pone al día y salen los avisos.
    - Un alias visto por primera vez solo se guarda.
    - Un alias que cambió de destino deja aviso y su fila en cambio (pasarela automático). El UPDATE va condicionado
      al destino de antes: si api y panel refrescan a la vez, solo uno lo cambia y solo ese avisa.
    - Cada modelo de lista.sin_precio deja un aviso, una sola vez en la vida (clave sin-precio:<id>).
    Todo se escribe de forma que dos contenedores a la vez no choquen. Regresa los alias que cambiaron."""
    cambiados: list[str] = []
    ids = [m.id for m in vistos]
    with motor.begin() as c:
        c.execute(delete(modelo).where(modelo.c.id.not_in(ids)))
        for m in vistos:
            valores = {"nombre": m.nombre, "familia": m.familia, "creado": m.creado, "visto": t}
            if not c.execute(update(modelo).where(modelo.c.id == m.id).values(**valores)).rowcount:
                try:
                    with c.begin_nested():
                        c.execute(insert(modelo).values(id=m.id, **valores))
                except IntegrityError:
                    pass                  # lo insertó el otro contenedor en este momento
        antes = dict(c.execute(select(alias_modelo.c.alias, alias_modelo.c.apunta_a)).all())
        for a, destino in sorted(lista.alias.items()):
            previo = antes.get(a)
            if previo is None:
                try:
                    with c.begin_nested():
                        c.execute(insert(alias_modelo).values(alias=a, apunta_a=destino, desde=t))
                except IntegrityError:
                    pass
            elif previo != destino and _mover_alias(c, a, previo, destino, t):
                cambiados.append(a)
        for m in lista.sin_precio:
            escribir(c, Aviso(f"sin-precio:{m.id}", _texto_sin_precio(m)))
    return cambiados


class Modelos:
    """Lo que consultan el proxy y /v1/catalogo: la última lista, en memoria. Se cambia de un golpe (un solo
    objeto), así que una petición nunca ve media lista."""

    def __init__(self):
        self.lista: Lista | None = None

    @property
    def cargado(self) -> bool:
        return self.lista is not None

    @property
    def alias(self) -> dict[str, str] | None:
        return self.lista.alias if self.lista is not None else None

    def poner(self, lista: Lista) -> None:
        self.lista = lista

    def catalogo(self) -> dict:
        """El cuerpo de GET /v1/catalogo."""
        lista = self.lista
        modelos_ = []
        for m in lista.modelos:
            p = PRECIOS[normalizar(m.id)]
            modelos_.append({"id": m.id, "nombre": m.nombre, "familia": m.familia,
                             "precio_usd_mtok": {"entrada": float(p.entrada), "salida": float(p.salida)}})
        return {"marcas": [{"id": "anthropic", "nombre": "Anthropic", "ruta": "/anthropic", "modelos": modelos_,
                            "alias": [{"id": a, "apunta_a": d} for a, d in sorted(lista.alias.items())]}],
                "actualizado": f"{lista.ts:%Y-%m-%dT%H:%M:%SZ}"}


def reescribir(cuerpo: bytes, ruta: str, alias: dict[str, str] | None) -> bytes:
    """Cambia claude-ultimo-* por el modelo real en el `model` de /v1/messages y count_tokens, y en el
    `params.model` de cada petición de un lote. Sin un alias, el cuerpo sale idéntico byte por byte.
    - Sin el texto «claude-ultimo-» en los bytes no se lee el JSON. Un alias escrito con escapes \\u no se ve y
      llega tal cual a Anthropic, que contesta 404: no abre nada, a diferencia de un file_id escondido.
    - Si se lee: object_pairs_hook marca los objetos con la llave model repetida (400 si ahí hay un alias),
      RecursionError es 400 y un cuerpo que no es JSON pasa tal cual (Anthropic lo rechaza).
    - Alias pedido sin catálogo cargado: 503. Alias cuya familia no tiene modelo con precio: 400.
    Un alias que no es de ALIAS (claude-ultimo-fable) pasa tal cual."""
    if ruta not in RUTAS_ALIAS or PREFIJO_ALIAS not in cuerpo:
        return cuerpo
    repetidos: list[dict] = []

    def objeto(pares):
        d = {}
        for k, v in pares:
            if k == "model" and k in d:
                repetidos.append(d)
            d[k] = v
        return d

    try:
        datos = json.loads(cuerpo, object_pairs_hook=objeto)
    except RecursionError:
        raise AliasInvalido(400, "invalid_request_error", "el JSON tiene demasiados niveles de anidación") from None
    except ValueError:
        return cuerpo
    if not isinstance(datos, dict):
        return cuerpo
    if ruta == "v1/messages/batches":
        peticiones = datos.get("requests")
        sitios = [p["params"] for p in peticiones if isinstance(p, dict) and isinstance(p.get("params"), dict)] \
            if isinstance(peticiones, list) else []
    else:
        sitios = [datos]
    cambio = False
    for s in sitios:
        pedido = s.get("model")
        if not (isinstance(pedido, str) and pedido in ALIAS):
            continue
        if any(s is r for r in repetidos):
            raise AliasInvalido(400, "invalid_request_error", "la llave model se repite en el mismo objeto")
        if alias is None:
            raise AliasInvalido(503, "api_error", "todavía no cargo el catálogo de modelos; reintenta")
        destino = alias.get(pedido)
        if destino is None:
            raise AliasInvalido(400, "invalid_request_error", f"{pedido} no apunta a ningún modelo con precio")
        s["model"] = destino
        cambio = True
    if not cambio:
        return cuerpo
    try:
        return json.dumps(datos, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (UnicodeEncodeError, ValueError):      # surrogado suelto o número fuera de rango (1e400)
        raise AliasInvalido(400, "invalid_request_error",
                            "el JSON trae un texto o un número que no se puede reenviar") from None
