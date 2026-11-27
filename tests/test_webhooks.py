"""Avisos firmados (pasarela/webhooks.py): firma, suscripciones, generar desde las tablas y entregar con reintentos."""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from pasarela import admin
from pasarela import webhooks as wh
from pasarela.bolsa import EstadoConcesion
from pasarela.esquema import ajuste, aviso, cambio, proyecto, registro, webhook, webhook_entrega

T = datetime(2026, 11, 26, 3, 0)
SECRETO = wh.nuevo_secreto()


def anotar(c, accion, detalle):
    c.execute(insert(cambio).values(ts=T, ordenado_por="prueba", cita="prueba", accion=accion,
                                    detalle=json.dumps(detalle)))


def proyectos(motor, *nombres):
    with motor.begin() as c:
        for n in nombres:
            c.execute(insert(proyecto).values(nombre=n, clase="produccion", llave_hash=n * 2, huella="ab12",
                                              activo=True, dueno="x", creado=T))


@pytest.fixture
def sus(motor):
    proyectos(motor, "crm-api", "crm-tareas", "otro")
    with motor.begin() as c:   # historia vieja: no debe salir
        c.execute(insert(aviso).values(ts=T, clave_unica="perdida:1:5d", texto="viejo", enviado=None))
        c.execute(insert(cambio).values(ts=T, ordenado_por="x", cita="x", accion="cuenta-en-uso",
                                        detalle=json.dumps({"antes": "a", "ahora": "b"})))
    wh.alta(motor, "crm", "https://crm.example/api/webhooks", SECRETO + "\n", ",".join(wh.EVENTOS),
            "crm-api,crm-tareas", anotar, t=T)
    return motor


def entregas(motor):
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(select(webhook_entrega).order_by(webhook_entrega.c.id))]


def test_firma_con_el_vector_de_standard_webhooks():
    firma = wh.firmar("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330,
                      b'{"test": 2432232314}')
    assert firma == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_doble_firma_separada_por_espacio():
    h = wh.encabezados([SECRETO, wh.nuevo_secreto()], "evt_1", 10, b"{}")
    assert len(h["webhook-signature"].split(" ")) == 2 and h["webhook-id"] == "evt_1"


@pytest.mark.parametrize("malo", ["whsec_corto", "sk-algo", SECRETO + "x", SECRETO + "\n" + SECRETO])
def test_secreto_mal_formado(malo):
    with pytest.raises(SystemExit):
        wh.leer_secreto(malo)


@pytest.mark.parametrize("url", ["http://crm.example.com/x", "ftp://x", "https://", "crm.example.com"])
def test_url_invalida(motor, url):
    with pytest.raises(SystemExit, match="URL"):
        wh.alta(motor, "x", url, SECRETO, "prueba", "", anotar, t=T)


def test_url_interna_por_http_se_acepta(motor):
    proyectos(motor, "crm-api")
    wh.alta(motor, "crm", "http://crm-api:8000/api/v1/webhooks/sacbe", SECRETO, "uso.diario", "crm-api",
            anotar, t=T)


def test_alta_valida_eventos_y_proyectos(motor):
    with pytest.raises(SystemExit, match="Eventos"):
        wh.alta(motor, "x", "https://a.b/c", SECRETO, "uso.semanal", "", anotar, t=T)
    with pytest.raises(SystemExit, match="proyectos"):
        wh.alta(motor, "x", "https://a.b/c", SECRETO, "uso.diario", "no-existe", anotar, t=T)


def test_alta_no_repite_la_historia_ni_se_duplica(sus):
    assert wh.generar(sus, t=T, concesiones=lambda t: {}) == 2              # solo uso.diario de ayer, dos proyectos
    assert {e["tipo"] for e in entregas(sus)} == {"uso.diario"}
    with pytest.raises(SystemExit, match="Ya existe"):
        wh.alta(sus, "crm", "https://a.b/c", SECRETO, "uso.diario", "", anotar, t=T)
    with sus.connect() as c:
        assert c.execute(select(cambio.c.accion).order_by(cambio.c.id.desc())).first()[0] == "webhook-alta"


def estado(id_=7):
    return EstadoConcesion(id_, "rmd", 200_000_000, date(2026, 11, 3), date(2026, 12, 3), True, True,
                           150_000_000, 120_000_000, 1_000_000)


