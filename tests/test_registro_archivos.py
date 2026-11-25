"""Registro de objetos en la base: archivos que nacen en respuestas, página del registro y borrado."""
from datetime import datetime, timedelta

from sqlalchemy import event, insert, select

from pasarela.almacen import (guardar_objeto, guardar_objetos, marcar_borrado, marcar_caducos, objeto_de,
                              objetos_entre, pagina_de_objetos)
from pasarela.esquema import objeto

T0 = datetime(2026, 11, 14, 12, 0, 0)


def sembrar(motor, filas):
    """filas: (id, tipo, cuenta, proyecto, segundos después de T0)."""
    with motor.begin() as c:
        for i, tipo, fuente, proyecto, s in filas:
            c.execute(insert(objeto).values(id=i, tipo=tipo, fuente=fuente, proyecto=proyecto,
                                            creado=T0 + timedelta(seconds=s), costo_registrado=False))


def cinco(motor):
    sembrar(motor, [("file_a", "archivo", "org1", "crm", 1), ("file_b", "archivo", "org2", "crm", 2),
                    ("file_c", "archivo", "org1", "crm", 3), ("file_d", "archivo", "org1", "crm", 3),
                    ("file_e", "archivo", "org2", "crm", 5), ("file_x", "archivo", "org1", "lotes", 4),
                    ("msgbatch_1", "lote", "org1", "crm", 6)])


def test_guardar_objetos_el_primero_se_lo_queda(motor):
    guardar_objeto(motor, "file_subido", "archivo", "org2", "lotes")
    assert guardar_objetos(motor, {"file_g1", "file_g2", "file_subido"}, "archivo", "org1", "crm") == 2
    assert guardar_objetos(motor, {"file_g1"}, "archivo", "org2", "lotes") == 0
    assert (objeto_de(motor, "file_g1")["fuente"], objeto_de(motor, "file_g1")["proyecto"]) == ("org1", "crm")
    assert (objeto_de(motor, "file_subido")["fuente"], objeto_de(motor, "file_subido")["proyecto"]) == ("org2", "lotes")
    assert objeto_de(motor, "file_g2")["tipo"] == "archivo" and objeto_de(motor, "file_g2")["costo_registrado"] is False
    assert guardar_objetos(motor, set(), "archivo", "org1", "crm") == 0


def test_pagina_del_mas_nuevo_al_mas_viejo_solo_del_proyecto_y_del_tipo(motor):
    cinco(motor)
    filas, hay_mas = pagina_de_objetos(motor, "crm", "archivo", 2)
    assert filas == [("file_e", "org2"), ("file_d", "org1")] and hay_mas is True
    filas, hay_mas = pagina_de_objetos(motor, "crm", "archivo", 2, despues="file_d")
    assert filas == [("file_c", "org1"), ("file_b", "org2")] and hay_mas is True      # mismo creado: decide el id
    filas, hay_mas = pagina_de_objetos(motor, "crm", "archivo", 2, despues="file_b")
    assert filas == [("file_a", "org1")] and hay_mas is False
    assert pagina_de_objetos(motor, "crm", "archivo", 2, despues="file_a") == ([], False)
    assert pagina_de_objetos(motor, "crm", "lote", 5) == ([("msgbatch_1", "org1")], False)
    assert pagina_de_objetos(motor, "nadie", "archivo", 5) == ([], False)


def test_pagina_hacia_atras(motor):
    cinco(motor)
    filas, hay_mas = pagina_de_objetos(motor, "crm", "archivo", 2, antes="file_b")
    assert filas == [("file_d", "org1"), ("file_c", "org1")] and hay_mas is True   # los 2 inmediatamente anteriores
    filas, hay_mas = pagina_de_objetos(motor, "crm", "archivo", 2, antes="file_d")
    assert filas == [("file_e", "org2")] and hay_mas is False
    assert pagina_de_objetos(motor, "crm", "archivo", 2, antes="file_e") == ([], False)


def test_cursor_de_otro_proyecto_o_desconocido_es_none(motor):
    cinco(motor)
    assert pagina_de_objetos(motor, "crm", "archivo", 2, despues="file_x") is None
    assert pagina_de_objetos(motor, "crm", "archivo", 2, antes="file_no_existe") is None


