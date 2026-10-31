"""
Web app для WARLORD RP: статистика стран и группировок с веб-редактором.
Читает и пишет в ту же SQLite БД, что и бот.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from functools import wraps
from typing import Any

from flask import (
    Flask, render_template, request, jsonify, abort,
    redirect, url_for, session, flash,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.warlord.db import Database  # noqa: E402
from app.warlord.config import config  # noqa: E402

db: Database | None = None

# --- Аутентификация ---
ADMIN_PASSWORD_HINT = "WARLORD_ADMIN_PASSWORD"


def _run(coro):
    """Запустить корутину в sync-контексте Flask."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _country_to_dict(c: dict[str, Any]) -> dict[str, Any]:
    def _num(v: Any, default: float = 0.0) -> float:
        try:
            return float(v or default)
        except (TypeError, ValueError):
            return default

    def _int(v: Any, default: int = 0) -> int:
        try:
            return int(v or default)
        except (TypeError, ValueError):
            return default

    def _loads_json(v: Any) -> Any:
        if not v:
            return []
        if isinstance(v, (list, dict)):
            return v
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return []

    return {
        "id": c.get("id"),
        "name": c.get("name") or "",
        "status": c.get("status") or "active",
        "ruler": c.get("ruler") or "",
        "party": c.get("party") or "",
        "ideology": c.get("ideology") or "",
        "color": c.get("color") or "",
        "flag_file_id": c.get("flag_file_id") or "",
        "population": _int(c.get("population")),
        "population_capacity": _int(c.get("population_capacity")),
        "budget": _num(c.get("budget")),
        "income_daily": _num(c.get("income_daily")),
        "expenses_daily": _num(c.get("expenses_daily")),
        "army_infantry": _int(c.get("army_infantry")),
        "army_light_vehicles": _int(c.get("army_light_vehicles")),
        "army_heavy_wheeled": _int(c.get("army_heavy_wheeled")),
        "army_artillery": _int(c.get("army_artillery")),
        "army_drones": _int(c.get("army_drones")),
        "army_navy": _int(c.get("army_navy")),
        "tax_level": c.get("tax_level") or "Средний",
        "stability": _num(c.get("stability")),
        "war_support": _num(c.get("war_support")),
        "happiness": _num(c.get("happiness")),
        "resources_json": _loads_json(c.get("resources_json")),
        "settlements_json": _loads_json(c.get("settlements_json")),
        "industry_json": _loads_json(c.get("industry_json")),
        "research_json": _loads_json(c.get("research_json")),
        "card_message_id": c.get("card_message_id"),
        "updated_at": c.get("updated_at") or "",
        "last_econ_at": c.get("last_econ_at") or "",
        "registered_at": c.get("registered_at") or "",
        "aliases_json": _loads_json(c.get("aliases_json")),
    }


# ------------------------------------------------------------------ Auth helpers

