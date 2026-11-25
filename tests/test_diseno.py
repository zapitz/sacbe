"""La documentación, el Dockerfile y el flujo de pruebas dicen la misma versión de Python que pyproject.toml."""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def minima() -> str:
    return re.search(r'requires-python = ">=(\d+\.\d+)"', (RAIZ / "pyproject.toml").read_text()).group(1)


def test_la_instalacion_dice_la_version_de_python_del_proyecto():
    texto = (RAIZ / "docs/instalacion.md").read_text()
    assert set(re.findall(r"Python (\d+\.\d+)", texto)) == {minima()}


def test_dockerfile_y_ci_usan_esa_version():
    assert f"FROM python:{minima()}-slim" in (RAIZ / "Dockerfile").read_text()
    assert f'python-version: "{minima()}"' in (RAIZ / ".github/workflows/pruebas.yml").read_text()