def test_borrado_sale_de_la_lista_y_sigue_de_cursor(motor):
    cinco(motor)
    assert marcar_borrado(motor, "file_c", "crm") is True
    assert marcar_borrado(motor, "file_c", "crm") is False             # ya estaba
    assert marcar_borrado(motor, "file_x", "crm") is False             # es de otro proyecto
    assert marcar_borrado(motor, "msgbatch_1", "crm") is False         # un lote no se borra así
    assert [i for i, _ in pagina_de_objetos(motor, "crm", "archivo", 9)[0]] == ["file_e", "file_d", "file_b", "file_a"]
    assert [i for i, _ in pagina_de_objetos(motor, "crm", "archivo", 9, despues="file_c")[0]] == ["file_b", "file_a"]
    o = objeto_de(motor, "file_c")
    assert (o["tipo"], o["fuente"], o["proyecto"]) == ("borrado", "org1", "crm")     # sigue fijando la cuenta
    with motor.connect() as c:
        assert c.execute(select(objeto.c.tipo).where(objeto.c.id == "file_x")).scalar() == "archivo"


def test_objetos_entre(motor):
    cinco(motor)
    assert objetos_entre(motor, "crm", "archivo", ["file_a", "file_e", "file_x", "msgbatch_1", "file_nada"]) == {
        "file_a": "org1", "file_e": "org2"}
    assert objetos_entre(motor, "crm", "archivo", []) == {}


def test_guardar_objetos_cuenta_los_que_entraron_sin_savepoints_ni_errores_de_llave(motor):
    """Un solo INSERT ... ON CONFLICT DO NOTHING con todos los ids. Nada de SAVEPOINT (en Postgres cada uno es
    un viaje de ida y vuelta) ni de IntegrityError atrapado. Cuenta con RETURNING: el rowcount en Postgres da -1."""
    guardar_objeto(motor, "file_vivo", "archivo", "org2", "lotes")
    sentencias = []

    def espia(conn, cursor, statement, *a):
        sentencias.append(statement)

    event.listen(motor, "before_cursor_execute", espia)
    try:
        n = guardar_objetos(motor, {"file_vivo", "file_n1", "file_n2", "file_n3"}, "archivo", "org1", "crm")
    finally:
        event.remove(motor, "before_cursor_execute", espia)
    assert n == 3
    assert not any("SAVEPOINT" in x.upper() for x in sentencias)
    inserts = [x for x in sentencias if x.upper().startswith("INSERT")]
    assert len(inserts) == 1 and all("ON CONFLICT" in x.upper() and "DO NOTHING" in x.upper() for x in inserts)
    assert objeto_de(motor, "file_vivo")["proyecto"] == "lotes"                       # el primero se lo queda
    assert guardar_objetos(motor, {"file_n1", "file_n2"}, "archivo", "org2", "otro") == 0


def test_la_sentencia_de_guardar_objetos_existe_para_sqlite_y_para_postgres():
    from sqlalchemy.dialects import postgresql, sqlite

    from pasarela.almacen import insertar_si_no_esta

    for dialecto in (sqlite.dialect(), postgresql.dialect()):
        sql = str(insertar_si_no_esta(dialecto.name).values(id="file_1", tipo="archivo", fuente="org2", proyecto="crm",
                                                            creado=T0, costo_registrado=False)
                  .compile(dialect=dialecto)).upper()
        assert "ON CONFLICT (ID) DO NOTHING" in sql, dialecto.name


def test_guardar_objetos_sin_ids_no_toca_la_base(motor):
    sentencias = []

    def espia(conn, cursor, statement, *a):
        sentencias.append(statement)

    event.listen(motor, "before_cursor_execute", espia)
    try:
        assert guardar_objetos(motor, set(), "archivo", "org1", "crm") == 0
    finally:
        event.remove(motor, "before_cursor_execute", espia)
    assert sentencias == []


def test_marcar_caducos_solo_toca_filas_del_proyecto_y_del_tipo(motor):
    """N8: aunque le lleguen ids ajenos o de otro tipo, solo cambia los archivos viejos de ese proyecto."""
    with motor.begin() as c:
        for i, tipo, proyecto in [("file_mio", "archivo", "crm"), ("file_ajeno", "archivo", "lotes"),
                                  ("msgbatch_mio", "lote", "crm")]:
            c.execute(insert(objeto).values(id=i, tipo=tipo, fuente="org1", proyecto=proyecto, creado=T0,
                                            costo_registrado=False))
    assert marcar_caducos(motor, "crm", "archivo", ["file_mio", "file_ajeno", "msgbatch_mio"]) == 1
    assert [objeto_de(motor, i)["tipo"] for i in ("file_mio", "file_ajeno", "msgbatch_mio")] == [
        "caduco", "archivo", "lote"]
