"""Asistente de videollamadas: escucha el sistema y responde en un teleprompter."""
import os
import queue
import sys
import threading

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


class Asistente:
    def __init__(self):
        self.fragmentos = queue.Queue(maxsize=20)
        self.lineas = queue.Queue()
        self.preguntas = queue.Queue()
        self.escuchando = False
        self.listo = False
        self.base = None
        self.clave = None
        self.cerebro = None
        self.ultima_pregunta = ""
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
        self.ventana.estado("Listo: pulsa ▶ para escuchar")

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
        # Hilo aparte: esperar a Claude no debe frenar la transcripcion
        while True:
            linea = self.lineas.get()
            try:
                self.cerebro.registrar(linea)
                clasificacion = self.cerebro.clasificar()
            except Exception as e:
                self.ventana.estado(describir(e))
                continue
            pregunta = clasificacion.pregunta.strip()
            if clasificacion.responder and pregunta and pregunta != self.ultima_pregunta:
                self.ultima_pregunta = pregunta
                self.preguntas.put((pregunta, None))

    def bucle_respuestas(self):
        while True:
            pregunta, imagen = self.preguntas.get()
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
