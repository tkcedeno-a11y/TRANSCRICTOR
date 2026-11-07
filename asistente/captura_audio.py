"""Captura del audio del sistema (WASAPI loopback) y segmentacion en frases por energia."""
import queue
import sys
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

from config import FRECUENCIA, UMBRAL_VOZ_RMS, SILENCIO_FIN_S, MIN_FRASE_S, MAX_FRASE_S

PREVIO_S = 0.3  # audio anterior al inicio de la frase que se conserva
TAM_BLOQUE = 1024  # frames por bloque del driver
N_PREVIO = int(PREVIO_S * FRECUENCIA)
N_SILENCIO = int(SILENCIO_FIN_S * FRECUENCIA)
N_MAX = int(MAX_FRASE_S * FRECUENCIA)
N_MIN = int(MIN_FRASE_S * FRECUENCIA)


def _buscar_loopback(p):
    """Devuelve el dispositivo de loopback equivalente a la salida por defecto."""
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    salida = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if salida.get("isLoopbackDevice"):
        return salida
    for lb in p.get_loopback_device_info_generator():
        if salida["name"] in lb["name"]:
            return lb
    raise RuntimeError("No hay dispositivo loopback para " + salida["name"])


def _a_mono_frecuencia(datos, canales, frecuencia_origen):
    """Convierte bytes float32 intercalados a mono float32 a FRECUENCIA Hz."""
    x = np.frombuffer(datos, dtype=np.float32)
    if canales > 1:
        x = x.reshape(-1, canales).mean(axis=1)
    if frecuencia_origen != FRECUENCIA and len(x) > 1:
        n_salida = int(round(len(x) * FRECUENCIA / frecuencia_origen))
        x = np.interp(np.linspace(0, len(x) - 1, n_salida), np.arange(len(x)), x)
    return x.astype(np.float32)


class CapturaAudio:
    def __init__(self, al_fragmento):
        self._al_fragmento = al_fragmento  # una llamada por frase
        self.nivel = 0.0  # ultimo RMS
        self._rms_max = 0.0  # maximo RMS visto (para pruebas)
        self._activo = False
        self._hilo = None
        self._cola = None
        self._t_ultimo = time.monotonic()
        self._en_frase = False
        self._frase = []
        self._n_frase = 0
        self._n_silencio = 0
        self._previo = np.zeros(0, dtype=np.float32)

    def iniciar(self):
        if self._hilo is not None and self._hilo.is_alive():
            return
        self._activo = True
        self._cola = queue.Queue(maxsize=500)  # descarta si el hilo se atrasa
        self._t_ultimo = time.monotonic()
        self._hilo = threading.Thread(target=self._bucle, daemon=True, name="captura-audio")
        self._hilo.start()

    def detener(self):
        self._activo = False
        if self._hilo is not None and self._hilo is not threading.current_thread():
            self._hilo.join(timeout=3)  # no esperar indefinidamente

    def _callback_audio(self, in_data, frame_count, time_info, status):
        # Solo encola los bytes; el procesamiento se hace en el hilo de captura
        try:
            self._cola.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)

    def _bucle(self):
        p = pyaudio.PyAudio()
        stream = None
        try:
            dev = _buscar_loopback(p)
            canales = int(dev["maxInputChannels"])
            frecuencia = int(dev["defaultSampleRate"])
            stream = p.open(
                format=pyaudio.paFloat32,
                channels=canales,
                rate=frecuencia,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=TAM_BLOQUE,
                stream_callback=self._callback_audio,
            )
            stream.start_stream()
            while self._activo:
                try:
                    datos = self._cola.get(timeout=0.1)
                except queue.Empty:
                    self._sin_datos()
                    continue
                self._procesar(_a_mono_frecuencia(datos, canales, frecuencia))
        except Exception as e:
            print(f"[captura] error: {e}", file=sys.stderr)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()
            self.nivel = 0.0

    def _sin_datos(self):
        # WASAPI loopback no entrega bloques en silencio: cierra por tiempo real
        self.nivel = 0.0
        if self._en_frase and time.monotonic() - self._t_ultimo >= SILENCIO_FIN_S:
            self._cerrar_frase()

    def _procesar(self, x):
        if len(x) == 0:
            return
        self._t_ultimo = time.monotonic()
        rms = float(np.sqrt(np.mean(np.square(x))))
        self.nivel = rms
        self._rms_max = max(self._rms_max, rms)
        voz = rms >= UMBRAL_VOZ_RMS

        if not self._en_frase:
            if not voz:
                self._previo = np.concatenate([self._previo, x])[-N_PREVIO:]
                return
            self._en_frase = True
            self._frase = [self._previo]
            self._n_frase = len(self._previo)
            self._n_silencio = 0

        self._frase.append(x)
        self._n_frase += len(x)
        self._n_silencio = 0 if voz else self._n_silencio + len(x)
        if self._n_silencio >= N_SILENCIO or self._n_frase >= N_MAX:
            self._cerrar_frase()

    def _cerrar_frase(self):
        audio = np.concatenate(self._frase).astype(np.float32)
        self._previo = audio[-N_PREVIO:]
        self._en_frase = False
        self._frase = []
        self._n_frase = 0
        self._n_silencio = 0
        if len(audio) < N_MIN:
            return  # frase demasiado corta
        try:
            self._al_fragmento(audio)
        except Exception as e:
            print(f"[captura] error en al_fragmento: {e}", file=sys.stderr)


if __name__ == "__main__":
    p = pyaudio.PyAudio()
    try:
        dispositivo = _buscar_loopback(p)
        print("Dispositivo:", dispositivo["name"])
    finally:
        p.terminate()

    frases = []
    captura = CapturaAudio(lambda a: frases.append(round(len(a) / FRECUENCIA, 2)))
    captura.iniciar()
    time.sleep(5)
    captura.detener()
    print(f"RMS maximo: {captura._rms_max:.5f}")
    print("Frases capturadas (s):", frases)
