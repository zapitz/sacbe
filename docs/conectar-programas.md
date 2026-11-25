# Conectar programas

Este es el contrato entre tus programas y Sacbé. Todo lo de aquí sale del código: `pasarela/proxy.py`, `pasarela/puerta.py`, `pasarela/llaves.py` y `ops/cliente-python/sitecustomize.py`.

## URL base y llave

El gestor imita la API de Anthropic bajo el prefijo `/anthropic`:

| Desde dónde | URL base |
|---|---|
| Un contenedor en la misma red que el gestor | `http://pasarela-ia-api:8080/anthropic` |
| Internet, por tu proxy inverso | `https://ia.example.com/anthropic` |

La llave `pia_...` del programa se usa como `api_key` normal del SDK. El gestor la acepta en `x-api-key` o en `Authorization: Bearer`, igual que Anthropic. Las cabeceras `anthropic-version` y `anthropic-beta` pasan tal cual.

Un programa de clase `extra` nunca llega a una organización con tarjeta. Uno de clase `produccion` sí, cuando se acaba el crédito de las demás.

## Rutas que acepta

Bajo `/anthropic`, con los métodos `GET`, `POST` y `DELETE` (lista `RUTAS` de `pasarela/proxy.py`):

| Ruta | Para qué |
|---|---|
| `/v1/messages` | Mensajes, con o sin stream. |
| `/v1/messages/count_tokens` | Contar tokens. |
| `/v1/models`, `/v1/models/{id}` | Modelos. |
| `/v1/messages/batches`, `/v1/messages/batches/{id}` | Crear, listar, ver y borrar lotes. |
| `/v1/messages/batches/{id}/results`, `/v1/messages/batches/{id}/cancel` | Resultados y cancelación. |
| `/v1/files`, `/v1/files/{id}`, `/v1/files/{id}/content` | Subir, listar, ver, borrar y bajar archivos. |

Cualquier otra ruta responde 404 con el prefijo `pasarela-ia:`.

Además, fuera de `/anthropic`, con la misma llave:

- `GET /v1/uso`: uso del mes (UTC) del programa dueño de la llave: peticiones, costo estimado en dólares, su última petición y la organización que le tocaría ahora. Con `?proyectos=todos` agrega los programas que la tabla `lectura` le deja ver (`ops/pia lectura ...`).
- `GET /v1/catalogo`: los modelos con precio y los alias, para armar menús.
- `GET /salud`: si el gestor está vivo. Por la puerta pública solo dice `{"ok": true}`.

Lotes y archivos se quedan en la organización donde se crearon: el gestor recuerda dónde nació cada uno y manda ahí las peticiones que lo usan. Una petición que mezcla archivos de dos organizaciones responde 400. Las listas de archivos y de lotes juntan los de todas las organizaciones, solo del programa que pregunta.

En `model` puedes usar `claude-ultimo-opus`, `claude-ultimo-sonnet` o `claude-ultimo-haiku`. El gestor los cambia por el modelo más nuevo de esa familia que tenga precio en `pasarela/precios.py`.

## Errores propios del gestor

Tienen la misma forma que los de Anthropic y el `error.message` siempre empieza con `pasarela-ia:`:

```json
{"type": "error", "error": {"type": "authentication_error", "message": "pasarela-ia: llave interna inválida"}}
```

| Código | `error.type` | Cuándo |
|---|---|---|
| 400 | `invalid_request_error` | JSON con demasiados niveles, archivos de dos organizaciones, alias que no apunta a ningún modelo, parámetros de lista que el gestor no admite, cursor que no es del programa. |
| 401 | `authentication_error` | Falta la llave `pia_` o no existe. |
| 402 | `billing_error` | No queda crédito para la clase del programa (`sin crédito gratis disponible`, `todas las fuentes están agotadas`). |
| 404 | `not_found_error` | Ruta que no pasa por el gestor; archivo o lote de otro programa; organización que ya no está configurada. Por la puerta pública, `ruta no disponible`. |
| 408 | `timeout_error` | Una subida pasó 30 s sin mandar datos. |
| 413 | `request_too_large` | Cuerpo de más de 32 MB, o subida de archivo de más de 100 MB. |
| 429 | `rate_limit_error` | Tope mensual del programa en modo corte; demasiadas subidas a la vez (con `retry-after: 2`); por la puerta pública, 20 llaves inválidas desde una IP en 10 minutos castigan a esa IP 15 minutos. |
| 502 | `api_error` | El gestor no pudo hablar con Anthropic o no pudo leer su respuesta. |
| 503 | `api_error` | El gestor todavía no carga proyectos o modelos, la base no contesta, o todas las conexiones hacia Anthropic están ocupadas. |

