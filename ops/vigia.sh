#!/usr/bin/env bash
# Vigía de Sacbé. Corre en el servidor donde están los contenedores, avisa y no arregla nada.
# Avisa de esto:
#   - un contenedor que no contesta /salud dos veces seguidas (10 minutos con el cron de abajo);
#   - la base sin responder tres veces seguidas;
#   - los avisos que el gestor deja en su tabla aviso (cuenta agotada, cambio de cuenta en uso, dinero, topes,
#     llaves). Se mandan en orden, hasta 10 por vuelta, y se marcan uno por uno; si el envío falla, se quedan
#     pendientes para la siguiente vuelta;
#   - una vez por hora, los programas con tráfico real que llevan 48 h sin pasar por el gestor (admin silencios);
#   - en cada vuelta, los avisos firmados (webhooks) a otros programas: admin webhook generar y entregar.
#
# Cada aviso sale como POST JSON {"texto": "..."} a PIA_AVISOS_URL (un webhook tuyo: chat, correo, lo que sea).
# Si existe PIA_AVISOS_TOKEN, va como «Authorization: Bearer <token>». Sin PIA_AVISOS_URL solo se escribe en el log
# y los avisos de la tabla quedan pendientes.
#
# Variables (en el entorno o en el archivo PIA_VIGIA_ENV, que se carga si existe):
#   PIA_AVISOS_URL     a dónde mandar los avisos, por ejemplo https://avisos.example.com/sacbe
#   PIA_AVISOS_TOKEN   opcional; nunca va en los argumentos de curl
#   PIA_VIGIA_ESTADO   archivo con los contadores entre vueltas (por omisión /var/lib/sacbe-vigia.state)
#   PIA_VIGIA_ENV      archivo con las variables de arriba, modo 600 (por omisión /etc/sacbe/vigia.env)
#
# Cron: */5 * * * * /ruta/a/sacbe/ops/vigia.sh >> /var/log/sacbe-vigia.log 2>&1
# Prueba sin mandar ni marcar nada: ops/vigia.sh --prueba
# Hace falta bash 4 o más (arreglos asociativos), docker, curl y python3.
set -uo pipefail
VIGIA_ENV="${PIA_VIGIA_ENV:-/etc/sacbe/vigia.env}"
# shellcheck disable=SC1090
[[ -f "$VIGIA_ENV" ]] && . "$VIGIA_ENV"
ESTADO="${PIA_VIGIA_ESTADO:-/var/lib/sacbe-vigia.state}"
AVISOS_URL="${PIA_AVISOS_URL:-}"
AVISOS_TOKEN="${PIA_AVISOS_TOKEN:-}"
LOG="[$(date '+%Y-%m-%d %H:%M:%S')]"
PRUEBA=false
[[ "${1:-}" == "--prueba" ]] && PRUEBA=true

enviar() {
  local texto="$1" rc token
  if $PRUEBA; then echo "$LOG PRUEBA, no se manda: $texto"; return 0; fi
  if [[ -z "$AVISOS_URL" ]]; then
    echo "$LOG sin PIA_AVISOS_URL; no se mandó (los avisos de la tabla se reintentan): $texto"
    return 1
  fi
  # La cabecera con el token va por un archivo de configuración de curl (sustitución de procesos), no en los
  # argumentos, para que no se vea en ps. Se escapan la diagonal inversa y las comillas del valor.
  token="${AVISOS_TOKEN//\\/\\\\}"; token="${token//\"/\\\"}"
  curl -sf -m 10 -X POST "$AVISOS_URL" -H "Content-Type: application/json" \
    -K <(if [[ -n "$token" ]]; then printf 'header = "Authorization: Bearer %s"\n' "$token"; fi) \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"texto": sys.argv[1]}))' "$texto")" >/dev/null
  rc=$?
  if (( rc != 0 )); then echo "$LOG el envío falló (curl salió con $rc); el aviso de la tabla queda pendiente: $texto"; return 1; fi
  echo "$LOG aviso enviado: $texto"
}

salud() {
  docker exec "$1" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/salud', timeout=5).read().decode())" 2>/dev/null
}

admin() {
  docker exec pasarela-ia-api python -m pasarela.admin "$@" 2>/dev/null
}

declare -A E
[[ -f "$ESTADO" ]] && while IFS='=' read -r k v; do [[ -n "$k" ]] && E[$k]="$v"; done < "$ESTADO"
J_API=""

for c in pasarela-ia-api pasarela-ia-panel; do
  j=$(salud "$c")
  clave="fallas_${c//-/_}"
  if [[ -z "$j" ]]; then
    E[$clave]=$(( ${E[$clave]:-0} + 1 ))
    (( ${E[$clave]} == 2 )) && enviar "pasarela-ia: $c no contesta desde hace 10 minutos. Los programas que pasan por ahí pueden estar fallando."
  else
    (( ${E[$clave]:-0} >= 2 )) && enviar "pasarela-ia: $c ya contesta de nuevo."
    E[$clave]=0
    [[ "$c" == pasarela-ia-api ]] && J_API="$j"
  fi
done

if [[ -n "$J_API" ]]; then
  base=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["base"])' "$J_API")
  if [[ "$base" != "True" ]]; then
    E[fallas_base]=$(( ${E[fallas_base]:-0} + 1 ))
    (( ${E[fallas_base]} == 3 )) && enviar "pasarela-ia: la base no responde. El gestor sigue pasando peticiones pero no registra el gasto."
  else
    E[fallas_base]=0
    if (( 10#$(date +%M) < 5 )) && ! $PRUEBA; then
      admin silencios --avisar >/dev/null || echo "$LOG no pude revisar los programas callados"
    fi
    if ! $PRUEBA; then   # avisos firmados a otros programas (webhook): primero se arman, luego se mandan
      admin webhook generar >/dev/null || echo "$LOG no pude generar los webhooks"
      admin webhook entregar | grep -v ' 0 entregados · 0 por reintentar · 0 fallidos' || true
    fi
    pendientes=$(admin avisos --pendientes --max 10)
    while IFS= read -r linea; do
      [[ -z "$linea" ]] && continue
      id=$(python3 -c 'import json,sys; print(int(json.loads(sys.argv[1])["id"]))' "$linea" 2>/dev/null) || continue
      texto=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["texto"])' "$linea" 2>/dev/null) || continue
      if enviar "$texto" && ! $PRUEBA; then
        admin avisos --marcar "$id" >/dev/null || echo "$LOG no pude marcar el aviso $id; puede repetirse"
      fi
    done <<< "$pendientes"
  fi
fi

$PRUEBA && { echo "$LOG PRUEBA terminada; estado sin tocar"; exit 0; }
: > "$ESTADO.tmp"
for k in "${!E[@]}"; do echo "$k=${E[$k]}" >> "$ESTADO.tmp"; done
mv "$ESTADO.tmp" "$ESTADO"
