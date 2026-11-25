"""Lee modelo y usage de las respuestas de Anthropic sin guardar el texto."""
import json

from .precios import COLUMNAS, avisar_sin_precio, costo_micro, tokens

LARGO_ID = 120                        # lo que cabe en objeto.id
TERCEROS = {"mcp_tool_result"}        # resultado que escribe un servidor MCP ajeno: ahí un file_id no es de Anthropic


def archivos_de_bloque(bloque) -> set[str]:
    """Los file_id, a cualquier profundidad, de un bloque de contenido que es resultado de una herramienta de
    servidor: su tipo termina en _tool_result (bash_code_execution_tool_result hoy, y los que vengan). No se miran
    tool_use ni server_tool_use, que los escribe el modelo, ni mcp_tool_result, que lo escribe un tercero: un
    file_id ahí no es un archivo que nació en esta respuesta."""
    if not isinstance(bloque, dict):
        return set()
    tipo = bloque.get("type")
    if not isinstance(tipo, str) or not tipo.endswith("_tool_result") or tipo in TERCEROS:
        return set()
    ids, pila = set(), [bloque]
    while pila:                                   # sin recursión: un anidamiento enorme no truena
        x = pila.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "file_id" and isinstance(v, str):
                    if 0 < len(v) <= LARGO_ID:
                        ids.add(v)
                else:
                    pila.append(v)
        elif isinstance(x, list):
            pila.extend(x)
    return ids


def archivos_de_mensaje(contenido) -> set[str]:
    """Los archivos que nacieron en un mensaje: la unión de archivos_de_bloque sobre su lista content."""
    ids: set[str] = set()
    for bloque in contenido if isinstance(contenido, list) else ():
        ids |= archivos_de_bloque(bloque)
    return ids


def mensaje_de_json(cuerpo: bytes) -> tuple[str | None, dict, set[str]]:
    """(modelo, usage, archivos que nacieron en la respuesta) de un mensaje completo. Lee el JSON una sola vez."""
    try:
        d = json.loads(cuerpo)
    except ValueError:
        return None, {}, set()
    if not isinstance(d, dict):
        return None, {}, set()
    return d.get("model"), d.get("usage") or {}, archivos_de_mensaje(d.get("content"))


def uso_de_json(cuerpo: bytes) -> tuple[str | None, dict]:
    modelo, uso, _ = mensaje_de_json(cuerpo)
    return modelo, uso


class LectorSSE:
    """Recorre un stream SSE. Solo toma message_start (modelo, usage inicial), message_delta (usage acumulado) y
    los file_id de los resultados de herramientas, que llegan enteros en un content_block_start."""
    LIMITE = 1_000_000

    def __init__(self):
        self._buf = b""
        self.modelo: str | None = None
        self.uso: dict = {}
        self.archivos: set[str] = set()           # los que faltan por registrar; sacar_archivos los entrega

    def alimentar(self, trozo: bytes) -> None:
        self._buf += trozo
        if b"\r" in self._buf:                    # CRLF sobre el búfer: un \r\n partido entre dos trozos también cuenta
            self._buf = self._buf.replace(b"\r\n", b"\n")
        while (i := self._buf.find(b"\n\n")) >= 0:
            evento, self._buf = self._buf[:i], self._buf[i + 2:]
            self._evento(evento)
        if len(self._buf) > self.LIMITE:      # un evento enorme sin cierre: se suelta
            self._buf = b""

    def _evento(self, evento: bytes) -> None:
        for linea in evento.split(b"\n"):
            if not linea.startswith(b"data:"):
                continue
            try:
                d = json.loads(linea[5:].strip())
            except ValueError:
                return
            if not isinstance(d, dict):
                return
            if d.get("type") == "message_start":
                m = d.get("message") or {}
                self.modelo = m.get("model")
                self.uso.update(m.get("usage") or {})
            elif d.get("type") == "message_delta":
                self.uso.update({k: v for k, v in (d.get("usage") or {}).items() if v is not None})
            elif d.get("type") == "content_block_start":
                self.archivos |= archivos_de_bloque(d.get("content_block"))

    def sacar_archivos(self) -> set[str]:
        """Entrega los file_id vistos desde la última vez y los olvida."""
        ids, self.archivos = self.archivos, set()
        return ids


class LectorJSONL:
    """Suma costo y tokens de los resultados de un lote (una línea JSON por petición) sin guardarlos."""

    def __init__(self):
        self._buf = b""
        self.micro = 0
        self.n = 0
        self.modelos: set[str] = set()
        self.tokens = {k: 0 for k in COLUMNAS}
        self.archivos: set[str] = set()           # file_id de resultados de herramientas; sacar_archivos los entrega

    def alimentar(self, trozo: bytes) -> None:
        self._buf += trozo
        while (i := self._buf.find(b"\n")) >= 0:
            linea, self._buf = self._buf[:i], self._buf[i + 1:]
            self._linea(linea)

    def terminar(self) -> None:
        if self._buf.strip():
            self._linea(self._buf)
        self._buf = b""

    def _linea(self, linea: bytes) -> None:
        try:
            d = json.loads(linea)
        except ValueError:
            return
        r = (d.get("result") or {}) if isinstance(d, dict) else {}
        if r.get("type") != "succeeded":
            return
        m = r.get("message") or {}
        if isinstance(m, dict):
            self.archivos |= archivos_de_mensaje(m.get("content"))
        uso = m.get("usage") or {}
        micro, conocido = costo_micro(m.get("model") or "", uso, lote=True)
        if not conocido:
            avisar_sin_precio(m.get("model") or "")
        self.micro += micro
        self.n += 1
        for k, v in tokens(uso).items():
            self.tokens[k] += v
        if m.get("model"):
            self.modelos.add(m["model"])

    def sacar_archivos(self) -> set[str]:
        """Entrega los file_id vistos desde la última vez y los olvida."""
        ids, self.archivos = self.archivos, set()
        return ids