Hay una excepción: el 402 `billing_error` con el mensaje `Este proyecto no puede gastar en la cuenta pagada.` no lleva el prefijo. Sale cuando un programa `extra` usa un lote o archivo que nació en una organización con tarjeta. Como no lleva prefijo, el cliente no pasa a directo y no gasta por su cuenta lo que el gestor le negó.

Todo lo que contesta Anthropic sale tal cual, con su código y su cuerpo. Por la red interna las respuestas llevan `x-pia-fuente` con la organización que se usó; por la puerta pública esa cabecera y `anthropic-organization-id` se quitan.

## La regla de respaldo directo

Cada programa guarda dos cosas: su llave `pia_` y su propia llave de Anthropic, para hablar directo cuando el gestor no puede. La regla:

1. **Pasa a directo** si no se pudo conectar con el gestor, si el gestor responde 502, 503 o 504, o si responde un error cuyo `error.message` empieza con `pasarela-ia:`. La petición se repite tal cual hacia `api.anthropic.com` con la llave de Anthropic del programa.
2. **Después del cambio, se queda 60 s en directo.** Pasado ese tiempo, la siguiente petición vuelve a probar el gestor.
3. **Un timeout de lectura nunca se repite.** La petición pudo llegar a Anthropic y cobrarse; repetirla pagaría dos veces. El error sube al programa. **Tres seguidos abren el enfriamiento** de 60 s, como un cambio a directo. Una respuesta buena reinicia la cuenta.
4. **Los errores propios de Anthropic se devuelven tal cual** (400, 401 de Anthropic, 429, 529 y demás). No se repiten en directo.
5. **La conexión al gestor tiene timeout de 3 s** y el cliente del gestor usa **`max_retries=0`**: el SDK no reintenta solo, la regla decide.

Las fallas que cuentan como "no se pudo conectar" son `ConnectError`, `ConnectTimeout`, `ReadError`, `WriteError`, `WriteTimeout` y `RemoteProtocolError` de httpx. `ReadTimeout` y `PoolTimeout` cuentan como timeout de lectura.

## La forma más fácil en Python: la librería `sacbe`

Si tu programa usa el SDK oficial de Anthropic, instala la librería y cambia una línea:

```bash
pip install "sacbe @ git+https://github.com/zapitz/sacbe@v0.2.0#subdirectory=cliente"
```

```python
from sacbe import Anthropic   # en lugar de: from anthropic import Anthropic
```

Con `SACBE_URL`, `SACBE_LLAVE` y `ANTHROPIC_API_KEY` en el entorno, aplica la regla de arriba por debajo del SDK. Una diferencia: los reintentos propios del SDK (`max_retries`, 2 por defecto) siguen activos, así que un timeout de lectura sí se repite por el gestor como se repetiría sin Sacbé. Para cumplir la regla 3 al pie de la letra, crea el cliente con `max_retries=0`. Detalles en [cliente/README.md](../cliente/README.md).

Las otras dos opciones siguen sirviendo: el ejemplo de abajo, si prefieres ver y controlar la regla en tu código, y el parche `sitecustomize.py`, si no puedes tocar el código del programa.

## Ejemplo en Python

Dos clientes del SDK `anthropic` y una función de respaldo. Es el archivo [ejemplos/cliente_con_respaldo.py](ejemplos/cliente_con_respaldo.py), probado sin red en `tests/test_ejemplo_integracion.py`:

