"""CRUD чек-листов и критериев + AI-генерация. Доступ только для админа."""
from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
)

from extensions import db
from models import Checklist, Criterion, Department
from auth.decorators import admin_required

checklists_bp = Blueprint("checklists", __name__, url_prefix="/checklists")


def _read_department_id(form):
    """department_id из формы: пусто/'0' → None (общий), иначе валидный отдел."""
    raw = (form.get("department_id") or "").strip()
    if not raw or raw == "0":
        return None
    if raw.isdigit() and db.session.get(Department, int(raw)) is not None:
        return int(raw)
    return None


def _parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _read_thresholds(form):
    """Прочитать и провалидировать пороги зон. Возвращает (green, yellow, error)."""
    green = _clamp(_parse_int(form.get("zone_green_min"), 80), 0, 100)
    yellow = _clamp(_parse_int(form.get("zone_yellow_min"), 60), 0, 100)
    if green <= yellow:
        return green, yellow, "Зелёный порог должен быть строго выше жёлтого."
    return green, yellow, None


@checklists_bp.route("/")
@admin_required
def index():
    checklists = Checklist.query.order_by(
        Checklist.is_active.desc(), Checklist.created_at.desc()
    ).all()
    departments = Department.query.order_by(Department.name).all()
    # активный чек-лист по каждому отделу + общий (для сводки сверху)
    active_map = {None: Checklist.query.filter_by(
        department_id=None, is_active=True).first()}
    for d in departments:
        active_map[d.id] = Checklist.query.filter_by(
            department_id=d.id, is_active=True).first()
    return render_template(
        "checklists/index.html", checklists=checklists,
        departments=departments, active_map=active_map,
    )


@checklists_bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    departments = Department.query.order_by(Department.name).all()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Укажите название чек-листа.", "error")
            return render_template(
                "checklists/new.html", form=request.form, departments=departments), 400

        green, yellow, err = _read_thresholds(request.form)
        if err:
            flash(err, "error")
            return render_template(
                "checklists/new.html", form=request.form, departments=departments), 400

        checklist = Checklist(
            name=name[:255],
            description=(request.form.get("description") or "").strip(),
            domain=(request.form.get("domain") or "").strip()[:255],
            department_id=_read_department_id(request.form),
            zone_green_min=green,
            zone_yellow_min=yellow,
            is_active=False,
        )
        db.session.add(checklist)
        db.session.commit()
        flash("Чек-лист создан. Добавьте критерии.", "success")
        return redirect(url_for("checklists.edit", checklist_id=checklist.id))

    return render_template("checklists/new.html", form={}, departments=departments)


@checklists_bp.route("/<int:checklist_id>")
@admin_required
def edit(checklist_id):
    checklist = db.session.get(Checklist, checklist_id) or abort(404)
    weight_sum = sum(c.weight for c in checklist.criteria)
    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "checklists/edit.html", checklist=checklist, weight_sum=weight_sum,
        departments=departments,
    )


@checklists_bp.route("/<int:checklist_id>/edit", methods=["POST"])
@admin_required
def update(checklist_id):
    checklist = db.session.get(Checklist, checklist_id) or abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Название не может быть пустым.", "error")
        return redirect(url_for("checklists.edit", checklist_id=checklist_id))

    green, yellow, err = _read_thresholds(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("checklists.edit", checklist_id=checklist_id))

    checklist.name = name[:255]
    checklist.description = (request.form.get("description") or "").strip()
    checklist.domain = (request.form.get("domain") or "").strip()[:255]
    new_dept = _read_department_id(request.form)
    # при смене отдела активного чек-листа снимаем активность (чтобы не было
    # двух активных в одном отделе); админ активирует заново явно
    if new_dept != checklist.department_id and checklist.is_active:
        checklist.is_active = False
        flash("Отдел изменён — активность снята, активируйте чек-лист заново.", "warning")
    checklist.department_id = new_dept
    checklist.zone_green_min = green
    checklist.zone_yellow_min = yellow
    db.session.commit()
    flash("Чек-лист сохранён.", "success")
    return redirect(url_for("checklists.edit", checklist_id=checklist_id))


@checklists_bp.route("/<int:checklist_id>/activate", methods=["POST"])
@admin_required
def activate(checklist_id):
    checklist = db.session.get(Checklist, checklist_id) or abort(404)
    if not checklist.criteria:
        flash("Нельзя активировать чек-лист без критериев.", "error")
        return redirect(url_for("checklists.edit", checklist_id=checklist_id))

    # единственный активный в рамках отдела (или среди общих, если отдел не задан)
    Checklist.query.filter_by(
        is_active=True, department_id=checklist.department_id
    ).update({"is_active": False})
    checklist.is_active = True
    db.session.commit()
    scope = checklist.department.name if checklist.department else "Все отделы (общий)"
    flash(f"Чек-лист «{checklist.name}» активирован для «{scope}».", "success")
    return redirect(url_for("checklists.index"))


