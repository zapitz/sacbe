from pasarela.precios import costo_micro, tokens


def test_opus_simple():
    # 1,000 de entrada a US$4/MTok = 4,000 micro; 500 de salida a US$20/MTok = 10,000 micro
    assert costo_micro("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 500}) == (14_000, True)


def test_lote_cuesta_la_mitad():
    assert costo_micro("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 500}, lote=True) == (7_000, True)


def test_cache_5m_1h_y_lectura():
    uso = {"input_tokens": 100, "output_tokens": 0, "cache_read_input_tokens": 10_000,
           "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 1000}}
    # sonnet 5.5: 100*2 + 10000*0.2 + 1000*2*1.25 + 1000*2*2 = 200 + 2000 + 2500 + 4000
    assert costo_micro("claude-sonnet-5-5", uso) == (8_700, True)


def test_cache_formato_viejo():
    uso = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000}
    assert costo_micro("claude-sonnet-5-5", uso) == (2_500, True)


def test_haiku_cambia_de_tarifa_arriba_de_100k():
    assert costo_micro("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 0})[0] == 10_000
    assert costo_micro("claude-haiku-5-5", {"input_tokens": 200_000, "output_tokens": 0})[0] == 100_000


def test_busqueda_web():
    uso = {"input_tokens": 0, "output_tokens": 0, "server_tool_use": {"web_search_requests": 3}}
    assert costo_micro("claude-opus-5-5", uso) == (30_000, True)


def test_modelo_desconocido_cobra_como_el_mas_caro():
    assert costo_micro("claude-nuevo-9", {"input_tokens": 1000, "output_tokens": 0}) == (10_000, False)


def test_opus_y_sonnet_4x():
    # opus 4.7 a 5/25: 1,000 * 5 + 500 * 25 = 17,500 micro
    assert costo_micro("claude-opus-4-7", {"input_tokens": 1000, "output_tokens": 500}) == (17_500, True)
    # sonnet 4.6 a 3/15/0.30: 1,000 * 3 + 500 * 15 + 10,000 * 0.30 = 13,500 micro
    uso = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 10_000}
    assert costo_micro("claude-sonnet-4-6", uso) == (13_500, True)
    assert costo_micro("claude-sonnet-4-6", {"input_tokens": 300_000, "output_tokens": 0}) == (900_000, True)  # sin recargo largo
    for m, micro in (("claude-opus-4-6", 5_000), ("claude-opus-4-5-20251101", 5_000), ("claude-sonnet-4-5", 3_000)):
        assert costo_micro(m, {"input_tokens": 1000, "output_tokens": 0}) == (micro, True)


def test_sufijo_de_fecha_y_corchetes():
    assert costo_micro("claude-haiku-4-5-20251001", {"input_tokens": 1000, "output_tokens": 0}) == (1_000, True)
    assert costo_micro("claude-opus-5-5[1m]", {"input_tokens": 1000, "output_tokens": 0}) == (4_000, True)


def test_modo_rapido_doble():
    assert costo_micro("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 0, "speed": "fast"}) == (8_000, True)


def test_tokens_columnas():
    t = tokens({"input_tokens": 5, "output_tokens": 6, "cache_read_input_tokens": 7,
                "cache_creation": {"ephemeral_5m_input_tokens": 8, "ephemeral_1h_input_tokens": 9},
                "server_tool_use": {"web_search_requests": 1}})
    assert t == {"entrada": 5, "salida": 6, "cache_5m": 8, "cache_1h": 9, "cache_lectura": 7, "busquedas": 1}
    assert tokens({}) == {"entrada": 0, "salida": 0, "cache_5m": 0, "cache_1h": 0, "cache_lectura": 0, "busquedas": 0}


def test_fable_5_2():
    # skill claude-api, shared/models.md: US$8 entrada, US$40 salida, lectura de caché US$0.20 por MTok
    uso = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 10_000}
    assert costo_micro("claude-fable-5-2", uso) == (8_000 + 20_000 + 2_000, True)
