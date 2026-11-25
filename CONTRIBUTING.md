# Cómo contribuir

Gracias por querer ayudar. Abre un issue antes de un cambio grande, para platicarlo.

## Pruebas

```sh
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pytest
```

Las pruebas usan SQLite en memoria y transportes falsos: no necesitan red ni llaves. Para correrlas contra Postgres: `docker compose -f compose.pruebas.yaml up --build --abort-on-container-exit`.

Las de `tests/test_vigia.py` necesitan bash 4 o más; con el bash 3.2 de macOS se saltan, y en el CI corren.

Todo cambio de comportamiento lleva su prueba. Un pull request entra con el CI en verde.

## Dependencias

Van fijadas con hash. Si agregas o cambias una, edita `requirements.in` o `requirements-dev.in` y regenera con pip-tools:

```sh
pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
```

Preferimos versiones con al menos 14 días de publicadas.

## Estilo

- Código, comentarios, mensajes y documentación en español de México, directo y sin rodeos.
- Los comentarios explican por qué, no qué.
- Los errores propios del gestor llevan el prefijo `pasarela-ia:`; no lo cambies, los clientes dependen de él.
- Nunca imprimas una llave, ni en un error: de las excepciones ajenas, solo el tipo.
- Nada de datos reales en las pruebas: organizaciones `org1` y `org2`, correos `@example.com`, dominios `example.com`.
- Líneas de unos 120 caracteres.
