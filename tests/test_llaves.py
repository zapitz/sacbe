from pasarela.llaves import hash_llave, llave_de_peticion, nueva_llave


def test_nueva_llave():
    a, b = nueva_llave("lotes"), nueva_llave("lotes")
    assert a.startswith("pia_lotes_") and len(a) >= len("pia_lotes_") + 32 and a != b


def test_hash_estable():
    assert hash_llave("x") == hash_llave("x") and len(hash_llave("x")) == 64


def test_llave_de_peticion():
    assert llave_de_peticion({"x-api-key": "pia_a_b"}) == "pia_a_b"
    assert llave_de_peticion({"authorization": "Bearer pia_a_b"}) == "pia_a_b"
    assert llave_de_peticion({"authorization": "Basic zzz"}) is None
    assert llave_de_peticion({}) is None
