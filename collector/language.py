from langdetect import detect, LangDetectException


def detect_language(text: str, fallback: str = "unknown") -> str:
    if not text or len(text.strip()) < 10:
        return fallback
    try:
        return detect(text)
    except LangDetectException:
        return fallback