```python
"""Cliente de Anthropic que pasa por Sacbé y regresa a la conexión directa si el gestor falla.

Usa dos clientes del SDK oficial `anthropic` (0.x, 1.x o 2.x): uno hacia el gestor, con la llave pia_, y otro
directo a api.anthropic.com, con la llave de Anthropic del programa. La regla es la de docs/conectar-programas.md:

  - Pasa a directo si no se pudo conectar con el gestor, si el gestor responde 502, 503 o 504, o si responde un
    error cuyo error.message empieza con «pasarela-ia:». Después del cambio, se queda ENFRIAMIENTO_S en directo.
  - Un timeout de lectura nunca se repite (la petición pudo llegar a Anthropic y cobrarse). Tres seguidos abren
    el enfriamiento.
  - Los errores propios de Anthropic se devuelven tal cual.
  - El cliente del gestor tiene max_retries=0 y 3 s para conectar.

Uso:
    cliente = ClienteConRespaldo.desde_entorno()
    respuesta = cliente.llamar(lambda c: c.messages.create(model="claude-...", max_tokens=512, messages=[...]))
"""
import os
import threading
import time

import anthropic

PREFIJO = "pasarela-ia:"
ENFRIAMIENTO_S = 60.0
TIMEOUTS_SEGUIDOS = 3
CONEXION_S = 3.0
CAIDA = frozenset({502, 503, 504})
# Fallas en las que la petición no llegó al gestor (o llegó rota) y se puede mandar directo sin pagar dos veces.
DE_CONEXION = frozenset({"ConnectError", "ConnectTimeout", "ReadError", "WriteError", "WriteTimeout",
                         "RemoteProtocolError"})
DIRECTO = "https://api.anthropic.com"


def _mensaje(error: anthropic.APIStatusError) -> str:
    cuerpo = error.body
    if isinstance(cuerpo, dict):
        detalle = cuerpo.get("error")
        if isinstance(detalle, dict) and isinstance(detalle.get("message"), str):
            return detalle["message"]
    return ""


class ClienteConRespaldo:
    def __init__(self, url_gestor: str, llave_gestor: str, llave_directa: str, *, reloj=time.monotonic,
                 http_gestor=None, http_directo=None, timeout_s: float = 600.0):
        extra_gestor = {"http_client": http_gestor} if http_gestor is not None else {}
        extra_directo = {"http_client": http_directo} if http_directo is not None else {}
        self.gestor = anthropic.Anthropic(base_url=url_gestor, api_key=llave_gestor, max_retries=0,
                                          timeout=anthropic.Timeout(timeout_s, connect=CONEXION_S), **extra_gestor)
        self.directo = anthropic.Anthropic(base_url=DIRECTO, api_key=llave_directa,
                                           timeout=anthropic.Timeout(timeout_s), **extra_directo)
        self._reloj = reloj
        self._candado = threading.Lock()
        self._hasta = 0.0
        self._timeouts = 0

    @classmethod
    def desde_entorno(cls, **opciones) -> "ClienteConRespaldo":
        """PASARELA_IA_URL (por ejemplo https://ia.example.com/anthropic), PASARELA_IA_LLAVE y ANTHROPIC_API_KEY."""
        return cls(os.environ["PASARELA_IA_URL"], os.environ["PASARELA_IA_LLAVE"], os.environ["ANTHROPIC_API_KEY"],
                   **opciones)

    def en_directo(self) -> bool:
        return self._reloj() < self._hasta

    def _regresar(self) -> None:
        with self._candado:
            self._hasta = self._reloj() + ENFRIAMIENTO_S

    def llamar(self, fn):
        """`fn` recibe un cliente de Anthropic y hace la llamada, por ejemplo lambda c: c.messages.create(...)."""
        if self.en_directo():
            return fn(self.directo)
        try:
            respuesta = fn(self.gestor)
        except anthropic.APIStatusError as e:
            with self._candado:
                self._timeouts = 0
            if e.status_code in CAIDA or _mensaje(e).startswith(PREFIJO):
                self._regresar()
                return fn(self.directo)
            raise                                 # error de Anthropic: tal cual
        except anthropic.APIConnectionError as e:  # APITimeoutError es una subclase: se distingue por la causa
            if type(e.__cause__).__name__ in DE_CONEXION:
                with self._candado:
                    self._timeouts = 0
                self._regresar()
                return fn(self.directo)
            if isinstance(e, anthropic.APITimeoutError):
                with self._candado:
                    self._timeouts += 1
                    abrir = self._timeouts >= TIMEOUTS_SEGUIDOS
                    if abrir:
                        self._timeouts = 0
                if abrir:
                    self._regresar()
            raise                                 # timeout de lectura: no se repite
        with self._candado:
            self._timeouts = 0
        return respuesta
```

