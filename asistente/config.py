"""Configuracion compartida del asistente. Edita aqui tu nombre y los modelos."""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PROYECTO = RAIZ.parent
CARPETA_TEMAS = next(PROYECTO.glob("Temas - *"), PROYECTO / "Temas")
ARCHIVO_ENV = RAIZ / ".env"

# A quien debe reconocer como destinatario de las preguntas
NOMBRE_USUARIO = "Thais"
APELLIDO_USUARIO = "Cedeño"
ASIGNATURA = "Microeconomía I"

# Claude
MODELO_FILTRO = "claude-haiku-5-5"
MODELO_RESPUESTA = "claude-haiku-5-5"  # el mas barato; "claude-sonnet-5-5" u "claude-opus-5-5" responden mejor
MODO_RAPIDO = True  # solo aplica con Opus: genera hasta 2.5x mas rapido al doble de precio
ESFUERZO_RESPUESTA = "low"  # low responde antes; sube a medium si quieres mas profundidad
LINEAS_DE_CONTEXTO = 12  # lineas recientes de la reunion que ve el filtro
TURNOS_DE_CONVERSACION = 10  # preguntas y respuestas anteriores que recuerda al responder

# Transcripcion local
MODELO_WHISPER = "base"  # en CPU ~1.5 s por frase
MODELO_WHISPER_GPU = "small"  # en GPU, mas preciso que "base" y aun asi rapido
USAR_GPU = True  # usa CUDA si esta disponible (requirements-gpu.txt); si falla, vuelve a CPU sola
HILOS_WHISPER = max(4, (os.cpu_count() or 8) // 2)  # nucleos fisicos aprox.; int8 no gana con hyperthreading
IDIOMA = "es"

# Audio
FRECUENCIA = 16_000
UMBRAL_VOZ_RMS = 0.006  # por debajo se considera silencio
SILENCIO_FIN_S = 0.4  # silencio que cierra una frase; subelo si corta a la gente a media idea
ESPERA_FIN_PREGUNTA_S = 1.2  # sin frases nuevas tras una pregunta: se da por terminada y se responde
ESPERA_MAX_PREGUNTA_S = 8.0  # si siguen hablando sin pausa, se responde igual pasado este tiempo
MIN_FRASE_S = 0.6
MAX_FRASE_S = 8.0  # una frase mas larga se transcribe en trozos, sin esperar a que termine
