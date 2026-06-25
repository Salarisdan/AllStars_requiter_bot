import html
import os

import httpx
from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HR_CHAT_ID = os.getenv("HR_CHAT_ID", "").strip()
HR_TOPIC_APPLICATIONS_ID = os.getenv("HR_TOPIC_APPLICATIONS_ID", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "allstars_hr_bot").strip()
LANDING_TITLE = os.getenv("LANDING_TITLE", "AllStars Agency").strip()

ENGLISH_LEVELS = {
    "A1": "A1 - Начинающий",
    "A2": "A2 - Элементарный",
    "B1": "B1 - Средний",
    "B2": "B2 - Выше среднего",
    "C1C2": "C1/C2 - Продвинутый",
}

SHIFT_LABELS = {
    "00-06": "00:00 - 06:00",
    "06-12": "06:00 - 12:00",
    "12-18": "12:00 - 18:00",
    "18-00": "18:00 - 00:00",
}


def _clean(value: str | None, limit: int = 500) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        text = text[:limit].strip()
    return text


def _pick_label(value: str | None, mapping: dict[str, str], fallback: str = "-") -> str:
    key = (value or "").strip()
    if not key:
        return fallback
    return mapping.get(key, key)


def _pick_many(values: list[str] | str | None, mapping: dict[str, str], fallback: str = "-") -> str:
    if values is None:
        return fallback
    if isinstance(values, str):
        items = [part.strip() for part in values.split(",") if part.strip()]
    else:
        items = [part.strip() for part in values if part and part.strip()]
    if not items:
        return fallback
    return ", ".join(mapping.get(item, item) for item in items)


def _bool_label(value: str | None, yes_label: str = "✅ Да", no_label: str = "❌ Нет") -> str:
    text = (value or "").strip().lower()
    if text in {"yes", "true", "1", "да", "да, согласен(а)", "✅ да"}:
        return yes_label
    if not text:
        return "-"
    if text in {"no", "false", "0", "нет", "❌ нет"}:
        return no_label
    return value.strip()


def _build_message(payload: dict[str, str]) -> str:
    psych_block = "\n".join(
        [
            f"<b>1. Классный рабочий день:</b> {html.escape(payload.get('psych_feedback', '-'))}",
            f"<b>2. Мечта:</b> {html.escape(payload.get('psych_low_result', '-'))}",
            f"<b>3. Крутой тимлид:</b> {html.escape(payload.get('psych_kpi_fail', '-'))}",
            f"<b>4. Психология резерв:</b> {html.escape(payload.get('psych_boundaries', '-'))}",
            f"<b>5. Конфликт:</b> {html.escape(payload.get('psych_conflict', '-'))}",
            f"<b>6. Стресс-контроль:</b> {html.escape(payload.get('psych_stress_control', '-'))}",
        ]
    )

    work_block = "\n".join(
        [
            f"<b>Смены:</b> {html.escape(_pick_many(payload.get('shifts'), SHIFT_LABELS))}",
            f"<b>Стаж:</b> {html.escape(payload.get('experience', '-'))}",
            f"<b>Топ страниц:</b> {html.escape(payload.get('top_pages', '-'))}",
            f"<b>Конверсия:</b> {html.escape(payload.get('conversion', '-'))}",
            f"<b>Типаж моделей:</b> {html.escape(payload.get('model_types', '-'))}",
            f"<b>Платформы (опыт):</b> {html.escape(payload.get('worked_platforms', '-'))}",
            f"<b>Ср чек:</b> {html.escape(payload.get('avg_check', '-'))}",
            f"<b>Причина ухода:</b> {html.escape(payload.get('leave_reason', '-'))}",
            f"<b>Основная деятельность/учеба:</b> {html.escape(payload.get('main_activity', '-'))}",
            f"<b>Детали деятельности:</b> {html.escape(payload.get('activity_detail', '-'))}",
            f"<b>График:</b> {html.escape(payload.get('work_schedule', '-'))}",
            f"<b>Финансовые ожидания:</b> {html.escape(payload.get('financial_expectations', '-'))}",
            f"<b>Gmail:</b> {html.escape(payload.get('gmail', '-'))}",
        ]
    )

    lines = [
        "<b>Новая заявка AllStars</b>",
        "",
        f"<b>Скрининг:</b> {html.escape(payload.get('screening', '-'))}",
        f"<b>Автотег:</b> {html.escape(payload.get('auto_tag', '-'))}",
        f"<b>Верификация:</b> {html.escape(_bool_label(payload.get('verification')))}",
        f"<b>Имя:</b> {html.escape(payload.get('name', '-'))}",
        f"<b>Возраст:</b> {html.escape(payload.get('age', '-'))}",
        f"<b>Откуда узнали:</b> {html.escape(payload.get('source', '-'))}",
        f"<b>Английский:</b> {html.escape(_pick_label(payload.get('english'), ENGLISH_LEVELS))}",
        "",
        "<b>Психо-блок:</b>",
        psych_block,
        "",
        "<b>Рабочий блок:</b>",
        work_block,
        "",
        f"<b>Комментарий:</b> {html.escape(payload.get('notes', '-'))}",
        "",
        "<b>Источник:</b> сайт",
    ]
    return "\n".join(lines)


