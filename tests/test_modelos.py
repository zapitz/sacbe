import json
from datetime import datetime

import pytest
from sqlalchemy import func, select

from pasarela import modelos
from pasarela.esquema import alias_modelo, aviso, cambio, modelo

T = datetime(2026, 11, 12, 15, 0)


def api(id_, creado, nombre=None):
    return {"type": "model", "id": id_, "display_name": nombre or id_, "created_at": creado}


PAGINA = [api("claude-opus-5-5", "2026-10-20T00:00:00Z", "Claude Opus 5.5"),
          api("claude-opus-5", "2026-03-01T00:00:00Z", "Claude Opus 5"),
          api("claude-sonnet-5-5", "2026-10-20T00:00:00Z", "Claude Sonnet 5.5"),
          api("claude-haiku-5-5", "2026-10-25T00:00:00Z", "Claude Haiku 5.5"),
          api("claude-haiku-4-5-20251001", "2025-10-01T00:00:00Z", "Claude Haiku 4.5"),
          api("claude-opus-4-1-20250805", "2025-08-05T00:00:00Z"),        # viejo y sin precio: no avisa
          api("claude-3-haiku-20240307", "2024-03-07T00:00:00Z")]         # nombre viejo, sin familia: no avisa


def lista_de(pagina, t=T):
    vistos = modelos.de_anthropic(pagina)
    return vistos, modelos.armar(vistos, t)


def contar(motor, tabla):
    with motor.connect() as c:
        return c.execute(select(func.count()).select_from(tabla)).scalar()


@pytest.mark.parametrize("id_,familia", [("claude-opus-5-5", "opus"), ("claude-haiku-4-5-20251001", "haiku"),
                                         ("claude-fable-5-1", "fable"), ("claude-3-haiku-20240307", None),
                                         ("claude-2.1", None), ("otra-cosa", None),
                                         ("claude-" + "a" * 20 + "-1", "a" * 20),
                                         ("claude-" + "a" * 21 + "-1", None)])
def test_familia_de(id_, familia):
    """la familia cabe en String(20); una más larga se queda sin familia."""
    assert modelos.familia_de(id_) == familia


