from decimal import Decimal

import pytest

from pasarela.config import cargar, variable_llave

BASE = {"PIA_DB_URL": "sqlite://", "PIA_FUENTES": "org1:gratis,org2:pagado",
        "PIA_LLAVE_ORG1": "sk-falsa-u", "PIA_LLAVE_ORG2": "sk-falsa-r"}


def test_fuentes_en_el_orden_del_env():
    c = cargar(BASE)
    assert [(f.nombre, f.tipo) for f in c.fuentes] == [("org1", "gratis"), ("org2", "pagado")]
    assert c.fuentes[0].llave == "sk-falsa-u"
    assert c.anthropic_url == "https://api.anthropic.com"
    assert c.agotada_s == 3600


def test_repr_no_muestra_llaves():
    assert "sk-falsa" not in repr(cargar(BASE))


def test_tipo_invalido():
    with pytest.raises(ValueError, match="no es gratis ni pagado"):
        cargar({**BASE, "PIA_FUENTES": "org1:gratuito"})


def test_falta_llave():
    env = dict(BASE)
    del env["PIA_LLAVE_ORG2"]
    with pytest.raises(ValueError, match="PIA_LLAVE_ORG2"):
        cargar(env)


def test_falta_base():
    env = dict(BASE)
    del env["PIA_DB_URL"]
    with pytest.raises(ValueError, match="PIA_DB_URL"):
        cargar(env)


def test_variables_nuevas_son_opcionales():
    """El .env de producción de hoy no trae ninguna: no debe romper."""
    c = cargar(BASE)
    assert c.tipo_cambio == Decimal("20") and c.tipo_cambio_supuesto is True and c.orden_s == 60


def test_tipo_de_cambio_y_ciclo():
    c = cargar({**BASE, "PIA_TIPO_CAMBIO": "18.45", "PIA_ORDEN_S": "0"})
    assert c.tipo_cambio == Decimal("18.45") and c.tipo_cambio_supuesto is False and c.orden_s == 0


@pytest.mark.parametrize("valor", ["veinte", "0", "-3", "NaN"])
def test_tipo_de_cambio_invalido(valor):
    with pytest.raises(ValueError, match="PIA_TIPO_CAMBIO"):
        cargar({**BASE, "PIA_TIPO_CAMBIO": valor})


def test_variable_llave():
    assert variable_llave("org1") == "PIA_LLAVE_ORG1"
    assert variable_llave("demo-alt") == "PIA_LLAVE_DEMO_ALT"


def test_sacbe_apagado_por_omision():
    c = cargar(BASE)
    assert c.sacbe is False and c.access_aud == "" and c.access_equipo == "" and c.panel_secreto == ""


def test_sacbe_encendido_con_su_acceso():
    c = cargar({**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "aud-123", "PIA_SACBE_HOST": "panel.example.com",
                "PIA_ACCESS_EQUIPO": "tu-equipo.cloudflareaccess.com", "PIA_PANEL_SECRETO": "s3creto"})
    assert c.sacbe is True and c.access_aud == "aud-123"
    assert c.access_equipo == "tu-equipo.cloudflareaccess.com" and c.panel_secreto == "s3creto"


@pytest.mark.parametrize("falta", ["PIA_ACCESS_AUD", "PIA_ACCESS_EQUIPO", "PIA_SACBE_HOST"])
def test_sacbe_sin_access_no_arranca(falta):
    env = {**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "aud-123", "PIA_ACCESS_EQUIPO": "tu-equipo.cloudflareaccess.com",
           "PIA_SACBE_HOST": "panel.example.com"}
    del env[falta]
    with pytest.raises(ValueError, match=falta):
        cargar(env)


def test_el_secreto_no_se_imprime():
    c = cargar({**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "aud-123", "PIA_ACCESS_EQUIPO": "e.cloudflareaccess.com",
                "PIA_SACBE_HOST": "panel.example.com", "PIA_PANEL_SECRETO": "s3creto-largo"})
    assert c.sacbe is True and "s3creto-largo" not in repr(c)


# La configuración del panel con login falla cerrada

@pytest.mark.parametrize("valor", ["true", "yes", "si", "sí", "2", "", " 1", "1 ", "01", "on"])
def test_sacbe_solo_acepta_1_o_0(valor):
    with pytest.raises(ValueError, match="PIA_SACBE"):
        cargar({**BASE, "PIA_SACBE": valor, "PIA_ACCESS_AUD": "a", "PIA_ACCESS_EQUIPO": "e.cloudflareaccess.com"})


def test_sacbe_0_o_ausente_es_panel_interno():
    assert cargar({**BASE, "PIA_SACBE": "0"}).sacbe is False
    assert cargar(BASE).sacbe is False


@pytest.mark.parametrize("sacbe", [None, "0"])
@pytest.mark.parametrize("aud", ["aud-123", ""])
def test_access_aud_sin_sacbe_no_arranca(sacbe, aud):
    """Un contenedor con las variables de Access nunca arranca como API o panel interno por error."""
    env = {**BASE, "PIA_ACCESS_AUD": aud}
    if sacbe is not None:
        env["PIA_SACBE"] = sacbe
    with pytest.raises(ValueError, match="PIA_ACCESS_AUD"):
        cargar(env)


def test_el_host_de_sacbe():
    env = {**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "a", "PIA_ACCESS_EQUIPO": "e.cloudflareaccess.com"}
    assert cargar({**env, "PIA_SACBE_HOST": " Otro.Example.COM "}).sacbe_host == "otro.example.com"


@pytest.mark.parametrize("host", [None, "", "   "])
def test_sin_host_el_panel_con_login_no_arranca(host):
    """No hay host por omisión: sin PIA_SACBE_HOST (o vacía) el panel con login no arranca."""
    env = {**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "a", "PIA_ACCESS_EQUIPO": "e.cloudflareaccess.com"}
    if host is not None:
        env["PIA_SACBE_HOST"] = host
    with pytest.raises(ValueError, match="PIA_SACBE_HOST"):
        cargar(env)


def test_sin_panel_con_login_el_host_es_opcional():
    assert cargar(BASE).sacbe_host == ""
    assert cargar({**BASE, "PIA_SACBE_HOST": "Panel.Example.com"}).sacbe_host == "panel.example.com"


def test_el_equipo_se_limpia():
    c = cargar({**BASE, "PIA_SACBE": "1", "PIA_ACCESS_AUD": "a", "PIA_ACCESS_EQUIPO": " https://Equipo.cloudflareaccess.com/ ",
                "PIA_SACBE_HOST": "panel.example.com"})
    assert c.access_equipo == "equipo.cloudflareaccess.com"
