# Instalación

## Requisitos

- Un servidor Linux con Docker y Docker Compose. La imagen usa Python 3.13 y Postgres 16.
- Una llave de API de Anthropic por cada organización de la consola que quieras usar.
- Opcional, para el saldo exacto: una llave de administrador por organización, en tu máquina, no en el servidor.
- Opcional, para el panel con login: un dominio en Cloudflare y una aplicación de Cloudflare Access.
- En tu máquina: `ssh` al servidor y `python3` para `ops/pia conciliar`.

## 1. El `.env`

```sh
cp .env.example .env
chmod 600 .env
```

Cada variable está explicada en `.env.example`. Las obligatorias:

| Variable | Qué es |
|---|---|
| `PIA_DB_CLAVE` | Contraseña de Postgres. |
| `PIA_DB_URL` | Conexión a la base, con la misma contraseña. |
| `PIA_FUENTES` | Tus organizaciones, en orden: `org1:gratis,org2:pagado`. |
| `PIA_LLAVE_<NOMBRE>` | La llave de API de cada organización. |
| `PIA_SACBE_HOST` | Host del panel con login, si lo usas. |

`gratis` quiere decir que la organización no tiene tarjeta y nunca cobra; `pagado`, que puede cobrar. El nombre de la variable de la llave sale del nombre de la organización en mayúsculas, con guiones como guion bajo: `mi-org` usa `PIA_LLAVE_MI_ORG`.

Las variables del panel con login (`PIA_ACCESS_AUD`, `PIA_ACCESS_EQUIPO`, `PIA_PANEL_SECRETO`) van en `sacbe.env`, que solo lee ese contenedor. Si `PIA_ACCESS_AUD` aparece en el `.env` común, la API y el panel interno no arrancan: es a propósito.

## 2. Levantar los contenedores

```sh
cp sacbe.env.example sacbe.env && chmod 600 sacbe.env   # aunque todavía no uses el panel con login
docker compose up -d --build
docker compose ps
```

El panel interno queda en `http://127.0.0.1:8090/panel` (o en `PIA_IP_PANEL:PIA_PUERTO_PANEL`). Para verlo desde tu máquina sin publicarlo, usa un túnel: `ssh -L 8090:127.0.0.1:8090 usuario@servidor`.

Con los valores de ejemplo de `sacbe.env`, `pasarela-ia-sacbe` arranca pero rechaza a todos con 403, porque ningún JWT trae ese AUD. Si dejas vacías `PIA_ACCESS_AUD`, `PIA_ACCESS_EQUIPO` o `PIA_SACBE_HOST`, no arranca y los demás sí. Puedes quitarlo del `compose.yaml` mientras configuras Access (paso 7). Cambia `PIA_PANEL_SECRETO` antes de usarlo: el de ejemplo es público.

## 3. Administración desde tu máquina

`ops/pia` corre cada orden por ssh dentro de `pasarela-ia-api`:

```sh
export PIA_SERVIDOR=admin@servidor.example.com
ops/pia cuentas
```

Toda orden que cambia algo pide `--por` (quién lo ordenó) y `--cita` (por qué, con sus palabras). Las dos quedan en la tabla `cambio`, con la hora. `ops/pia cambios -n 20` muestra la bitácora.

Las llaves nuevas de `alta` y `rotar` se guardan en `$PIA_SECRETOS/proyectos/<nombre>.env` (por omisión `~/.config/sacbe`), con permisos 600, y no se imprimen.

## 4. Cuentas y créditos

Da de alta cada organización con su tipo y el correo con que entras a ella en la consola:

```sh
ops/pia cuenta agregar org1 --correo-plan dueno@example.com --tarjeta no --por "admin" --cita "alta de org1"
ops/pia cuenta agregar org2 --correo-plan dueno@example.com --tarjeta si --por "admin" --cita "alta de org2"
```

Después, cada concesión de crédito que veas en la consola (Billing, créditos). La API de Anthropic no las expone, así que esto es a mano:

```sh
ops/pia credito agregar --cuenta org1 --monto 200 --otorgada 2026-11-15 --vence 2026-12-15 \
  --por "admin" --cita "concesión de noviembre"
ops/pia saldos
```

`--vence` es el primer día en que el crédito ya no cuenta. Si todavía no sabes la fecha, usa `--estimada`: sin `--vence` se supone que vence 33 días después, y luego la confirmas con `ops/pia credito confirmar`.

## 5. Programas

```sh
ops/pia alta crm --clase produccion --por "admin" --cita "el CRM pasa por el gestor"
ops/pia alta lotes --clase extra --por "admin" --cita "procesos por lotes"
ops/pia tope lotes --usd 20 --modo corte --por "admin" --cita "tope de lotes"
```

- `produccion`: puede llegar a la organización con tarjeta cuando se acaba el crédito.
- `extra`: solo crédito de organizaciones sin tarjeta. Si no queda, recibe 402.
- `--modo aviso` solo avisa al llegar al tope; `--modo corte` responde 429 hasta el día 1 del mes siguiente.

Cómo conectar cada programa: [conectar-programas.md](conectar-programas.md).

