"""Ventana tipo teleprompter con efecto de cristal (PySide6).

Todos los metodos publicos, salvo `ejecutar`, pueden llamarse desde otros hilos:
pasan por senales de Qt, que los entregan en el hilo de la interfaz.
"""
import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QBuffer, QIODevice, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

ANCHO, ALTO = 860, 240
RADIO = 18
MAX_LADO_CAPTURA = 1568  # mas alla Claude la reduce igual
REFRESCO_MS = 60  # el texto que llega se pinta en tandas, no letra a letra (evita parpadeo)
MAX_BLOQUES = 60  # parrafos que se conservan; los mas viejos se borran solos
WDA_EXCLUDEFROMCAPTURE = 0x11


def _formato(color, px, peso):
    fuente = QFont("Segoe UI")
    fuente.setPixelSize(px)
    fuente.setWeight(peso)
    formato = QTextCharFormat()
    formato.setFont(fuente)
    formato.setForeground(QColor(color))
    return formato

ESTILO = """
QLabel#estado { color: rgba(255,255,255,190); font: 600 12px 'Segoe UI'; }
QTextBrowser { background: transparent; border: none; color: white; font: 600 23px 'Segoe UI'; }
QPushButton#control {
  color: white; font: 20px 'Segoe UI Symbol'; border-radius: 28px;
  background: rgba(255,255,255,34); border: 1px solid rgba(255,255,255,105);
}
QPushButton#control:hover { background: rgba(255,255,255,62); }
QPushButton#control:pressed { background: rgba(255,255,255,20); }
QPushButton#control[activo="true"] { background: rgba(255,92,92,130); border-color: rgba(255,190,190,190); }
QPushButton#cerrar { color: rgba(255,255,255,150); background: transparent; border: none; font: 15px 'Segoe UI'; }
QPushButton#cerrar:hover { color: white; }
"""


class _AccentPolicy(ctypes.Structure):
    _fields_ = [("estado", ctypes.c_int), ("banderas", ctypes.c_int), ("color", ctypes.c_uint), ("animacion", ctypes.c_int)]


class _DatosComposicion(ctypes.Structure):
    _fields_ = [("atributo", ctypes.c_int), ("datos", ctypes.c_void_p), ("tamano", ctypes.c_size_t)]


def aplicar_cristal(hwnd):
    """Desenfoca lo que hay detras de la ventana (acrilico de Windows) y redondea sus esquinas."""
    acento = _AccentPolicy(4, 2, 0x55201810, 0)  # color en ABGR: tinte oscuro translucido
    datos = _DatosComposicion(19, ctypes.cast(ctypes.pointer(acento), ctypes.c_void_p), ctypes.sizeof(acento))
    ctypes.windll.user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(datos))
    esquina = ctypes.c_int(2)  # DWMWCP_ROUND
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(esquina), ctypes.sizeof(esquina))


def excluir_de_capturas(hwnd):
    """Mantiene la ventana visible localmente y la excluye de capturas de pantalla en Windows 10 2004+."""
    if sys.getwindowsversion().build < 19041:
        return False

    funcion = ctypes.windll.user32.SetWindowDisplayAffinity
    funcion.argtypes = (wintypes.HWND, wintypes.DWORD)
    funcion.restype = wintypes.BOOL
    return bool(funcion(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE))


class _Senales(QObject):
    estado = Signal(str)
    nivel = Signal(float)
    nueva = Signal(str)
    texto = Signal(str)
    fin = Signal()
    escucha = Signal(bool)


