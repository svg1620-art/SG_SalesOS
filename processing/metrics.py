"""Метрики коммуникации из транскрибации: баланс «говорил/слушал».

Считаем долю времени, что говорил менеджер (talk) против времени, что говорил
клиент — то есть менеджер слушал (listen). Если есть тайминги (start/end) —
берём длительность реплик; иначе оцениваем по длине текста (прокси).
"""

_CHARS_PER_SEC = 15.0  # грубая оценка темпа речи, если нет таймингов


def _seg_duration(seg) -> float:
    start = seg.get("start")
    end = seg.get("end")
    if start is not None and end is not None and end > start:
        return float(end) - float(start)
    return len((seg.get("text") or "").strip()) / _CHARS_PER_SEC


def _accumulate(segments, mgr=0.0, cli=0.0):
    for seg in segments or []:
        speaker = seg.get("speaker")
        if speaker == "manager":
            mgr += _seg_duration(seg)
        elif speaker == "client":
            cli += _seg_duration(seg)
    return mgr, cli


def talk_listen(call):
    """Баланс менеджера по звонку: {'talk': %, 'listen': %} или None.

    None — когда роли неизвестны (моно-звонок) или нет транскрибации.
    """
    mgr, cli = _accumulate(call.transcript_json)
    total = mgr + cli
    if total <= 0:
        return None
    talk = round(mgr / total * 100)
    return {"talk": talk, "listen": 100 - talk}


def aggregate_talk_listen(calls):
    """Средний баланс по набору звонков (по суммарной длительности реплик)."""
    mgr = cli = 0.0
    for call in calls:
        mgr, cli = _accumulate(call.transcript_json, mgr, cli)
    total = mgr + cli
    if total <= 0:
        return None
    talk = round(mgr / total * 100)
    return {"talk": talk, "listen": 100 - talk}
