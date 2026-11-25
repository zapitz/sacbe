"""Horas: todo se guarda en UTC; solo para mostrar se pasa a la zona de PIA_ZONA_HORARIA."""
import os
import re
import sys
from datetime import datetime, timedelta, timezone, tzinfo

DESFASE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def zona_de(texto: str | None) -> tuple[tzinfo, str]:
    """La zona y su nombre para el panel. Acepta un nombre IANA («America/Mexico_City») o un desfase fijo («-06:00»).
    Vacío o desconocido: UTC. El nombre IANA necesita la base de zonas del sistema (o el paquete tzdata); el desfase
    fijo no necesita nada pero no sigue el horario de verano."""
    texto = (texto or "").strip()
    if not texto or texto.upper() == "UTC":
        return timezone.utc, "UTC"
    m = DESFASE.match(texto)
    if m:
        signo, h, mi = m.groups()
        delta = timedelta(hours=int(h), minutes=int(mi)) * (-1 if signo == "-" else 1)
        if abs(delta) < timedelta(hours=24):
            return timezone(delta), f"UTC{texto}"
    else:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(texto), texto
        except Exception:  # noqa: BLE001  sin base de zonas o nombre mal escrito
            pass
    print(f"pasarela: PIA_ZONA_HORARIA={texto!r} no es una zona válida; las horas se muestran en UTC", file=sys.stderr)
    return timezone.utc, "UTC"


ZONA, NOMBRE_ZONA = zona_de(os.environ.get("PIA_ZONA_HORARIA"))


def ahora() -> datetime:
    """UTC sin zona: así se guarda todo en la base (igual en Postgres y en SQLite)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def en_local(t: datetime) -> datetime:
    """Una hora de la base (UTC sin zona) en la zona de PIA_ZONA_HORARIA, solo para mostrarla."""
    return t.replace(tzinfo=timezone.utc).astimezone(ZONA)
