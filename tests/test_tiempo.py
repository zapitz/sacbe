"""La zona horaria del panel: nombre IANA, desfase fijo o UTC."""
from datetime import datetime, timedelta

import pytest

from pasarela import tiempo


def test_desfase_fijo():
    zona, nombre = tiempo.zona_de("-06:00")
    assert nombre == "UTC-06:00" and zona.utcoffset(None) == timedelta(hours=-6)


@pytest.mark.parametrize("texto", [None, "", "  ", "UTC", "utc"])
def test_vacio_es_utc(texto):
    assert tiempo.zona_de(texto)[1] == "UTC"


@pytest.mark.parametrize("texto", ["Marte/Olympus", "+25:00", "-5", "hora local"])
def test_lo_desconocido_cae_en_utc_y_avisa(texto, capsys):
    assert tiempo.zona_de(texto)[1] == "UTC"
    assert "PIA_ZONA_HORARIA" in capsys.readouterr().err


def test_nombre_iana_si_el_sistema_lo_tiene():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        zoneinfo.ZoneInfo("America/Mexico_City")
    except Exception:  # noqa: BLE001
        pytest.skip("este sistema no trae la base de zonas")
    zona, nombre = tiempo.zona_de("America/Mexico_City")
    assert nombre == "America/Mexico_City"
    assert datetime(2026, 11, 12, 12, 0, tzinfo=zona).utcoffset() == timedelta(hours=-6)


def test_las_pruebas_usan_menos_cinco():
    assert tiempo.NOMBRE_ZONA == "UTC-05:00"
    assert tiempo.en_local(datetime(2026, 11, 12, 15, 42)).strftime("%H:%M") == "10:42"
