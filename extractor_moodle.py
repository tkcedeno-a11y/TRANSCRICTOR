"""Descarga los temas de un curso de Moodle y los convierte a PDF.

Abre Chrome en el curso, espera a que inicies sesion a mano y sigue solo.
La contrasena nunca pasa por este script: solo se guarda la cookie de sesion
en .sesion.json para no pedirte el login en cada ejecucion.

Uso:
    .venv\\Scripts\\python.exe extractor_moodle.py            # descarga todo
    .venv\\Scripts\\python.exe extractor_moodle.py --listar   # solo muestra el inventario
    .venv\\Scripts\\python.exe extractor_moodle.py --url "https://.../course/view.php?id=123"
"""
import argparse
import base64
import html
import io
import json
import mimetypes
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import pypdfium2 as pdfium
from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


URL_CURSO = "https://micampusvirtual.espe.edu.ec/course/view.php?id=7109&section=1"
BASE = Path(__file__).resolve().parent
ARCHIVO_SESION = BASE / ".sesion.json"
ESPERA_LOGIN_S = 600
PAUSA_S = 0.5  # entre peticiones, para no saturar el servidor
TIMEOUT_MS = 300_000

DESCARGABLES = {"resource", "folder"}
IMPRIMIBLES = {"page", "book", "assign", "imscp"}
TIPO_IMAGEN, TIPO_OBJETO_WEB = 15, 652
TIPOS_TEXTO = {19, 612}
TIPO_ENUNCIADO, TIPO_OPCION = 79, 80
DIAPOSITIVAS_OMITIDAS = {"Portada", "Resultado"}
MIN_LADO_FIGURA = 60
TAMANO_FONDO = (1024, 627)
DOCUMENTOS = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|odt|odp|ods|rtf|zip|rar)(\?|$)", re.I)

WORD = {".doc", ".docx", ".rtf", ".odt"}
POWERPOINT = {".ppt", ".pptx", ".pps", ".ppsx", ".odp"}
EXCEL = {".xls", ".xlsx", ".ods", ".csv"}
IMAGENES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
TEXTO = {".txt", ".html", ".htm"}

CSS_IMPRESION = """
nav, footer, #page-footer, .drawer, .drawer-toggles, .navbar, .activity-navigation,
.secondary-navigation, .btn-footer-popover, #nav-drawer { display: none !important; }
#page, #page-wrapper, #page-content, #region-main { margin: 0 !important; padding: 0 !important; }
"""

JS_INDICE = """
() => [...document.querySelectorAll('.courseindex-section')].map(s => ({
  nombre: (s.querySelector('a.courseindex-link') || {}).textContent?.trim() || `Seccion ${s.dataset.number}`,
  items: [...s.querySelectorAll('.courseindex-item')].map(i => {
    const a = i.querySelector('a.courseindex-link') || i.querySelector('a[href*="/mod/"]');
    return a ? {href: a.href, nombre: a.textContent.trim()} : null;
  }).filter(Boolean),
}))
"""

# El contenido real de un paquete Captivate: texto, figuras y objetos web de cada diapositiva
JS_CONTENIDO = """
() => {
  const porUid = {};
  for (const [k, v] of Object.entries(cp.D)) {
    if (v && typeof v === 'object' && v.uid !== undefined) porUid[v.uid] = k;
  }
  const texto = v => {
    if (typeof v === 'string' && v.trim().startsWith('{')) { try { v = JSON.parse(v); } catch (e) {} }
    if (v && typeof v === 'object') v = v['1024'] || Object.values(v).pop() || '';
    return typeof v === 'string' ? v : '';
  };
  return cp.model.data.project_main.slides.split(',').map(s => {
    const d = cp.D[s], vistos = new Set(), bloques = [];
    for (const x of d.si || []) {
      const uids = ((cp.D[x.n] || {}).stl || []).flatMap(e => e.stsi || []);
      for (const k of uids.length ? uids.map(u => porUid[u]) : [x.n + 'c']) {
        if (!k || vistos.has(k)) continue;
        vistos.add(k);
        const c = cp.D[k] || {};
        bloques.push({
          nombre: k,
          tipo: (cp.D[k.replace(/c$/, '')] || {}).type,
          y: (c.b || [])[1],
          texto: texto(c.accstr).trim(),
          imagen: c.ip || '',
          web: c.wou || '',
        });
      }
    }
    return {titulo: d.lb.replace(/\\s+/g, ' ').trim(), pregunta: !!d.qs, bloques};
  });
}
"""

