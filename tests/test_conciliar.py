import ast
import io
import json
import urllib.error
import urllib.parse
from datetime import date, datetime
from pathlib import Path

import pytest

from pasarela import conciliar

LLAVE = "sk-ant-admin-falsa-123"
P1 = {"data": [{"starting_at": "2026-11-09T00:00:00Z", "ending_at": "2026-11-10T00:00:00Z",
                "results": [{"currency": "USD", "amount": "123.4567"}, {"currency": "USD", "amount": "10"}]}],
      "has_more": True, "next_page": "pag_2"}
P2 = {"data": [{"starting_at": "2026-11-10T00:00:00Z", "ending_at": "2026-11-11T00:00:00Z", "results": []}],
      "has_more": False, "next_page": None}


class Respuesta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Abrir:
    """urlopen de mentira: entrega las páginas en orden (o lanza la excepción que toque) y guarda lo pedido."""

    def __init__(self, paginas):
        self.paginas = list(paginas)
        self.pedidas = []

    def __call__(self, req, timeout=None):
        self.pedidas.append(req)
        p = self.paginas.pop(0)
        if isinstance(p, Exception):
            raise p
        return Respuesta(json.dumps(p).encode())


def consulta(req):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


def secretos(carpeta: Path, cuentas=("org1",)) -> Path:
    for c in cuentas:
        (carpeta / f"anthropic_admin_{c}.env").write_text(f'ANTHROPIC_ADMIN_KEY="{LLAVE}"\n')
    (carpeta / "anthropic_org1.env").write_text("ANTHROPIC_API_KEY=sk-falsa-normal\n")   # no es de admin: se ignora
    return carpeta


def test_a_micro():
    assert conciliar.a_micro("123.4567") == 1_234_567
    assert conciliar.a_micro("10") == 100_000 and conciliar.a_micro(0.5) == 5_000


def test_totales_suma_y_pagina():
    abrir = Abrir([P1, P2])
    dias = conciliar.totales(LLAVE, datetime(2026, 11, 9), datetime(2026, 11, 11), abrir)
    assert dias == {"2026-11-09": 1_334_567, "2026-11-10": 0}
    q1, q2 = consulta(abrir.pedidas[0]), consulta(abrir.pedidas[1])
    assert q1["bucket_width"] == ["1d"] and q1["starting_at"] == ["2026-11-09T00:00:00Z"] and "page" not in q1
    assert q2["page"] == ["pag_2"]
    assert abrir.pedidas[0].get_header("X-api-key") == LLAVE
    assert abrir.pedidas[0].get_header("Anthropic-version") == conciliar.VERSION


def test_moneda_que_no_es_dolar_falla():
    otra = {"data": [{"starting_at": "2026-11-09T00:00:00Z", "results": [{"currency": "MXN", "amount": "1"}]}],
            "has_more": False}
    with pytest.raises(ValueError):
        conciliar.totales(LLAVE, datetime(2026, 11, 9), datetime(2026, 11, 10), Abrir([otra]))


def test_main_saca_solo_totales(tmp_path, capsys):
    abrir = Abrir([P1, P2])
    assert conciliar.main(["--secretos", str(secretos(tmp_path)), "--dias", "2"], abrir, date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 1_334_567},
                                                      {"dia": "2026-11-10", "micro": 0}]}}
    assert "falsa" not in out + err and len(abrir.pedidas) == 2


def test_una_cuenta_que_falla_no_tumba_a_las_demas(tmp_path, capsys):
    error = urllib.error.HTTPError(conciliar.URL, 401, "no", {}, None)
    abrir = Abrir([P2, error])                       # se piden en orden alfabético: org1 y luego org2
    assert conciliar.main(["--secretos", str(secretos(tmp_path, ("org1", "org2")))], abrir, date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert list(json.loads(out)["cuentas"]) == ["org1"]
    assert "org2 falló (HTTP 401)" in err and "falsa" not in out + err


@pytest.mark.parametrize("error", [ValueError(f"Invalid header value b'{LLAVE}\\n'"),
                                   urllib.error.URLError(f"x-api-key {LLAVE}\n")])
def test_la_llave_de_admin_nunca_sale_en_stderr(tmp_path, capsys, error):
    """de una excepción ajena solo se imprime el tipo; su texto puede traer la llave."""
    assert conciliar.main(["--secretos", str(secretos(tmp_path))], Abrir([error]), date(2026, 11, 11)) == 1
    out, err = capsys.readouterr()
    assert "falsa" not in out + err and f"org1 falló ({type(error).__name__})" in err


def test_sin_ninguna_cuenta_sale_con_1(tmp_path, capsys):
    abrir = Abrir([urllib.error.URLError("sin red")])
    assert conciliar.main(["--secretos", str(secretos(tmp_path))], abrir, date(2026, 11, 11)) == 1
    out, err = capsys.readouterr()
    assert out == "" and "falsa" not in err


def test_el_dia_de_hoy_no_se_manda(tmp_path, capsys):
    hoy = {"data": [{"starting_at": "2026-11-11T00:00:00Z", "results": [{"currency": "USD", "amount": "5"}]}],
           "has_more": False}
    conciliar.main(["--secretos", str(secretos(tmp_path))], Abrir([hoy]), date(2026, 11, 11))
    assert json.loads(capsys.readouterr().out) == {"cuentas": {"org1": []}}


def test_solo_biblioteca_estandar():
    """Corre con cualquier python3, sin el .venv: nada de dependencias ni imports del paquete."""
    arbol = ast.parse(Path(conciliar.__file__).read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "argparse", "json", "os", "re", "sys", "urllib", "datetime", "decimal", "pathlib"}
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(arbol))


