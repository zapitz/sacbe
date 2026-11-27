# Avisos firmados (webhooks)

Sacbé puede avisarle a otro programa lo que pasa con su gasto: un `POST` firmado a una URL tuya cada vez que ocurre algo. Sigue el formato de [Standard Webhooks](https://www.standardwebhooks.com/), así que hay librerías para verificar la firma en casi cualquier lenguaje.

Todo lo de aquí sale de `pasarela/webhooks.py`.

## Eventos

| Tipo | Cuándo sale | `datos` |
|---|---|---|
| `uso.diario` | Cada día, desde las 00:15 UTC, con el día anterior. Uno por proyecto suscrito, aunque no haya gastado nada. | `fecha`, `proyecto`, `peticiones`, `costo_usd`, `por_modelo` (lista con `modelo`, `peticiones`, `costo_usd`), `por_cuenta` (lista con `cuenta`, `costo_usd`) |
| `saldo.bajo` | Cuando Sacbé calcula que una concesión de crédito va a vencer con dinero sin usar: a 5 días y a 2 días. Es el mismo criterio de los avisos de `PIA_AVISOS_URL`. | `cuenta`, `concesion`, `quedan_usd`, `vence`, `se_pierden_usd`, `dias` |
| `cuenta.cambio` | Cuando cambia la organización de la que se está gastando. | `de`, `a`, `motivo` (hoy siempre `null`) |
| `llave.rotada` | Cuando rotan o dan de baja la llave de un proyecto suscrito. Nunca trae la llave. | `proyecto`, `accion` (`rotar` o `baja`), `huella` (últimos 4 caracteres), `por`, `creado` |
| `prueba` | Solo con `ops/pia webhook prueba <nombre>`. | `mensaje` |

Los montos van en USD, como texto con 4 decimales (`"3.1875"`). Las fechas y horas van en UTC.

## El sobre

```json
{"id":"evt_5d1c...","tipo":"uso.diario","creado":"2026-11-26T00:15:02Z","datos":{"fecha":"2026-11-25","proyecto":"facturas","peticiones":412,"costo_usd":"3.1875","por_modelo":[{"modelo":"claude-sonnet-4-5","peticiones":380,"costo_usd":"3.0102"}],"por_cuenta":[{"cuenta":"org1","costo_usd":"3.1875"}]}}
```

## La firma

Tres encabezados:

- `webhook-id`: el mismo valor que el campo `id`. Sirve para descartar repetidos.
- `webhook-timestamp`: segundos Unix del envío.
- `webhook-signature`: `v1,` seguido del base64 de `HMAC-SHA256(llave, "{webhook-id}.{webhook-timestamp}.{cuerpo}")`. El cuerpo son los bytes exactos que llegaron. La llave son los bytes de decodificar en base64 lo que va después de `whsec_` en el secreto.

Durante las 24 horas después de rotar el secreto, Sacbé firma con los dos y los separa con un espacio: `v1,abc... v1,def...`. Acepta el aviso si cualquiera de las dos coincide.

## Qué debe hacer tu receptor

1. Calcular la firma y compararla en tiempo constante (`hmac.compare_digest` en Python).
2. Rechazar si `webhook-timestamp` está a más de 5 minutos de tu reloj.
3. Guardar los `webhook-id` vistos al menos 24 horas y no procesar dos veces el mismo.
4. Contestar 2xx en menos de 10 segundos y hacer el trabajo después.
5. Ordenar por el campo `creado`: el orden de llegada no está garantizado.

Ejemplo en Python, sin librerías aparte:

```python
import base64, hashlib, hmac, time

def verificar(secreto: str, encabezados: dict, cuerpo: bytes) -> bool:
    id_ = encabezados["webhook-id"]
    ts = int(encabezados["webhook-timestamp"])
    if abs(time.time() - ts) > 300:
        return False
    llave = base64.b64decode(secreto.removeprefix("whsec_"))
    esperada = base64.b64encode(hmac.new(llave, f"{id_}.{ts}.".encode() + cuerpo, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(f.removeprefix("v1,"), esperada)
               for f in encabezados["webhook-signature"].split(" ") if f.startswith("v1,"))
```

## Reintentos

Si tu receptor no contesta 2xx, o tarda más de 10 segundos, Sacbé reintenta a 1 minuto, 5 minutos, 30 minutos, 2 horas y 6 horas. Los envía el vigía, que corre cada 5 minutos, así que cada reintento sale en la primera vuelta después de esa espera. Todos los reintentos usan el mismo `webhook-id`, con una firma nueva por la hora del envío.

Después del último intento, el aviso queda como fallido y Sacbé escribe un aviso en su tabla para que el vigía lo mande a `PIA_AVISOS_URL`.

Sacbé no sigue redirecciones.

## Dar de alta una suscripción

Genera el secreto en tu máquina y guárdalo en un archivo que solo tú puedas leer:

```bash
umask 077
python3 -c 'import secrets,base64;print("whsec_"+base64.b64encode(secrets.token_bytes(32)).decode())' > ~/.config/sacbe/facturas.whsec
```

Dalo de alta pasándolo por la entrada estándar, así no aparece en la terminal ni en el historial:

```bash
ops/pia webhook alta facturas --url https://facturas.example.com/webhooks/sacbe \
  --proyectos facturas-api,facturas-tareas --por "tu nombre" --cita "por qué" < ~/.config/sacbe/facturas.whsec
```

- `--eventos` elige cuáles; por defecto van `uso.diario`, `saldo.bajo`, `cuenta.cambio` y `llave.rotada`.
- `--proyectos` dice de qué proyectos salen `uso.diario` y `llave.rotada`. `saldo.bajo` y `cuenta.cambio` son de todo el gestor.
- La URL debe ser `https://`. Solo se acepta `http://` hacia un nombre interno sin puntos (por ejemplo `http://facturas-api:8000/...`), para un programa en la misma red de Docker.
- El alta no manda la historia vieja. El primer `uso.diario` es el de ayer y sale en la siguiente vuelta del vigía.

Pasa el mismo archivo al programa que recibe, también por la entrada estándar o por su gestor de secretos.

Las demás órdenes:

```bash
ops/pia webhook prueba facturas                       # un aviso de tipo prueba, a la cola
ops/pia webhook lista                                 # suscripciones, huella del secreto y cuántas entregas van
ops/pia webhook rotar facturas --por … --cita … < nuevo.whsec   # 24 h con doble firma
ops/pia webhook cambiar facturas --url … | --eventos … | --proyectos … | --activo no --por … --cita …
```

Cada alta, rotación y cambio queda en la bitácora (`ops/pia cambios`) con la huella del secreto, nunca con el secreto.

## Lo que guarda Sacbé

- El secreto vive en la tabla `webhook`, en claro, porque hace falta para firmar. Trata la base como trataste las llaves de Anthropic.
- Cada envío queda en `webhook_entrega` con el sobre, el número de intentos y el último resultado (`HTTP 200`, `ConnectTimeout`...). No guarda lo que contestó tu receptor.