JS_CUERPO_INCRUSTADO = """
() => {
  document.querySelectorAll('script, style, link').forEach(e => e.remove());
  const vacia = !document.body.innerText.trim() && !document.images.length;
  document.querySelectorAll('iframe').forEach(e => e.remove());
  document.querySelectorAll('img[src]').forEach(e => e.setAttribute('src', e.src));
  document.querySelectorAll('a[href]').forEach(e => e.setAttribute('href', e.href));
  return vacia ? '' : document.body.innerHTML;
}
"""

PLANTILLA_TEMA = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><base href="{raiz}">
<style>
body, body * {{ font-family: Calibri, Arial, sans-serif !important; }}
body {{ font-size: 11.5pt; line-height: 1.45; color: #111; }}
h1 {{ font-size: 21pt; margin: 0 0 4pt; color: #1f3864; }}
.curso {{ color: #555; margin: 0 0 18pt; padding-bottom: 8pt; border-bottom: 1.5pt solid #1f3864; }}
h2 {{ font-size: 14.5pt; margin: 18pt 0 6pt; color: #1f3864; break-after: avoid; }}
p {{ margin: 0 0 7pt; text-align: justify; }}
img {{ display: block; max-width: 100%; height: auto; margin: 8pt auto; break-inside: avoid; }}
table {{ border-collapse: collapse; max-width: 100%; }}
.pregunta {{ break-inside: avoid; margin-bottom: 12pt; }}
.pregunta p {{ font-weight: bold; }}
.pregunta ol {{ margin: 3pt 0 0; }}
a {{ color: #1f4e9c; word-break: break-all; }}
</style></head><body>
<h1>{titulo}</h1>
<p class="curso">{curso}</p>
{cuerpo}
</body></html>
"""

PIE_DE_PAGINA = (
    '<div style="width:100%;text-align:center;font-size:9px;color:#666">'
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
)

@dataclass
class Actividad:
    cmid: str
    tipo: str
    nombre: str
    url: str


@dataclass
class Seccion:
    nombre: str
    actividades: list = field(default_factory=list)


@dataclass
class Resumen:
    pdfs: list = field(default_factory=list)
    sin_convertir: list = field(default_factory=list)
    omitidos: list = field(default_factory=list)
    enlaces: list = field(default_factory=list)
    errores: list = field(default_factory=list)


def limpiar(nombre, largo=80):
    nombre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", nombre)
    nombre = re.sub(r"\s+", " ", nombre).strip(" .")
    return nombre[:largo].rstrip(" .") or "sin_nombre"


def ruta_libre(ruta):
    if not ruta.exists():
        return ruta
    n = 2
    while True:
        candidata = ruta.with_name(f"{ruta.stem} ({n}){ruta.suffix}")
        if not candidata.exists():
            return candidata
        n += 1


def es_curso(url, host):
    p = urlparse(url)
    return p.hostname == host and re.search(r"/course/(view|section)\.php", p.path) is not None


def cargar(pagina, url):
    pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    try:
        pagina.wait_for_load_state("networkidle", timeout=20_000)
    except PlaywrightTimeout:
        pass
    if "/login/" in urlparse(pagina.url).path:
        raise RuntimeError("la sesion expiro: vuelve a ejecutar el script")


# --- Inicio de sesion ---------------------------------------------------------

def sesion_valida(contexto, url, host):
    pagina = contexto.new_page()
    try:
        pagina.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return es_curso(pagina.url, host)
    finally:
        pagina.close()


def login_manual(playwright, url, host):
    navegador = playwright.chromium.launch(channel="chrome", headless=False)
    contexto = navegador.new_context(no_viewport=True)
    contexto.new_page().goto(url, timeout=60_000)
    print(">>> Inicia sesion en la ventana de Chrome. Yo continuo cuando entres al curso.", flush=True)
    limite = time.time() + ESPERA_LOGIN_S
    try:
        while time.time() < limite:
            if not contexto.pages:
                sys.exit("Se cerro la ventana de Chrome antes de iniciar sesion.")
            # wait_for_timeout y no time.sleep: Playwright solo actualiza page.url mientras corre
            try:
                contexto.pages[0].wait_for_timeout(1000)
            except PlaywrightError:
                continue
            if any(es_curso(p.url, host) for p in contexto.pages):
                contexto.pages[0].wait_for_timeout(2000)
                contexto.storage_state(path=str(ARCHIVO_SESION))
                print("Sesion iniciada.", flush=True)
                return
        sys.exit("Se acabo el tiempo de espera para iniciar sesion.")
    finally:
        navegador.close()


# --- Inventario del curso -----------------------------------------------------

def inventariar(contexto, url):
    p = urlparse(url)
    curso_id = parse_qs(p.query)["id"][0]
    pagina = contexto.new_page()
    cargar(pagina, f"{p.scheme}://{p.netloc}{p.path}?id={curso_id}")
    pagina.wait_for_selector(".courseindex-item", state="attached", timeout=30_000)
    titulo = pagina.title().split("|")[1].strip() if "|" in pagina.title() else f"Curso {curso_id}"
    secciones = []
    for s in pagina.evaluate(JS_INDICE):
        seccion = Seccion(s["nombre"])
        for item in s["items"]:
            m = re.search(r"/mod/(\w+)/view\.php\?(?:.*&)?id=(\d+)", item["href"])
            if m:  # las etiquetas no tienen pagina propia
                seccion.actividades.append(Actividad(m.group(2), m.group(1), item["nombre"], item["href"]))
        secciones.append(seccion)
    pagina.close()
    return titulo, secciones

# --- Descarga -----------------------------------------------------------------

def nombre_de_respuesta(respuesta):
    cd = respuesta.headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)', cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    return unquote(urlparse(respuesta.url).path.rsplit("/", 1)[-1])


def guardar(respuesta, carpeta, base=None):
    """Guarda la respuesta; `base` reemplaza el nombre pero conserva la extension real."""
    original = nombre_de_respuesta(respuesta)
    ext = Path(original).suffix.lower()
    if not ext:
        tipo = respuesta.headers.get("content-type", "").split(";")[0].strip()
        ext = mimetypes.guess_extension(tipo) or ""
    nombre = limpiar(base or Path(original).stem) + ext
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = ruta_libre(carpeta / nombre)
    destino.write_bytes(respuesta.body())
    return destino


def pluginfiles(contenido, filtro):
    encontrados = re.findall(r"""https?://[^"'<>\s]+?pluginfile\.php/[^"'<>\s]+""", contenido)
    urls = [html.unescape(u) for u in encontrados]
    return list(dict.fromkeys(u for u in urls if filtro(u)))


def obtener(contexto, url):
    respuesta = contexto.request.get(url, timeout=TIMEOUT_MS)
    if not respuesta.ok:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    return respuesta


def descargar_recurso(contexto, act, carpeta, base):
    respuesta = obtener(contexto, act.url + "&redirect=1")
    tipo = respuesta.headers.get("content-type", "")
    if "text/html" in tipo and "pluginfile.php" not in respuesta.url:
        enlaces = pluginfiles(respuesta.text(), lambda u: "mod_resource/content" in u)
        if not enlaces:
            raise RuntimeError("no se encontro el archivo dentro de la pagina del recurso")
        respuesta = obtener(contexto, enlaces[0])
    return [guardar(respuesta, carpeta, base)]


def descargar_carpeta(contexto, act, carpeta, base):
    contenido = obtener(contexto, act.url).text()
    enlaces = pluginfiles(contenido, lambda u: "mod_folder/content" in u)
    destino = carpeta / base
    return [guardar(obtener(contexto, u), destino) for u in enlaces]


def resolver_enlace(contexto, act, carpeta, base, resumen):
    respuesta = contexto.request.get(act.url, max_redirects=0, timeout=60_000)
    destino = respuesta.headers.get("location", "")
    if not destino:
        m = re.search(r'class="urlworkaround".*?href="([^"]+)"', respuesta.text(), re.S)
        destino = html.unescape(m.group(1)) if m else act.url
    if urlparse(destino).path.lower().endswith(".pdf"):
        try:
            return [guardar(obtener(contexto, destino), carpeta, base)]
        except Exception:
            pass
    resumen.enlaces.append(f"{act.nombre}: {destino}")
    return []


def imprimir(pagina, url, destino):
    cargar(pagina, url)
    pagina.add_style_tag(content=CSS_IMPRESION)
    pagina.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => i.loading = 'eager')")
    destino.parent.mkdir(parents=True, exist_ok=True)
    pagina.pdf(
        path=str(destino),
        format="A4",
        print_background=True,
        margin={"top": "15mm", "bottom": "15mm", "left": "12mm", "right": "12mm"},
    )
    return pagina.content()


def parrafos(texto):
    """Captivate separa los parrafos con varios espacios seguidos."""
    salida = []
    for parrafo in re.split(r"\s{2,}", texto):
        parrafo = html.escape(parrafo.strip())
        parrafo = re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', parrafo)
        if parrafo:
            salida.append(f"<p>{parrafo}</p>")
    return "\n".join(salida)


def es_figura(ruta):
    """Descarta iconos, fondos de pantalla completa y personajes (mucho mas altos que anchos)."""
    m = re.search(r"_(\d+)_(\d+)\.\w+$", ruta)
    if not m:
        return True
    ancho, alto = int(m.group(1)), int(m.group(2))
    return max(ancho, alto) >= MIN_LADO_FIGURA and alto <= ancho * 1.25 and (ancho, alto) != TAMANO_FONDO

def imagenes_del_paquete(contexto, raiz):
    """Captivate no publica las imagenes sueltas: van en base64 dentro de dr/imgN.json."""
    mapa = obtener(contexto, raiz + "dr/imgmd.json").text()
    imagenes = {}
    for archivo in set(re.findall(r':\s*"(img\d+\.json)"', mapa)):
        contenido = obtener(contexto, raiz + "dr/" + archivo).text()
        imagenes.update(re.findall(r'"(dr/[^"]+)":\s*"([^"]+)"', contenido))
    return imagenes


def html_figura(ruta, imagenes):
    if not ruta or ruta not in imagenes or not es_figura(ruta):
        return ""
    tipo = "jpeg" if ruta.lower().endswith((".jpg", ".jpeg")) else "png"
    return f'<img src="data:image/{tipo};base64,{imagenes[ruta]}">'


def pdf_a_imagenes(datos):
    salida = []
    for pagina in pdfium.PdfDocument(datos):
        imagen = io.BytesIO()
        pagina.render(scale=2).to_pil().convert("RGB").save(imagen, "PNG")
        salida.append(f'<img src="data:image/png;base64,{base64.b64encode(imagen.getvalue()).decode()}">')
    return "\n".join(salida)


def html_incrustado(contexto, url):
    """Contenido de un objeto web: un .pdf o una pagina .htm. Vacio si solo envuelve un video."""
    if urlparse(url).path.lower().endswith(".pdf"):
        return pdf_a_imagenes(obtener(contexto, url).body())
    pagina = contexto.new_page()
    try:
        cargar(pagina, url)
        return pagina.evaluate(JS_CUERPO_INCRUSTADO)
    finally:
        pagina.close()


def html_diapositiva(contexto, diapositiva, raiz, repetidos, imagenes):
    partes = []
    for b in diapositiva["bloques"]:
        if b["nombre"].startswith("eLB_"):
            continue
        if b["tipo"] == TIPO_IMAGEN:
            partes.append(html_figura(b["imagen"], imagenes))
        elif b["tipo"] == TIPO_OBJETO_WEB:
            if b["web"]:
                partes.append(html_incrustado(contexto, urljoin(raiz, b["web"])))
        elif b["tipo"] in TIPOS_TEXTO and b["texto"] not in repetidos:
            es_encabezado = (b["y"] or 0) < 10
            if not es_encabezado and len(b["texto"]) > 1 and "$$" not in b["texto"]:
                partes.append(parrafos(b["texto"]))
    return "\n".join(p for p in partes if p)


def html_pregunta(diapositiva, imagenes):
    enunciado = figuras = ""
    opciones = []
    for b in diapositiva["bloques"]:
        if b["tipo"] == TIPO_ENUNCIADO:
            enunciado = parrafos(b["texto"])
        elif b["tipo"] == TIPO_OPCION:
            opciones.append(f"<li>{html.escape(b['texto'])}</li>")
        elif b["tipo"] == TIPO_IMAGEN:
            figuras += html_figura(b["imagen"], imagenes)
    return f'<div class="pregunta">{enunciado}{figuras}<ol type="a">{"".join(opciones)}</ol></div>'


def convertir_paquete(contexto, act, destino, curso):
    """Arma un documento PDF con el contenido de un paquete IMS de Adobe Captivate."""
    pagina = contexto.new_page()
    try:
        cargar(pagina, act.url)
        src = next((f.url for f in pagina.frames if "mod_imscp/content" in f.url), None)
        if not src:
            raise RuntimeError("el paquete no tiene contenido embebido")
        etiqueta = pagina.locator("a.ygtvlabel").first
        subtitulo = etiqueta.inner_text().strip() if etiqueta.count() else ""
        cargar(pagina, src)
        pagina.wait_for_function("window.cpInfoSlideCount > 0", timeout=60_000)
        diapositivas = pagina.evaluate(JS_CONTENIDO)
        raiz = src.rsplit("/", 1)[0] + "/"
        imagenes = imagenes_del_paquete(contexto, raiz)

        # Lo que se repite en varias diapositivas es la plantilla, no el contenido
        apariciones = Counter(
            x for d in diapositivas for x in {v for b in d["bloques"] for v in (b["texto"], b["imagen"])} if x
        )
        repetidos = {x for x, n in apariciones.items() if n >= 3}
        imagenes = {ruta: datos for ruta, datos in imagenes.items() if ruta not in repetidos}

        cuerpo = []
        hay_autoevaluacion = False
        for d in diapositivas:
            if d["titulo"] in DIAPOSITIVAS_OMITIDAS:
                continue
            if d["pregunta"]:
                if not hay_autoevaluacion:
                    cuerpo.append("<h2>Autoevaluación</h2>")
                    hay_autoevaluacion = True
                cuerpo.append(html_pregunta(d, imagenes))
            else:
                contenido = html_diapositiva(contexto, d, raiz, repetidos, imagenes)
                if contenido:
                    cuerpo.append(f"<h2>{html.escape(d['titulo'])}</h2>\n{contenido}")

        titulo = f"{act.nombre}: {subtitulo}" if subtitulo else act.nombre
        pagina.goto("about:blank")
        pagina.set_content(
            PLANTILLA_TEMA.format(raiz=raiz, titulo=html.escape(titulo), curso=html.escape(curso), cuerpo="\n".join(cuerpo)),
            wait_until="networkidle",
            timeout=TIMEOUT_MS,
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        pagina.pdf(
            path=str(destino),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=PIE_DE_PAGINA,
            margin={"top": "20mm", "bottom": "20mm", "left": "22mm", "right": "22mm"},
        )
    finally:
        pagina.close()

def adjuntos(contexto, contenido, carpeta):
    enlaces = pluginfiles(contenido, lambda u: DOCUMENTOS.search(u) and "/mod_resource/" not in u)
    return [guardar(obtener(contexto, u), carpeta / "adjuntos") for u in enlaces]


# --- Conversion a PDF ---------------------------------------------------------

class Office:
    """Word/PowerPoint/Excel por COM, con las macros desactivadas."""

    def __init__(self):
        self.apps = {}

    def app(self, nombre, alertas):
        if nombre not in self.apps:
            import win32com.client

            app = win32com.client.DispatchEx(f"{nombre}.Application")
            app.DisplayAlerts = alertas
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
            self.apps[nombre] = app
        return self.apps[nombre]

    def word(self, origen, destino):
        doc = self.app("Word", 0).Documents.Open(
            str(origen), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False, Visible=False
        )
        try:
            doc.ExportAsFixedFormat(str(destino), 17)
        finally:
            doc.Close(0)

    def powerpoint(self, origen, destino):
        pres = self.app("PowerPoint", 1).Presentations.Open(str(origen), True, False, False)
        try:
            pres.SaveAs(str(destino), 32)
        finally:
            pres.Close()

    def excel(self, origen, destino):
        libro = self.app("Excel", False).Workbooks.Open(str(origen), 0, True)
        try:
            libro.ExportAsFixedFormat(0, str(destino))
        finally:
            libro.Close(False)

    def cerrar(self):
        for nombre, app in self.apps.items():
            # PowerPoint comparte instancia: no cerrar si tienes algo abierto
            if nombre == "PowerPoint" and app.Presentations.Count > 0:
                continue
            app.Quit()


def convertir(archivo, office, pagina_sin_js):
    """Devuelve la ruta del PDF, o None si el formato no se puede convertir."""
    ext = archivo.suffix.lower()
    if ext == ".pdf":
        return archivo
    destino = ruta_libre(archivo.with_suffix(".pdf"))
    if ext in WORD:
        office.word(archivo, destino)
    elif ext in POWERPOINT:
        office.powerpoint(archivo, destino)
    elif ext in EXCEL:
        office.excel(archivo, destino)
    elif ext in IMAGENES:
        with Image.open(archivo) as imagen:
            imagen.convert("RGB").save(destino, "PDF")
    elif ext in TEXTO:
        contenido = archivo.read_text(encoding="utf-8", errors="replace")
        if ext == ".txt":
            contenido = f"<pre style='white-space:pre-wrap'>{html.escape(contenido)}</pre>"
        pagina_sin_js.set_content(contenido, wait_until="domcontentloaded")
        pagina_sin_js.pdf(path=str(destino), format="A4")
    else:
        return None
    originales = archivo.parent / "_originales"
    originales.mkdir(exist_ok=True)
    shutil.move(str(archivo), str(ruta_libre(originales / archivo.name)))
    return destino


# --- Flujo principal ----------------------------------------------------------

def procesar_actividad(contexto, pagina, act, carpeta, base, resumen, curso):
    if act.tipo == "resource":
        return descargar_recurso(contexto, act, carpeta, base)
    if act.tipo == "folder":
        return descargar_carpeta(contexto, act, carpeta, base)
    if act.tipo == "url":
        return resolver_enlace(contexto, act, carpeta, base, resumen)
    destino = ruta_libre(carpeta / f"{base}.pdf")
    if act.tipo == "imscp":
        convertir_paquete(contexto, act, destino, curso)
        return [destino]
    url = act.url
    if act.tipo == "book":
        url = url.replace("/mod/book/view.php", "/mod/book/tool/print/index.php")
    contenido = imprimir(pagina, url, destino)
    return [destino] + adjuntos(contexto, contenido, carpeta)


def descargar_curso(contexto, ctx_sin_js, titulo, secciones):
    salida = BASE / f"Temas - {limpiar(titulo)}"
    salida.mkdir(exist_ok=True)
    archivo_estado = salida / ".estado.json"
    hechos = json.loads(archivo_estado.read_text("utf-8")) if archivo_estado.exists() else {}
    resumen = Resumen()
    office = Office()
    pagina = contexto.new_page()
    pagina_sin_js = ctx_sin_js.new_page()

    def registrar(archivos, etiqueta):
        for archivo in archivos:
            try:
                pdf = convertir(archivo, office, pagina_sin_js)
            except Exception as e:
                resumen.errores.append(f"{etiqueta}: no se pudo convertir {archivo.name}: {e}")
                print(f"    ERROR al convertir {archivo.name}: {e}", flush=True)
                continue
            if pdf:
                resumen.pdfs.append(str(pdf.relative_to(salida)))
            else:
                resumen.sin_convertir.append(str(archivo.relative_to(salida)))

    try:
        for i, seccion in enumerate(secciones):
            carpeta = salida / f"{i:02d} - {limpiar(seccion.nombre, 60)}"
            print(f"\n[{i + 1}/{len(secciones)}] {seccion.nombre}", flush=True)

            for j, act in enumerate(seccion.actividades, 1):
                if act.cmid in hechos:
                    continue
                if act.tipo not in DESCARGABLES | IMPRIMIBLES | {"url"}:
                    resumen.omitidos.append(f"[{act.tipo}] {act.nombre}")
                    continue
                print(f"  - [{act.tipo}] {act.nombre}", flush=True)
                try:
                    base = f"{j:02d} - {limpiar(act.nombre)}"
                    archivos = procesar_actividad(contexto, pagina, act, carpeta, base, resumen, titulo)
                    registrar(archivos, act.nombre)
                    hechos[act.cmid] = True
                    archivo_estado.write_text(json.dumps(hechos), "utf-8")
                except Exception as e:
                    resumen.errores.append(f"{act.nombre}: {e}")
                    print(f"    ERROR: {e}", flush=True)
                time.sleep(PAUSA_S)
    finally:
        archivo_estado.write_text(json.dumps(hechos), "utf-8")
        office.cerrar()

    escribir_resumen(salida, resumen)
    return salida, resumen


def escribir_resumen(salida, resumen):
    bloques = [
        ("PDF generados", resumen.pdfs),
        ("Descargados sin convertir", resumen.sin_convertir),
        ("Enlaces externos (no descargados)", resumen.enlaces),
        ("Actividades omitidas", resumen.omitidos),
        ("Errores", resumen.errores),
    ]
    lineas = []
    for titulo, elementos in bloques:
        lineas += [f"== {titulo}: {len(elementos)} ==", *elementos, ""]
    (salida / "_RESUMEN.txt").write_text("\n".join(lineas), "utf-8")
    print("\n" + "\n".join(f"{t}: {len(e)}" for t, e in bloques), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--url", default=URL_CURSO, help="URL del curso")
    parser.add_argument("--listar", action="store_true", help="solo inventariar, sin descargar")
    args = parser.parse_args()
    host = urlparse(args.url).hostname

    with sync_playwright() as p:
        if ARCHIVO_SESION.exists():
            navegador = p.chromium.launch(channel="chrome", headless=True)
            valida = sesion_valida(navegador.new_context(storage_state=str(ARCHIVO_SESION)), args.url, host)
            navegador.close()
        else:
            valida = False
        if not valida:
            login_manual(p, args.url, host)

        # El resto va sin ventana: Chrome solo genera PDF en modo headless
        navegador = p.chromium.launch(channel="chrome", headless=True)
        contexto = navegador.new_context(
            storage_state=str(ARCHIVO_SESION),
        )
        print("Leyendo la estructura del curso...", flush=True)
        titulo, secciones = inventariar(contexto, args.url)

        print(f"\nCurso: {titulo}")
        for s in secciones:
            print(f"\n# {s.nombre} ({len(s.actividades)} actividades)")
            for a in s.actividades:
                print(f"   [{a.tipo}] {a.nombre}")
        sys.stdout.flush()
        (BASE / "inventario.json").write_text(
            json.dumps({"curso": titulo, "secciones": [asdict(s) for s in secciones]}, ensure_ascii=False, indent=2),
            "utf-8",
        )

        if not args.listar:
            ctx_sin_js = navegador.new_context(java_script_enabled=False)
            salida, _ = descargar_curso(contexto, ctx_sin_js, titulo, secciones)
            print(f"\nListo: {salida}", flush=True)
        navegador.close()


if __name__ == "__main__":
    main()