def test_saldo_bajo_sale_del_aviso_de_perdida(sus):
    with sus.begin() as c:
        c.execute(insert(aviso).values(ts=T, clave_unica="perdida:7:5d", texto="t", enviado=None))
        c.execute(insert(aviso).values(ts=T, clave_unica="tope:x:2026-11:80", texto="t", enviado=None))
    wh.generar(sus, t=T, concesiones=lambda t: {7: estado()})
    [e] = [x for x in entregas(sus) if x["tipo"] != "uso.diario"]
    cuerpo = json.loads(e["cuerpo"])
    assert e["tipo"] == "saldo.bajo" and cuerpo["id"] == e["evento"]
    assert cuerpo["datos"] == {"cuenta": "rmd", "concesion": 7, "quedan_usd": "150.0000", "vence": "2026-12-03",
                               "se_pierden_usd": "120.0000", "dias": 7}


def test_cuenta_cambio_y_llave_rotada_solo_de_sus_proyectos(sus):
    admin.rotar(sus, "crm-api", "Ariel (chat)", "rota")
    admin.rotar(sus, "otro", "Ariel (chat)", "rota")
    admin.baja(sus, "crm-tareas", "Ariel (chat)", "baja")
    with sus.begin() as c:
        c.execute(insert(cambio).values(ts=T, ordenado_por="pasarela (automático)", cita="x", accion="cuenta-en-uso",
                                        detalle=json.dumps({"accion": "cuenta-en-uso", "antes": "rmd",
                                                            "ahora": "urtaza"})))
    wh.generar(sus, t=T)
    tipos = [(e["tipo"], json.loads(e["cuerpo"])["datos"]) for e in entregas(sus) if e["tipo"] != "uso.diario"]
    assert [t for t, _ in tipos] == ["llave.rotada", "llave.rotada", "cuenta.cambio"]
    assert tipos[0][1]["proyecto"] == "crm-api" and tipos[0][1]["accion"] == "rotar"
    assert tipos[0][1]["por"] == "Ariel (chat)" and "pia_" not in json.dumps(tipos)
    assert tipos[1][1] | {} == {**tipos[1][1], "proyecto": "crm-tareas", "accion": "baja", "huella": "ab12"}
    assert tipos[2][1] == {"de": "rmd", "a": "urtaza", "motivo": None}


def test_uso_diario_despues_de_las_0015_y_una_sola_vez(sus):
    dia = datetime(2026, 11, 25, 10)
    with sus.begin() as c:
        for modelo, costo, fuente in [("claude-sonnet-4-5", 3_000_000, "rmd"), ("claude-haiku-4-5", 187_500, "rmd"),
                                      ("claude-sonnet-4-5", 1, "urtaza")]:
            c.execute(insert(registro).values(ts=dia, proyecto="crm-api", fuente=fuente, ruta="v1/messages",
                                              modelo=modelo, status=200, lote=False, entrada=1, salida=1, cache_5m=0,
                                              cache_1h=0, cache_lectura=0, busquedas=0, costo_micro=costo,
                                              latencia_ms=1, request_id=None, reintentos=0))
    assert wh.generar(sus, t=T) == 2                                   # el 25, al dar de alta el 26 a las 03:00
    assert wh.generar(sus, t=T + timedelta(hours=1)) == 0
    assert wh.generar(sus, t=datetime(2026, 11, 27, 0, 10)) == 0       # antes de las 00:15
    assert wh.generar(sus, t=datetime(2026, 11, 27, 0, 20)) == 2       # el 26
    with sus.begin() as c:   # el cursor quedó atrás: lo ya encolado no se repite
        c.execute(ajuste.update().where(ajuste.c.clave == "wh:crm:diario").values(valor=20261124))
    assert wh.generar(sus, t=datetime(2026, 11, 27, 0, 25)) == 0
    datos = {json.loads(e["cuerpo"])["datos"]["fecha"] + json.loads(e["cuerpo"])["datos"]["proyecto"]:
             json.loads(e["cuerpo"])["datos"] for e in entregas(sus)}
    d = datos["2026-11-25crm-api"]
    assert d["peticiones"] == 3 and d["costo_usd"] == "3.1875"
    assert d["por_modelo"][0] == {"modelo": "claude-sonnet-4-5", "peticiones": 2, "costo_usd": "3.0000"}
    assert d["por_cuenta"] == [{"cuenta": "rmd", "costo_usd": "3.1875"}, {"cuenta": "urtaza", "costo_usd": "0.0000"}]
    assert datos["2026-11-25crm-tareas"]["peticiones"] == 0


