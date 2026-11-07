"""Configuracion compartida del asistente. Edita aqui tu nombre y los modelos."""
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
MODELO_RESPUESTA = "claude-opus-5-5"
ESFUERZO_RESPUESTA = "low"  # low responde antes; sube a medium si quieres mas profundidad
LINEAS_DE_CONTEXTO = 12  # lineas recientes de la reunion que ve cada modelo

# Transcripcion local
MODELO_WHISPER = "base"  # ~1.5 s por frase en este equipo; "small" es mas preciso pero ~3x mas lento
HILOS_WHISPER = 4
IDIOMA = "es"

# Audio
FRECUENCIA = 16_000
UMBRAL_VOZ_RMS = 0.006  # por debajo se considera silencio
SILENCIO_FIN_S = 0.5  # silencio que cierra una frase; subelo si corta a la gente a media idea
MIN_FRASE_S = 0.6
MAX_FRASE_S = 12.0
