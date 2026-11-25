# Seguridad

Un modelo de amenazas breve: qué protege Sacbé, de quién y cómo. Para reportar una vulnerabilidad, mira [SECURITY.md](../SECURITY.md).

## Qué se protege

- Las llaves de API de Anthropic de tus organizaciones. Solo viven en el `.env` del servidor y en la memoria de `pasarela-ia-api`.
- Las llaves `pia_` de los programas. En la base solo queda su sha256 y sus últimos 4 caracteres.
- El crédito: que un programa no gaste donde no debe ni más de lo que debe.
- La bitácora: quién cambió qué, cuándo y por qué.

Lo que Sacbé no protege: el servidor mismo. Quien tenga root en el servidor, o acceso a Docker, ve las llaves de Anthropic. Sacbé no es una bóveda de secretos.

## Llaves

- Las llaves `pia_` se generan con `secrets.token_urlsafe` y se guardan solo como hash. Si pierdes una, se rota; no se puede recuperar.
- Una llave nueva se ve una sola vez. Por `ops/pia`, la llave se guarda en tu máquina con permisos 600 y no se imprime. Por el panel, en una pantalla única: `/panel/llave/<token>`, con un token de 32 bytes, de un solo uso, solo para quien hizo el cambio, durante 5 minutos, con `Cache-Control: no-store` y `Referrer-Policy: no-referrer`. El token vive solo en la memoria del proceso.
- El gestor no imprime llaves en los registros. De los errores de la base y de httpx solo imprime el tipo, porque su texto puede traer parámetros o cabeceras.
- Las llaves de administrador de Anthropic (para la conciliación) nunca llegan al servidor: `ops/pia conciliar` las usa en tu máquina y solo manda totales por día.

## La puerta pública

Lo que se publica en internet es la API (`ia.example.com`), siempre detrás de un proxy inverso que pone `X-Pia-Puerta: publica`:

- Solo pasan `/anthropic/v1/*`, `/v1/catalogo`, `/v1/uso` y `/salud`. El proxy y la app tienen la misma lista.
- 20 llaves inválidas desde una IP en 10 minutos castigan a esa IP 15 minutos con 429. Solo cuenta el 401 que da el propio gestor, no el de Anthropic.
- Se quitan `x-pia-fuente` y `anthropic-organization-id` de las respuestas, para no decir qué organizaciones hay detrás.
- `/salud` solo dice si el gestor está vivo.

Si el proxy no pone la cabecera, la app trata la petición como interna. Por eso la API no publica puertos en `compose.yaml` y la cabecera es obligatoria en los ejemplos de `docs/ejemplos/`.

## El panel con login

- **Cloudflare Access delante.** La app no confía en que Access esté bien configurado: verifica en cada petición el JWT de `Cf-Access-Jwt-Assertion` (RS256, `aud`, `iss`, `exp` con 60 s de margen) con las llaves públicas del equipo, y busca el correo en la tabla `usuario`. Sin JWT válido o sin usuario activo, 403.
- **Falla cerrada.** Con `PIA_SACBE=1`, faltar `PIA_ACCESS_AUD`, `PIA_ACCESS_EQUIPO` o `PIA_SACBE_HOST` impide arrancar. `PIA_SACBE` solo acepta `1` o `0`. Un contenedor con `PIA_ACCESS_AUD` y sin `PIA_SACBE=1` tampoco arranca, para que la API o el panel interno nunca hereden la configuración del panel con login.
- **Un solo Host.** Solo contesta a `PIA_SACBE_HOST`; cualquier otro Host da 404 (salvo `/salud`, para el healthcheck). Esto corta el DNS rebinding.
- **No monta el proxy.** El contenedor del panel con login no tiene rutas hacia Anthropic ni le habla: el catálogo de modelos lo lee de la base. Ojo: como comparte el `.env`, sí tiene las llaves de Anthropic en su entorno.
- **Ficha HMAC.** Cada formulario lleva un HMAC-SHA256 del correo y el día, con `PIA_PANEL_SECRETO`. Vale ese día y el siguiente (UTC) y se compara en tiempo constante. Una página de otro sitio no puede leerla, así que no puede armar un POST válido.
- **Origin.** El POST debe traer `Origin` igual al host del panel, o `Sec-Fetch-Site: same-origin` si no trae `Origin`. Sin ninguno, 403.
- **Papel en el servidor.** El papel se revisa en el servidor en cada acción, no en la página.
- **Formulario acotado.** 16 KB y 40 campos como máximo, sin caracteres de control, y «confirmo» en las acciones que borran o quitan.

## CSP sin JavaScript

Las páginas del panel no llevan JavaScript. La política es:

```
default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
```

Sin scripts no hay XSS que robe la ficha. `frame-ancestors 'none'` impide meter el panel en un iframe (clickjacking) y `form-action 'self'` impide que un formulario mande datos a otro sitio. Además van `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` y `Cache-Control: no-store`. Todo lo que viene de la base se escapa al armar el HTML.

## Por qué el panel interno es de solo lectura

El panel interno no tiene login: quien llega a su IP entra como dueño. Si además tuviera formularios, una página cualquiera que el dueño abra en su navegador podría apuntar un nombre de DNS a esa IP privada (DNS rebinding), leer el panel con la ficha y mandar cambios. Por eso:

- No tiene `/panel/accion` ni `/panel/llave/<token>`: las rutas no existen y dan 404.
- No genera fichas ni muestra botones.
- Solo debe publicarse en `127.0.0.1` o en una IP privada, nunca en `0.0.0.0`.

Para hacer cambios se usa el panel con login, o `ops/pia` por ssh.

## Avisos y vigía

- `ops/vigia.sh` manda el token de `PIA_AVISOS_TOKEN` por un archivo de configuración de curl (`-K`), no en los argumentos, para que no se vea en `ps`.
- Los avisos no llevan llaves. Sí llevan nombres de programas y organizaciones y montos: el destino de `PIA_AVISOS_URL` debe ser tuyo.

## La base

- Postgres solo está en la red `pasarela-ia-red`, sin puertos publicados.
- Las consultas tienen `statement_timeout` de 10 s y el pool espera 5 s: una base lenta no detiene las respuestas a los programas, que siguen pasando aunque el registro de uso se retrase.
- El gestor no guarda el contenido de peticiones ni respuestas.