def test_de_anthropic_ignora_lo_raro():
    vistos = modelos.de_anthropic([
        "texto", {"id": 5}, {"id": "x" * 81}, {"id": ""},
        {"id": "claude-opus-5-5", "created_at": "no es fecha"},
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-10-20T00:00:00Z"}])
    assert vistos == [modelos.Modelo("claude-opus-5-5", "Claude Opus 5.5", "opus", datetime(2026, 10, 20))]
    sin_nombre = modelos.de_anthropic([{"id": "claude-opus-5", "created_at": "mal"}])[0]
    assert (sin_nombre.nombre, sin_nombre.creado) == ("claude-opus-5", modelos.MUY_VIEJO)


def test_armar_alias_y_orden():
    _, lista = lista_de(PAGINA)
    assert lista.alias == {"claude-ultimo-opus": "claude-opus-5-5", "claude-ultimo-sonnet": "claude-sonnet-5-5",
                           "claude-ultimo-haiku": "claude-haiku-5-5"}
    assert [m.id for m in lista.modelos] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5",
                                             "claude-opus-5", "claude-haiku-4-5-20251001"]
    assert lista.sin_precio == ()


def test_sin_precio_solo_lo_nuevo():
    """avisa si es más nuevo que todo lo de su familia con precio, o si su familia no tiene precio."""
    _, lista = lista_de(PAGINA + [api("claude-opus-6", "2026-11-10T00:00:00Z"),
                                  api("claude-nova-1", "2026-11-01T00:00:00Z")])
    assert [m.id for m in lista.sin_precio] == ["claude-nova-1", "claude-opus-6"]
    assert lista.alias["claude-ultimo-opus"] == "claude-opus-5-5"         # el alias sigue en uno con precio
    assert "claude-opus-6" not in {m.id for m in lista.modelos}


def test_sin_precio_con_fecha_ilegible_en_familia_sin_precio_avisa():
    """Con created_at ilegible (MUY_VIEJO): si la familia no tiene ningún modelo con precio, igual avisa."""
    _, lista = lista_de(PAGINA + [api("claude-nova-1", "no es fecha")])
    assert [m.id for m in lista.sin_precio] == ["claude-nova-1"]


def test_alias_sin_modelo_con_precio_no_existe():
    _, lista = lista_de([api("claude-opus-5-5", "2026-10-20T00:00:00Z")])
    assert lista.alias == {"claude-ultimo-opus": "claude-opus-5-5"}


def test_catalogo_json():
    m = modelos.Modelos()
    assert not m.cargado and m.alias is None
    m.poner(lista_de(PAGINA)[1])
    cat = m.catalogo()
    marca = cat["marcas"][0]
    assert (marca["id"], marca["nombre"], marca["ruta"]) == ("anthropic", "Anthropic", "/anthropic")
    opus = next(x for x in marca["modelos"] if x["id"] == "claude-opus-5-5")
    assert opus == {"id": "claude-opus-5-5", "nombre": "Claude Opus 5.5", "familia": "opus",
                    "precio_usd_mtok": {"entrada": 4.0, "salida": 20.0}}
    assert {"id": "claude-ultimo-opus", "apunta_a": "claude-opus-5-5"} in marca["alias"]
    assert [a["id"] for a in marca["alias"]] == sorted(a["id"] for a in marca["alias"])
    assert cat["actualizado"] == "2026-11-12T15:00:00Z"
    json.dumps(cat)


def test_guardar_y_cargar(motor):
    vistos, lista = lista_de(PAGINA)
    assert modelos.guardar_modelos(motor, vistos, lista, T) == []
    assert contar(motor, aviso) == 0 and contar(motor, alias_modelo) == 3
    leidos, ts = modelos.cargar_modelos(motor)
    assert sorted(m.id for m in leidos) == sorted(m.id for m in vistos) and ts == T
    assert modelos.armar(leidos, ts).alias == lista.alias


def test_alias_que_cambia_avisa_una_vez(motor):
    sin_haiku_nuevo = [x for x in PAGINA if x["id"] != "claude-haiku-5-5"]
    v1, l1 = lista_de(sin_haiku_nuevo)
    modelos.guardar_modelos(motor, v1, l1, T)
    assert l1.alias["claude-ultimo-haiku"] == "claude-haiku-4-5-20251001"
    v2, l2 = lista_de(PAGINA)
    assert modelos.guardar_modelos(motor, v2, l2, T) == ["claude-ultimo-haiku"]
    assert modelos.guardar_modelos(motor, v2, l2, T) == []
    with motor.connect() as c:
        avisos_ = [dict(f._mapping) for f in c.execute(select(aviso))]
        ch = [dict(f._mapping) for f in c.execute(select(cambio))]
        destino = c.execute(select(alias_modelo.c.apunta_a)
                            .where(alias_modelo.c.alias == "claude-ultimo-haiku")).scalar()
    assert [a["clave_unica"] for a in avisos_] == \
           ["alias:claude-ultimo-haiku:claude-haiku-4-5-20251001:claude-haiku-5-5:2026-11-12"]
    assert "claude-ultimo-haiku ahora apunta a claude-haiku-5-5" in avisos_[0]["texto"]
    assert [(x["ordenado_por"], x["accion"]) for x in ch] == [("pasarela (automático)", "alias-cambio")]
    assert json.loads(ch[0]["detalle"])["antes"] == "claude-haiku-4-5-20251001"
    assert destino == "claude-haiku-5-5"


def test_si_el_otro_contenedor_ya_lo_cambio_no_avisa(motor):
    v1, l1 = lista_de([x for x in PAGINA if x["id"] != "claude-haiku-5-5"])
    modelos.guardar_modelos(motor, v1, l1, T)
    with motor.begin() as c:                                  # el otro contenedor llegó primero
        c.execute(alias_modelo.update().where(alias_modelo.c.alias == "claude-ultimo-haiku")
                  .values(apunta_a="claude-haiku-5-5"))
    v2, l2 = lista_de(PAGINA)
    assert modelos.guardar_modelos(motor, v2, l2, T) == []
    assert contar(motor, aviso) == 0


def test_mover_alias_condicionado_al_destino_de_antes(motor):
    """el otro contenedor lo movió entre mi lectura y mi UPDATE. Mi `previo` ya no es el guardado:
    no se mueve otra vez y este llamador no deja aviso ni cambio."""
    a = "claude-ultimo-haiku"
    v1, l1 = lista_de([x for x in PAGINA if x["id"] != "claude-haiku-5-5"])
    modelos.guardar_modelos(motor, v1, l1, T)                 # el alias queda en claude-haiku-4-5-20251001
    with motor.begin() as c:                                  # el otro contenedor lo mueve a otro destino
        c.execute(alias_modelo.update().where(alias_modelo.c.alias == a).values(apunta_a="claude-haiku-5-5"))
    with motor.begin() as c:
        assert modelos._mover_alias(c, a, "claude-haiku-4-5-20251001", "claude-haiku-5-5", T) is False
    assert contar(motor, aviso) == 0 and contar(motor, cambio) == 0
    with motor.begin() as c:                                  # con el destino de antes correcto sí mueve y avisa
        assert modelos._mover_alias(c, a, "claude-haiku-5-5", "claude-haiku-6", T) is True
    assert contar(motor, aviso) == 1 and contar(motor, cambio) == 1
    with motor.connect() as c:
        assert c.execute(select(alias_modelo.c.apunta_a).where(alias_modelo.c.alias == a)).scalar() == "claude-haiku-6"


def test_modelo_nuevo_sin_precio_avisa_una_vez(motor):
    vistos, lista = lista_de(PAGINA + [api("claude-opus-6", "2026-11-10T00:00:00Z")])
    modelos.guardar_modelos(motor, vistos, lista, T)
    modelos.guardar_modelos(motor, vistos, lista, T)
    with motor.connect() as c:
        filas = [dict(f._mapping) for f in c.execute(select(aviso))]
    assert [f["clave_unica"] for f in filas] == ["sin-precio:claude-opus-6"]
    assert "pasarela/precios.py" in filas[0]["texto"] and "claude-ultimo-opus" in filas[0]["texto"]


def test_modelo_que_desaparece_se_borra(motor):
    vistos, lista = lista_de(PAGINA)
    modelos.guardar_modelos(motor, vistos, lista, T)
    menos, lista2 = lista_de([x for x in PAGINA if x["id"] != "claude-opus-5"])
    modelos.guardar_modelos(motor, menos, lista2, T)
    assert "claude-opus-5" not in {m.id for m in modelos.cargar_modelos(motor)[0]}
    assert contar(motor, modelo) == len(PAGINA) - 1
