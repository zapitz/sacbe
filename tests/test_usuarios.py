import pytest
from sqlalchemy import func, select

from pasarela import admin
from pasarela.esquema import cambio, usuario, usuario_proyecto

POR = "admin"
CITA = "«da de alta a mi equipo»"


def _dueno(motor, correo="dueno@example.com"):
    admin.usuario_alta(motor, correo, "Admin", "dueno", POR, CITA)


def _cambios(motor):
    with motor.connect() as c:
        return [dict(f._mapping) for f in c.execute(select(cambio).order_by(cambio.c.id))]


def test_alta_guarda_en_minusculas_y_deja_bitacora(motor):
    admin.usuario_alta(motor, "  Rosa@Example.COM ", " Rosa ", "finanzas", POR, CITA)
    with motor.connect() as c:
        u = dict(c.execute(select(usuario)).one()._mapping)
    assert (u["correo"], u["nombre"], u["papel"], u["activo"], u["por"]) == \
           ("rosa@example.com", "Rosa", "finanzas", True, POR)
    ch, = _cambios(motor)
    assert (ch["accion"], ch["ordenado_por"], ch["cita"]) == ("usuario-alta", POR, CITA)
    assert "rosa@example.com" in ch["detalle"] and "finanzas" in ch["detalle"]


@pytest.mark.parametrize("correo", ["", "sin-arroba", "a@b", "a@@b.co", "a b@c.mx", "@c.mx", "a@.mx", "a@b.\nco"])
def test_correo_invalido(motor, correo):
    with pytest.raises(SystemExit) as e:
        admin.usuario_alta(motor, correo, "X", "lector", POR, CITA)
    assert "correo" in str(e.value.code).lower()
    assert _cambios(motor) == []


@pytest.mark.parametrize("papel", ["admin", "Dueño", "DUENO", "", "dueño"])
def test_papel_invalido(motor, papel):
    with pytest.raises(SystemExit) as e:
        admin.usuario_alta(motor, "a@b.mx", "A", papel, POR, CITA)
    assert "papel" in str(e.value.code).lower() and "dueno" in str(e.value.code)
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(usuario)).scalar() == 0
    assert _cambios(motor) == []


def test_alta_sin_nombre_o_sin_cita_no_cambia_nada(motor):
    with pytest.raises(SystemExit):
        admin.usuario_alta(motor, "a@b.mx", "  ", "lector", POR, CITA)
    with pytest.raises(SystemExit):
        admin.usuario_alta(motor, "a@b.mx", "A", "lector", POR, "  ")
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(usuario)).scalar() == 0
    assert _cambios(motor) == []


def test_alta_repetida_falla_pero_baja_se_puede_reactivar(motor):
    _dueno(motor)
    admin.usuario_alta(motor, "a@b.mx", "A", "lector", POR, CITA)
    with pytest.raises(SystemExit) as e:
        admin.usuario_alta(motor, "A@B.mx", "A", "lector", POR, CITA)
    assert "ya existe" in str(e.value.code)
    admin.usuario_baja(motor, "a@b.mx", POR, CITA)
    admin.usuario_alta(motor, "a@b.mx", "Ana", "responsable", POR, CITA)
    u = admin.usuario_de(motor, "a@b.mx")
    assert (u["nombre"], u["papel"], u["activo"]) == ("Ana", "responsable", True)


def test_baja_es_suave_y_usuario_de_solo_ve_activos(motor):
    _dueno(motor)
    admin.usuario_alta(motor, "a@b.mx", "A", "lector", POR, CITA)
    assert admin.usuario_de(motor, "A@B.MX ")["papel"] == "lector"
    admin.usuario_baja(motor, "a@b.mx", POR, "«fuera»")
    assert admin.usuario_de(motor, "a@b.mx") is None
    with motor.connect() as c:
        assert c.execute(select(usuario.c.activo).where(usuario.c.correo == "a@b.mx")).scalar() is False
    assert _cambios(motor)[-1]["accion"] == "usuario-baja"
    with pytest.raises(SystemExit):
        admin.usuario_baja(motor, "a@b.mx", POR, CITA)           # ya está de baja
    with pytest.raises(SystemExit):
        admin.usuario_baja(motor, "nadie@b.mx", POR, CITA)


