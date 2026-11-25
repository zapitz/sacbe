"""Pruebas de ops/cliente-python/sitecustomize.py (la pasarela automática para programas en Python)."""

import asyncio
import importlib.util
import json
import pathlib

import httpx
import httpx2
import pytest

RUTA = pathlib.Path(__file__).resolve().parents[1] / "ops" / "cliente-python" / "sitecustomize.py"
GESTOR = "http://pasarela-ia-api:8080/anthropic"


@pytest.fixture(params=["httpx", "httpx2"])
def lib(request):
    return {"httpx": httpx, "httpx2": httpx2}[request.param]


class Falso:
    """Hace de red: decide qué contesta cada host y anota lo que se pidió."""

    def __init__(self, gestor, lib):
        self.gestor = gestor  # Exception o (estado, cuerpo); la excepción se crea con la librería del caso
        self.lib = lib
        self.pedidas = []

    def responder(self, request):
        self.pedidas.append((request.url.host, request.url.path, request.headers.get("x-api-key"), request.content))
        if request.url.host == "api.anthropic.com":
            return self.lib.Response(200, json={"de": "directo"})
        if isinstance(self.gestor, str):
            raise getattr(self.lib, self.gestor)("falla de prueba")
        estado, cuerpo = self.gestor
        return self.lib.Response(estado, json=cuerpo)


@pytest.fixture
def cargar(monkeypatch, lib):
    def _cargar(gestor, url=GESTOR):
        falso = Falso(gestor, lib)

        def sync(self, request):
            return falso.responder(request)

        async def asincrono(self, request):
            return falso.responder(request)

        for m in (httpx, httpx2):
            monkeypatch.setattr(m.HTTPTransport, "handle_request", sync)
            monkeypatch.setattr(m.AsyncHTTPTransport, "handle_async_request", asincrono)
            monkeypatch.delattr(m, "_pasarela_ia_instalada", raising=False)
        monkeypatch.setenv("PASARELA_IA_URL", url)
        monkeypatch.setenv("PASARELA_IA_LLAVE", "pia_interna")
        spec = importlib.util.spec_from_file_location("pasarela_cliente_prueba", RUTA)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, falso

    return _cargar


def _pedir_sync(lib, path="/v1/messages"):
    with lib.Client() as c:
        return c.post("https://api.anthropic.com" + path, headers={"x-api-key": "directa"},
                      content=json.dumps({"m": 1}))


def _pedir_async(lib, path="/v1/messages"):
    async def go():
        async with lib.AsyncClient() as c:
            return await c.post("https://api.anthropic.com" + path, headers={"x-api-key": "directa"},
                                content=json.dumps({"m": 1}))
    return asyncio.run(go())


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
def test_va_por_el_gestor_con_llave_interna(cargar, pedir, lib):
    mod, falso = cargar((200, {"de": "gestor"}))
    r = pedir(lib)
    assert r.json() == {"de": "gestor"}
    assert falso.pedidas == [("pasarela-ia-api", "/anthropic/v1/messages", "pia_interna", b'{"m": 1}')]
    assert mod.estado()["enrutadas"] == 1


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
def test_count_tokens_tambien(cargar, pedir, lib):
    _, falso = cargar((200, {"de": "gestor"}))
    pedir(lib, "/v1/messages/count_tokens")
    assert falso.pedidas[0][1] == "/anthropic/v1/messages/count_tokens"


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
@pytest.mark.parametrize("gestor", [
    "ConnectError", "ConnectTimeout", "RemoteProtocolError",
    (502, {"x": 1}), (503, {"x": 1}), (504, {"x": 1}),
    (402, {"error": {"message": "pasarela-ia: sin crédito"}}),
    (401, {"error": {"message": "pasarela-ia: llave interna inválida"}}),
])
def test_regresa_directo_con_la_llave_original(cargar, pedir, gestor, lib):
    mod, falso = cargar(gestor)
    r = pedir(lib)
    assert r.json() == {"de": "directo"}
    assert falso.pedidas[-1] == ("api.anthropic.com", "/v1/messages", "directa", b'{"m": 1}')
    assert mod.estado()["regresos"] == 1
    # enfriamiento: la siguiente sale directo sin probar el gestor
    pedir(lib)
    assert [p[0] for p in falso.pedidas].count("pasarela-ia-api") == 1


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
def test_error_de_anthropic_se_devuelve_tal_cual(cargar, pedir, lib):
    mod, falso = cargar((429, {"error": {"message": "rate limited"}}))
    r = pedir(lib)
    assert r.status_code == 429 and r.json()["error"]["message"] == "rate limited"
    assert len(falso.pedidas) == 1 and mod.estado()["regresos"] == 0


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
def test_read_timeout_no_se_repite_y_tres_abren_enfriamiento(cargar, pedir, lib):
    mod, falso = cargar("ReadTimeout")
    for _ in range(3):
        with pytest.raises(lib.ReadTimeout):
            pedir(lib)
    assert all(p[0] == "pasarela-ia-api" for p in falso.pedidas) and len(falso.pedidas) == 3
    assert mod.estado()["regresos"] == 1
    assert pedir(lib).json() == {"de": "directo"}


@pytest.mark.parametrize("pedir", [_pedir_sync, _pedir_async])
def test_otras_rutas_salen_directo(cargar, pedir, lib):
    _, falso = cargar((200, {"de": "gestor"}))
    pedir(lib, "/v1/messages/batches")
    assert falso.pedidas[0][0] == "api.anthropic.com"


def test_sin_configuracion_no_instala(monkeypatch):
    monkeypatch.delenv("PASARELA_IA_URL", raising=False)
    antes = httpx.HTTPTransport.handle_request, httpx2.HTTPTransport.handle_request
    spec = importlib.util.spec_from_file_location("pasarela_cliente_vacio", RUTA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (httpx.HTTPTransport.handle_request, httpx2.HTTPTransport.handle_request) == antes
    assert mod.estado()["activa"] is False


def test_instala_en_las_dos_librerias(cargar):
    mod, _ = cargar((200, {}))
    assert mod.estado()["librerias"] == ["httpx", "httpx2"]


def test_avisa_si_el_sdk_usa_una_libreria_sin_cubrir(cargar, monkeypatch, capsys, tmp_path):
    sdk = tmp_path / "anthropic"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("")
    (sdk / "_base_client.py").write_text("import httpx3\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    cargar((200, {}))
    assert "ALERTA el SDK de anthropic usa 'httpx3'" in capsys.readouterr().err


def test_no_avisa_si_el_sdk_esta_cubierto(cargar, monkeypatch, capsys, tmp_path):
    sdk = tmp_path / "anthropic"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("")
    (sdk / "_base_client.py").write_text("import httpx2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    cargar((200, {}))
    assert "ALERTA" not in capsys.readouterr().err
