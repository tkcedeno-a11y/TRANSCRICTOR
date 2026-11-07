"""Base de conocimiento: extrae el texto de los PDF de los temas para el asistente."""
import os
import re
import unicodedata
from pathlib import Path

from pypdf import PdfReader

from config import CARPETA_TEMAS

# Carpeta del asistente y archivo de cache con la extraccion
RAIZ = Path(__file__).resolve().parent
ARCHIVO_CACHE = RAIZ / "base_conocimiento.txt"
CARPETA_PARCIAL = "01 - Primer Parcial"

# Patrones de nombre (aplicados a nombres normalizados en NFC)
_PATRON_TEMA = re.compile(r"^.+ - Tema (\d+)\.pdf$")
_PATRON_PRESENTACION = re.compile(r"^.+ - Presentación Tema (\d+)\.pdf$")

# Patrones para leer de nuevo la cache al calcular el resumen
_PATRON_DOC = re.compile(r'<documento nombre="([^"]*)">(.*?)</documento>', re.DOTALL)
_PATRON_PAGINA = re.compile(r'<pagina n="\d+">(.*?)</pagina>', re.DOTALL)


def _normalizar(texto: str) -> str:
    """Pasa el texto a forma NFC para que los acentos comparen igual."""
    return unicodedata.normalize("NFC", texto)


def _listar_pdf() -> list[Path]:
    """Devuelve los PDF a incluir en orden determinista (sin depender del sistema de archivos)."""
    carpeta = CARPETA_TEMAS / CARPETA_PARCIAL
    temas, presentaciones, adjuntos = [], [], []

    # Solo PDF directos de la carpeta; _originales y videos quedan fuera
    for pdf in carpeta.glob("*.pdf"):
        nombre = _normalizar(pdf.name)
        if m := _PATRON_TEMA.match(nombre):
            temas.append((int(m.group(1)), pdf))
        elif m := _PATRON_PRESENTACION.match(nombre):
            presentaciones.append((int(m.group(1)), pdf))

    # Adjuntos: todos los PDF de la subcarpeta adjuntos
    adjuntos = list((carpeta / "adjuntos").glob("*.pdf"))

    temas.sort(key=lambda t: (t[0], _normalizar(t[1].name)))
    presentaciones.sort(key=lambda t: (t[0], _normalizar(t[1].name)))
    adjuntos.sort(key=lambda p: (_normalizar(p.name).casefold(), _normalizar(p.name)))

    pdfs = [p for _, p in temas] + [p for _, p in presentaciones] + adjuntos
    if not pdfs:
        raise FileNotFoundError(
            f"No hay PDF de temas para la base de conocimiento en: {carpeta}"
        )
    return pdfs


def _normalizar_texto(texto: str) -> str:
    """Unifica saltos de linea, colapsa espacios repetidos y lineas vacias."""
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    lineas = [re.sub(r"[ \t\f\v\xa0]+", " ", linea).strip() for linea in texto.split("\n")]

    salida = []
    for linea in lineas:
        # Como mucho una linea vacia seguida
        if linea == "" and (not salida or salida[-1] == ""):
            continue
        salida.append(linea)
    return "\n".join(salida).strip()


def _extraer_documento(pdf: Path) -> str:
    """Devuelve la etiqueta <documento> con una <pagina> por cada pagina del PDF."""
    lector = PdfReader(str(pdf))
    # Sin prefijo numerico ni extension; las comillas se escapan para el atributo
    nombre = re.sub(r"^\d+ - ", "", _normalizar(pdf.stem)).replace('"', "&quot;")

    partes = [f'<documento nombre="{nombre}">']
    for n, pagina in enumerate(lector.pages, start=1):
        texto = _normalizar_texto(pagina.extract_text() or "")
        partes.append(f'<pagina n="{n}">{texto}</pagina>')
    partes.append("</documento>")
    return "\n".join(partes)


def _cache_vigente(pdfs: list[Path]) -> bool:
    """La cache vale si existe y es mas nueva que todos los PDF incluidos."""
    if not ARCHIVO_CACHE.exists():
        return False
    mtime_cache = os.path.getmtime(ARCHIVO_CACHE)
    return all(mtime_cache > os.path.getmtime(p) for p in pdfs)


def cargar_base() -> str:
    """Devuelve todo el texto de la base, en orden determinista."""
    pdfs = _listar_pdf()
    if _cache_vigente(pdfs):
        return ARCHIVO_CACHE.read_text(encoding="utf-8")

    texto = "\n".join(_extraer_documento(p) for p in pdfs)
    # newline="\n" para que los bytes sean iguales en cualquier ejecucion
    ARCHIVO_CACHE.write_text(texto, encoding="utf-8", newline="\n")
    return texto


def resumen_base() -> list[tuple[str, int]]:
    """Devuelve (nombre_documento, caracteres) de cada documento cargado."""
    resumen = []
    for m in _PATRON_DOC.finditer(cargar_base()):
        nombre = m.group(1).replace("&quot;", '"')
        contenido = "".join(_PATRON_PAGINA.findall(m.group(2)))
        resumen.append((nombre, len(contenido)))
    return resumen


if __name__ == "__main__":
    documentos = resumen_base()
    for nombre, caracteres in documentos:
        print(f"{nombre}: {caracteres} caracteres")
    total = sum(c for _, c in documentos)
    print(f"Total: {total} caracteres en {len(documentos)} documentos")
