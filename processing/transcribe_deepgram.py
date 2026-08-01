"""Транскрибация звонка через Deepgram (pre-recorded API).

Альтернатива OpenAI: Deepgram расшифровывает стерео одним запросом с
`multichannel=true` (каждый канал = отдельная сторона разговора) и точными
таймингами — идеально для записей Мегафон ВАТС. Для моно используем
`diarize=true` (разметка спикеров), роли позже уточняет Claude (heuristic).

Прямой REST-вызов через httpx (без SDK) — чтобы не конфликтовать с пиннингом
openai/httpx. Формат результата тот же, что у OpenAI-транскрайбера:
список реплик {speaker, start, end, text} + режим диаризации.
"""
import os

import httpx
from pydub import AudioSegment

_DG_URL = "https://api.deepgram.com/v1/listen"
_TIMEOUT = 300  # длинные записи качаются/жуются не мгновенно

_CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".flac": "audio/flac", ".aac": "audio/aac", ".webm": "audio/webm",
}


def _api_key() -> str:
    from settings_store import deepgram_api_key
    key = deepgram_api_key()
    if not key:
        raise RuntimeError("Ключ Deepgram не задан (Настройки → Транскрибация).")
    return key


def _content_type(path: str) -> str:
    return _CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "audio/mpeg")


def _request(path: str, params: dict) -> dict:
    """Отправить файл в Deepgram, вернуть распарсенный JSON (или бросить)."""
    with open(path, "rb") as f:
        audio = f.read()
    headers = {
        "Authorization": f"Token {_api_key()}",
        "Content-Type": _content_type(path),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as cl:
            r = cl.post(_DG_URL, params=params, headers=headers, content=audio)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Deepgram: сеть — {exc}")
    if r.status_code == 401:
        raise RuntimeError("Deepgram: 401 — неверный DEEPGRAM_API_KEY.")
    if r.status_code != 200:
        raise RuntimeError(f"Deepgram HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _base_params() -> dict:
    from settings_store import deepgram_model
    model = deepgram_model()
    return {
        "model": model,
        "language": "ru",
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",  # готовая нарезка на реплики с таймингами
    }


def _utterances(resp: dict) -> list[dict]:
    return ((resp.get("results") or {}).get("utterances")) or []


def _channel_transcript(resp: dict, channel: int) -> str:
    """Фолбэк: цельный текст канала, если реплики не пришли."""
    channels = ((resp.get("results") or {}).get("channels")) or []
    if channel < len(channels):
        alts = (channels[channel] or {}).get("alternatives") or []
        if alts:
            return (alts[0].get("transcript") or "").strip()
    return ""


def _transcribe_stereo(path: str, mgr_channel: int) -> list[dict]:
    """Стерео: multichannel — каждый канал отдельно, склейка по времени."""
    params = _base_params()
    params["multichannel"] = "true"
    resp = _request(path, params)

    transcript: list[dict] = []
    for u in _utterances(resp):
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        ch = int(u.get("channel") or 0)
        speaker = "manager" if ch == mgr_channel else "client"
        transcript.append({
            "start": float(u.get("start") or 0.0),
            "end": float(u.get("end") or 0.0),
            "text": text,
            "speaker": speaker,
        })

    # фолбэк: реплик нет — берём цельный текст каждого канала
    if not transcript:
        for ch in (0, 1):
            text = _channel_transcript(resp, ch)
            if text:
                speaker = "manager" if ch == mgr_channel else "client"
                transcript.append({"start": 0.0, "end": None, "text": text,
                                   "speaker": speaker})

    transcript.sort(key=lambda s: s.get("start") or 0.0)
    return transcript


def _transcribe_mono(path: str) -> list[dict]:
    """Моно: diarize — разметка спикеров; роли уточнит Claude (heuristic)."""
    params = _base_params()
    params["diarize"] = "true"
    resp = _request(path, params)

    transcript: list[dict] = []
    for u in _utterances(resp):
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        transcript.append({
            "start": float(u.get("start") or 0.0),
            "end": float(u.get("end") or 0.0),
            "text": text,
            # роли (менеджер/клиент) определит Claude на анализе
            "speaker": "unknown",
        })
    if not transcript:
        text = _channel_transcript(resp, 0)
        if text:
            transcript.append({"start": 0.0, "end": None, "text": text,
                               "speaker": "unknown"})
    return transcript


def transcribe_call(call) -> tuple[list[dict], str]:
    """Транскрибировать звонок через Deepgram. → (transcript, diarization_mode)."""
    path = call.audio_path
    if not path or not os.path.exists(path):
        raise RuntimeError(f"Аудиофайл не найден: {path}")

    # число каналов — по локальному файлу (дёшево), чтобы выбрать режим
    try:
        channels = AudioSegment.from_file(path).channels
    except Exception:  # noqa: BLE001
        channels = 1

    if channels >= 2:
        mgr_channel = call.manager_channel
        if mgr_channel not in (0, 1):
            mgr_channel = 0
        return _transcribe_stereo(path, mgr_channel), "stereo"

    return _transcribe_mono(path), "heuristic"
