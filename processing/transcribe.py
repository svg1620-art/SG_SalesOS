"""Транскрибация звонка (OpenAI) со стерео-диаризацией.

Мегафон ВАТС пишет честное стерео: стороны разговора на разных каналах. Режем на
два моно, транскрибируем отдельно, размечаем спикеров по каналу и склеиваем по
таймингам. Для моно — один прогон, спикеров позже размечает Claude (heuristic).

Результат — список реплик: {speaker, start, end, text}.
"""
import os
import tempfile

from flask import current_app
from openai import OpenAI
from pydub import AudioSegment


def _client() -> OpenAI:
    key = current_app.config.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY не задан в окружении.")
    return OpenAI(api_key=key)


def _seg_attr(seg, name, default=None):
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)


def _transcribe_file(client: OpenAI, model: str, path: str) -> list[dict]:
    """Транскрибировать один файл → список сегментов {start, end, text}.

    Пытаемся получить посегментные тайминги (verbose_json + segment); если модель
    их не поддерживает (gpt-4o-transcribe отдаёт только текст) — один блок текста.
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
        # модель/формат без таймингов — обычный json (только текст)
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
            out.append(
                {
                    "start": float(start or 0.0),
                    "end": (float(end) if end is not None else None),
                    "text": text,
                }
            )
    else:
        text = (_seg_attr(resp, "text", "") or "").strip()
        if text:
            out.append({"start": 0.0, "end": None, "text": text})
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
            # для исходящего инициатор-менеджер обычно на первом канале
            mgr_channel = 0
        transcript: list[dict] = []
        for idx, mono in enumerate(mono_channels):
            speaker = "manager" if idx == mgr_channel else "client"
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".mp3", delete=False
                ) as tmp:
                    tmp_path = tmp.name
                mono.export(tmp_path, format="mp3")
                for seg in _transcribe_file(client, model, tmp_path):
                    seg["speaker"] = speaker
                    transcript.append(seg)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        transcript.sort(key=lambda s: s.get("start") or 0.0)
        return transcript, "stereo"

    # моно — спикеров разметит Claude на анализе
    segments = _transcribe_file(client, model, path)
    for seg in segments:
        seg["speaker"] = "unknown"
    return segments, "heuristic"
