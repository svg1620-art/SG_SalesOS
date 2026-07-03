"""Анализ звонка через Claude по активному чек-листу.

Один структурированный вызов: подаём транскрибацию с ролями + критерии чек-листа,
просим строго JSON (баллы по критериям, зона считается бэкендом, саммери,
рекомендации, упущенные моменты). Парсим через extract_json.
"""
from claude_client import claude_complete
from utils import extract_json

_SYSTEM = (
    "Ты — старший тренер отдела продаж и специалист ОКК. Оцениваешь звонок "
    "менеджера строго по предоставленному чек-листу. Отвечаешь строго на русском "
    "и строго в формате JSON без текста вокруг."
)

_SPEAKER_RU = {"manager": "Менеджер", "client": "Клиент", "unknown": "Говорящий"}


def _format_transcript(transcript: list[dict]) -> str:
    lines = []
    for seg in transcript or []:
        speaker = _SPEAKER_RU.get(seg.get("speaker"), "Говорящий")
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{speaker}] {text}")
    return "\n".join(lines) or "(пустая транскрибация)"


def _format_criteria(checklist) -> str:
    lines = []
    for c in checklist.criteria:
        flag = ", КРИТИЧНЫЙ" if c.is_critical else ""
        lines.append(
            f"- criterion_id={c.id} | «{c.title}» (вес {c.weight}{flag}): "
            f"{c.description or 'без описания'}"
        )
    return "\n".join(lines)


def build_analysis_prompt(checklist, transcript: list[dict], diarization: str) -> str:
    heuristic_note = ""
    if diarization != "stereo":
        heuristic_note = (
            "\nВНИМАНИЕ: роли говорящих не размечены (моно-запись). Сам определи по "
            "смыслу реплик, где менеджер, а где клиент.\n"
        )
    return f"""\
Оцени звонок отдела продаж по чек-листу.

ЧЕК-ЛИСТ (критерии, вес, что считается «хорошо»):
{_format_criteria(checklist)}
{heuristic_note}
ТРАНСКРИБАЦИЯ ЗВОНКА:
\"\"\"
{_format_transcript(transcript)}
\"\"\"

Правила оценки:
- Каждый критерий оценивай по шкале 0–10 (score), max_score всегда 10.
- evidence — короткая дословная цитата из транскрибации, подтверждающая оценку
  (или пустая строка, если критерий вообще не проявлен).
- is_missed=true, если критерий провален или упущен.
- overall_score (0–100) посчитай как взвешенную сумму: сумма (score/10 * вес).
- summary — 3–5 предложений: как прошёл звонок в целом.
- recommendations — коучинг по навыкам (2–5 шт.), priority: high|med|low.
- missed_moments — упущенные моменты (может быть пустым). Для каждого укажи
  quote — ТОЧНУЮ дословную цитату из транскрибации (без «[Менеджер]»/«[Клиент]»),
  рядом с которой менеджер упустил момент; по ней подсветим место в тексте.

Верни СТРОГО JSON такого вида (criterion_id брать из чек-листа выше):
{{
  "overall_score": 0,
  "summary": "...",
  "criteria": [
    {{"criterion_id": 0, "score": 0, "max_score": 10,
      "evidence": "...", "comment": "...", "is_missed": false}}
  ],
  "recommendations": [
    {{"skill": "...", "priority": "high", "text": "..."}}
  ],
  "missed_moments": [
    {{"quote": "дословная цитата из транскрибации",
      "label": "...", "explanation": "..."}}
  ]
}}
"""


def analyze_call(call, checklist) -> dict:
    """Прогнать анализ через Claude, вернуть распарсенный dict.

    Бросает ValueError/RuntimeError при недоступности модели или неразборчивом
    ответе — обрабатывается воркером (статус failed).
    """
    prompt = build_analysis_prompt(
        checklist, call.transcript_json or [], call.diarization or "heuristic"
    )
    raw = claude_complete(prompt, system=_SYSTEM, max_tokens=4000)
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("Модель вернула не JSON-объект анализа.")
    return data
