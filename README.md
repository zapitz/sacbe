# Sacbé

Sacbé es un gestor de llaves y de crédito de la API de Anthropic para **tus** organizaciones de la consola de Anthropic y **tus** programas. Se pone entre tus programas y `api.anthropic.com`: cada programa usa una llave propia `pia_...`, y el gestor decide con qué organización sale cada petición y lleva la cuenta de lo que gasta cada uno.

El código del servidor vive en el paquete de Python `pasarela` y corre en los contenedores `pasarela-ia-*`. La librería para conectar tus programas en Python está en [cliente/](cliente/) y se llama `sacbe`.

## Qué resuelve

- **Primero se gasta el crédito que vence antes.** Si tienes varias organizaciones con concesiones de crédito que vencen en fechas distintas, el gestor manda cada petición a la que vence antes, mientras le quede saldo. Las organizaciones con tarjeta van al final y solo para los programas de clase `produccion`.
- **Una llave `pia_` y un tope por programa.** Las llaves reales de Anthropic solo viven en el servidor. Cada programa tiene su llave interna, que se puede rotar o dar de baja sin tocar las demás. A cada programa le puedes poner un tope mensual, que solo avisa o que corta con 429.
- **Uso por programa.** Cada petición deja modelo, tokens, costo estimado y organización; nunca el contenido. Un programa puede preguntar su propio uso en `GET /v1/uso`.
- **Panel con papeles y costos.** Dueño, finanzas, responsable y lector, detrás de Cloudflare Access, con una página de costos por día y por modelo, programa u organización. Ver [docs/panel.md](docs/panel.md).
- **Regreso automático a directo.** Si el gestor se cae, tus programas siguen hablando directo con Anthropic con su propia llave y regresan solos al gestor. Ver [docs/conectar-programas.md](docs/conectar-programas.md).

## Qué NO es

- No usa ni convierte suscripciones de claude.ai o de Claude Code. Solo maneja llaves de la API de Anthropic de organizaciones que tú administras en la consola.
- No es para revender ni para compartir acceso con terceros. Es para repartir tu propio crédito entre tus propios programas.
- No es una bóveda de secretos. Guarda las llaves `pia_` solo como hash, pero las llaves de Anthropic viven en el `.env` del servidor.

El uso de la API está sujeto a los términos comerciales de Anthropic: https://www.anthropic.com/legal/commercial-terms. Léelos antes de usar Sacbé.

Sacbé no está afiliado a Anthropic. Claude y Anthropic son marcas de Anthropic, PBC.

## Límite principal

La API de Anthropic no expone las concesiones de crédito (monto, fecha en que se otorgan y fecha en que vencen). Por eso se dan de alta a mano, con lo que ves en la consola:

```sh
ops/pia credito agregar --cuenta org1 --monto 200 --otorgada 2026-11-15 --vence 2026-12-15 \
  --por "admin" --cita "concesión vista en la consola"
```

El gasto de cada petición se estima con la lista de precios de `pasarela/precios.py`. Con una llave de administrador de cada organización, la conciliación diaria (`ops/pia conciliar`) lee el reporte de costos de Anthropic y corrige el saldo. Sin llave de administrador, el saldo es estimado y el panel lo dice.

## Arquitectura

```
programa ──(llave pia_)──> pasarela-ia-api ──(llave real de la org)──> api.anthropic.com
   │                            │  elige la org: crédito que vence antes, luego sin tarjeta, luego con tarjeta
   │                            │  registra modelo, tokens y costo (nunca el contenido)
   └──(si el gestor falla)──────┴────────────────────────────────────> api.anthropic.com directo
pasarela-ia-db      Postgres: proyectos, uso, concesiones, topes, usuarios, bitácora de cambios, avisos
pasarela-ia-panel   panel interno de solo lectura, solo en 127.0.0.1 o una IP privada
pasarela-ia-sacbe   panel con login (Cloudflare Access): el dueño y los responsables hacen cambios
ops/pia             administración por ssh desde tu máquina; cada cambio pide --por y --cita
ops/vigia.sh        cron en el servidor: salud, base y avisos pendientes por webhook
```

## Instalación rápida

Requisitos: Docker con Compose, un servidor Linux y una llave de API por cada organización de la consola de Anthropic.

```sh
git clone https://github.com/zapitz/sacbe.git && cd sacbe
cp .env.example .env && chmod 600 .env          # PIA_DB_CLAVE, PIA_FUENTES, PIA_LLAVE_<NOMBRE>
cp sacbe.env.example sacbe.env && chmod 600 sacbe.env
docker compose up -d --build
docker exec -i pasarela-ia-api python -m pasarela.admin alta crm --clase produccion --por "admin" --cita "primer programa"
```

La última orden imprime la llave `pia_` del programa una sola vez. Los pasos completos, con Cloudflare Access, el proxy inverso y el vigía, están en [docs/instalacion.md](docs/instalacion.md).

## Documentación

- [docs/instalacion.md](docs/instalacion.md): requisitos, `.env`, cuentas, primer usuario, Cloudflare Access, proxy y vigía.
- [docs/conectar-programas.md](docs/conectar-programas.md): el contrato de integración, la regla de respaldo directo y ejemplos.
- [docs/avisos-firmados.md](docs/avisos-firmados.md): avisos firmados (webhooks) a tus programas: gasto del día, crédito por perderse, cambio de cuenta y llaves rotadas.
- [cliente/](cliente/): la librería `sacbe` para Python. Cambias `from anthropic import Anthropic` por `from sacbe import Anthropic` y listo.
- [docs/panel.md](docs/panel.md): papeles y qué puede hacer cada uno.
- [docs/seguridad.md](docs/seguridad.md): modelo de amenazas.
- [docs/ejemplos/](docs/ejemplos/): Traefik, Caddy, cron de conciliación y el cliente de Python con respaldo.
- [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md).

## Licencia

Apache-2.0. Copyright 2026 Ariel Urtaza. Ver [LICENSE](LICENSE) y [NOTICE](NOTICE).

## Summary in English

Sacbé is a self-hosted key and credit manager for the Anthropic API, for **your own** Anthropic Console organizations and **your own** programs. It sits between your programs and `api.anthropic.com`.

- Spends first the credit grant that expires first, across several organizations; organizations with a card come last and only for `produccion` programs.
- Gives each program its own internal `pia_` key and an optional monthly cap (warn or hard stop).
- Records model, tokens, estimated cost and organization per request, never the content.
- Ships a read-only internal panel and a login panel behind Cloudflare Access, with owner, finance, responsible and reader roles.
- Clients fall back to calling Anthropic directly when the gateway fails (connection error, 502/503/504, or an error whose message starts with `pasarela-ia:`), stay direct for 60 s, and never retry a read timeout.

It does not use or convert claude.ai or Claude Code subscriptions, it is not meant for reselling or sharing access with third parties, and it is not a secrets vault. API use is subject to Anthropic's commercial terms: https://www.anthropic.com/legal/commercial-terms.

The Anthropic API does not expose credit grants, so they are entered by hand with `ops/pia credito agregar`. Without an admin key per organization, balances are estimates.

The code, comments and docs are in Mexican Spanish. Sacbé is not affiliated with Anthropic. Claude and Anthropic are trademarks of Anthropic, PBC. Licensed under Apache-2.0.
