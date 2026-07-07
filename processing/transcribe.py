"""Транскрибация звонка (OpenAI) со стерео-диаризацией.

Мегафон ВАТС пишет честное стерео: стороны разговора на разных каналах. Режем на
два моно. Чтобы получить настоящий ДИАЛОГ (реплики по очереди), каждый канал
дробим на реплики по паузам (pydub detect_nonsilent), транскрибируем каждую
реплику отдельно и склеиваем оба канала по таймингам. Так работает даже с
gpt-4o-transcribe, который не отдаёт посегментные тайминги.

Для моно — один прогон, спикеров позже размечает Claude (heuristic).

Результат — список реплик: {speaker, start, end, text}.
"""
import os
import tempfile

from flask import current_app
from openai import OpenAI
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

# параметры нарезки каналов на реплики
_MIN_SILENCE_MS = 700   # пауза, разделяющая реплики
_MIN_CHUNK_MS = 350     # игнорируем совсем короткие всплески (шум)
_PAD_MS = 150           # добавка по краям реплики, чтобы не срезать слова
_MAX_CHUNKS_PER_CHANNEL = 120  # выше — не дробим (защита от лавины запросов)


def _client() -> OpenAI:
    key = current_app.config.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY не задан в окружении.")
    return OpenAI(api_key=key)


def _seg_attr(seg, name, default=None):
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def _transcribe_path(client: OpenAI, model: str, path: str) -> list[dict]:
    """Транскрибировать файл → сегменты {start, end, text}.

    Если модель отдаёт посегментные тайминги (verbose_json) — используем их;
    иначе один блок текста (start=0).
    """
    resp = None
    try:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model,
                file=f,
                language="ru",
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
    except Exception:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model, file=f, language="ru", response_format="json"
            )

    segments = _seg_attr(resp, "segments", None)
    out: list[dict] = []
    if segments:
        for s in segments:
            text = (_seg_attr(s, "text", "") or "").strip()
            if not text:
                continue
            start = _seg_attr(s, "start", 0.0)
            end = _seg_attr(s, "end", None)
            out.append({
                "start": float(start or 0.0),
                "end": (float(end) if end is not None else None),
                "text": text,
            })
    else:
        text = (_seg_attr(resp, "text", "") or "").strip()
        if text:
            out.append({"start": 0.0, "end": None, "text": text})
    return out


def _transcribe_audio_text(client: OpenAI, model: str, audio: AudioSegment) -> str:
    """Транскрибировать фрагмент AudioSegment → плоский текст."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        audio.export(tmp_path, format="mp3")
        with open(tmp_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model, file=f, language="ru", response_format="json"
            )
        return (_seg_attr(resp, "text", "") or "").strip()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _utterance_ranges(mono: AudioSegment) -> list[tuple[int, int]]:
    """Диапазоны речи (мс) в моно-канале по паузам."""
    floor = mono.dBFS
    thresh = (floor - 16) if floor != float("-inf") else -40
    try:
        ranges = detect_nonsilent(
            mono, min_silence_len=_MIN_SILENCE_MS,
            silence_thresh=thresh, seek_step=10,
        )
    except Exception:
        return []
    return [(s, e) for s, e in ranges if (e - s) >= _MIN_CHUNK_MS]


def _transcribe_channel(client, model, mono: AudioSegment, speaker: str) -> list[dict]:
    """Канал → список реплик с таймингами (дробим по паузам)."""
    ranges = _utterance_ranges(mono)
    # нет чёткой нарезки или слишком много кусков — берём канал целиком
    if not ranges or len(ranges) > _MAX_CHUNKS_PER_CHANNEL:
        text = _transcribe_audio_text(client, model, mono)
        return [{"start": 0.0, "end": None, "text": text, "speaker": speaker}] if text else []

    out: list[dict] = []
    dur_ms = len(mono)
    for start_ms, end_ms in ranges:
        lo = max(0, start_ms - _PAD_MS)
        hi = min(dur_ms, end_ms + _PAD_MS)
        text = _transcribe_audio_text(client, model, mono[lo:hi])
        if text:
            out.append({
                "start": start_ms / 1000.0,
                "end": end_ms / 1000.0,
                "text": text,
                "speaker": speaker,
            })
    # если по какой-то причине всё пусто — фолбэк на целый канал
    if not out:
        text = _transcribe_audio_text(client, model, mono)
        if text:
            out.append({"start": 0.0, "end": None, "text": text, "speaker": speaker})
    return out


def transcribe_call(call) -> tuple[list[dict], str]:
    """Транскрибировать звонок. Возвращает (transcript, diarization_mode).

    diarization_mode: 'stereo' (разделение по каналам) | 'heuristic' (моно).
    """
    path = call.audio_path
    if not path or not os.path.exists(path):
        raise RuntimeError(f"Аудиофайл не найден: {path}")

    client = _client()
    model = current_app.config.get("OPENAI_TRANSCRIBE_MODEL") or "gpt-4o-transcribe"
    audio = AudioSegment.from_file(path)

    if audio.channels >= 2:
        mono_channels = audio.split_to_mono()[:2]
        # какой канал — менеджер: явный флаг, иначе дефолт по направлению
        mgr_channel = call.manager_channel
        if mgr_channel not in (0, 1):
            mgr_channel = 0
        transcript: list[dict] = []
        for idx, mono in enumerate(mono_channels):
            speaker = "manager" if idx == mgr_channel else "client"
            transcript.extend(_transcribe_channel(client, model, mono, speaker))
        # склейка обоих каналов по времени → реплики по очереди
        transcript.sort(key=lambda s: s.get("start") or 0.0)
        return transcript, "stereo"

    # моно — спикеров разметит Claude на анализе
    segments = _transcribe_path(client, model, path)
    for seg in segments:
        seg["speaker"] = "unknown"
    return segments, "heuristic"
