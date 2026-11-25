"""Tablas de la pasarela. Horas en UTC sin zona; dinero en micro-USD enteros."""
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, event)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()
ID = BigInteger().with_variant(Integer, "sqlite")

proyecto = Table(
    "proyecto", metadata,
    Column("nombre", String(40), primary_key=True),
    Column("clase", String(12), nullable=False),               # produccion | extra
    Column("llave_hash", String(64), nullable=False, unique=True),
    Column("huella", String(8), nullable=False),               # últimos 4 caracteres, para reconocerla
    Column("activo", Boolean, nullable=False),
    Column("dueno", String(40), nullable=False),
    Column("creado", DateTime, nullable=False),
)

fuente_estado = Table(
    "fuente_estado", metadata,
    Column("nombre", String(40), primary_key=True),
    Column("agotada_hasta", DateTime),
    Column("motivo", Text),
)

registro = Table(
    "registro", metadata,
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("proyecto", String(40), nullable=False, index=True),
    Column("fuente", String(40), nullable=False),
    Column("ruta", String(80), nullable=False),
    Column("modelo", String(80)),
    Column("status", Integer, nullable=False),
    Column("lote", Boolean, nullable=False),
    Column("entrada", BigInteger, nullable=False),
    Column("salida", BigInteger, nullable=False),
    Column("cache_5m", BigInteger, nullable=False),
    Column("cache_1h", BigInteger, nullable=False),
    Column("cache_lectura", BigInteger, nullable=False),
    Column("busquedas", Integer, nullable=False),
    Column("costo_micro", BigInteger, nullable=False),
    Column("latencia_ms", Integer, nullable=False),
    Column("request_id", String(80)),
    Column("reintentos", Integer, nullable=False),
)

objeto = Table(
    "objeto", metadata,
    Column("id", String(120), primary_key=True),
    Column("tipo", String(10), nullable=False),                # lote | archivo
    Column("fuente", String(40), nullable=False),
    Column("proyecto", String(40), nullable=False, index=True),
    Column("creado", DateTime, nullable=False),
    Column("costo_registrado", Boolean, nullable=False),
)

cambio = Table(
    "cambio", metadata,
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("ordenado_por", String(80), nullable=False),
    Column("cita", Text, nullable=False),
    Column("accion", String(40), nullable=False),
    Column("detalle", Text, nullable=False),
)


# Solo tablas nuevas: create_all no altera las que ya existen.

cuenta = Table(
    "cuenta", metadata,                                        # una organización de la consola de Anthropic
    Column("nombre", String(40), primary_key=True),            # el mismo nombre que en PIA_FUENTES y PIA_LLAVE_<NOMBRE>
    Column("correo_plan", String(120), nullable=False),
    Column("tiene_tarjeta", Boolean, nullable=False),
    Column("activa", Boolean, nullable=False),
    Column("llave_vence", DateTime),                           # vencimiento de la llave de servicio, si se sabe
    Column("creada", DateTime, nullable=False),
)

concesion = Table(
    "concesion", metadata,                                     # un crédito gratis de una cuenta
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("cuenta", String(40), nullable=False, index=True),
    Column("monto_micro", BigInteger, nullable=False),
    Column("otorgada", DateTime, nullable=False),              # 00:00 UTC del día de la concesión
    Column("vence", DateTime, nullable=False),                 # 00:00 UTC del día en que ya no cuenta
    Column("origen", String(10), nullable=False),              # consola | estimada
    Column("confirmada", Boolean, nullable=False),
    Column("creada", DateTime, nullable=False),
)

conciliacion = Table(
    "conciliacion", metadata,                                  # costo por día según el reporte de Anthropic
    Column("cuenta", String(40), primary_key=True),
    Column("dia", DateTime, primary_key=True),                 # 00:00 UTC del día
    Column("micro", BigInteger, nullable=False),
    Column("recibido", DateTime, nullable=False),
)

tope = Table(
    "tope", metadata,                                          # tope mensual por proyecto (opcional)
    Column("proyecto", String(40), primary_key=True),
    Column("tope_mensual_micro", BigInteger, nullable=False),
    Column("modo", String(6), nullable=False),                 # aviso | corte
    Column("actualizado", DateTime, nullable=False),
)

