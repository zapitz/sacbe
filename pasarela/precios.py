"""Costo estimado en micro-USD. Precio en USD por millón de tokens = micro-USD por token.
Precios de lista de Anthropic, noviembre de 2026. Es una estimación: la conciliación con el reporte de costos corrige."""
import re
import sys
from dataclasses import dataclass

COLUMNAS = ("entrada", "salida", "cache_5m", "cache_1h", "cache_lectura", "busquedas")


@dataclass(frozen=True)
class Precio:
    entrada: float
    salida: float
    lectura: float                    # lectura de caché
    largo: "Precio | None" = None     # tarifa de prompts de más de 100K tokens (Haiku 5.5)


PRECIOS = {
    # Fable 5.2, precio publicado en noviembre de 2026: US$8 / US$40, lectura US$0.20;
    # escrituras de caché US$10 (5 min) y US$16 (1 h), lotes a la mitad: los mismos factores de costo_micro.
    "claude-fable-5-2": Precio(8, 40, 0.20),
    "claude-fable-5-1": Precio(10, 50, 0.25),
    "claude-mythos-5-1": Precio(10, 50, 0.25),
    "claude-fable-5": Precio(10, 50, 1.0),
    "claude-mythos-5": Precio(10, 50, 1.0),
    "claude-opus-5-5": Precio(4, 20, 0.20),
    "claude-opus-5": Precio(5, 25, 0.50),
    "claude-opus-4-8": Precio(5, 25, 0.50),
    "claude-opus-4-7": Precio(5, 25, 0.50),
    "claude-opus-4-6": Precio(5, 25, 0.50),
    "claude-opus-4-5": Precio(5, 25, 0.50),
    "claude-sonnet-5-5": Precio(2, 10, 0.20),
    "claude-sonnet-5": Precio(2, 10, 0.20),
    "claude-sonnet-4-6": Precio(3, 15, 0.30),
    "claude-sonnet-4-5": Precio(3, 15, 0.30),
    "claude-haiku-5-5": Precio(0.10, 0.50, 0.01, largo=Precio(0.50, 2.50, 0.05)),
    "claude-haiku-4-5": Precio(1, 5, 0.10),
}
CARO = PRECIOS["claude-fable-5-1"]
UMBRAL_LARGO = 100_000
BUSQUEDA_MICRO = 10_000               # US$10 por cada 1,000 búsquedas web


_AVISADOS: set[str] = set()


def avisar_sin_precio(modelo) -> None:
    """Una línea en stderr por modelo y por proceso: así se nota que falta en PRECIOS sin llenar el log."""
    nombre = str(modelo)[:80]
    if nombre not in _AVISADOS:
        _AVISADOS.add(nombre)
        print(f"pasarela: modelo sin precio {nombre}, se estimó como Fable 5.1", file=sys.stderr)


def normalizar(modelo: str) -> str:
    m = re.sub(r"\[.*\]$", "", modelo or "")
    return re.sub(r"-\d{8}$", "", m)


def tokens(uso: dict) -> dict[str, int]:
    cc = uso.get("cache_creation") or {}
    e5 = cc.get("ephemeral_5m_input_tokens")
    if e5 is None:
        e5 = uso.get("cache_creation_input_tokens") or 0
    return {"entrada": uso.get("input_tokens") or 0,
            "salida": uso.get("output_tokens") or 0,
            "cache_5m": e5 or 0,
            "cache_1h": cc.get("ephemeral_1h_input_tokens") or 0,
            "cache_lectura": uso.get("cache_read_input_tokens") or 0,
            "busquedas": (uso.get("server_tool_use") or {}).get("web_search_requests") or 0}


def costo_micro(modelo: str, uso: dict, lote: bool = False) -> tuple[int, bool]:
    """Devuelve (micro-USD, modelo_conocido). Un modelo desconocido se cobra como el más caro."""
    p = PRECIOS.get(normalizar(modelo))
    conocido = p is not None
    p = p or CARO
    t = tokens(uso)
    if p.largo and t["entrada"] + t["cache_lectura"] + t["cache_5m"] + t["cache_1h"] > UMBRAL_LARGO:
        p = p.largo
    micro = (t["entrada"] * p.entrada + t["salida"] * p.salida + t["cache_lectura"] * p.lectura
             + t["cache_5m"] * p.entrada * 1.25 + t["cache_1h"] * p.entrada * 2)
    if uso.get("speed") == "fast":
        micro *= 2
    if lote:
        micro *= 0.5
    return round(micro + t["busquedas"] * BUSQUEDA_MICRO), conocido