## 6. Primer usuario dueño

El panel con login solo deja entrar a correos dados de alta:

```sh
ops/pia usuario alta dueno@example.com --papel dueno --por "admin" --cita "primer dueño"
```

Siempre queda al menos un dueño activo: el gestor no deja dar de baja ni cambiar de papel al último. Los papeles están en [panel.md](panel.md).

## 7. Cloudflare Access

1. En Cloudflare Zero Trust, crea una aplicación de tipo self-hosted para `panel.example.com`, con una política que solo deje entrar a los correos de tu equipo.
2. Copia el **Application Audience (AUD) Tag** de la aplicación a `PIA_ACCESS_AUD` en `sacbe.env`.
3. Pon el dominio de tu equipo en `PIA_ACCESS_EQUIPO`, por ejemplo `tu-equipo.cloudflareaccess.com`. De ahí se bajan las llaves públicas que firman el JWT.
4. Genera `PIA_PANEL_SECRETO` con `python3 -c "import secrets; print(secrets.token_hex(32))"`. Sin él, el panel con login solo muestra.
5. Pon `PIA_SACBE_HOST=panel.example.com` en el `.env`. Sin esa variable el panel con login no arranca, y con ella solo contesta a ese Host.
6. El registro DNS de `panel.example.com` debe ir con proxy de Cloudflare (nube naranja), para que Access quede delante.

El contenedor verifica el JWT en cada petición (firma RS256, `aud`, `iss` y `exp`). El correo del JWT debe existir y estar activo en la tabla `usuario`.

## 8. Proxy inverso

`pasarela-ia-api` y `pasarela-ia-sacbe` no publican puertos. Conecta tu proxy inverso a la red `pasarela-ia-red`, o une esos dos contenedores a la red del proxy en `compose.yaml`. Hay ejemplos en [ejemplos/traefik.yml](ejemplos/traefik.yml) y [ejemplos/Caddyfile](ejemplos/Caddyfile).

Para la API pública (`ia.example.com`) hay dos reglas que no son opcionales:

- El proxy debe poner `X-Pia-Puerta: publica` en cada petición y pisar la que mande el cliente. Sin esa cabecera la app trata la petición como interna: no castiga llaves inválidas y deja salir las cabeceras `x-pia-fuente` y `anthropic-organization-id`.
- Solo deben pasar `/anthropic/v1/*`, `/v1/catalogo`, `/v1/uso` y `/salud`.

La app toma la IP del cliente del último valor de `X-Forwarded-For`. Si pones Cloudflare con proxy delante de `ia.example.com`, ese valor es una IP de Cloudflare; en ese caso configura tu proxy para que reescriba `X-Forwarded-For` con `CF-Connecting-IP`, o deja `ia.example.com` sin proxy de Cloudflare.

Los programas que corren en el mismo servidor no necesitan pasar por el proxy: el gestor entra a la red de cada app y la app usa `http://pasarela-ia-api:8080/anthropic`. El comentario de `compose.yaml` explica cómo.

## 9. Vigía

`ops/vigia.sh` corre en el servidor cada 5 minutos. Revisa `/salud` de la API y del panel interno, revisa la base, manda los avisos pendientes del gestor y, una vez por hora, avisa de los programas que dejaron de pasar por el gestor.

Cada aviso sale como `POST` con JSON `{"texto": "..."}` a `PIA_AVISOS_URL`, con `Authorization: Bearer $PIA_AVISOS_TOKEN` si lo defines. Sin `PIA_AVISOS_URL` solo escribe en el log y los avisos quedan pendientes.

```sh
sudo install -d -m 700 /etc/sacbe
printf 'PIA_AVISOS_URL=https://avisos.example.com/sacbe\nPIA_AVISOS_TOKEN=cambia-este-token\n' | sudo tee /etc/sacbe/vigia.env
sudo chmod 600 /etc/sacbe/vigia.env
ops/vigia.sh --prueba          # muestra qué mandaría, sin mandar ni marcar nada
```

Cron (`crontab -e` del usuario que puede usar docker):

```
*/5 * * * * /ruta/a/sacbe/ops/vigia.sh >> /var/log/sacbe-vigia.log 2>&1
```

Necesita bash 4 o más, `curl` y `python3` en el servidor.

## 10. Conciliación diaria

En tu máquina, guarda una llave de administrador por organización en `~/.config/sacbe/anthropic_admin_<cuenta>.env`:

```sh
mkdir -p ~/.config/sacbe && chmod 700 ~/.config/sacbe
printf 'ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...\n' > ~/.config/sacbe/anthropic_admin_org1.env
chmod 600 ~/.config/sacbe/anthropic_admin_org1.env
ops/pia conciliar --cita "primera conciliación"
```

`conciliar` lee el reporte de costos de cada organización en tu máquina y solo manda al servidor totales por día. Las llaves de administrador nunca salen de tu máquina. Para correrlo cada día, mira [ejemplos/conciliar.cron](ejemplos/conciliar.cron). Si tu `python3` no encuentra certificados raíz, define `SSL_CERT_FILE`.