class _Medidor(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(90, 5)
        self.valor = 0.0

    def paintEvent(self, _evento):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(self.rect(), 2.5, 2.5)
        p.setBrush(QColor(140, 230, 255, 230))
        p.drawRoundedRect(0, 0, int(self.width() * min(self.valor / 0.12, 1.0)), self.height(), 2.5, 2.5)


class Ventana(QWidget):
    def __init__(self, al_play, al_capturar):
        """al_play(activo) devuelve True si pudo cambiar; al_capturar(png: bytes)."""
        self._app = QApplication.instance() or QApplication(sys.argv)
        super().__init__()
        self._al_play = al_play
        self._al_capturar = al_capturar
        self._senales = _Senales()
        self._pendiente = []  # fragmentos recibidos que aun no se pintaron
        self._formato_pregunta = _formato("#a8e6ff", 14, QFont.Weight.DemiBold)
        self._formato_respuesta = _formato("white", 23, QFont.Weight.DemiBold)
        self._construir()
        self._refresco = QTimer(self)
        self._refresco.setSingleShot(True)
        self._refresco.setInterval(REFRESCO_MS)
        self._refresco.timeout.connect(self._volcar)
        s = self._senales
        s.estado.connect(self._etiqueta.setText)
        s.nivel.connect(self._poner_nivel)
        s.nueva.connect(self._poner_pregunta)
        s.texto.connect(self._agregar)
        s.fin.connect(self._volcar)
        s.escucha.connect(self._poner_escucha)

    def _construir(self):
        self.setWindowTitle("Asistente")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(ESTILO)
        self.resize(ANCHO, ALTO)
        self.setMinimumSize(520, 150)
        pantalla = self._app.primaryScreen().availableGeometry()
        self.move(pantalla.center().x() - ANCHO // 2, pantalla.top() + 18)

        self._boton_play = QPushButton("▶", objectName="control")
        self._boton_captura = QPushButton("⛶", objectName="control")
        self._boton_play.setToolTip("Empezar o detener la escucha")
        self._boton_captura.setToolTip("Capturar mi pantalla y responder")
        for boton in (self._boton_play, self._boton_captura):
            boton.setFixedSize(56, 56)
            boton.setCursor(Qt.PointingHandCursor)
        self._boton_play.clicked.connect(self._alternar)
        self._boton_captura.clicked.connect(self._capturar)

        botones = QVBoxLayout()
        botones.setSpacing(12)
        botones.addStretch()
        botones.addWidget(self._boton_play)
        botones.addWidget(self._boton_captura)
        botones.addStretch()

        self._etiqueta = QLabel("Cargando...", objectName="estado")
        # Ignored: un texto de estado largo no reacomoda (ni hace parpadear) la ventana
        self._etiqueta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._medidor = _Medidor()
        cerrar = QPushButton("✕", objectName="cerrar")
        cerrar.setFixedSize(22, 22)
        cerrar.setCursor(Qt.PointingHandCursor)
        cerrar.clicked.connect(self.close)
        cabecera = QHBoxLayout()
        cabecera.addWidget(self._etiqueta, 1)
        cabecera.addWidget(self._medidor)
        cabecera.addWidget(cerrar)

        self._texto = QTextBrowser()
        self._texto.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._texto.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._texto.setOpenExternalLinks(False)
        self._texto.setUndoRedoEnabled(False)
        self._texto.document().setMaximumBlockCount(MAX_BLOQUES)

        derecha = QVBoxLayout()
        derecha.setSpacing(6)
        derecha.addLayout(cabecera)
        derecha.addWidget(self._texto, 1)
        agarre = QHBoxLayout()
        agarre.addStretch()
        agarre.addWidget(QSizeGrip(self))
        derecha.addLayout(agarre)

        fila = QHBoxLayout(self)
        fila.setContentsMargins(18, 12, 12, 6)
        fila.setSpacing(16)
        fila.addLayout(botones)
        fila.addLayout(derecha, 1)

    def showEvent(self, evento):
        super().showEvent(evento)
        hwnd = int(self.winId())
        aplicar_cristal(hwnd)
        excluir_de_capturas(hwnd)

    def paintEvent(self, _evento):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        marco = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        forma = QPainterPath()
        forma.addRoundedRect(marco, RADIO, RADIO)
        brillo = QLinearGradient(0, 0, 0, self.height())
        brillo.setColorAt(0.0, QColor(255, 255, 255, 62))
        brillo.setColorAt(0.35, QColor(255, 255, 255, 20))
        brillo.setColorAt(1.0, QColor(255, 255, 255, 8))
        p.fillPath(forma, brillo)
        borde = QLinearGradient(0, 0, 0, self.height())
        borde.setColorAt(0.0, QColor(255, 255, 255, 170))
        borde.setColorAt(1.0, QColor(255, 255, 255, 45))
        p.setPen(QPen(borde, 1))
        p.drawPath(forma)

    def mousePressEvent(self, evento):
        if evento.button() == Qt.LeftButton:
            self.windowHandle().startSystemMove()

    def keyPressEvent(self, evento):
        if evento.key() == Qt.Key_Escape:
            self.close()

    def _alternar(self):
        activar = not self._boton_play.property("activo")
        if self._al_play(activar):
            self._poner_escucha(activar)

    def _poner_escucha(self, activo):
        self._boton_play.setText("■" if activo else "▶")
        self._boton_play.setProperty("activo", activo)
        self._boton_play.style().unpolish(self._boton_play)
        self._boton_play.style().polish(self._boton_play)

    def _poner_nivel(self, rms):
        if abs(rms - self._medidor.valor) < 0.002:
            return  # sin cambio visible: no repintar
        self._medidor.valor = rms
        self._medidor.update()

    def _capturar(self):
        # Se oculta un instante para que esta ventana no salga en la captura
        self.hide()
        QTimer.singleShot(220, self._tomar_captura)

    def _tomar_captura(self):
        imagen = self.screen().grabWindow(0)
        self.show()
        if max(imagen.width(), imagen.height()) > MAX_LADO_CAPTURA:
            imagen = imagen.scaled(MAX_LADO_CAPTURA, MAX_LADO_CAPTURA, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        memoria = QBuffer()
        memoria.open(QIODevice.WriteOnly)
        imagen.save(memoria, "PNG")
        self._al_capturar(bytes(memoria.data()))

    def _al_final(self):
        """Cursor propio al final del documento: no mueve el cursor visible ni hace saltar la vista."""
        cursor = QTextCursor(self._texto.document())
        cursor.movePosition(QTextCursor.End)
        return cursor

    def _escribir(self, escribir):
        """Aplica un cambio al texto y, si se estaba viendo el final, sigue mostrando el final."""
        barra = self._texto.verticalScrollBar()
        seguir = barra.value() >= barra.maximum() - 4
        escribir()
        if seguir:
            barra.setValue(barra.maximum())

    def _poner_pregunta(self, pregunta):
        # La respuesta anterior se queda arriba (como en un chat) en vez de borrarse de golpe
        self._volcar()

        def escribir():
            cursor = self._al_final()
            vacio = self._texto.document().isEmpty()
            bloque = QTextBlockFormat()
            bloque.setTopMargin(0 if vacio else 14)
            if vacio:
                cursor.setBlockFormat(bloque)
            else:
                cursor.insertBlock(bloque)
            cursor.insertText(pregunta, self._formato_pregunta)
            cursor.insertBlock(QTextBlockFormat())

        self._escribir(escribir)

    def _agregar(self, delta):
        self._pendiente.append(delta)
        if not self._refresco.isActive():
            self._refresco.start()

    def _volcar(self):
        self._refresco.stop()
        if not self._pendiente:
            return
        texto = "".join(self._pendiente)
        self._pendiente.clear()

        def escribir():
            cursor = self._al_final()
            for i, parte in enumerate(texto.split("\n")):
                if i:
                    cursor.insertBlock(QTextBlockFormat())
                if parte:
                    cursor.insertText(parte, self._formato_respuesta)

        self._escribir(escribir)

    # Interfaz para otros hilos
    def estado(self, texto):
        self._senales.estado.emit(texto)

    def nivel(self, rms):
        self._senales.nivel.emit(rms)

    def nueva_respuesta(self, pregunta):
        self._senales.nueva.emit(pregunta)

    def texto_respuesta(self, delta):
        self._senales.texto.emit(delta)

    def fin_respuesta(self):
        self._senales.fin.emit()

    def escucha(self, activo):
        self._senales.escucha.emit(activo)

    def ejecutar(self):
        # Aplicar la exclusion antes de que DWM muestre la ventana por primera vez.
        excluir_de_capturas(int(self.winId()))
        self.show()
        self._app.exec()