def test_usuario_de_desconocido_da_none(motor):
    assert admin.usuario_de(motor, "nadie@b.mx") is None
    assert admin.usuario_de(motor, "") is None


def test_no_se_da_de_baja_al_ultimo_dueno(motor):
    _dueno(motor)
    n = len(_cambios(motor))
    with pytest.raises(SystemExit) as e:
        admin.usuario_baja(motor, "dueno@example.com", POR, CITA)
    assert "dueño" in str(e.value.code)
    assert admin.usuario_de(motor, "dueno@example.com") is not None
    assert len(_cambios(motor)) == n
    admin.usuario_alta(motor, "otro@ejemplo.mx", "Otro", "dueno", POR, CITA)
    admin.usuario_baja(motor, "dueno@example.com", POR, CITA)     # ya hay otro dueño activo
    with pytest.raises(SystemExit):
        admin.usuario_baja(motor, "otro@ejemplo.mx", POR, CITA)


def test_no_se_quita_el_papel_al_ultimo_dueno(motor):
    _dueno(motor)
    n = len(_cambios(motor))
    with pytest.raises(SystemExit) as e:
        admin.usuario_papel(motor, "dueno@example.com", "lector", POR, CITA)
    assert "dueño" in str(e.value.code)
    assert admin.usuario_de(motor, "dueno@example.com")["papel"] == "dueno"
    assert len(_cambios(motor)) == n
    admin.usuario_papel(motor, "dueno@example.com", "dueno", POR, CITA)      # seguir siendo dueño no estorba
    admin.usuario_alta(motor, "otro@ejemplo.mx", "Otro", "lector", POR, CITA)
    admin.usuario_papel(motor, "otro@ejemplo.mx", "dueno", POR, CITA)
    admin.usuario_papel(motor, "dueno@example.com", "finanzas", POR, CITA)
    assert admin.usuario_de(motor, "dueno@example.com")["papel"] == "finanzas"


def test_un_dueno_inactivo_no_cuenta_como_dueno(motor):
    _dueno(motor)
    admin.usuario_alta(motor, "otro@ejemplo.mx", "Otro", "dueno", POR, CITA)
    admin.usuario_baja(motor, "otro@ejemplo.mx", POR, CITA)
    with pytest.raises(SystemExit):
        admin.usuario_baja(motor, "dueno@example.com", POR, CITA)


def test_papel_invalido_o_usuario_inexistente(motor):
    _dueno(motor)
    admin.usuario_alta(motor, "a@b.mx", "A", "lector", POR, CITA)
    with pytest.raises(SystemExit) as e:
        admin.usuario_papel(motor, "a@b.mx", "jefe", POR, CITA)
    assert "papel" in str(e.value.code).lower()
    with pytest.raises(SystemExit):
        admin.usuario_papel(motor, "nadie@b.mx", "lector", POR, CITA)
    assert admin.usuario_de(motor, "a@b.mx")["papel"] == "lector"


