"""Сид дефолтного чек-листа «Универсальная оценка звонка отдела продаж».

Приложение A ТЗ: 8 домен-агностичных критериев, веса в сумме = 100,
критичные (ядро продажи) — №2, 4, 5, 7. Активен по умолчанию.
"""
from extensions import db

DEFAULT_CHECKLIST_NAME = "Универсальная оценка звонка отдела продаж"

DEFAULT_CRITERIA = [
    {
        "title": "Установление контакта",
        "weight": 8,
        "is_critical": False,
        "description": "Поздоровался, представился, назвал компанию, "
        "доброжелательный тон, обозначил цель звонка.",
    },
    {
        "title": "Выявление потребностей / квалификация",
        "weight": 20,
        "is_critical": True,
        "description": "Задавал открытые вопросы, выяснил задачу и боль клиента, "
        "ситуацию, критерии выбора, кто принимает решение, сроки/бюджет "
        "(по применимости).",
    },
    {
        "title": "Активное слушание",
        "weight": 10,
        "is_critical": False,
        "description": "Не перебивал, уточнял, резюмировал слова клиента, "
        "реагировал на сказанное, а не по скрипту.",
    },
    {
        "title": "Презентация через ценность",
        "weight": 18,
        "is_critical": True,
        "description": "Говорил на языке выгод под выявленную потребность "
        "(а не перечислял функции), связывал решение с болью клиента.",
    },
    {
        "title": "Работа с возражениями",
        "weight": 15,
        "is_critical": True,
        "description": "Выслушал возражение, не спорил, отработал по существу, "
        "проверил, что возражение снято.",
    },
    {
        "title": "Инициатива и ведение диалога",
        "weight": 8,
        "is_critical": False,
        "description": "Вёл разговор, удерживал структуру, не отдавал инициативу "
        "клиенту, управлял темпом.",
    },
    {
        "title": "Договорённость о следующем шаге",
        "weight": 15,
        "is_critical": True,
        "description": "Зафиксировал конкретный следующий шаг с датой/временем "
        "(встреча, КП, повторный звонок), получил согласие.",
    },
    {
        "title": "Речь и вежливость",
        "weight": 6,
        "is_critical": False,
        "description": "Грамотная чистая речь, без слов-паразитов и грубости, "
        "корректное завершение разговора.",
    },
]


def seed_default_checklist(app, activate: bool = True) -> tuple[bool, str]:
    """Создать дефолтный чек-лист, если его ещё нет. Идемпотентно.

    Возвращает (created, message). Существующий чек-лист не перезаписываем,
    чтобы не затирать правки админа.
    """
    from models import Checklist, Criterion

    existing = Checklist.query.filter_by(name=DEFAULT_CHECKLIST_NAME).first()
    if existing is not None:
        return False, f"Дефолтный чек-лист уже существует (id={existing.id})."

    checklist = Checklist(
        name=DEFAULT_CHECKLIST_NAME,
        description="Домен-агностичный чек-лист по умолчанию (Приложение A ТЗ). "
        "Применим к любой сфере; правьте веса/описания под свой процесс.",
        domain="Универсальный",
        zone_green_min=80,
        zone_yellow_min=60,
        is_active=activate,
    )
    for order_index, item in enumerate(DEFAULT_CRITERIA):
        checklist.criteria.append(
            Criterion(
                title=item["title"],
                description=item["description"],
                weight=item["weight"],
                order_index=order_index,
                is_critical=item["is_critical"],
            )
        )

    if activate:
        # единственный активный чек-лист
        Checklist.query.filter_by(is_active=True).update({"is_active": False})

    db.session.add(checklist)
    db.session.commit()
    total = sum(c["weight"] for c in DEFAULT_CRITERIA)
    return True, (
        f"Дефолтный чек-лист создан (id={checklist.id}, критериев="
        f"{len(DEFAULT_CRITERIA)}, сумма весов={total}, активен={activate})."
    )
