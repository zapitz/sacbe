"""Avisos firmados (webhooks) hacia otros programas, con el formato de Standard Webhooks (standardwebhooks.com).

Sobre: {"id": "evt_...", "tipo": "...", "creado": "AAAA-MM-DDTHH:MM:SSZ", "datos": {...}}. Montos en texto con 4
decimales (USD); horas en UTC. Encabezados webhook-id, webhook-timestamp y webhook-signature, que es "v1," + base64 de
HMAC-SHA256(llave, "{id}.{timestamp}.{cuerpo}"), con la llave = bytes de base64 después de "whsec_". Al rotar, durante
24 h se firma con los dos secretos, separados por espacio.

Eventos (cada suscripción elige cuáles y de qué proyectos):
  uso.diario     el día anterior, uno por proyecto suscrito, desde las 00:15 UTC
  saldo.bajo     cuando nace un aviso «perdida:<concesión>:<5d|2d>» (el mismo criterio que los avisos de PIA_AVISOS_URL)
  cuenta.cambio  cuando cambia la cuenta en uso (fila «cuenta-en-uso» de la bitácora)
  llave.rotada   cuando rotan o dan de baja la llave de un proyecto suscrito; nunca trae la llave
  prueba         solo con `webhook prueba`

Nada de esto corre dentro de las peticiones: `generar` lee las tablas aviso, cambio y registro desde un cursor y
llena webhook_entrega; `entregar` manda lo que toca. Las dos las corre ops/vigia.sh cada 5 minutos.
Reintentos: 1 min, 5 min, 30 min, 2 h y 6 h (en la práctica a la siguiente vuelta del vigía). Después, fallido y
aviso por PIA_AVISOS_URL. Los reintentos usan el mismo webhook-id y una firma nueva con la hora del envío.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from .esquema import ajuste, aviso, cambio, proyecto, registro, webhook, webhook_entrega
from .tiempo import ahora

EVENTOS = ("uso.diario", "saldo.bajo", "cuenta.cambio", "llave.rotada")
REINTENTOS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2),
              timedelta(hours=6))
TIMEOUT_S = 10.0
DOBLE_FIRMA = timedelta(hours=24)
HORA_DIARIO = timedelta(minutes=15)
DIAS_ATRAS_MAX = 3                   # si el vigía estuvo parado, uso.diario se pone al día hasta 3 días
POR_VUELTA = 20
SECRETO = re.compile(r"whsec_[A-Za-z0-9+/]{43}=")     # 32 bytes en base64
URL = re.compile(r"(https://[^\s/]+|http://[a-z0-9][a-z0-9-]*(:\d+)?)(/\S*)?")   # http solo a un nombre interno


def nuevo_secreto() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def firmar(secreto: str, id_: str, ts: int, cuerpo: bytes) -> str:
    llave = base64.b64decode(secreto.removeprefix("whsec_"))
    mac = hmac.new(llave, f"{id_}.{ts}.".encode() + cuerpo, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def encabezados(secretos: list[str], id_: str, ts: int, cuerpo: bytes) -> dict:
    return {"content-type": "application/json", "user-agent": "sacbe-webhooks/1",
            "webhook-id": id_, "webhook-timestamp": str(ts),
            "webhook-signature": " ".join(firmar(s, id_, ts, cuerpo) for s in secretos)}


def sobre(id_: str, tipo: str, creado: datetime, datos: dict) -> str:
    return json.dumps({"id": id_, "tipo": tipo, "creado": f"{creado:%Y-%m-%dT%H:%M:%SZ}", "datos": datos},
                      ensure_ascii=False, separators=(",", ":"))


def id_evento(suscripcion: str, llave: str) -> str:
    """Determinista: si generar corre dos veces sobre lo mismo, la llave única de webhook_entrega lo descarta."""
    return "evt_" + hashlib.sha256(f"{suscripcion}|{llave}".encode()).hexdigest()[:26]


def monto(micro: int) -> str:
    return f"{micro / 1_000_000:.4f}"


def leer_secreto(texto: str) -> str:
    s = texto.strip()
    if not SECRETO.fullmatch(s):
        raise SystemExit("El secreto debe ser whsec_ + base64 de 32 bytes, en una sola línea.")
    return s


def _lista(texto: str, validos=None) -> str:
    partes = [p.strip() for p in texto.split(",") if p.strip()]
    if validos is not None:
        malos = [p for p in partes if p not in validos]
        if malos:
            raise SystemExit(f"Eventos que no existen: {', '.join(malos)}. Son: {', '.join(validos)}.")
    return ",".join(partes)


# --- suscripciones ---------------------------------------------------------------------------------------------

def _cursor(c, clave: str) -> int:
    return c.execute(select(ajuste.c.valor).where(ajuste.c.clave == clave)).scalar() or 0


def _poner_cursor(c, clave: str, valor: int, t: datetime) -> None:
    if c.execute(update(ajuste).where(ajuste.c.clave == clave).values(valor=valor, actualizado=t)).rowcount == 0:
        c.execute(insert(ajuste).values(clave=clave, valor=valor, actualizado=t))


def _claves(nombre: str) -> tuple[str, str, str]:
    return f"wh:{nombre}:aviso", f"wh:{nombre}:cambio", f"wh:{nombre}:diario"


def alta(motor, nombre: str, url: str, secreto: str, eventos: str, proyectos: str, cambio_fn, t=None) -> None:
    """No repite avisos ni cambios viejos. El primer uso.diario es el de ayer (sale en la siguiente vuelta después de las
    00:15 UTC)."""
    t = t or ahora()
    if not URL.fullmatch(url):
        raise SystemExit("La URL debe ser https://... (o http://nombre-interno:puerto/... dentro de tu servidor).")
    secreto = leer_secreto(secreto)
    ev = _lista(eventos, EVENTOS)
    pr = _lista(proyectos)
    with motor.begin() as c:
        faltan = set(pr.split(",")) - set(c.execute(select(proyecto.c.nombre)).scalars()) if pr else set()
        if faltan:
            raise SystemExit(f"No existen los proyectos: {', '.join(sorted(faltan))}.")
        try:
            c.execute(insert(webhook).values(nombre=nombre, url=url, secreto=secreto, secreto_anterior=None,
                                             anterior_hasta=None, eventos=ev, proyectos=pr, activo=True, creado=t))
        except IntegrityError:
            raise SystemExit(f"Ya existe la suscripción {nombre}; usa webhook rotar o webhook cambiar.") from None
        ca, cc, cd = _claves(nombre)
        _poner_cursor(c, ca, c.execute(select(func.max(aviso.c.id))).scalar() or 0, t)
        _poner_cursor(c, cc, c.execute(select(func.max(cambio.c.id))).scalar() or 0, t)
        _poner_cursor(c, cd, int(f"{t - timedelta(days=2):%Y%m%d}"), t)   # ayer sí se manda
        cambio_fn(c, "webhook-alta", {"nombre": nombre, "url": url, "eventos": ev, "proyectos": pr,
                                      "huella": secreto[-4:]})


def rotar(motor, nombre: str, secreto: str, cambio_fn, t=None) -> None:
    t = t or ahora()
    secreto = leer_secreto(secreto)
    with motor.begin() as c:
        viejo = c.execute(select(webhook.c.secreto).where(webhook.c.nombre == nombre)).scalar()
        if viejo is None:
            raise SystemExit(f"No existe la suscripción {nombre}.")
        c.execute(update(webhook).where(webhook.c.nombre == nombre).values(
            secreto=secreto, secreto_anterior=viejo, anterior_hasta=t + DOBLE_FIRMA))
        cambio_fn(c, "webhook-rotar", {"nombre": nombre, "huella": secreto[-4:], "anterior": viejo[-4:]})


def cambiar(motor, nombre: str, cambio_fn, url=None, eventos=None, proyectos=None, activo=None) -> None:
    valores = {}
    if url is not None:
        if not URL.fullmatch(url):
            raise SystemExit("La URL debe ser https://... (o http://nombre-interno:puerto/...).")
        valores["url"] = url
    if eventos is not None:
        valores["eventos"] = _lista(eventos, EVENTOS)
    if proyectos is not None:
        valores["proyectos"] = _lista(proyectos)
    if activo is not None:
        valores["activo"] = activo
    with motor.begin() as c:
        if not c.execute(update(webhook).where(webhook.c.nombre == nombre).values(**valores)).rowcount:
            raise SystemExit(f"No existe la suscripción {nombre}.")
        cambio_fn(c, "webhook-cambiar", {"nombre": nombre, **valores})


def lista(motor) -> list[dict]:
    with motor.connect() as c:
        subs = [dict(f._mapping) for f in c.execute(select(webhook).order_by(webhook.c.nombre))]
        for s in subs:
            cuenta = dict(c.execute(select(webhook_entrega.c.estado, func.count())
                                    .where(webhook_entrega.c.webhook == s["nombre"])
                                    .group_by(webhook_entrega.c.estado)).all())
            s["entregas"] = cuenta
            s["huella"] = s.pop("secreto")[-4:]
            s.pop("secreto_anterior")
    return subs


# --- generar -----------------------------------------------------------------------------------------------------

def _encolar(c, sus: str, llave: str, tipo: str, creado: datetime, datos: dict, t: datetime) -> bool:
    id_ = id_evento(sus, llave)
    try:
        with c.begin_nested():
            c.execute(insert(webhook_entrega).values(
                evento=id_, webhook=sus, tipo=tipo, cuerpo=sobre(id_, tipo, creado, datos), creado=creado,
                intentos=0, proximo=t, estado="pendiente", ultimo=None))
    except IntegrityError:
        return False
    return True


def encolar_prueba(motor, nombre: str, t=None) -> str:
    t = t or ahora()
    with motor.begin() as c:
        if c.execute(select(webhook.c.nombre).where(webhook.c.nombre == nombre)).scalar() is None:
            raise SystemExit(f"No existe la suscripción {nombre}.")
        llave = f"prueba:{t.isoformat()}"
        _encolar(c, nombre, llave, "prueba", t, {"mensaje": "aviso de prueba de Sacbé"}, t)
    return id_evento(nombre, llave)


def _uso_dia(c, proyecto_: str, dia: datetime) -> dict:
    fin = dia + timedelta(days=1)
    cond = (registro.c.proyecto == proyecto_, registro.c.ts >= dia, registro.c.ts < fin)
    pet, micro = c.execute(select(func.count(), func.coalesce(func.sum(registro.c.costo_micro), 0))
                           .where(*cond)).one()
    por_modelo = [{"modelo": m or "sin modelo", "peticiones": n, "costo_usd": monto(int(s))} for m, n, s in c.execute(
        select(registro.c.modelo, func.count(), func.coalesce(func.sum(registro.c.costo_micro), 0))
        .where(*cond).group_by(registro.c.modelo).order_by(func.sum(registro.c.costo_micro).desc()))]
    por_cuenta = [{"cuenta": f, "costo_usd": monto(int(s))} for f, s in c.execute(
        select(registro.c.fuente, func.coalesce(func.sum(registro.c.costo_micro), 0))
        .where(*cond).group_by(registro.c.fuente).order_by(registro.c.fuente))]
    return {"fecha": f"{dia:%Y-%m-%d}", "proyecto": proyecto_, "peticiones": int(pet), "costo_usd": monto(int(micro)),
            "por_modelo": por_modelo, "por_cuenta": por_cuenta}


PERDIDA = re.compile(r"perdida:(\d+):(5d|2d)")


def generar(motor, t=None, concesiones=None) -> int:
    """Llena webhook_entrega. `concesiones(t)` da {id: EstadoConcesion} para saldo.bajo (admin le pasa la bolsa)."""
    t = t or ahora()
    nuevos = 0
    with motor.connect() as c:
        subs = [dict(f._mapping) for f in c.execute(select(webhook).where(webhook.c.activo.is_(True)))]
    estados = None
    for s in subs:
        nombre = s["nombre"]
        ev = set(s["eventos"].split(",")) if s["eventos"] else set()
        pr = set(s["proyectos"].split(",")) if s["proyectos"] else set()
        ca, cc, cd = _claves(nombre)
        with motor.begin() as c:
            # Avisos: saldo.bajo
            desde = _cursor(c, ca)
            filas = c.execute(select(aviso.c.id, aviso.c.ts, aviso.c.clave_unica).where(aviso.c.id > desde)
                              .order_by(aviso.c.id)).all()
            for id_, ts, clave in filas:
                m = PERDIDA.fullmatch(clave)
                if m and "saldo.bajo" in ev:
                    if estados is None:
                        estados = concesiones(t) if concesiones else {}
                    e = estados.get(int(m.group(1)))
                    if e is not None:
                        nuevos += _encolar(c, nombre, f"aviso:{id_}", "saldo.bajo", ts, {
                            "cuenta": e.cuenta, "concesion": e.id, "quedan_usd": monto(e.restante_micro),
                            "vence": f"{e.vence:%Y-%m-%d}", "se_pierden_usd": monto(e.perdida_micro),
                            "dias": (e.vence - t.date()).days}, t)
            if filas:
                _poner_cursor(c, ca, filas[-1][0], t)
            # Bitácora: cuenta.cambio y llave.rotada
            desde = _cursor(c, cc)
            filas = c.execute(select(cambio).where(cambio.c.id > desde).order_by(cambio.c.id)).all()
            for f in filas:
                try:
                    d = json.loads(f.detalle)
                except ValueError:
                    d = {}
                if f.accion == "cuenta-en-uso" and "cuenta.cambio" in ev:
                    nuevos += _encolar(c, nombre, f"cambio:{f.id}", "cuenta.cambio", f.ts,
                                       {"de": d.get("antes"), "a": d.get("ahora"), "motivo": d.get("motivo")}, t)
                elif f.accion in ("proyecto-rotar", "proyecto-baja") and "llave.rotada" in ev \
                        and d.get("nombre") in pr:
                    huella = d.get("huella") or c.execute(select(proyecto.c.huella)
                                                          .where(proyecto.c.nombre == d["nombre"])).scalar()
                    nuevos += _encolar(c, nombre, f"cambio:{f.id}", "llave.rotada", f.ts, {
                        "proyecto": d["nombre"], "accion": "rotar" if f.accion == "proyecto-rotar" else "baja",
                        "huella": huella, "por": f.ordenado_por, "creado": f"{f.ts:%Y-%m-%dT%H:%M:%SZ}"}, t)
            if filas:
                _poner_cursor(c, cc, filas[-1].id, t)
            # uso.diario
            hoy = t.replace(hour=0, minute=0, second=0, microsecond=0)
            if "uso.diario" in ev and pr and t >= hoy + HORA_DIARIO:
                ultimo = _cursor(c, cd)
                ayer = hoy - timedelta(days=1)
                dia = max(ayer - timedelta(days=DIAS_ATRAS_MAX - 1),
                          datetime.strptime(str(ultimo), "%Y%m%d") + timedelta(days=1) if ultimo else ayer)
                while dia <= ayer:
                    for p in sorted(pr):
                        nuevos += _encolar(c, nombre, f"diario:{p}:{dia:%Y-%m-%d}", "uso.diario", t,
                                           _uso_dia(c, p, dia), t)
                    _poner_cursor(c, cd, int(f"{dia:%Y%m%d}"), t)
                    dia += timedelta(days=1)
    return nuevos


# --- entregar ----------------------------------------------------------------------------------------------------

def _secretos(s: dict, t: datetime) -> list[str]:
    lista_ = [s["secreto"]]
    if s.get("secreto_anterior") and s.get("anterior_hasta") and t < s["anterior_hasta"]:
        lista_.append(s["secreto_anterior"])
    return lista_


def enviar_http(url: str, cuerpo: bytes, headers: dict) -> tuple[int | None, str]:
    import httpx
    try:
        r = httpx.post(url, content=cuerpo, headers=headers, timeout=TIMEOUT_S, follow_redirects=False)
        return r.status_code, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return None, type(e).__name__


def entregar(motor, t=None, enviar=enviar_http, avisar=None, reloj=time.time) -> dict:
    """Manda lo pendiente que ya toca. `avisar(clave, texto)` escribe un aviso (tabla aviso) al marcar un fallido."""
    t = t or ahora()
    res = {"entregados": 0, "reintentos": 0, "fallidos": 0}
    with motor.connect() as c:
        subs = {f.nombre: dict(f._mapping) for f in c.execute(select(webhook))}
        filas = c.execute(select(webhook_entrega).where(webhook_entrega.c.estado == "pendiente",
                                                        webhook_entrega.c.proximo <= t)
                          .order_by(webhook_entrega.c.id).limit(POR_VUELTA)).all()
    for f in filas:
        s = subs.get(f.webhook)
        if s is None or not s["activo"]:
            continue
        cuerpo = f.cuerpo.encode()
        estado, detalle = enviar(s["url"], cuerpo, encabezados(_secretos(s, t), f.evento, int(reloj()), cuerpo))
        intentos = f.intentos + 1
        if estado is not None and 200 <= estado < 300:
            valores = {"estado": "entregado", "intentos": intentos, "proximo": None, "ultimo": detalle}
            res["entregados"] += 1
        elif intentos > len(REINTENTOS):
            valores = {"estado": "fallido", "intentos": intentos, "proximo": None, "ultimo": detalle}
            res["fallidos"] += 1
            if avisar:
                avisar(f"webhook-fallido:{f.id}",
                       f"pasarela-ia: el aviso {f.tipo} para {f.webhook} no se pudo entregar después de "
                       f"{intentos} intentos (último: {detalle}). Revisa su receptor: {s['url']}")
        else:
            valores = {"intentos": intentos, "proximo": t + REINTENTOS[intentos - 1], "ultimo": detalle}
            res["reintentos"] += 1
        with motor.begin() as c:
            c.execute(update(webhook_entrega).where(webhook_entrega.c.id == f.id).values(**valores))
    return res
