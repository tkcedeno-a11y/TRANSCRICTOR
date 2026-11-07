"""Transcripcion local de frases con faster-whisper."""
import numpy as np
from faster_whisper import WhisperModel

from config import MODELO_WHISPER, HILOS_WHISPER, IDIOMA, NOMBRE_USUARIO, APELLIDO_USUARIO, ASIGNATURA

# Pista de vocabulario para Whisper (en espanol)
PROMPT_INICIAL = (
    f"Clase de {ASIGNATURA} con {NOMBRE_USUARIO} {APELLIDO_USUARIO}. "
    "Terminos: costo de oportunidad, elasticidad, frontera de posibilidades de produccion, "
    "excedente del consumidor."
)
UMBRAL_NO_VOZ = 0.6  # segmentos con no_speech_prob por encima se descartan


class Transcriptor:
    def __init__(self):
        self._modelo = WhisperModel(
            MODELO_WHISPER,
            device="cpu",
            compute_type="int8",
            cpu_threads=HILOS_WHISPER,
        )

    def transcribir(self, audio):
        """Devuelve el texto de un audio float32 a 16 kHz, o "" si no hay habla."""
        if audio is None or len(audio) == 0:
            return ""
        segmentos, _ = self._modelo.transcribe(
            np.asarray(audio, dtype=np.float32),
            language=IDIOMA,
            beam_size=1,
            temperature=0.0,  # sin reintentos lentos cuando el audio es malo
            without_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=PROMPT_INICIAL,
        )
        # transcribe devuelve un generador: se consume aqui dentro
        textos = [s.text.strip() for s in segmentos if s.no_speech_prob <= UMBRAL_NO_VOZ]
        return " ".join(t for t in textos if t).strip()
