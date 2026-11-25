"""El vigía con docker y curl de mentira. Hace falta bash 4 (arreglos asociativos): con bash 3.2 (macOS) se salta."""
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
VIGIA = RAIZ / "ops" / "vigia.sh"


def hay_bash4() -> bool:
    b = shutil.which("bash")
    return bool(b) and subprocess.run([b, "-c", "declare -A x"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not VIGIA.exists() or not hay_bash4(),
                                reason="hace falta bash 4 (macOS trae bash 3.2) y ops/ en la imagen de pruebas")

SALUD = '{"ok": true, "base": true, "proyectos": 2, "agotadas": ["org1"], "fallas_registro": 0}'
URL = "https://avisos.example.com/sacbe"


def preparar(tmp_path: Path, curl_sale: int = 0, url: str | None = URL, token: str | None = "token-falso") -> tuple[dict, Path]:
    llamadas = tmp_path / "llamadas.txt"
    (tmp_path / "docker").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "docker $*" >> {llamadas}
        case "$*" in
          *"avisos --pendientes"*) printf '%s\\n' '{{"id": 7, "texto": "pasarela-ia: uno"}}' '{{"id": 8, "texto": "pasarela-ia: dos"}}' ;;
          *"avisos --marcar"*) echo "marcados 1" ;;
          *"python -c"*) echo '{SALUD}' ;;
        esac
        """))
    (tmp_path / "curl").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "curl $*" >> {llamadas}
        while [ $# -gt 0 ]; do
          [ "$1" = "-K" ] && cat "$2" >> {tmp_path}/cabecera.txt
          [ "$1" = "-d" ] && printf '%s\\n' "$2" >> {tmp_path}/cuerpos.txt
          shift
        done
        exit {curl_sale}
        """))
    for f in ("docker", "curl"):
        (tmp_path / f).chmod(0o755)
    variables = tmp_path / "vigia.env"
    lineas = []
    if url is not None:
        lineas.append(f"PIA_AVISOS_URL={url}")
    if token is not None:
        lineas.append(f"PIA_AVISOS_TOKEN={token}")
    variables.write_text("\n".join(lineas) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIA_")}
    env.update({"PATH": f"{tmp_path}:{os.environ['PATH']}", "PIA_VIGIA_ENV": str(variables),
                "PIA_VIGIA_ESTADO": str(tmp_path / "estado")})
    return env, llamadas


def correr(env, *args):
    return subprocess.run(["bash", str(VIGIA), *args], env=env, capture_output=True, text=True, timeout=60)


def test_prueba_lista_los_avisos_y_no_marca(tmp_path):
    env, llamadas = preparar(tmp_path)
    r = correr(env, "--prueba")
    assert r.returncode == 0, r.stderr
    assert "PRUEBA, no se manda: pasarela-ia: uno" in r.stdout and "PRUEBA, no se manda: pasarela-ia: dos" in r.stdout
    assert "--marcar" not in llamadas.read_text() and "curl" not in llamadas.read_text()
    assert not (tmp_path / "estado").exists()


def test_manda_json_a_la_url_y_marca_uno_por_uno(tmp_path):
    env, llamadas = preparar(tmp_path)
    r = correr(env)
    assert r.returncode == 0, r.stderr
    texto = llamadas.read_text()
    assert "avisos --marcar 7" in texto and "avisos --marcar 8" in texto
    assert texto.count("curl ") == 2 and texto.count(URL) == 2
    assert "inspect" not in texto                    # ya no busca ningún contenedor puente
    cuerpos = [json.loads(x) for x in (tmp_path / "cuerpos.txt").read_text().splitlines()]
    assert cuerpos == [{"texto": "pasarela-ia: uno"}, {"texto": "pasarela-ia: dos"}]
    assert "token-falso" not in texto                # el token no va en los argumentos de curl
    assert 'header = "Authorization: Bearer token-falso"' in (tmp_path / "cabecera.txt").read_text()


def test_sin_token_no_manda_authorization(tmp_path):
    env, llamadas = preparar(tmp_path, token=None)
    assert correr(env).returncode == 0
    assert llamadas.read_text().count("curl ") == 2
    assert "Authorization" not in (tmp_path / "cabecera.txt").read_text()


def test_sin_url_solo_escribe_en_el_log_y_no_marca(tmp_path):
    env, llamadas = preparar(tmp_path, url=None)
    r = correr(env)
    assert r.returncode == 0, r.stderr
    texto = llamadas.read_text()
    assert "curl" not in texto and "--marcar" not in texto and "avisos --pendientes" in texto
    assert r.stdout.count("sin PIA_AVISOS_URL; no se mandó") == 2


def test_si_el_envio_falla_no_marca(tmp_path):
    env, llamadas = preparar(tmp_path, curl_sale=22)
    r = correr(env)
    assert r.returncode == 0, r.stderr
    texto = llamadas.read_text()
    assert "avisos --pendientes" in texto           # el script llegó a leer los avisos
    assert texto.count("curl ") == 2                # y los intentó mandar los dos
    assert "--marcar" not in texto
    assert "el envío falló (curl salió con 22)" in r.stdout
    assert "token-falso" not in r.stdout + r.stderr
