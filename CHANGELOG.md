# Cambios

Todos los cambios importantes de este proyecto se anotan aquí.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y el proyecto usa [versionado semántico](https://semver.org/lang/es/).

## [Sin publicar]

## [0.1.0] - 2026-11-25

Primera versión pública. Copyright 2026 Ariel Urtaza, bajo Apache-2.0.

### Agregado

- Proxy hacia la API de Anthropic bajo `/anthropic/v1/*`: mensajes (con stream), conteo de tokens, modelos, lotes y archivos.
- Varias organizaciones de la consola de Anthropic en `PIA_FUENTES`, con una llave por organización.
- Orden de gasto: primero la concesión de crédito que vence antes, luego organizaciones sin tarjeta, al final las de tarjeta y solo para programas de clase `produccion`.
- Una llave interna `pia_` por programa, guardada solo como hash, con alta, rotación y baja.
- Topes mensuales por programa en modo aviso o corte, y presupuesto mensual de lo pagado con tarjeta.
- Registro de uso por petición (modelo, tokens, costo estimado y organización, nunca el contenido), `GET /v1/uso` y `GET /v1/catalogo`.
- Alias `claude-ultimo-opus`, `claude-ultimo-sonnet` y `claude-ultimo-haiku`.
- Lotes y archivos fijados a la organización donde nacieron, con listas que juntan las de todas.
- Concesiones de crédito dadas de alta a mano y conciliación diaria con el reporte de costos de Anthropic desde tu máquina.
- Página de costos en el panel (`/panel/costo`): barras por día apiladas por modelo, programa u organización, totales y tabla, sin JavaScript.
- Vigía de programas callados: avisa si un programa con tráfico real lleva 48 h sin pasar por el gestor (`ops/pia silencios`).
- Panel interno de solo lectura y panel con login detrás de Cloudflare Access, con papeles dueño, finanzas, responsable y lector.
- `ops/pia` para administrar por ssh, con bitácora de cambios (`--por` y `--cita`).
- `ops/vigia.sh`: salud, base y avisos por webhook (`PIA_AVISOS_URL`).
- `ops/cliente-python/sitecustomize.py`: regreso automático a directo para programas en Python con httpx o httpx2 (SDK de Anthropic 0.x a 2.x).
- Zona horaria del panel configurable con `PIA_ZONA_HORARIA`.
- Ejemplos de Traefik, Caddy, cron de conciliación y cliente de Python con respaldo en `docs/ejemplos/`.

[Sin publicar]: https://github.com/zapitz/sacbe/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zapitz/sacbe/releases/tag/v0.1.0
