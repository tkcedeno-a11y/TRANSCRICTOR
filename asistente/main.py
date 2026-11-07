"""Asistente de videollamadas: escucha el sistema y responde en un teleprompter."""
import os
import queue
import sys
import threading
import time
import re
import unicodedata
from collections import deque

import anthropic
from dotenv import load_dotenv

import config
from base_conocimiento import cargar_base
from captura_audio import CapturaAudio
from cerebro import PETICION_CAPTURA, Cerebro
from transcripcion import Transcriptor
from ventana import Ventana


def describir(error):
    if isinstance(error, anthropic.AuthenticationError):
        return "API key inválida: revisa asistente/.env"
    if isinstance(error, anthropic.RateLimitError):
        return "Límite de la API: espera unos segundos"
    if isinstance(error, anthropic.APIStatusError):
        return f"Error de Claude ({error.status_code})"
    if isinstance(error, anthropic.APIConnectionError):
        return "Sin conexión con Claude"
    print(f"[asistente] {error!r}", file=sys.stderr)
    return f"Error: {error}"


# Palabras que no cambian de que trata una pregunta
_RELLENO = set("""
a al alguien algo como con cual cuales cuando de del el ella ellos en entre es esta este esto explica
explicar explicame explicarnos la las le les lo los me mi nos o para pero podria podrias por puede
puedes que quien se ser si sobre son su sus te tu un una uno unos unas y ya hay dime decir digan
pasa pasaria ocurre sucede seria significa entiende entienden entendemos piensan opinan creen cree
bueno entonces osea sea vamos ver mas muy tambien eso esa ese esos esas
""".split())


def _palabras_clave(texto):
    sin_tildes = unicodedata.normalize("NFKD", texto.casefold()).encode("ascii", "ignore").decode()
    palabras = (p[:-1] if len(p) > 4 and p.endswith("s") else p for p in re.findall(r"\w+", sin_tildes))
    return frozenset(p for p in palabras if p not in _RELLENO)


