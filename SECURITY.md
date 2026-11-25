# Seguridad

## Cómo reportar una vulnerabilidad

Repórtala en privado por GitHub Security Advisories: en la pestaña **Security** del repositorio, elige **Report a vulnerability**. No abras un issue público ni la menciones en un pull request.

Incluye, si puedes:

- qué versión o commit usas;
- qué pasa y qué esperabas que pasara;
- los pasos para reproducirlo, con datos falsos;
- qué puede hacer quien lo aproveche.

No mandes llaves reales, ni de Anthropic ni `pia_`. Si una llave quedó expuesta, rótala primero (`ops/pia rotar <programa>` o en la consola de Anthropic) y después reporta.

Vas a recibir una respuesta en el mismo aviso. Cuando haya arreglo, se publica una versión nueva y el aviso.

## Versiones con soporte

Solo la última versión publicada recibe arreglos de seguridad.

## Alcance

El modelo de amenazas está en [docs/seguridad.md](docs/seguridad.md). Quedan fuera los problemas que requieren root en el servidor o acceso a Docker, y la configuración de Cloudflare Access o del proxy inverso que no siga `docs/instalacion.md`.