@checklists_bp.route("/<int:checklist_id>/delete", methods=["POST"])
@admin_required
def delete(checklist_id):
    checklist = db.session.get(Checklist, checklist_id) or abort(404)
    db.session.delete(checklist)
    db.session.commit()
    flash("Чек-лист удалён.", "success")
    return redirect(url_for("checklists.index"))


# --- Критерии ------------------------------------------------------------

@checklists_bp.route("/<int:checklist_id>/criteria", methods=["POST"])
@admin_required
def add_criterion(checklist_id):
    checklist = db.session.get(Checklist, checklist_id) or abort(404)
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("У критерия должен быть заголовок.", "error")
        return redirect(url_for("checklists.edit", checklist_id=checklist_id))

    next_order = (max((c.order_index for c in checklist.criteria), default=-1)) + 1
    criterion = Criterion(
        checklist_id=checklist.id,
        title=title[:255],
        description=(request.form.get("description") or "").strip(),
        weight=_clamp(_parse_int(request.form.get("weight"), 0), 0, 100),
        order_index=next_order,
        is_critical=bool(request.form.get("is_critical")),
    )
    db.session.add(criterion)
    db.session.commit()
    flash("Критерий добавлен.", "success")
    return redirect(url_for("checklists.edit", checklist_id=checklist_id))


@checklists_bp.route("/criteria/<int:criterion_id>/edit", methods=["POST"])
@admin_required
def update_criterion(criterion_id):
    criterion = db.session.get(Criterion, criterion_id) or abort(404)
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("У критерия должен быть заголовок.", "error")
        return redirect(url_for("checklists.edit", checklist_id=criterion.checklist_id))

    criterion.title = title[:255]
    criterion.description = (request.form.get("description") or "").strip()
    criterion.weight = _clamp(_parse_int(request.form.get("weight"), 0), 0, 100)
    criterion.is_critical = bool(request.form.get("is_critical"))
    db.session.commit()
    flash("Критерий сохранён.", "success")
    return redirect(url_for("checklists.edit", checklist_id=criterion.checklist_id))


@checklists_bp.route("/criteria/<int:criterion_id>/delete", methods=["POST"])
@admin_required
def delete_criterion(criterion_id):
    criterion = db.session.get(Criterion, criterion_id) or abort(404)
    checklist_id = criterion.checklist_id
    db.session.delete(criterion)
    db.session.commit()
    flash("Критерий удалён.", "success")
    return redirect(url_for("checklists.edit", checklist_id=checklist_id))


# --- AI-генерация --------------------------------------------------------

@checklists_bp.route("/generate", methods=["GET", "POST"])
@admin_required
def generate():
    if request.method == "POST":
        description = (request.form.get("description") or "").strip()
        domain = (request.form.get("domain") or "").strip()
        if not description:
            flash("Опишите процесс продаж для генерации.", "error")
            return render_template("checklists/generate.html", form=request.form), 400

        try:
            from checklists.ai import generate_checklist_draft

            draft = generate_checklist_draft(description, domain)
        except RuntimeError as exc:
            # нет ключа/модели
            flash(f"AI недоступен: {exc}", "error")
            return render_template("checklists/generate.html", form=request.form), 400
        except Exception as exc:  # noqa: BLE001
            flash(f"Не удалось сгенерировать чек-лист: {exc}", "error")
            return render_template("checklists/generate.html", form=request.form), 400

        # создаём как черновик (неактивный) — админ проверит и активирует
        checklist = Checklist(
            name=draft["name"][:255],
            description=draft["description"],
            domain=domain[:255] or "Универсальный",
            zone_green_min=80,
            zone_yellow_min=60,
            is_active=False,
        )
        for order_index, item in enumerate(draft["criteria"]):
            checklist.criteria.append(
                Criterion(
                    title=item["title"],
                    description=item["description"],
                    weight=item["weight"],
                    order_index=order_index,
                    is_critical=item["is_critical"],
                )
            )
        db.session.add(checklist)
        db.session.commit()
        flash(
            "Черновик сгенерирован. Проверьте критерии и веса, при необходимости "
            "поправьте и активируйте.",
            "success",
        )
        return redirect(url_for("checklists.edit", checklist_id=checklist.id))

    return render_template("checklists/generate.html", form={})