def _send_to_telegram(message: str) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    if not HR_CHAT_ID:
        raise RuntimeError("HR_CHAT_ID is not configured")

    payload: dict[str, object] = {
        "chat_id": int(HR_CHAT_ID) if HR_CHAT_ID.lstrip("-").isdigit() else HR_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if HR_TOPIC_APPLICATIONS_ID.lstrip("-").isdigit():
        payload["message_thread_id"] = int(HR_TOPIC_APPLICATIONS_ID)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    with httpx.Client(timeout=12.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")


@app.get("/")
def index():
    return render_template(
        "index.html",
        bot_username=BOT_USERNAME,
        landing_title=LANDING_TITLE,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/apply")
def apply():
    source = request.get_json(silent=True) or request.form
    shifts_value = source.get("shifts", "")
    if isinstance(shifts_value, list):
        shifts_text = ", ".join(shifts_value)
    else:
        shifts_text = _clean(shifts_value, 120)

    payload = {
        "source": _clean(source.get("source"), 200),
        "name": _clean(source.get("name"), 120),
        "age": _clean(source.get("age"), 40),
        "verification": _clean(source.get("verification"), 80),
        "english": _clean(source.get("english"), 20),
        "contact": _clean(source.get("contact"), 120),
        "shifts": shifts_text,
        "experience": _clean(source.get("experience"), 300),
        "top_pages": _clean(source.get("top_pages"), 200),
        "conversion": _clean(source.get("conversion"), 120),
        "model_types": _clean(source.get("model_types"), 200),
        "worked_platforms": _clean(source.get("worked_platforms"), 200),
        "leave_reason": _clean(source.get("leave_reason"), 300),
        "financial_expectations": _clean(source.get("financial_expectations"), 200),
        "gmail": _clean(source.get("gmail"), 120),
        "avg_check": _clean(source.get("avg_check"), 120),
        "main_activity": _clean(source.get("main_activity"), 160),
        "activity_detail": _clean(source.get("activity_detail"), 400),
        "work_schedule": _clean(source.get("work_schedule"), 120),
        "psych_feedback": _clean(source.get("psych_feedback"), 500),
        "psych_low_result": _clean(source.get("psych_low_result"), 500),
        "psych_kpi_fail": _clean(source.get("psych_kpi_fail"), 500),
        "psych_boundaries": _clean(source.get("psych_boundaries"), 500),
        "psych_conflict": _clean(source.get("psych_conflict"), 500),
        "psych_stress_control": _clean(source.get("psych_stress_control"), 500),
        "notes": _clean(source.get("notes"), 500),
        "screening": _clean(source.get("screening"), 60),
        "auto_tag": _clean(source.get("auto_tag"), 60),
    }

    if not payload["verification"]:
        return jsonify({"ok": False, "error": "Подтверди NDA и верификацию"}), 400

    required_fields = [
        "source", "name", "age", "english", "contact", "shifts",
        "experience", "top_pages", "conversion", "model_types", "worked_platforms",
        "leave_reason", "financial_expectations", "gmail", "avg_check", "main_activity",
        "work_schedule",
    ]
    missing_required = [field for field in required_fields if not payload.get(field)]
    if missing_required:
        return jsonify({"ok": False, "error": "Заполни все обязательные поля"}), 400

    try:
        _send_to_telegram(_build_message(payload))
    except Exception as exc:
        app.logger.exception("Failed to send landing page application")
        return jsonify({"ok": False, "error": f"Не удалось отправить заявку: {exc}"}), 502

    return jsonify({"ok": True, "message": "Заявка отправлена"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)