Uso:

```python
from cliente_con_respaldo import ClienteConRespaldo

cliente = ClienteConRespaldo.desde_entorno()   # PASARELA_IA_URL, PASARELA_IA_LLAVE y ANTHROPIC_API_KEY
r = cliente.llamar(lambda c: c.messages.create(
    model="claude-ultimo-sonnet", max_tokens=512,
    messages=[{"role": "user", "content": "Hola"}]))
```

Con `stream=True` la regla aplica al abrir el stream: un error del gestor llega antes del primer evento. Si el stream se corta a la mitad, no se repite.

Si el programa es asíncrono, la misma lógica sirve con `anthropic.AsyncAnthropic` y `await` en `llamar`.

## Parche automático para Python: `sitecustomize.py`

Si no quieres tocar el código de un programa en Python, `ops/cliente-python/sitecustomize.py` aplica la misma regla por debajo del SDK. Python carga `sitecustomize` solo al arrancar cualquier proceso (uvicorn, gunicorn, celery, scripts) si su carpeta está en `PYTHONPATH`.

Montaje en el compose del programa:

```yaml
services:
  mi-app:
    volumes:
      - ./sacbe/ops/cliente-python:/pasarela-cliente:ro
    environment:
      PYTHONPATH: /pasarela-cliente
      PASARELA_IA_URL: http://pasarela-ia-api:8080/anthropic
      PASARELA_IA_LLAVE: ${PASARELA_IA_LLAVE}
      PASARELA_IA_AVISO: "1"          # opcional: imprime una línea al activarse
```

Qué hace:

- Parcha `HTTPTransport` y `AsyncHTTPTransport` de **httpx** y de **httpx2**. El SDK de Anthropic 0.x usa httpx; el 1.x y el 2.x usan httpx2. Con eso cubre del 0.x al 2.x sin tocar la imagen.
- Solo desvía `POST /v1/messages` y `POST /v1/messages/count_tokens` que van a `api.anthropic.com`. Lotes, archivos y modelos siguen directo.
- Cambia la llave del programa por `PASARELA_IA_LLAVE` en la misma cabecera que usaba (`x-api-key` o `Authorization: Bearer`) y aplica la regla de respaldo con la petición original.
- Al arrancar, mira qué librería importa el SDK de `anthropic` instalado. Si no es una de las que cubre, escribe en stderr `ALERTA el SDK de anthropic usa '...' y no esta cubierta`: esas llamadas saldrían directo sin que el gestor las vea.
- Sin `PASARELA_IA_URL` o sin `PASARELA_IA_LLAVE` no hace nada. Nunca impide que el programa arranque.

Si la imagen ya trae su propio `sitecustomize.py`, el de `PYTHONPATH` gana y el otro deja de cargarse. Revisa antes de montarlo.

Para quitarlo, borra `PYTHONPATH` y el volumen, y recrea el contenedor.

## Otros lenguajes

Cualquier cliente que deje cambiar la URL base y la llave funciona. La regla de respaldo la tienes que escribir tú, con los mismos cinco puntos.

curl:

```sh
curl https://ia.example.com/anthropic/v1/messages \
  -H "x-api-key: $PASARELA_IA_LLAVE" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model": "claude-ultimo-haiku", "max_tokens": 64, "messages": [{"role": "user", "content": "Hola"}]}'
```

TypeScript con `@anthropic-ai/sdk`:

```ts
import Anthropic from "@anthropic-ai/sdk";

const gestor = new Anthropic({
  baseURL: "https://ia.example.com/anthropic",
  apiKey: process.env.PASARELA_IA_LLAVE,
  maxRetries: 0,
});
const directo = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
```

Uso del mes:

```sh
curl -H "x-api-key: $PASARELA_IA_LLAVE" https://ia.example.com/v1/uso
```

## El caché del prompt

El caché de prompts de Anthropic vive en cada organización. Cuando el gestor cambia de organización (porque se acabó un crédito o porque entró otro que vence antes), la primera petición en la organización nueva no encuentra el caché y lo vuelve a escribir. Pasa una vez por cambio. Lo mismo al pasar a directo y al regresar.