class Asistente:
    def __init__(self):
        self.fragmentos = queue.Queue(maxsize=20)
        self.lineas = queue.Queue()
        self.preguntas = queue.Queue()
        self.recientes = deque(maxlen=5)  # (momento, palabras clave) de las ultimas preguntas respondidas
        self.escuchando = False
        self.listo = False
        self.base = None
        self.clave = None
        self.cerebro = None
        self.ventana = Ventana(self.al_play, self.al_capturar)
        self.captura = CapturaAudio(self.al_fragmento)

    def preparar_cerebro(self):
        """Relee .env, asi la clave se puede pegar sin reiniciar la aplicacion."""
        load_dotenv(config.ARCHIVO_ENV, override=True)
        clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not clave.startswith("sk-"):
            self.ventana.estado("Falta la API key: pégala en asistente/.env")
            return False
        if clave != self.clave:
            self.clave = clave
            self.cerebro = Cerebro(self.base)
        return True

    def al_play(self, activar):
        if activar:
            if not self.listo:
                self.ventana.estado("Aún cargando, espera un momento...")
                return False
            if not self.preparar_cerebro():
                return False
            # Precalienta cache y conexion mientras empieza la reunion
            threading.Thread(target=self.cerebro.calentar, daemon=True).start()
            self.captura.iniciar()
            self.ventana.estado("Escuchando")
        else:
            threading.Thread(target=self.captura.detener, daemon=True).start()
            self.ventana.estado("Detenido")
        self.escuchando = activar
        return True

    def al_capturar(self, png):
        if self.listo and self.preparar_cerebro():
            self.preguntas.put((PETICION_CAPTURA, png))

    def al_fragmento(self, audio):
        if not self.escuchando:
            return
        try:
            self.fragmentos.put_nowait(audio)
        except queue.Full:
            self.ventana.estado("Transcripción atrasada: se omitió una frase")

    def preparar(self):
        """Carga lo pesado sin bloquear la ventana y arranca los hilos de trabajo."""
        try:
            self.base = cargar_base()
            self.transcriptor = Transcriptor()
        except Exception as e:
            self.ventana.estado(f"Error al iniciar: {e}")
            return
        for bucle in (self.bucle_transcripcion, self.bucle_filtro, self.bucle_respuestas, self.bucle_nivel):
            threading.Thread(target=bucle, daemon=True).start()
        self.listo = True
        motor = "GPU" if self.transcriptor.dispositivo == "cuda" else "CPU"
        self.ventana.estado(f"Listo ({motor}): pulsa ▶ para escuchar")

    def bucle_nivel(self):
        evento = threading.Event()
        while not evento.wait(0.1):
            self.ventana.nivel(self.captura.nivel)

    def bucle_transcripcion(self):
        while True:
            audio = self.fragmentos.get()
            try:
                texto = self.transcriptor.transcribir(audio)
            except Exception as e:
                self.ventana.estado(describir(e))
                continue
            if texto:
                self.ventana.estado(f"Oí: {texto[:90]}")
                self.lineas.put(texto)

    def bucle_filtro(self):
        """Clasifica lo que se oye y lanza la respuesta cuando la pregunta termino.

        Una pregunta larga llega en varias frases. En vez de responder a la primera parte, se
        guarda como pendiente, se actualiza con cada frase nueva (Claude la reescribe completa)
        y solo se responde tras config.ESPERA_FIN_PREGUNTA_S sin frases nuevas ni voz.
        """
        pendiente = None
        t_ultima = 0.0
        while True:
            espera = None
            if pendiente:
                espera = max(0.05, t_ultima + config.ESPERA_FIN_PREGUNTA_S - time.monotonic())
            try:
                lineas = [self.lineas.get(timeout=espera)]
            except queue.Empty:
                hablando = self.captura.hablando
                tope = time.monotonic() - t_ultima >= config.ESPERA_MAX_PREGUNTA_S
                if hablando and not tope:
                    continue  # sigue hablando: puede estar terminando la pregunta
                if not self.repetida(pendiente):
                    self.cerebro.marcar_respondida(pendiente)
                    self.recientes.append((time.monotonic(), _palabras_clave(pendiente)))
                    self.preguntas.put((pendiente, None))
                pendiente = None
                continue
            # Si se acumularon lineas mientras Claude clasificaba, se registran todas
            # y se clasifica solo la ultima: evita ir cada vez mas atrasado
            while True:
                try:
                    lineas.append(self.lineas.get_nowait())
                except queue.Empty:
                    break
            t_ultima = time.monotonic()
            try:
                for linea in lineas:
                    self.cerebro.registrar(linea)
                clasificacion = self.cerebro.clasificar(pendiente)
            except Exception as e:
                self.ventana.estado(describir(e))
                continue
            pregunta = clasificacion.pregunta.strip()
            if clasificacion.responder and pregunta and not self.repetida(pregunta):
                pendiente = pregunta  # la version mas reciente es la mas completa
                self.ventana.estado("Pregunta detectada: esperando a que termine...")

    def repetida(self, pregunta):
        """True si pregunta lo mismo que una ya respondida, con otras palabras de relleno.

        Compara solo las palabras con contenido: "¿Qué es la oferta?" y "¿Qué es la demanda?"
        se parecen letra a letra pero son preguntas distintas. Las reformulaciones mas libres
        las descarta el filtro de Claude, que recibe la lista de ya respondidas.
        """
        nueva = _palabras_clave(pregunta)
        if not nueva:
            return False
        if any(nueva == _palabras_clave(previa) for previa in self.cerebro.respondidas):
            return True
        # Poco despues de responder, el profesor suele repetir la pregunta con otras palabras:
        # si comparte la mayoria de sus palabras clave con una reciente, es la misma
        ahora = time.monotonic()
        for momento, previa in self.recientes:
            menor = min(len(nueva), len(previa))
            if ahora - momento <= config.VENTANA_REPETIDAS_S and menor >= 2 and len(nueva & previa) / menor >= 0.6:
                return True
        return False

    def siguiente_pregunta(self):
        """Espera una pregunta; si se acumularon varias, salta a la mas reciente (las capturas no se saltan)."""
        pendientes = [self.preguntas.get()]
        while True:
            try:
                pendientes.append(self.preguntas.get_nowait())
            except queue.Empty:
                break
        capturas = [p for p in pendientes if p[1]]
        habladas = [p for p in pendientes if not p[1]]
        elegidas = capturas + habladas[-1:]
        for resto in elegidas[1:]:
            self.preguntas.put(resto)
        return elegidas[0]

    def bucle_respuestas(self):
        while True:
            pregunta, imagen = self.siguiente_pregunta()
            self.ventana.nueva_respuesta("Captura de pantalla" if imagen else pregunta)
            try:
                for delta in self.cerebro.responder(pregunta, imagen):
                    self.ventana.texto_respuesta(delta)
            except Exception as e:
                self.ventana.texto_respuesta(describir(e))
            self.ventana.fin_respuesta()

    def ejecutar(self):
        threading.Thread(target=self.preparar, daemon=True).start()
        try:
            self.ventana.ejecutar()
        finally:
            self.captura.detener()


if __name__ == "__main__":
    Asistente().ejecutar()