ajuste = Table(
    "ajuste", metadata,                                        # valores sueltos; hoy: presupuesto_pagado_micro
    Column("clave", String(40), primary_key=True),
    Column("valor", BigInteger, nullable=False),
    Column("actualizado", DateTime, nullable=False),
)

aviso = Table(
    "aviso", metadata,                                         # los manda ops/vigia.sh a PIA_AVISOS_URL
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("clave_unica", String(160), nullable=False, unique=True),
    Column("texto", Text, nullable=False),
    Column("enviado", DateTime),                               # None: pendiente
)


# También solo tablas nuevas.

modelo = Table(
    "modelo", metadata,                                        # la última lista buena de GET /v1/models
    Column("id", String(80), primary_key=True),
    Column("nombre", String(120), nullable=False),
    Column("familia", String(20)),                             # opus, sonnet, haiku...; None con nombres viejos
    Column("creado", DateTime, nullable=False),                # created_at de Anthropic, UTC sin zona
    Column("visto", DateTime, nullable=False),                 # cuándo se leyó esa lista
)

alias_modelo = Table(
    "alias_modelo", metadata,                                  # a qué modelo apunta cada alias «lo más nuevo»
    Column("alias", String(40), primary_key=True),
    Column("apunta_a", String(80), nullable=False),
    Column("desde", DateTime, nullable=False),
)

lectura = Table(
    "lectura", metadata,                                       # quién puede ver el uso del mes de quién (/v1/uso)
    Column("lector", String(40), primary_key=True),
    Column("visto", String(40), primary_key=True),
    Column("creado", DateTime, nullable=False),
)

# También solo tablas nuevas. Las demás tablas no declaran llaves foráneas y lo validan en admin.py;
# estas dos sí las declaran porque la spec lo pide. admin.py igual valida antes, para dar un mensaje en español.

usuario = Table(
    "usuario", metadata,                                       # quién puede entrar al panel de Sacbé
    Column("correo", String(120), primary_key=True),           # siempre en minúsculas y sin espacios
    Column("nombre", String(80), nullable=False),
    Column("papel", String(12), nullable=False),               # dueno | finanzas | responsable | lector (ASCII)
    Column("activo", Boolean, nullable=False),
    Column("creado", DateTime, nullable=False),
    Column("por", String(80), nullable=False),
)

usuario_proyecto = Table(
    "usuario_proyecto", metadata,                              # qué proyectos ve y maneja cada usuario
    Column("correo", String(120), ForeignKey("usuario.correo"), primary_key=True),
    Column("proyecto", String(40), ForeignKey("proyecto.nombre"), primary_key=True),
)

POOL, DESBORDE = 5, 5
# Hilos para llamadas a la base, uno por cada conexión posible, en dos limitadores: las filas de uso que se escriben
# ya enviada la respuesta no pueden quitarle turno a lo que una petición o el refresco están esperando.
HILOS_LECTURA = 4                     # fuente_fijada, /salud, refresco, recalcular de la bolsa, marcar_agotada,
                                      # guardar_creado, guardar_generados, quitar_de_la_lista y los listados
HILOS_ESCRITURA = POOL + DESBORDE - HILOS_LECTURA   # 6: filas de uso y costo de lotes, después de responder


def crear_motor(url: str) -> Engine:
    if url.startswith("sqlite"):
        motor = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})

        # Receta de SQLAlchemy para pysqlite: sin esto el driver no emite BEGIN y un SAVEPOINT que sea lo primero de
        # la transacción la abre y la cierra solo (su RELEASE hace commit). Así SQLite se porta como Postgres.
        @event.listens_for(motor, "connect")
        def _sin_transaccion_del_driver(dbapi_conexion, _registro):
            dbapi_conexion.isolation_level = None

        @event.listens_for(motor, "begin")
        def _begin_explicito(conexion):
            conexion.exec_driver_sql("BEGIN")
    else:
        # keepalives y tcp_user_timeout: una conexión muerta sin aviso (red caída, base congelada) se corta en
        # segundos en vez de dejar un hilo colgado lo que tarde el TCP del sistema.
        motor = create_engine(url, pool_pre_ping=True, pool_size=POOL, max_overflow=DESBORDE, pool_timeout=5,
                              connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000",
                                            "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
                                            "keepalives_count": 3, "tcp_user_timeout": 10000})
    metadata.create_all(motor)
    return motor
