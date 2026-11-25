# sacbe (cliente de Python)

Cliente de Anthropic que pasa por el gestor [Sacbé](https://github.com/zapitz/sacbe) y regresa solo a la conexión directa si el gestor falla. Es el SDK oficial `anthropic` con una regla de respaldo por debajo: tu código no cambia.

## Instalar

Desde GitHub:

```bash
pip install "sacbe @ git+https://github.com/zapitz/sacbe@v0.2.0#subdirectory=cliente"
```

Necesita Python 3.9 o más nuevo y el SDK `anthropic` 0.40 o más nuevo (probado con 0.42, 0.109, 0.125, 1.11, 2.12 y 2.14).

## Usar

Cambia una línea:

```python
# antes
from anthropic import Anthropic
# después
from sacbe import Anthropic

cliente = Anthropic()
respuesta = cliente.messages.create(
    model="claude-opus-5-5",
    max_tokens=512,
    messages=[{"role": "user", "content": "Hola"}],
)
```

Y define tres variables de entorno:

| Variable | Qué es |
|---|---|
| `SACBE_URL` | La dirección del gestor con `/anthropic` al final, por ejemplo `https://ia.example.com/anthropic`. |
| `SACBE_LLAVE` | La llave `pia_` del programa, la que da `ops/pia alta`. |
| `ANTHROPIC_API_KEY` | La llave de Anthropic del programa. Solo se usa si el gestor falla. |

También puedes pasarlas al crear el cliente: `Anthropic(sacbe_url=..., sacbe_llave=..., api_key=...)`. Se aceptan los nombres viejos `PASARELA_IA_URL` y `PASARELA_IA_LLAVE`.

Para código asíncrono, `from sacbe import AsyncAnthropic`.

Todo lo demás es el SDK de siempre: streaming, herramientas, `count_tokens`, lotes, archivos, `with_options()`.

## Qué hace por debajo

Solo `POST /v1/messages` y `POST /v1/messages/count_tokens` van al gestor, con la llave `pia_`. Lo demás (lotes, archivos, modelos) sale directo a Anthropic. La regla es la de [docs/conectar-programas.md](../docs/conectar-programas.md):

1. **Pasa a directo** con tu llave de Anthropic si no pudo conectar con el gestor, si el gestor responde 502, 503 o 504, o si responde un error cuyo mensaje empieza con `pasarela-ia:` (por ejemplo, el programa llegó a su tope).
2. **Se queda 60 s en directo** y después vuelve a probar el gestor.
3. **Un timeout de lectura no se manda a directo.** La petición pudo llegar a Anthropic y cobrarse. Tres seguidos abren los 60 s de directo.
4. **Los errores de Anthropic llegan tal cual** (400, 429, 529 y demás).
5. **Para conectar con el gestor espera 3 s** como máximo.

Cada cambio a directo se anota en stderr: `sacbe: regreso a conexion directa (motivo=... estado=...)`. Para ver cómo va dentro del programa: `sacbe.estado()`.

Los reintentos propios del SDK (`max_retries`, 2 por defecto) siguen funcionando igual que sin Sacbé. Si no quieres que el SDK repita un timeout, crea el cliente con `max_retries=0`.

## Casos especiales

- **Sin `SACBE_URL` o `SACBE_LLAVE`:** es el cliente normal de Anthropic. Sirve para desarrollo local.
- **Sin `ANTHROPIC_API_KEY`:** todo va al gestor y no hay a dónde regresar; los errores del gestor suben al programa.
- **Con `base_url` o `ANTHROPIC_BASE_URL` apuntando a otro lado:** no se toca. Si antes conectabas el programa poniendo `ANTHROPIC_BASE_URL` al gestor, quítala al pasarte a esta librería.
- **Con tu propio `http_client`:** da error. Si necesitas otro transporte (un proxy de salida, pruebas), pásalo con `transporte=`.

## Pruebas

```bash
cd cliente
pip install anthropic pytest
python -m pytest
```

Las pruebas no usan red: arman un gestor falso y revisan cada caso de la regla con el SDK real.

## Licencia

Apache-2.0. Copyright 2026 Ariel Urtaza.