class Receptor:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.vistos = []

    def __call__(self, url, cuerpo, headers):
        self.vistos.append((url, cuerpo, headers))
        r = self.respuestas.pop(0)
        return (r, f"HTTP {r}") if isinstance(r, int) else (None, r)


def test_entrega_firmada_que_se_verifica(sus):
    wh.encolar_prueba(sus, "crm", t=T)
    rec = Receptor([200])
    assert wh.entregar(sus, t=T, enviar=rec, reloj=lambda: 1_800_000_000)["entregados"] == 1
    url, cuerpo, h = rec.vistos[0]
    assert url == "https://crm.example/api/webhooks" and h["webhook-timestamp"] == "1800000000"
    assert h["webhook-signature"] == wh.firmar(SECRETO, h["webhook-id"], 1_800_000_000, cuerpo)
    assert entregas(sus)[0]["estado"] == "entregado"
    assert wh.entregar(sus, t=T + timedelta(hours=1), enviar=rec)["entregados"] == 0


def test_reintentos_y_fallido_con_aviso(sus):
    wh.encolar_prueba(sus, "crm", t=T)
    rec = Receptor([500, "ConnectTimeout", 503, 404, 500, 500])
    avisos = []
    t = T
    for espera in (timedelta(0),) + wh.REINTENTOS:
        t += espera
        assert wh.entregar(sus, t=t - timedelta(seconds=1), enviar=rec)["entregados"] == 0 or espera == timedelta(0)
        wh.entregar(sus, t=t, enviar=rec, avisar=lambda clave, texto: avisos.append((clave, texto)))
    e = entregas(sus)[0]
    assert e["estado"] == "fallido" and e["intentos"] == 6 and e["proximo"] is None
    assert len(rec.vistos) == 6 and len({h["webhook-id"] for _, _, h in rec.vistos}) == 1
    assert avisos and "no se pudo entregar" in avisos[0][1]


def test_rotar_firma_con_los_dos_durante_24_h(sus):
    nuevo = wh.nuevo_secreto()
    wh.rotar(sus, "crm", nuevo, anotar, t=T)
    for t, n in [(T + timedelta(hours=1), 2), (T + timedelta(hours=25), 1)]:
        wh.encolar_prueba(sus, "crm", t=t)
        rec = Receptor([200])
        wh.entregar(sus, t=t, enviar=rec, reloj=lambda: 5)
        _, cuerpo, h = rec.vistos[0]
        firmas = h["webhook-signature"].split(" ")
        assert len(firmas) == n and firmas[0] == wh.firmar(nuevo, h["webhook-id"], 5, cuerpo)


def test_apagada_no_genera_ni_entrega(sus):
    wh.encolar_prueba(sus, "crm", t=T)
    wh.cambiar(sus, "crm", anotar, activo=False)
    rec = Receptor([])
    assert wh.entregar(sus, t=T, enviar=rec)["entregados"] == 0 and not rec.vistos


def test_admin_alta_lee_el_secreto_de_la_entrada(motor, monkeypatch, capsys):
    proyectos(motor, "crm-api")
    monkeypatch.setattr(admin, "crear_motor", lambda url: motor)
    monkeypatch.setenv("PIA_DB_URL", "sqlite://")
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRETO + "\n"))
    admin.main(["webhook", "alta", "crm", "--url", "https://crm.example/w", "--proyectos", "crm-api",
                "--por", "Dirección", "--cita", "da de alta"])
    salida = capsys.readouterr().out
    assert "alta hacia https://crm.example/w" in salida and SECRETO not in salida
    admin.main(["webhook", "lista"])
    salida = capsys.readouterr().out
    assert f"…{SECRETO[-4:]}" in salida and SECRETO not in salida
    with motor.connect() as c:
        fila = c.execute(select(cambio).where(cambio.c.accion == "webhook-alta")).one()
        assert fila.ordenado_por == "Dirección" and SECRETO not in fila.detalle
        assert c.execute(select(webhook.c.secreto)).scalar() == SECRETO