def _require_auth(f):
    """Декоратор: требует авторизацию админа."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Секретный ключ для сессий (обязателен)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

    # БД инициализируется лениво при первом запросе
    _db_initialized = False

    def _ensure_db():
        nonlocal _db_initialized
        global db
        if not _db_initialized:
            db = Database(config.DB_PATH)
            _run(db.connect())
            _db_initialized = True

    @app.before_request
    def _init_db_on_request():
        _ensure_db()

    @app.teardown_appcontext
    def _close_db(exc):
        pass

    # ------------------------------------------------------------------ Auth routes

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            expected = os.getenv(ADMIN_PASSWORD_HINT, "admin")
            if password == expected:
                session["admin"] = True
                session.permanent = True
                next_url = request.args.get("next") or url_for("index")
                flash("Вход выполнен", "success")
                return redirect(next_url)
            flash("Неверный пароль", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("admin", None)
        flash("Вы вышли из системы", "info")
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ Pages

    @app.route("/")
    @_require_auth
    def index():
        countries = _run(db.list_countries()) if db else []
        search = request.args.get("q", "").strip().lower()
        status_filter = request.args.get("status", "").strip()

        items = [_country_to_dict(c) for c in countries]
        if search:
            items = [c for c in items if search in c["name"].lower()]
        if status_filter:
            items = [c for c in items if c["status"] == status_filter]

        sort = request.args.get("sort", "name")
        order = request.args.get("order", "asc")
        reverse = order == "desc"
        if sort == "population":
            items.sort(key=lambda c: c["population"], reverse=reverse)
        elif sort == "budget":
            items.sort(key=lambda c: c["budget"], reverse=reverse)
        elif sort == "army":
            items.sort(key=lambda c: c["army_infantry"], reverse=reverse)
        elif sort == "name":
            items.sort(key=lambda c: c["name"], reverse=reverse)
        else:
            items.sort(key=lambda c: c["name"], reverse=reverse)

        statuses = sorted({c["status"] for c in items})
        return render_template(
            "countries.html",
            countries=items,
            search=search,
            status_filter=status_filter,
            statuses=statuses,
            sort=sort,
            order=order,
        )

    @app.route("/country/<path:name>")
    @_require_auth
    def country_detail(name: str):
        country = _run(db.get_country(name)) if db else None
        if not country:
            abort(404)
        return render_template("country.html", country=_country_to_dict(country))

    @app.route("/country/<path:name>/edit", methods=["GET", "POST"])
    @_require_auth
    def country_edit(name: str):
        """Редактирование всех полей страны через HTML-форму."""
        country = _run(db.get_country(name)) if db else None
        if not country:
            abort(404)
        c = _country_to_dict(country)

        if request.method == "POST":
            field = request.form.get("field")
            value = request.form.get("value", "")

            try:
                if field in (
                    "population", "population_capacity", "budget",
                    "income_daily", "expenses_daily",
                    "army_infantry", "army_light_vehicles",
                    "army_heavy_wheeled", "army_artillery",
                    "army_drones", "army_navy",
                    "stability", "happiness", "war_support",
                ):
                    new_val = float(value)
                    if field in ("stability", "happiness", "war_support"):
                        new_val = min(100.0, max(0.0, new_val))
                    else:
                        new_val = max(0.0, new_val)
                    _run(db.set_country_value(c["id"], field, new_val))
                    _run(db.set_country_override(c["id"], field, new_val))
                    flash(f"{field} обновлён: {new_val}", "success")
                elif field in ("ruler", "party", "ideology", "color", "tax_level", "status"):
                    _run(db.set_country_value(c["id"], field, str(value)))
                    flash(f"{field} обновлён: {value}", "success")
                else:
                    flash(f"Поле {field} не поддерживается", "error")
            except (TypeError, ValueError) as exc:
                flash(f"Ошибка: {exc}", "error")

            return redirect(url_for("country_edit", name=name))

        # Поля для редактирования с описаниями
        editable_fields = [
            ("population", "Население", "number"),
            ("population_capacity", "Лимит населения", "number"),
            ("budget", "Казна (бюджет)", "number"),
            ("income_daily", "Доход в день", "number"),
            ("expenses_daily", "Расходы в день", "number"),
            ("army_infantry", "Пехота", "number"),
            ("army_light_vehicles", "Лёгкая техника", "number"),
            ("army_heavy_wheeled", "Тяжёлая техника", "number"),
            ("army_artillery", "Артиллерия", "number"),
            ("army_drones", "БПЛА", "number"),
            ("army_navy", "Флот", "number"),
            ("stability", "Стабильность (%)", "number"),
            ("happiness", "Счастье (%)", "number"),
            ("war_support", "Поддержка войны (%)", "number"),
            ("ruler", "Правитель", "text"),
            ("party", "Партия", "text"),
            ("ideology", "Идеология", "text"),
            ("color", "Цвет", "text"),
            ("tax_level", "Налоговый уровень", "text"),
            ("status", "Статус", "text"),
        ]

        return render_template(
            "country_edit.html",
            country=c,
            editable_fields=editable_fields,
        )

    # ------------------------------------------------------------------ API

    @app.route("/api/countries")
    @_require_auth
    def api_countries():
        countries = _run(db.list_countries()) if db else []
        return jsonify([_country_to_dict(c) for c in countries])

    @app.route("/api/country/<path:name>")
    @_require_auth
    def api_country(name: str):
        country = _run(db.get_country(name)) if db else None
        if not country:
            return jsonify({"error": "not found"}), 404
        return jsonify(_country_to_dict(country))

    @app.route("/api/stats")
    @_require_auth
    def api_stats():
        countries = _run(db.list_countries()) if db else []
        items = [_country_to_dict(c) for c in countries]
        total_pop = sum(c["population"] for c in items)
        total_budget = sum(c["budget"] for c in items)
        total_army = sum(c["army_infantry"] for c in items)
        return jsonify({
            "countries": len(items),
            "total_population": total_pop,
            "total_budget": total_budget,
            "total_army_infantry": total_army,
            "by_status": {
                s: len([c for c in items if c["status"] == s])
                for s in sorted({c["status"] for c in items})
            },
        })

    @app.route("/api/country/<path:name>/edit", methods=["POST"])
    @_require_auth
    def api_edit_country(name: str):
        data = request.get_json(force=True) or {}
        field = data.get("field")
        delta = data.get("delta", 0)
        if not field:
            return jsonify({"error": "field required"}), 400
        try:
            delta = float(delta)
        except (TypeError, ValueError):
            return jsonify({"error": "delta must be number"}), 400

        country = _run(db.get_country(name))
        if not country:
            return jsonify({"error": "country not found"}), 404

        try:
            current = float(country.get(field) or 0)
        except (TypeError, ValueError):
            return jsonify({"error": f"field {field} is not numeric"}), 400

        new = current + delta
        if field in ("stability", "happiness", "war_support"):
            new = min(100.0, max(0.0, new))
        else:
            new = max(0.0, new)

        _run(db.set_country_value(country["id"], field, new))
        _run(db.set_country_override(country["id"], field, new))

        return jsonify({
            "ok": True,
            "country": name,
            "field": field,
            "old": current,
            "new": new,
            "locked": True,
        })

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
