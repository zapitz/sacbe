import pytest

from pasarela.config import Fuente
from pasarela.orden import Candidata, SinCredito, es_falta_de_saldo, ordenar


def test_produccion_gratis_primero_luego_la_que_cobra(config):
    assert ordenar("produccion", config.fuentes, set()) == [Candidata("org1", "gratis"), Candidata("org2", "pagado")]


def test_extra_solo_gratis(config):
    assert ordenar("extra", config.fuentes, set()) == [Candidata("org1", "gratis")]


def test_agotada_se_salta(config):
    assert ordenar("produccion", config.fuentes, {"org1"}) == [Candidata("org2", "pagado")]


def test_extra_sin_gratis(config):
    with pytest.raises(SinCredito, match="gratis"):
        ordenar("extra", config.fuentes, {"org1"})


def test_produccion_con_todo_marcado_igual_prueba_la_pagada(config):
    """la marca de una pagada solo la manda al final; producción nunca se queda sin cuenta."""
    assert ordenar("produccion", config.fuentes, {"org1", "org2"}) == [Candidata("org2", "pagado")]


def test_produccion_pagada_marcada_va_al_final(config):
    assert ordenar("produccion", config.fuentes, {"org2"}) == [Candidata("org1", "gratis"), Candidata("org2", "pagado")]
    fuentes = (*config.fuentes, Fuente("otra", "pagado", "sk-falsa-otra"))
    assert ordenar("produccion", fuentes, {"org2"}) == \
           [Candidata("org1", "gratis"), Candidata("otra", "pagado"), Candidata("org2", "pagado")]


def test_produccion_solo_con_gratis_agotadas(config):
    with pytest.raises(SinCredito, match="agotadas"):
        ordenar("produccion", config.fuentes[:1], {"org1"})


def test_extra_con_todo_marcado_nunca_llega_a_la_pagada(config):
    with pytest.raises(SinCredito, match="gratis"):
        ordenar("extra", config.fuentes, {"org1", "org2"})


@pytest.mark.parametrize("status,cuerpo,esperado", [
    (400, b'{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to access the Anthropic API."}}', True),
    (400, b'{"type":"error","error":{"type":"invalid_request_error","message":"You have reached your specified workspace API usage limits."}}', True),
    (402, b"", True),
    (400, b'{"type":"error","error":{"type":"billing_error","message":"x"}}', True),
    (400, b'{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens: Field required"}}', False),
    (429, b'{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed the rate limit"}}', False),
    (500, b'{"type":"error","error":{"type":"api_error","message":"billing"}}', False),
    (400, b"[]", False),
    (400, b"no es json", False),
])
def test_falta_de_saldo(status, cuerpo, esperado):
    assert es_falta_de_saldo(status, cuerpo) is esperado