def pagina_con(*montos, dia="2026-11-09", has_more=False, next_page=None):
    return {"data": [{"starting_at": f"{dia}T00:00:00Z",
                      "results": [{"currency": "USD", "amount": m} for m in montos]}],
            "has_more": has_more, "next_page": next_page}


def test_dia_con_total_negativo_sale_como_cero_y_avisa(tmp_path, capsys):
    neto = {"data": [{"starting_at": "2026-11-09T00:00:00Z", "results": [{"currency": "USD", "amount": "-5"}]},
                     {"starting_at": "2026-11-10T00:00:00Z",
                      "results": [{"currency": "USD", "amount": "10"}, {"currency": "USD", "amount": "-4"}]}],
            "has_more": False}
    assert conciliar.main(["--secretos", str(secretos(tmp_path))], Abrir([neto]), date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"cuentas": {"org1": [{"dia": "2026-11-09", "micro": 0},
                                                      {"dia": "2026-11-10", "micro": 60_000}]}}
    assert "org1 2026-11-09 sumó un total negativo" in err and "2026-11-10 sumó" not in err and "falsa" not in err


def test_la_cuenta_con_negativos_no_tumba_a_las_demas(tmp_path, capsys):
    abrir = Abrir([pagina_con("3"), pagina_con("-1")])
    assert conciliar.main(["--secretos", str(secretos(tmp_path, ("org2", "org1")))], abrir, date(2026, 11, 11)) == 0
    cuentas = json.loads(capsys.readouterr().out)["cuentas"]
    assert cuentas == {"org2": [{"dia": "2026-11-09", "micro": 0}], "org1": [{"dia": "2026-11-09", "micro": 30_000}]}


@pytest.mark.parametrize("cursor", [None, "", "falta"])
def test_has_more_sin_cursor_nuevo_falla_de_inmediato(cursor):
    pagina = pagina_con("1", has_more=True)
    if cursor != "falta":
        pagina["next_page"] = cursor
    else:
        del pagina["next_page"]
    abrir = Abrir([pagina, pagina_con("1")])
    with pytest.raises(conciliar.ErrorDeCuenta):
        conciliar.totales(LLAVE, datetime(2026, 11, 9), datetime(2026, 11, 10), abrir)
    assert len(abrir.pedidas) == 1                      # no se manda page=None ni se sigue pidiendo


def test_cursor_repetido_falla_sin_dar_vueltas():
    p2 = pagina_con("1", has_more=True, next_page="pag_2")
    abrir = Abrir([pagina_con("1", has_more=True, next_page="pag_2"), p2, p2, p2])
    with pytest.raises(conciliar.ErrorDeCuenta):
        conciliar.totales(LLAVE, datetime(2026, 11, 9), datetime(2026, 11, 10), abrir)
    assert len(abrir.pedidas) == 2


def test_main_avisa_cursor_sin_avanzar_por_cuenta(tmp_path, capsys):
    abrir = Abrir([pagina_con("2"), pagina_con("1", has_more=True)])      # org1 responde, org2 falla
    assert conciliar.main(["--secretos", str(secretos(tmp_path, ("org2", "org1")))], abrir, date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert list(json.loads(out)["cuentas"]) == ["org1"] and "org2 falló" in err and "cursor" in err


@pytest.mark.parametrize("dias", ["0", "-3", "abc"])
def test_dias_debe_ser_uno_o_mas(tmp_path, capsys, dias):
    abrir = Abrir([])
    with pytest.raises(SystemExit) as salida:
        conciliar.main(["--secretos", str(secretos(tmp_path)), "--dias", dias], abrir, date(2026, 11, 11))
    assert salida.value.code == 2 and abrir.pedidas == []


def test_archivo_sin_la_variable_dice_cual_archivo_y_las_demas_siguen(tmp_path, capsys):
    secretos(tmp_path, ("org1",))
    (tmp_path / "anthropic_admin_org2.env").write_text("OTRA_COSA=sk-ant-admin-falsa-999\n")
    assert conciliar.main(["--secretos", str(tmp_path)], Abrir([P2]), date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert "org2 falló (anthropic_admin_org2.env no trae ANTHROPIC_ADMIN_KEY)" in err
    assert list(json.loads(out)["cuentas"]) == ["org1"] and "falsa" not in out + err


def test_moneda_inesperada_dice_la_moneda_y_las_demas_siguen(tmp_path, capsys):
    mxn = {"data": [{"starting_at": "2026-11-09T00:00:00Z", "results": [{"currency": "MXN", "amount": "1"}]}],
           "has_more": False}
    assert conciliar.main(["--secretos", str(secretos(tmp_path, ("org2", "org1")))], Abrir([P2, mxn]),
                          date(2026, 11, 11)) == 0
    out, err = capsys.readouterr()
    assert "org2 falló (moneda inesperada 'MXN')" in err
    assert list(json.loads(out)["cuentas"]) == ["org1"] and "falsa" not in out + err
