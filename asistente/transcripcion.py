"""Transcripcion local de frases con faster-whisper (GPU si hay CUDA, si no CPU)."""
import os
import sys
import sysconfig
from pathlib import Path

import numpy as np

from config import MODELO_WHISPER, MODELO_WHISPER_GPU, HILOS_WHISPER, IDIOMA, NOMBRE_USUARIO, APELLIDO_USUARIO, ASIGNATURA, USAR_GPU


def _registrar_dll_cuda():
    """En Windows, las DLL de los paquetes pip nvidia-* no estan en PATH: se registran aqui."""
    if os.name != "nt":
        return
    base = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    for carpeta in base.glob("*/bin"):
        try:
            os.add_dll_directory(str(carpeta))
            os.environ["PATH"] = str(carpeta) + os.pathsep + os.environ.get("PATH", "")
        except OSError:
            pass


_registrar_dll_cuda()
from faster_whisper import WhisperModel  # noqa: E402  (despues de registrar las DLL)

# Pista para Whisper: con tildes y bien escrita, porque imita el estilo del prompt
PROMPT_INICIAL = f"Clase de {ASIGNATURA} con {NOMBRE_USUARIO} {APELLIDO_USUARIO}, en español."
# Vocabulario que Whisper debe preferir cuando duda (no frena la transcripcion)
VOCABULARIO = (
    "costo de oportunidad, elasticidad precio de la demanda, elasticidad cruzada, "
    "frontera de posibilidades de producción, excedente del consumidor, excedente del productor, "
    "utilidad marginal, curva de indiferencia, restricción presupuestaria, tasa marginal de sustitución, "
    "oferta, demanda, equilibrio de mercado, ceteris paribus, bienes sustitutos, bienes complementarios, "
    "bien inferior, bien normal, costo marginal, costo fijo, costo variable, rendimientos decrecientes, "
    "competencia perfecta, monopolio, oligopolio, microeconomía, macroeconomía"
)
UMBRAL_NO_VOZ = 0.6  # segmentos con no_speech_prob por encima se descartan
CONTEXTO_PREVIO = 200  # caracteres de la frase anterior que se pasan como contexto


def _hay_cuda():
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class Transcriptor:
    def __init__(self):
        self.dispositivo = "cpu"
        self._anterior = ""  # ultima frase transcrita: da continuidad a frases partidas
        if USAR_GPU and _hay_cuda():
            try:
                self._modelo = WhisperModel(MODELO_WHISPER_GPU, device="cuda", compute_type="int8_float16")
                self._calentar()  # las DLL de CUDA se cargan aqui: si faltan, falla y se usa CPU
                self.dispositivo = "cuda"
                return
            except Exception as e:
                print(f"[transcripcion] GPU no disponible, uso CPU: {e}", file=sys.stderr)
        self._modelo = WhisperModel(
            MODELO_WHISPER,
            device="cpu",
            compute_type="int8",
            cpu_threads=HILOS_WHISPER,
        )
        self._calentar()

    def _calentar(self):
        """La primera transcripcion es lenta (reserva memoria, carga kernels): se hace al arrancar."""
        ruido = (np.random.default_rng(0).standard_normal(16_000) * 0.01).astype(np.float32)
        # Sin VAD, para que el encoder y el decoder corran de verdad
        segmentos, _ = self._modelo.transcribe(ruido, language=IDIOMA, beam_size=1, vad_filter=False)
        list(segmentos)

    def transcribir(self, audio):
        """Devuelve el texto de un audio float32 a 16 kHz, o "" si no hay habla."""
        if audio is None or len(audio) == 0:
            return ""
        prompt = PROMPT_INICIAL
        if self._anterior:
            prompt += " " + self._anterior[-CONTEXTO_PREVIO:]
        segmentos, _ = self._modelo.transcribe(
            np.asarray(audio, dtype=np.float32),
            language=IDIOMA,
            # En GPU buscar entre 5 hipotesis es casi gratis y acierta mas; en CPU costaria ~2x
            beam_size=5 if self.dispositivo == "cuda" else 1,
            temperature=0.0,  # sin reintentos lentos cuando el audio es malo
            without_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            hotwords=VOCABULARIO,
        )
        # transcribe devuelve un generador: se consume aqui dentro
        textos = [s.text.strip() for s in segmentos if s.no_speech_prob <= UMBRAL_NO_VOZ]
        texto = " ".join(t for t in textos if t).strip()
        if texto:
            self._anterior = texto
        return texto
