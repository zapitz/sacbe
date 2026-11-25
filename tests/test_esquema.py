import os

from sqlalchemy import MetaData, create_engine, func, insert, inspect, select

from pasarela.esquema import crear_motor, metadata
from pasarela.tiempo import ahora


def test_crea_tablas(motor):
    assert set(inspect(motor).get_table_names()) >= {"proyecto", "fuente_estado", "registro", "objeto", "cambio"}


def test_ahora_sin_zona():
    assert ahora().tzinfo is None


def test_motor_postgres_con_tiempos_limite(monkeypatch):
    """Una base lenta no debe sumar 30 s a cada peticion: se mira la configuracion sin conectar."""
    from pasarela import esquema

    vistos = {}
    monkeypatch.setattr(esquema, "create_engine", lambda url, **kw: vistos.update(kw) or object())
    monkeypatch.setattr(esquema.metadata, "create_all", lambda m: None)
    esquema.crear_motor("postgresql+psycopg://x@127.0.0.1:1/x")
    assert vistos["pool_timeout"] == 5
    assert vistos["connect_args"] == {"connect_timeout": 5, "options": "-c statement_timeout=10000",
                                      "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
                                      "keepalives_count": 3, "tcp_user_timeout": 10000}
    assert vistos["pool_size"] + vistos["max_overflow"] == 10


COLUMNAS_PLAN_A = {
    "proyecto": {"nombre", "clase", "llave_hash", "huella", "activo", "dueno", "creado"},
    "fuente_estado": {"nombre", "agotada_hasta", "motivo"},
    "registro": {"id", "ts", "proyecto", "fuente", "ruta", "modelo", "status", "lote", "entrada", "salida",
                 "cache_5m", "cache_1h", "cache_lectura", "busquedas", "costo_micro", "latencia_ms", "request_id",
                 "reintentos"},
    "objeto": {"id", "tipo", "fuente", "proyecto", "creado", "costo_registrado"},
    "cambio": {"id", "ts", "ordenado_por", "cita", "accion", "detalle"},
}
NUEVAS = {"cuenta", "concesion", "conciliacion", "tope", "ajuste", "aviso"}
NUEVAS_C = {"modelo", "alias_modelo", "lectura"}


def test_tablas_del_plan_a_no_cambian():
    """create_all no altera tablas existentes: una columna nueva aquí no aparecería en producción."""
    for nombre, columnas in COLUMNAS_PLAN_A.items():
        assert {c.name for c in metadata.tables[nombre].columns} == columnas, nombre


def test_crea_las_tablas_nuevas(motor):
    assert set(inspect(motor).get_table_names()) >= NUEVAS | NUEVAS_C


def test_lectura_es_tabla_nueva_con_llave_doble():
    t = metadata.tables["lectura"]
    assert {c.name for c in t.columns} == {"lector", "visto", "creado"}
    assert {c.name for c in t.primary_key.columns} == {"lector", "visto"}


def test_base_del_plan_a_conserva_sus_filas(tmp_path):
    url = os.environ.get("PIA_PRUEBAS_DB") or f"sqlite:///{tmp_path}/plan_a.db"
    viejo = MetaData()
    for nombre in COLUMNAS_PLAN_A:
        metadata.tables[nombre].to_metadata(viejo)
    m = create_engine(url)
    metadata.drop_all(m)
    viejo.create_all(m)
    t = ahora()
    with m.begin() as c:
        c.execute(insert(metadata.tables["proyecto"]).values(nombre="crm", clase="produccion", llave_hash="h" * 64,
                                                             huella="abcd", activo=True, dueno="admin", creado=t))
        c.execute(insert(metadata.tables["cambio"]).values(ts=t, ordenado_por="admin", cita="«x»",
                                                           accion="proyecto-alta", detalle="{}"))
    m.dispose()
    nuevo = crear_motor(url)
    assert set(inspect(nuevo).get_table_names()) >= NUEVAS | NUEVAS_C | set(COLUMNAS_PLAN_A)
    with nuevo.connect() as c:
        for nombre in ("proyecto", "cambio"):
            assert c.execute(select(func.count()).select_from(metadata.tables[nombre])).scalar() == 1
    if not url.startswith("sqlite"):
        metadata.drop_all(nuevo)
    nuevo.dispose()
