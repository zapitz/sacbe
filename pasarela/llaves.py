"""Llaves internas por proyecto. En la base solo se guarda su sha256 y los últimos 4 caracteres."""
import hashlib
import secrets


def nueva_llave(proyecto: str) -> str:
    return f"pia_{proyecto}_{secrets.token_urlsafe(24)}"


def hash_llave(llave: str) -> str:
    return hashlib.sha256(llave.encode()).hexdigest()


def llave_de_peticion(headers) -> str | None:
    k = headers.get("x-api-key")
    if k:
        return k.strip()
    a = headers.get("authorization") or ""
    if a.lower().startswith("bearer "):
        return a[7:].strip() or None
    return None