def test_asignar_y_quitar_proyectos(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.alta(motor, "lotes", "extra", POR, CITA)
    admin.usuario_alta(motor, "a@b.mx", "A", "responsable", POR, CITA)
    admin.usuario_asignar(motor, "A@b.mx", "lotes", POR, CITA)
    admin.usuario_asignar(motor, "a@b.mx", "crm", POR, CITA)
    assert admin.usuario_de(motor, "a@b.mx")["proyectos"] == ["crm", "lotes"]
    assert _cambios(motor)[-1]["accion"] == "usuario-asignar"
    with pytest.raises(SystemExit):
        admin.usuario_asignar(motor, "a@b.mx", "crm", POR, CITA)             # ya lo tiene
    admin.usuario_quitar(motor, "a@b.mx", "crm", POR, CITA)
    assert admin.usuario_de(motor, "a@b.mx")["proyectos"] == ["lotes"]
    assert _cambios(motor)[-1]["accion"] == "usuario-quitar"
    with pytest.raises(SystemExit):
        admin.usuario_quitar(motor, "a@b.mx", "crm", POR, CITA)              # ya no lo tenía


def test_asignar_exige_usuario_y_proyecto_existentes(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    admin.usuario_alta(motor, "a@b.mx", "A", "responsable", POR, CITA)
    n = len(_cambios(motor))
    with pytest.raises(SystemExit) as e:
        admin.usuario_asignar(motor, "a@b.mx", "noexiste", POR, CITA)
    assert "noexiste" in str(e.value.code)
    with pytest.raises(SystemExit):
        admin.usuario_asignar(motor, "nadie@b.mx", "crm", POR, CITA)
    with motor.connect() as c:
        assert c.execute(select(func.count()).select_from(usuario_proyecto)).scalar() == 0
    assert len(_cambios(motor)) == n


def test_usuarios_lista_con_proyectos(motor):
    admin.alta(motor, "crm", "produccion", POR, CITA)
    _dueno(motor)
    admin.usuario_alta(motor, "a@b.mx", "A", "responsable", POR, CITA)
    admin.usuario_alta(motor, "z@b.mx", "Z", "lector", POR, CITA)
    admin.usuario_asignar(motor, "a@b.mx", "crm", POR, CITA)
    admin.usuario_baja(motor, "z@b.mx", POR, CITA)
    lista = admin.usuarios(motor)
    assert [u["correo"] for u in lista] == ["a@b.mx", "dueno@example.com", "z@b.mx"]
    por_correo = {u["correo"]: u for u in lista}
    assert por_correo["a@b.mx"]["proyectos"] == ["crm"]
    assert por_correo["dueno@example.com"]["proyectos"] == []
    assert por_correo["z@b.mx"]["activo"] is False
    assert set(por_correo["a@b.mx"]) >= {"correo", "nombre", "papel", "activo", "creado", "por", "proyectos"}


def test_main_usuario(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    admin.main(["alta", "crm", "--clase", "produccion", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["usuario", "alta", "Admin2@Example.org", "--nombre", "Admin", "--papel", "dueno", "--por", POR,
                "--cita", CITA])
    admin.main(["usuario", "alta", "c@example.com", "--papel", "responsable", "--por", POR, "--cita", CITA])
    admin.main(["usuario", "asignar", "c@example.com", "crm", "--por", POR, "--cita", CITA])
    admin.main(["usuario", "papel", "c@example.com", "--papel", "lector", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["usuario", "lista"])
    salida = capsys.readouterr().out
    assert "admin2@example.org" in salida and "dueno" in salida and "c@example.com" in salida
    assert "lector" in salida and "crm" in salida
    admin.main(["usuario", "quitar", "c@example.com", "crm", "--por", POR, "--cita", CITA])
    admin.main(["usuario", "baja", "c@example.com", "--por", POR, "--cita", CITA])
    capsys.readouterr()
    admin.main(["usuario", "lista"])
    assert "baja" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        admin.main(["usuario", "baja", "admin2@example.org", "--por", POR, "--cita", CITA])    # el último dueño
    admin.main(["cambios", "-n", "20"])
    assert "usuario-alta" in capsys.readouterr().out


@pytest.mark.parametrize("orden", [["alta", "a@b.mx", "--papel", "lector"], ["baja", "a@b.mx"],
                                   ["papel", "a@b.mx", "--papel", "lector"], ["asignar", "a@b.mx", "crm"],
                                   ["quitar", "a@b.mx", "crm"]])
def test_main_usuario_sin_por_no_corre(tmp_path, monkeypatch, orden):
    monkeypatch.setenv("PIA_DB_URL", f"sqlite:///{tmp_path}/a.db")
    with pytest.raises(SystemExit):
        admin.main(["usuario", *orden, "--cita", CITA])
    assert not (tmp_path / "a.db").exists()
