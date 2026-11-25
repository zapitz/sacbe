"""Conciliación diaria desde tu máquina (no desde el servidor).
Lee el reporte de costos de cada cuenta con su llave de administrador y escribe en stdout, como JSON, el total por
día cerrado (UTC) en micro-USD. ops/pia conciliar lo manda al gestor por ssh.
Las llaves de administrador nunca salen de tu máquina: la salida solo trae cuenta, día y monto.
Solo biblioteca estándar: corre con cualquier python3 reciente.
Uso: python3 -m pasarela.conciliar [--dias 8] [--secretos ~/.config/sacbe]"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

URL = "https://api.anthropic.com/v1/organizations/cost_report"
VERSION = "2023-06-01"
ARCHIVO = re.compile(r"^anthropic_admin_([a-z][a-z0-9-]{1,30})\.env$")
VARIABLE = "ANTHROPIC_ADMIN_KEY"
POR_PAGINA = 31
PAGINAS_MAX = 100


class ErrorDeCuenta(ValueError):
    """Falla de una cuenta con un mensaje que es seguro imprimir: nunca trae la llave ni texto de la respuesta."""


def dias_positivos(texto: str) -> int:
    n = int(texto)
    if n < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o más")
    return n


def cuentas_con_llave(carpeta: Path) -> dict[str, Path]:
    return {m.group(1): p for p in sorted(carpeta.glob("anthropic_admin_*.env")) if (m := ARCHIVO.match(p.name))}


def leer_llave(ruta: Path) -> str:
    for linea in ruta.read_text().splitlines():
        clave, _, valor = linea.strip().partition("=")
        if clave.strip() == VARIABLE:
            valor = valor.strip().strip('"').strip("'")
            if valor:
                return valor
    raise ErrorDeCuenta(f"{ruta.name} no trae {VARIABLE}")


def a_micro(monto) -> int:
    """El reporte da el costo en centavos de USD como texto decimal: 1 centavo = 10,000 micro-USD."""
    return int((Decimal(str(monto)) * 10_000).to_integral_value(rounding=ROUND_HALF_UP))


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def pedir(llave: str, params: dict, abrir=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params, doseq=True),
                                 headers={"x-api-key": llave, "anthropic-version": VERSION,
                                          "user-agent": "pasarela-ia-conciliar"})
    with abrir(req, timeout=30) as r:
        return json.loads(r.read())


def totales(llave: str, desde: datetime, hasta: datetime, abrir=urllib.request.urlopen) -> dict[str, int]:
    params = {"starting_at": iso(desde), "ending_at": iso(hasta), "bucket_width": "1d", "limit": POR_PAGINA}
    dias: dict[str, int] = {}
    for _ in range(PAGINAS_MAX):              # un cursor que no avanza no se queda dando vueltas
        pagina = pedir(llave, params, abrir)
        for cubeta in pagina.get("data") or []:
            dia = str(cubeta["starting_at"])[:10]
            micro = 0
            for r in cubeta.get("results") or []:
                moneda = r.get("currency") or "USD"
                if moneda != "USD":
                    raise ErrorDeCuenta(f"moneda inesperada {str(moneda)[:10]!r}")
                micro += a_micro(r["amount"])
            dias[dia] = dias.get(dia, 0) + micro
        if not pagina.get("has_more"):
            return dias
        cursor = pagina.get("next_page")
        if not cursor or cursor == params.get("page"):     # sin cursor nuevo no hay página que pedir
            raise ErrorDeCuenta("el reporte dice que hay más páginas pero no da un cursor nuevo")
        params = {**params, "page": cursor}
    raise ErrorDeCuenta("el reporte no terminó de paginar")


def main(argv=None, abrir=urllib.request.urlopen, hoy: date | None = None) -> int:
    p = argparse.ArgumentParser(prog="pasarela.conciliar")
    p.add_argument("--dias", type=dias_positivos, default=8, help="días cerrados hacia atrás (por defecto 8)")
    p.add_argument("--secretos", default=os.environ.get("PIA_SECRETOS") or str(Path.home() / ".config" / "sacbe"))
    a = p.parse_args(argv)
    hoy = hoy or datetime.now(timezone.utc).date()
    hasta = datetime(hoy.year, hoy.month, hoy.day)
    desde = hasta - timedelta(days=a.dias)
    salida: dict[str, list] = {}
    for nombre, ruta in cuentas_con_llave(Path(a.secretos).expanduser()).items():
        try:
            dias = totales(leer_llave(ruta), desde, hasta, abrir)
        except urllib.error.HTTPError as e:
            print(f"conciliar: {nombre} falló (HTTP {e.code})", file=sys.stderr)
            continue
        except ErrorDeCuenta as e:
            print(f"conciliar: {nombre} falló ({e})", file=sys.stderr)
            continue
        except Exception as e:  # noqa: BLE001  solo el tipo: el texto podría traer la URL o parte de la respuesta
            print(f"conciliar: {nombre} falló ({type(e).__name__})", file=sys.stderr)
            continue
        cerrados = []
        for d, m in sorted(dias.items()):
            if d >= f"{hoy:%Y-%m-%d}":
                continue
            if m < 0:                              # un reembolso o crédito: el gestor rechaza negativos, va como 0
                print(f"conciliar: {nombre} {d} sumó un total negativo, se manda como 0", file=sys.stderr)
                m = 0
            cerrados.append({"dia": d, "micro": m})
        salida[nombre] = cerrados
        total = sum(x["micro"] for x in cerrados) / 1_000_000
        print(f"conciliar: {nombre} {len(cerrados)} días, US${total:,.2f}", file=sys.stderr)
    if not salida:
        print("conciliar: ninguna cuenta respondió", file=sys.stderr)
        return 1
    print(json.dumps({"cuentas": salida}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
