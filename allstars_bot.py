import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
import gspread
from gspread.exceptions import SpreadsheetNotFound
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  КОНФИГ
# ─────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "8326443265:AAFAC5HFM_Bubhqya0xImJAkdvwt3LQdyXI")
HR_CHAT_ID = 863939675
BOT_USERNAME     = os.getenv("BOT_USERNAME", "allstars_hr_bot")
BANNER_GDRIVE_ID = "1-15wE_zOrskUqb5sClN4hTS_Bi91AlwE"

# ── Открытые смены по платформам — меняй в Railway Variables ─
# Формат: коды через запятую.  Коды: 00-06 | 06-12 | 12-18 | 18-00
# Пример: OPEN_SHIFTS_ONLYFANS = "06-12"
#         OPEN_SHIFTS_FANSLY   = "12-18,18-00"
def _parse_shifts(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]

OPEN_SHIFTS_ONLYFANS = _parse_shifts(os.getenv("OPEN_SHIFTS_ONLYFANS", "12-18"))
OPEN_SHIFTS_FANSLY   = _parse_shifts(os.getenv("OPEN_SHIFTS_FANSLY",   "12-18"))


def get_open_shifts_for(platform: str) -> list[str]:
    """Возвращает список открытых смен для выбранной платформы."""
    if platform == "OnlyFans":
        return OPEN_SHIFTS_ONLYFANS
    elif platform == "Fansly":
        return OPEN_SHIFTS_FANSLY
    else:  # Обе платформы — объединение
        return list(set(OPEN_SHIFTS_ONLYFANS) | set(OPEN_SHIFTS_FANSLY))

# ── ID изображений для каждого раздела ──────────────────
SECTION_IMAGES = {
    "about":       "1N8EuEGEKR2uCLBeXUA_WQspnzKwfpdPw",
    "conditions":  "1yEg7By3nmVMu57QM46KRqjtKcZ9TQHIl",
    "nda":         "1aC3j4r16Dt9TbgaXKGgnJ3PbpeBsQtCR",
    "tools":       "1RBySWfDwIwIGcP3XCjxcxz3KoEgHT7dr",
    "training":    "1E6u46te5RCMRoruB7BEIXjePuawjzK08",
    "faq":         "1cYC1HRayaxfoqzdOlcw91TyTZMToJ9es",
    "form":        "1WMxw8uEs3cySYFTLsr7jsbcCaDU28QLn",
}

# Кэш TG file_id — после первой загрузки не обращаемся к Drive повторно
_tg_file_cache: dict[str, str] = {}  # key → tg file_id

def load_google_creds() -> dict:
    """Load service account JSON from env, handling escaped newlines/base64 payloads."""
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip()

    try:
        creds = json.loads(raw)
    except json.JSONDecodeError:
        # Some platforms store secrets as base64 to avoid escaping issues.
        decoded = base64.b64decode(raw).decode("utf-8")
        creds = json.loads(decoded)

    private_key = creds.get("private_key")
    if isinstance(private_key, str):
        creds["private_key"] = private_key.replace("\\n", "\n").strip()

    return creds


GOOGLE_CREDS = load_google_creds()
SPREADSHEET_NAME = os.environ.get("GOOGLE_SPREADSHEET_NAME", "AllStarsLeads")
SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
MAIN_WORKSHEET_TITLE = os.environ.get("GOOGLE_MAIN_WORKSHEET_NAME", "AllStarsLeads")
MAIN_HEADERS = [
    "Дата", "TG Username", "TG ID",
    "Источник", "Имя", "Возраст",
    "Английский", "Платформа", "Смены",
    "Стаж", "Топ страниц", "Конверсия", "Типаж моделей", "Платформы (опыт)", "Ср чек",
    "Основная деятельность/учеба", "График",
    "Финансовые ожидания", "Gmail", "Верификация",
    "Скрининг", "Автотег",
]

# ─────────────────────────────────────────────
#  СОСТОЯНИЯ ДИАЛОГА
# ─────────────────────────────────────────────
Q_DUPLICATE, Q1_SOURCE, Q2_NAME, Q3_AGE, Q5_ENGLISH, Q6_PLATFORM, Q7_SHIFT, Q8_EXPERIENCE, Q9_TOP_PAGES, Q10_CONVERSION, Q11_MODEL_TYPES, Q12_WORKED_PLATFORMS, Q13_FINANCIAL, Q14_GMAIL, Q15_AVG_CHECK, Q16_MAIN_ACTIVITY, Q17_SCHEDULE, Q18_VERIFICATION, Q_WAITLIST = range(19)

# ─────────────────────────────────────────────
#  GOOGLE SHEETS — кэшированный клиент
# ─────────────────────────────────────────────
_gs_client = None
_gs_sheet  = None
_gs_rejections = None  # Лист с отказами от верификации


def _extract_spreadsheet_id(value: str) -> str:
    """Extract spreadsheet id from plain id or full Google Sheets URL."""
    raw = (value or "").strip()
    if not raw:
        return ""
    marker = "/spreadsheets/d/"
    if marker in raw:
        tail = raw.split(marker, 1)[1]
        return tail.split("/", 1)[0].split("?", 1)[0]
    return raw


def open_spreadsheet(client: gspread.Client):
    """Open spreadsheet by explicit id first, then fallback to title/key in SPREADSHEET_NAME."""
    if SPREADSHEET_ID:
        return client.open_by_key(_extract_spreadsheet_id(SPREADSHEET_ID))

    name_or_id = SPREADSHEET_NAME.strip()
    possible_id = _extract_spreadsheet_id(name_or_id)

    if "/spreadsheets/d/" in name_or_id:
        return client.open_by_key(possible_id)

    try:
        return client.open(name_or_id)
    except SpreadsheetNotFound:
        # If title lookup failed, try treating the same value as spreadsheet id.
        if possible_id and possible_id != name_or_id:
            return client.open_by_key(possible_id)
        if possible_id and len(possible_id) >= 30 and " " not in possible_id:
            return client.open_by_key(possible_id)
        raise

def get_sheet():
    global _gs_client, _gs_sheet
    try:
        if _gs_sheet is not None:
            _gs_sheet.spreadsheet.fetch_sheet_metadata()
            return _gs_sheet
    except Exception as e:
        logger.error(f"Sheet health check failed: {e}")
        _gs_client = None
        _gs_sheet  = None

    try:
        logger.info("Connecting to Google Sheets...")
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds      = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
        _gs_client = gspread.authorize(creds)
        logger.info("Authorized successfully, opening spreadsheet...")
        spreadsheet = open_spreadsheet(_gs_client)
        try:
            _gs_sheet = spreadsheet.worksheet(MAIN_WORKSHEET_TITLE)
        except Exception:
            try:
                _gs_sheet = spreadsheet.sheet1
                logger.warning(
                    f"Worksheet '{MAIN_WORKSHEET_TITLE}' not found. Falling back to sheet1: '{_gs_sheet.title}'"
                )
            except Exception:
                _gs_sheet = spreadsheet.add_worksheet(
                    title=MAIN_WORKSHEET_TITLE,
                    rows=1000,
                    cols=max(len(MAIN_HEADERS), 12),
                )
        logger.info("Spreadsheet opened successfully!")

        if _gs_sheet.col_count < len(MAIN_HEADERS):
            try:
                _gs_sheet.add_cols(len(MAIN_HEADERS) - _gs_sheet.col_count)
            except Exception as col_err:
                logger.warning(
                    f"Could not extend worksheet columns: {type(col_err).__name__}: {col_err}"
                )

        if not _gs_sheet.row_values(1):
            _gs_sheet.append_row(MAIN_HEADERS)
        return _gs_sheet
    except SpreadsheetNotFound as e:
        sa_email = GOOGLE_CREDS.get("client_email", "unknown")
        logger.error(
            "Spreadsheet not found/access denied. "
            f"GOOGLE_SPREADSHEET_NAME='{SPREADSHEET_NAME}', GOOGLE_SPREADSHEET_ID='{SPREADSHEET_ID}', "
            f"service_account='{sa_email}'. "
            "Share the target sheet with this service account or set GOOGLE_SPREADSHEET_ID.",
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.error(f"Failed to connect: {type(e).__name__}: {e}", exc_info=True)
        raise


def get_main_worksheet():
    return get_sheet()


def parse_interview_datetime(date_str: str, time_str: str):
    """
    Преобразует:
    date_str = '23.03.2026'
    time_str = '16:30'
    в timezone-aware datetime Europe/Moscow
    """
    if not date_str or not time_str:
        return None

    date_str = str(date_str).strip()
    time_str = str(time_str).strip()

    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        return dt.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    except Exception:
        return None


def has_reminder_marker(comments: str, row_number: int) -> bool:
    s = str(comments or "")
    return "[REMINDER_SENT " in s


async def interview_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Каждые 5 минут:
    - читает AllStarsLeads
    - ищет статус 'Собеседование'
    - если до собеса <= 60 минут и > 0 минут
    - и ещё не отправляли reminder
    -> шлёт сообщение и ставит маркер в 'Комментарии'
    """
    try:
        ws = get_main_worksheet()
        rows = ws.get_all_values()

        if not rows or len(rows) < 2:
            return

        headers = rows[0]

        def col_idx(name: str):
          try:
              return headers.index(name)
          except ValueError:
              return -1

        idx_username = col_idx("Username")
        if idx_username == -1:
            idx_username = col_idx("TG Username")

        idx_user_id = col_idx("ID")
        if idx_user_id == -1:
            idx_user_id = col_idx("TG ID")

        idx_name = col_idx("Как вас зовут?")
        if idx_name == -1:
            idx_name = col_idx("Имя")

        idx_status = col_idx("Статус")
        idx_date = col_idx("Дата собеседования")
        idx_time = col_idx("Время собеседования")
        idx_comments = col_idx("Комментарии")

        if min(idx_user_id, idx_status, idx_date, idx_time, idx_comments) == -1:
            logger.error("Reminder job: required columns not found in AllStarsLeads")
            return

        now_msk = datetime.now(ZoneInfo("Europe/Moscow"))

        for row_number, row in enumerate(rows[1:], start=2):
            try:
                # защита от коротких строк
                def safe_get(idx):
                    return row[idx] if idx >= 0 and idx < len(row) else ""

                telegram_user_id = str(safe_get(idx_user_id)).strip()
                candidate_name = str(safe_get(idx_name)).strip()
                status = str(safe_get(idx_status)).strip()
                interview_date = str(safe_get(idx_date)).strip()
                interview_time = str(safe_get(idx_time)).strip()
                comments = str(safe_get(idx_comments)).strip()

                if not telegram_user_id:
                    continue

                if status != "Собеседование":
                    continue

                if not interview_date or not interview_time:
                    continue

                if has_reminder_marker(comments, row_number):
                    continue

                interview_dt = parse_interview_datetime(interview_date, interview_time)
                if not interview_dt:
                    continue

                diff = interview_dt - now_msk
                minutes_left = diff.total_seconds() / 60

                # Напоминаем за 60 минут до начала, но не после старта
                if not (0 <= minutes_left <= 60):
                    continue

                text = (
                    f"Привет! 🙌\n\n"
                    f"Напоминаем, что у вас сегодня собеседование с Allstars\n"
                    f"в {interview_time} по мск.\n\n"
                    f"Будем ждать 😊"
                )

                await context.bot.send_message(
                    chat_id=int(telegram_user_id),
                    text=text
                )

                marker = f"[REMINDER_SENT {now_msk.strftime('%Y-%m-%d %H:%M')}]"
                new_comments = f"{comments}\n{marker}".strip() if comments else marker

                # Комментарии у тебя в колонке Q
                ws.update_acell(f"Q{row_number}", new_comments)

                logger.info(
                    f"Reminder sent to user_id={telegram_user_id}, row={row_number}, "
                    f"candidate={candidate_name}, interview={interview_date} {interview_time}"
                )

            except Exception as row_err:
                logger.error(f"Reminder row error #{row_number}: {type(row_err).__name__}: {row_err}", exc_info=True)

    except Exception as e:
        logger.error(f"Reminder job failed: {type(e).__name__}: {e}", exc_info=True)


def get_rejections_sheet():
    """Возвращает лист 'Отказы', создаёт его если не существует."""
    global _gs_rejections, _gs_client
    try:
        if _gs_rejections is not None:
            return _gs_rejections
        # Убеждаемся что основной клиент подключён
        get_sheet()
        spreadsheet = _gs_client.open(SPREADSHEET_NAME)
        # Ищем лист "Отказы"
        try:
            _gs_rejections = spreadsheet.worksheet("Отказы")
        except Exception:
            # Создаём новый лист
            _gs_rejections = spreadsheet.add_worksheet(title="Отказы", rows=1000, cols=6)
            _gs_rejections.append_row(["Дата", "TG Username", "TG ID", "Имя", "Возраст", "Источник"])
        return _gs_rejections
    except Exception as e:
        logger.error(f"Rejections sheet error: {type(e).__name__}: {e}", exc_info=True)
        raise


def save_to_sheet(data: dict) -> bool:
    global _gs_client, _gs_sheet
    row = [
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        data.get("username", ""), data.get("user_id", ""),
        data.get("source", ""),   data.get("name", ""),
        data.get("age", ""),      data.get("english", ""),
        data.get("platform", ""), data.get("shifts", ""),
        data.get("experience", ""), data.get("top_pages", ""), data.get("conversion", ""),
        data.get("model_types", ""), data.get("worked_platforms", ""),
        data.get("avg_check", ""),
        data.get("main_activity", ""),
        data.get("work_schedule", ""),
        data.get("financial_expectations", ""),
        data.get("email", ""),
        data.get("verification", ""),
        data.get("screening", ""),
        data.get("auto_tag", ""),
    ]

    for attempt in (1, 2):
        try:
            logger.info(f"Saving to Google Sheets (attempt {attempt})...")
            sheet = get_sheet()
            if sheet.col_count < len(row):
                try:
                    sheet.add_cols(len(row) - sheet.col_count)
                except Exception as col_err:
                    logger.warning(
                        f"Could not extend worksheet columns before append: {type(col_err).__name__}: {col_err}"
                    )

            update_row = data.get("update_row_number")
            if update_row:
                start_col = "A"
                end_col = chr(ord("A") + len(row) - 1)
                sheet.update(f"{start_col}{update_row}:{end_col}{update_row}", [row])
                logger.info(f"Updated existing candidate row #{update_row}")
            else:
                sheet.append_row(row)
            logger.info("Saved to Google Sheets successfully!")
            return True
        except Exception as e:
            logger.error(f"Sheets error (attempt {attempt}): {type(e).__name__}: {e}", exc_info=True)
            _gs_client = None
            _gs_sheet = None

    return False


def save_rejection(data: dict) -> bool:
    """Сохраняет отказ от верификации в отдельный лист."""
    try:
        sheet = get_rejections_sheet()
        sheet.append_row([
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            data.get("username", ""),
            data.get("user_id", ""),
            data.get("name", ""),
            data.get("age", ""),
            data.get("source", ""),
        ])
        logger.info("Rejection saved to sheet.")
        return True
    except Exception as e:
        logger.error(f"Rejection save error: {type(e).__name__}: {e}", exc_info=True)
        return False


def get_waitlist_sheet():
    """Возвращает лист 'Ожидание', создаёт если не существует."""
    global _gs_client
    try:
        get_sheet()
        spreadsheet = _gs_client.open(SPREADSHEET_NAME)
        try:
            return spreadsheet.worksheet("Ожидание")
        except Exception:
            sheet = spreadsheet.add_worksheet(title="Ожидание", rows=1000, cols=20)
            sheet.append_row([
                "Дата", "TG Username", "TG ID",
                "Откуда узнали", "Имя", "Возраст",
                "Английский", "Платформа", "Смена",
                "Стаж", "Топ страниц", "Конверсия", "Типаж моделей", "Платформы (опыт)", "Ср чек",
                "Основная деятельность/учеба", "График",
                "Финансовые ожидания", "Gmail", "Верификация", "Скрининг", "Автотег",
            ])
            return sheet
    except Exception as e:
        logger.error(f"Waitlist sheet error: {type(e).__name__}: {e}", exc_info=True)
        raise


def save_waitlist(data: dict) -> bool:
    """Сохраняет кандидата в лист ожидания — полная анкета."""
    try:
        sheet = get_waitlist_sheet()
        row = [
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            data.get("username", ""),
            data.get("user_id", ""),
            data.get("source", ""),
            data.get("name", ""),
            data.get("age", ""),
            data.get("english", ""),
            data.get("platform", ""),
            data.get("shifts", ""),
            data.get("experience", ""),
            data.get("top_pages", ""),
            data.get("conversion", ""),
            data.get("model_types", ""),
            data.get("worked_platforms", ""),
            data.get("avg_check", ""),
            data.get("main_activity", ""),
            data.get("work_schedule", ""),
            data.get("financial_expectations", ""),
            data.get("email", ""),
            data.get("verification", ""),
            data.get("screening", ""),
            data.get("auto_tag", ""),
        ]

        if sheet.col_count < len(row):
            try:
                sheet.add_cols(len(row) - sheet.col_count)
            except Exception as col_err:
                logger.warning(
                    f"Could not extend waitlist worksheet columns before append: {type(col_err).__name__}: {col_err}"
                )

        sheet.append_row(row)
        logger.info("Waitlist entry saved.")
        return True
    except Exception as e:
        logger.error(f"Waitlist save error: {type(e).__name__}: {e}", exc_info=True)
        return False


def source_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👥 От друга/реферала"), KeyboardButton("📢 Telegram-канал")],
            [KeyboardButton("🎯 Реклама"), KeyboardButton("✍️ Другое")],
            [KeyboardButton("❌ Отменить заполнение")],
        ],
        resize_keyboard=True,
    )


def main_activity_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Да, работа"), KeyboardButton("Да, учеба")],
            [KeyboardButton("И работа, и учеба"), KeyboardButton("Нет")],
            [KeyboardButton("❌ Отменить заполнение")],
        ],
        resize_keyboard=True,
    )


def estimate_minutes_left(step: int, total: int) -> int:
    remaining_steps = max(total - step, 0)
    return max(1, min(4, (remaining_steps + 3) // 4))


def question_header(step: int, total: int = 17) -> str:
    return f"{progress(step, total)}\n⏱ Осталось ~{estimate_minutes_left(step, total)} минуты\n\n"


def _reminder_job_name(user_id: int, suffix: str) -> str:
    return f"form_reminder:{user_id}:{suffix}"


def cancel_form_reminders(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not context.job_queue:
        return
    for suffix in ("2h", "24h"):
        for job in context.job_queue.get_jobs_by_name(_reminder_job_name(user_id, suffix)):
            job.schedule_removal()


def schedule_form_reminders(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    if not context.job_queue:
        return
    cancel_form_reminders(context, user_id)

    context.job_queue.run_once(
        form_reminder_2h_job,
        when=timedelta(hours=2),
        data={"chat_id": chat_id, "user_id": user_id},
        name=_reminder_job_name(user_id, "2h"),
    )
    context.job_queue.run_once(
        form_reminder_24h_job,
        when=timedelta(hours=24),
        data={"chat_id": chat_id, "user_id": user_id},
        name=_reminder_job_name(user_id, "24h"),
    )


def set_form_step(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, step: str):
    context.user_data["current_step"] = step
    context.user_data["form_active"] = True
    schedule_form_reminders(context, user_id, chat_id)


def track_dropoff(context: ContextTypes.DEFAULT_TYPE, reason: str):
    stats = context.application.bot_data.setdefault("dropoff_stats", {})
    stats[reason] = stats.get(reason, 0) + 1
    logger.info(f"Dropoff tracked: {reason}; stats={stats}")


async def form_reminder_2h_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    if not chat_id or not user_id:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Привет! Напоминаем про анкету в Allstars 🙌\n\n"
            "Ты остановился(ась) на середине заполнения."
            " Вернуться можно в любой момент через кнопку «📝 Заполнить анкету»."
        ),
        reply_markup=main_keyboard(),
    )


async def form_reminder_24h_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    if not chat_id or not user_id:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Всё ещё ждем твою анкету 💫\n\n"
            "Если актуально, просто нажми «📝 Заполнить анкету» и продолжим."
        ),
        reply_markup=main_keyboard(),
    )


def find_existing_application_row(user_id: int) -> int | None:
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()
        if not rows:
            return None

        headers = rows[0]
        idx_tg_id = headers.index("TG ID") if "TG ID" in headers else -1
        if idx_tg_id == -1:
            return None

        for row_num, row in enumerate(rows[1:], start=2):
            if idx_tg_id < len(row) and str(row[idx_tg_id]).strip() == str(user_id):
                return row_num
    except Exception as e:
        logger.error(f"Duplicate check failed: {type(e).__name__}: {e}")
    return None


def is_valid_gmail(email: str) -> bool:
    email = (email or "").strip().lower()
    return bool(re.fullmatch(r"[a-z0-9._%+-]+@gmail\.com", email))


def _parse_number(text: str) -> float | None:
    m = re.search(r"(\d+(?:[\.,]\d+)?)", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def score_candidate(data: dict) -> tuple[str, str]:
    score = 0
    eng_map = {"A1": 0, "A2": 1, "B1": 2, "B2": 3, "C1C2": 4}
    eng = data.get("english", "")
    score += eng_map.get(eng, 0)

    exp_num = _parse_number(data.get("experience", "") or "") or 0
    score += 3 if exp_num >= 6 else 2 if exp_num >= 3 else 1 if exp_num > 0 else 0

    platform = data.get("platform", "")
    score += 2 if platform == "Обе платформы" else 1 if platform else 0

    shifts = [s.strip() for s in str(data.get("shifts", "")).split(",") if s.strip()]
    score += 2 if len(shifts) >= 2 else 1 if len(shifts) == 1 else 0

    fin = _parse_number(data.get("financial_expectations", "") or "")
    if fin is not None:
        score += 2 if fin <= 2000 else 1

    if score >= 10:
        screening = "A"
    elif score >= 6:
        screening = "B"
    else:
        screening = "C"

    if screening == "A":
        tag = "Strong"
    elif screening == "C" and exp_num == 0:
        tag = "Junior"
    elif screening == "B":
        tag = "New"
    else:
        tag = "Needs Review"

    return screening, tag


# ─────────────────────────────────────────────
#  ТЕКСТЫ
# ─────────────────────────────────────────────

WELCOME_TEXT = """\
✦ ── ✦ ── ✦  *ALLSTARS AGENCY*  ✦ ── ✦ ── ✦

Мы — профессиональное агентство по работе с моделями на OnlyFans и Fansly.

_3 года на рынке · 16 активных моделей · Реальный карьерный рост_

Выбери раздел, который тебя интересует 👇\
"""

# ── ОБ АГЕНТСТВЕ ──
ABOUT_MENU_TEXT = "🏆 *ЧТО ТАКОЕ ALLSTARS?*\n\nВыбери тему, чтобы узнать подробнее:"

ABOUT_AGENCY_TEXT = """\
╔══════════════════════════════╗
║      🏆  О АГЕНТСТВЕ        ║
╚══════════════════════════════╝

*Allstars* — агентство полного цикла по ведению моделей на платформах OnlyFans и Fansly.

📅 *На рынке:* 3 года
🔧 *Формат:* полное ведение — от стратегии до продаж

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Мы не стартап и не одиночная команда.

У нас выстроенная система:
├ 👤 Операторы (чаттеры)
├ ⭐ Старшие операторы
├ 👑 Тимлиды
├ 🎓 Менторы
└ 🎬 Контент-менеджеры

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 *Мы работаем по цифрам:*
├ Средний чек
├ Конверсия в продажу
├ % открытия PPV
└ Рост страниц

_Оператор у нас — это не просто человек, который отвечает в чате, а часть системы продаж._\
"""

ABOUT_MODELS_TEXT = """\
╔══════════════════════════════╗
║    📊  НАШИ МОДЕЛИ          ║
╚══════════════════════════════╝

Сейчас в агентстве:

🔵 *Fansly* — 9 моделей
🟠 *OnlyFans* — 7 моделей

_Модели разного грейда (топа страниц)._

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
❓ *Что такое топ страницы?*

Топ — рейтинг модели среди всех страниц платформы.
⚠️ Важно: *чем меньше цифра — тем популярнее страница.*

┌─────────────────────────────┐
│ 🎓 Топ ~10–20%             │
│   Страница обучения         │
│   Оборот: 2–2,5 тыс. $ /мес│
│   Нет фикс. срока, рост     │
│   возможен уже через неск.  │
│   смен при хорошем результате│
├─────────────────────────────┤
│ ⭐ Топ 1–5%                │
│   Хорошие рабочие страницы  │
│   Стабильный доход          │
├─────────────────────────────┤
│ 💎 Топ 0.5%                │
│   Максимальный уровень      │
│   Оборот: ~60 000 $ /мес   │
└─────────────────────────────┘

✅ В агентстве есть *реальный карьерный рост* — от обучающей страницы до топ-0.5%.\
"""

# ── ИНСТРУМЕНТЫ ──
TOOLS_MENU_TEXT = "🛠 *ИНСТРУМЕНТЫ И ЭКОСИСТЕМА*\n\nВыбери, что хочешь узнать:"

TOOLS_ONLYMONSTER_TEXT = """\
╔══════════════════════════════╗
║  🌐  ONLYMONSTER            ║
╚══════════════════════════════╝

*OnlyMonster* — основной рабочий инструмент оператора.

Вся работа с фанами происходит через этот браузер:

├ 💬 Переписка с фанами
├ 💸 Отправка платных сообщений
├ 🎁 Продажа кастомов
└ 📤 Рассылка контента

_Ничего не нужно устанавливать — работаешь через браузер._\
"""

TOOLS_CRM_TEXT = """\
╔══════════════════════════════╗
║  ⚙️  CRM ALLSTARS           ║
╚══════════════════════════════╝

Помимо OnlyMonster у нас есть *собственная CRM* — единая экосистема агентства.

В CRM ты:
├ ▶️ Начинаешь и заканчиваешь смену
├ 📝 Пишешь отчёты
├ 💰 Видишь свои балансы и продажи
├ ✅ Подтверждаешь сделанные продажи
└ 🎬 Запрашиваешь кастомы у модели

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 *Автоматизация:*

CRM автоматически подтягивает продажи из OnlyMonster.

_Тебе не нужно ничего считать вручную — всё фиксируется само._\
"""

TOOLS_AI_TEXT = """\
╔══════════════════════════════╗
║  🤖  AI-ТЕХНОЛОГИИ          ║
╚══════════════════════════════╝

Одна из сильных сторон Allstars — активное использование AI.

Это сделано *не для замены людей*, а чтобы оператор зарабатывал быстрее и больше.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔧 *Что реализовано:*

🎙 *Голосовые сообщения*
└ Генерация голоса модели прямо в CRM

🖼 *Изображения и открытки*
└ Визуальный контент под любой запрос фана

🎥 *Липсинг-видео*
└ Библиотека видео, где модель говорит имя фана и фразу

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 *Как это работает на практике:*

Ты меняешь имя фана → получаешь ощущение личного обращения за секунды.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ *Правила:*

Для VIP-фанов и постоянных платников мы запрашиваем реальный контент у модели.
_Связь с моделью ведётся через Telegram-чат._\
"""

TOOLS_CONTENT_TEXT = """\
╔══════════════════════════════╗
║  🎬  РАБОТА С КОНТЕНТОМ     ║
╚══════════════════════════════╝

Тебе не нужно думать, откуда брать контент.

*Что уже есть:*
├ ✅ Основной контент загружен
└ 🔄 Контент-менеджеры регулярно догружают новый

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*Через Telegram-чат с моделью можно:*

├ 👙 Уточнить наличие белья / образа
├ 🎁 Запросить кастом через CRM
└ Уточнить детали кастома, если они не указаны в CRM или инфо о модели

_Контент-менеджеры следят за наполнением страниц, чтобы у тебя всегда было что продавать._\
"""

# ── ОБУЧЕНИЕ ──
TRAINING_TEXT = """\
╔══════════════════════════════╗
║  🎓  ОБУЧЕНИЕ И АДАПТАЦИЯ   ║
╚══════════════════════════════╝

У нас нет классических лекций.
Формат — *сразу в практику*, но не в одиночку.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*Этап 1 — Гайд* 📖
Подробный Notion-документ.
Содержит: воронку продаж, скрипты, сексинг, кастомы, интерфейс платформ.
_Задача — внимательно изучить, не просто пролистать._

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*Этап 2 — Тест* 📝
Google-форма · 20 вопросов (10 закрытых + 10 открытых).
Проверяет не заучивание, а мышление.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*Этап 3 — Тест-смена* 🔍
├ 2–2,5 часа со старшим оператором
├ Показ CRM, страницы модели, логики продаж
└ Все продажи засчитываются

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*Этап 4 — Первые смены* 🚀
├ 2-я — самостоятельно, но с поддержкой
├ 3-я — разбор с Team Lead / старшим
├ 4-я — самостоятельная
└ 5-я — финальный разбор и решение

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💬 *Мы не бросаем людей.*
Всегда можно задать вопрос — коллегам, старшим или Team Lead.\
"""

# ── УСЛОВИЯ ──
CONDITIONS_TEXT = """\
╔══════════════════════════════╗
║   💰  УСЛОВИЯ РАБОТЫ        ║
╚══════════════════════════════╝

💵 *Ставка:* 20% от тотала + 2% за выполнение плана
📅 *Выплаты:* каждый вторник
🔐 *Формат:* криптокошелёк
🌐 *Английский:* обязателен от уровня A2

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔥 *БОНУСНАЯ СИСТЕМА*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

👥 *За реферала:*
├ +100$ за каждого, кто отработает месяц
└ +200$, если реферал сделает 3k+ за месяц

🎯 *Бонусы за кастомы в неделю:*
├ 3 кастома → +25$
├ 5 кастомов → +35$
└ 7+ кастомов → +45$

📊 Личный недельный план с доп. мотивацией
📨 Платные рассылки & продажи архива

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 *КАРЬЕРНЫЙ РОСТ*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎓 Ментор — обучает новичков
⭐ Старший оператор — проверяет диалоги
👑 Тимлид — управляет командой

_Если есть амбиции — у нас есть куда расти!_\
"""

# ── NDA ──
NDA_TEXT = """\
╔══════════════════════════════╗
║   📋  ВХОД В КОМАНДУ        ║
╚══════════════════════════════╝

*До тест-смены:*
1️⃣ Созвон с HR — знакомство и ответы на вопросы
2️⃣ Изучение гайда + тест (20 вопросов)
3️⃣ Тест-смена — 2–2,5 ч. со старшим оператором

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*После успешной тест-смены:*

🪪 *Верификация личности:*
├ Фото/скан документа (паспорт / ID / права / ВНЖ)
├ Данные не замазываются
└ Короткое видео с документом в руках

📝 *Подписание NDA:*
└ Только номер документа и адрес

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ *Важно:* верификация — ТОЛЬКО после тест-смены.
Стандартная процедура для безопасности всей команды.\
"""

# ── FAQ ──
FAQ_TEXT = """\
╔══════════════════════════════╗
║      ❓  FAQ                ║
╚══════════════════════════════╝

▸ *Нужен ли опыт?*
Нет, обучаем с нуля. Главное — желание учиться.

▸ *Минимальный возраст?*
18 лет — строго.

▸ *Сколько можно зарабатывать?*
От 500$ на старте, стремишься к 2k+.
Потолка нет — всё зависит от тебя.

▸ *Можно работать из любой страны?*
Да, работаем полностью удалённо.

▸ *Как часто выплаты?*
Каждый вторник на криптокошелёк.

▸ *Что такое кастом?*
Персональный контент для конкретного фана — один из главных источников дохода оператора.

▸ *Нужно ли знать английский?*
Да, обязательно от уровня A2. Чем выше уровень — тем более топовые страницы доступны.

▸ *Что если не пройду тест-смену?*
Разбираем ситуацию индивидуально. Мы не бросаем.\
"""

# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📝 Заполнить анкету")],
            [KeyboardButton("📘 Гайд агентства Allstars")],
            [KeyboardButton("🏢 Об агентстве"),     KeyboardButton("🛠 Инструменты")],
            [KeyboardButton("💰 Условия работы"),   KeyboardButton("🎓 Обучение")],
            [KeyboardButton("📋 NDA и верификация"), KeyboardButton("❓ FAQ")],
            [KeyboardButton("👥 Поделиться с другом")],
        ],
        resize_keyboard=True,
    )

def about_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 О компании",          callback_data="about_agency")],
        [InlineKeyboardButton("📊 Наши модели и топы",  callback_data="about_models")],
    ])

def tools_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 OnlyMonster",          callback_data="tool_onlymonster")],
        [InlineKeyboardButton("⚙️ CRM Allstars",         callback_data="tool_crm")],
        [InlineKeyboardButton("🤖 AI-технологии",        callback_data="tool_ai")],
        [InlineKeyboardButton("🎬 Работа с контентом",   callback_data="tool_content")],
    ])

def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отменить заполнение")]],
        resize_keyboard=True,
    )

def english_keyboard():
    levels = [
        ("🔰 A1 — Начинающий",      "A1"),
        ("📗 A2 — Элементарный",     "A2"),
        ("📘 B1 — Средний",          "B1"),
        ("📙 B2 — Выше среднего",    "B2"),
        ("🏆 C1/C2 — Продвинутый",  "C1C2"),
    ]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"eng_{c}")] for t, c in levels]
    )

def platform_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 OnlyFans",      callback_data="plat_onlyfans")],
        [InlineKeyboardButton("⚡ Fansly",         callback_data="plat_fansly")],
        [InlineKeyboardButton("💎 Обе платформы", callback_data="plat_both")],
    ])

def shift_keyboard(selected=None, open_shifts=None):
    selected    = selected    or []
    open_shifts = open_shifts or []
    shifts = [
        ("🌙 00:00 – 06:00", "00-06"),
        ("🌅 06:00 – 12:00", "06-12"),
        ("☀️ 12:00 – 18:00", "12-18"),
        ("🌆 18:00 – 00:00", "18-00"),
    ]
    kb = []
    for label, code in shifts:
        is_open     = code in open_shifts
        is_selected = code in selected
        if is_open:
            prefix = "✅ " if is_selected else "🟢 "
            suffix = " — НАБОР" if not is_selected else ""
        else:
            prefix = "✅ " if is_selected else "☐ "
            suffix = ""
        kb.append([InlineKeyboardButton(
            f"{prefix}{label}{suffix}",
            callback_data=f"shift_{code}",
        )])
    kb.append([InlineKeyboardButton("✔️ Подтвердить выбор", callback_data="shift_done")])
    return InlineKeyboardMarkup(kb)

def verification_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Показать подробнее", callback_data="nda_more")],
        [InlineKeyboardButton("✅ Да, согласен(а)", callback_data="verif_yes")],
        [InlineKeyboardButton("❌ Нет, не готов(а)", callback_data="verif_no")],
    ])


def schedule_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("5/2"), KeyboardButton("6/1")],
            [KeyboardButton("❌ Отменить заполнение")],
        ],
        resize_keyboard=True,
    )

# ─────────────────────────────────────────────
#  ПРОГРЕСС-БАР
# ─────────────────────────────────────────────
def progress(step: int, total: int = 17) -> str:
    if step == total:
        return "🏆" * total + f"  {step}/{total}"
    filled = "🟩" * step
    empty  = "⬜" * (total - step)
    return f"{filled}{empty}  {step}/{total}"

# ─────────────────────────────────────────────
#  TYPING INDICATOR — имитация живого ответа
# ─────────────────────────────────────────────
async def typing(update: Update, delay: float = 1.2):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(delay)

# ─────────────────────────────────────────────
#  ИЗОБРАЖЕНИЯ — универсальная отправка с кэшом
# ─────────────────────────────────────────────
async def send_section_photo(
    update: Update,
    gdrive_id: str,
    cache_key: str,
    caption: str,
    reply_markup=None,
) -> bool:
    """
    Отправляет фото раздела с подписью.
    Первый раз — скачивает с Google Drive, потом использует TG file_id из кэша.
    Возвращает True при успехе.
    """
    global _tg_file_cache
    try:
        # Показываем upload_photo пока грузится
        await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)

        tg_id = _tg_file_cache.get(cache_key)
        if tg_id:
            photo_src = tg_id
        else:
            photo_src = f"https://drive.google.com/uc?export=download&id={gdrive_id}"

        msg = await update.effective_chat.send_photo(
            photo=photo_src,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        # Кэшируем TG file_id
        if cache_key not in _tg_file_cache:
            _tg_file_cache[cache_key] = msg.photo[-1].file_id

        return True
    except Exception as e:
        logger.error(f"Photo send error [{cache_key}]: {e}")
        return False


async def send_banner(update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str = None):
    """Welcome-баннер при /start."""
    await send_section_photo(
        update,
        gdrive_id=BANNER_GDRIVE_ID,
        cache_key="welcome",
        caption=caption,
    )

# ─────────────────────────────────────────────
#  УВЕДОМЛЕНИЕ HR
# ─────────────────────────────────────────────
async def notify_hr(context: ContextTypes.DEFAULT_TYPE, data: dict):
    if not HR_CHAT_ID:
        logger.warning("HR_CHAT_ID не задан — уведомление не отправлено.")
        return

    def e(val):
        """Экранирует спецсимволы Markdown в пользовательских данных."""
        return str(val).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    text = (
        "🔔 *Новая заявка AllStars!*\n\n"
        f"*Скрининг:* {e(data.get('screening', '—'))}\n"
        f"*Автотег:* {e(data.get('auto_tag', '—'))}\n\n"
        f"*Платформа:* {e(data.get('platform', '—'))}\n"
        f"*Имя:* {e(data.get('name', '—'))}\n"
        f"*Тг:* @{e(data.get('username', '—'))} ({e(data.get('user_id', '—'))})\n"
        f"*Возраст:* {e(data.get('age', '—'))}\n"
        f"*Стаж:* {e(data.get('experience', '—'))}\n"
        f"*Топ страниц:* {e(data.get('top_pages', '—'))}\n"
        f"*Конверсия:* {e(data.get('conversion', '—'))}\n"
        f"*Типаж моделей:* {e(data.get('model_types', '—'))}\n"
        f"*Платформы (опыт):* {e(data.get('worked_platforms', '—'))}\n"
        f"*Ср чек:* {e(data.get('avg_check', '—'))}\n"
        f"*Смена:* {e(data.get('shifts', '—'))}\n"
        f"*Откуда вы о нас узнали:* {e(data.get('source', '—'))}\n"
        f"*Есть ли основная деятельность/учеба:* {e(data.get('main_activity', '—'))}\n\n"
        f"*График 5/2 или 6/1:* {e(data.get('work_schedule', '—'))}\n\n"
        f"*Финансовые ожидания:* {e(data.get('financial_expectations', '—'))}\n\n"
        f"*Mail:* {e(data.get('email', '—'))}"
    )
    try:
        await context.bot.send_message(chat_id=HR_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"HR notify error: {e}")

# ─────────────────────────────────────────────
#  ОСНОВНЫЕ HANDLERS
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    # ── Онбординг: цепочка сообщений с паузами ──
    await update.message.reply_text("Привет! 👋")
    await asyncio.sleep(0.8)

    await typing(update, delay=1.4)
    await update.message.reply_text(
        "Я бот агентства *Allstars* — помогу тебе узнать всё о работе у нас и отправить заявку.",
        parse_mode="Markdown",
    )
    await asyncio.sleep(0.6)

    await typing(update, delay=1.0)
    await update.message.reply_text(
        "Мы работаем на рынке *3 года*, ведём *16 моделей* на OnlyFans и Fansly.\n"
        "Здесь ты найдёшь всю информацию — от условий до инструментов. 🚀",
        parse_mode="Markdown",
    )
    await asyncio.sleep(0.5)

    # ── Баннер с подписью ──
    await send_banner(
        update, context,
        caption="✦ ── ✦ ── ✦  *ALLSTARS AGENCY*  ✦ ── ✦ ── ✦\n\n_Выбери раздел, который тебя интересует_ 👇",
    )

    # ── Главное меню с кнопкой «Поделиться» ──
    await update.message.reply_text(
        "Используй меню ниже 👇",
        reply_markup=main_keyboard(),
    )


async def start_form_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q1_SOURCE")
    await send_section_photo(
        update,
        gdrive_id=SECTION_IMAGES["form"],
        cache_key="form",
        caption=(
            "📋 *Анкета Allstars*\n\n"
            "17 вопросов · ~4 минуты\n\n"
            "_Нажми кнопку ниже, чтобы начать 👇_"
        ),
    )
    await asyncio.sleep(0.4)
    await update.effective_chat.send_message(
        text=(
            f"{question_header(0, 17)}"
            "*Вопрос 1 из 17:*\n"
            "Откуда вы о нас узнали?"
        ),
        parse_mode="Markdown",
        reply_markup=source_keyboard(),
    )
    return Q1_SOURCE


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📝 Заполнить анкету":
        existing_row = find_existing_application_row(update.effective_user.id)
        if existing_row:
            context.user_data["duplicate_row"] = existing_row
            await update.message.reply_text(
                "Мы уже видим вашу предыдущую заявку. Обновить ее новыми данными?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♻️ Да, обновить", callback_data="dup_update_yes")],
                    [InlineKeyboardButton("🆕 Нет, создать новую", callback_data="dup_update_no")],
                ]),
            )
            return Q_DUPLICATE

        return await start_form_flow(update, context)

    elif text == "🏢 Об агентстве":
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["about"],
            cache_key="about",
            caption=(
                "🏢 *Об агентстве*\n\n"
                "_3 года на рынке · 16 моделей · Системный подход_\n\n"
                "Выбери тему 👇"
            ),
            reply_markup=about_inline_keyboard(),
        )

    elif text == "🛠 Инструменты":
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["tools"],
            cache_key="tools",
            caption=(
                "🛠 *Инструменты и экосистема*\n\n"
                "_OnlyMonster · CRM · AI-технологии · Контент_\n\n"
                "Выбери тему 👇"
            ),
            reply_markup=tools_inline_keyboard(),
        )

    elif text == "📘 Гайд агентства Allstars":
        await typing(update, delay=0.6)
        await update.message.reply_text(
            "📘 *Гайд агентства Allstars*\n\n"
            "Почему доступ закрыт:\n"
            "1) Материалы доступны только кандидатам после проверки\n"
            "2) Доступ выдает HR после заполнения анкеты\n\n"
            "✅ Уже 120+ кандидатов прошли этот этап и получили доступ к гайдам.\n\n"
            "Если открыть ссылки сейчас - Notion покажет отказ в доступе.\n\n"
            "Подтвердите, что вы понимаете это условие, и только после этого откроются ссылки.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("☑️ Я понимаю, что доступ выдаст HR", callback_data="guide_ack")],
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="guide_back_menu")],
            ]),
        )

    elif text == "💰 Условия работы":
        await typing(update, delay=0.8)
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["conditions"],
            cache_key="conditions",
            caption=CONDITIONS_TEXT,
            reply_markup=main_keyboard(),
        )

    elif text == "🎓 Обучение":
        await typing(update, delay=0.8)
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["training"],
            cache_key="training",
            caption=TRAINING_TEXT,
            reply_markup=main_keyboard(),
        )

    elif text == "📋 NDA и верификация":
        await typing(update, delay=0.6)
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["nda"],
            cache_key="nda",
            caption=(
                "📋 *NDA и верификация — кратко*\n\n"
                "• Документы запрашиваем только после тест-смены\n"
                "• Это стандарт безопасности для защиты моделей и команды\n"
                "• Данные не передаются третьим лицам\n\n"
                "Нажмите «Показать подробнее», если хотите полный регламент."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📖 Показать подробнее", callback_data="nda_menu_more")],
                [InlineKeyboardButton("⬅️ Назад в меню", callback_data="guide_back_menu")],
            ]),
        )

    elif text == "❓ FAQ":
        await typing(update, delay=0.6)
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["faq"],
            cache_key="faq",
            caption=FAQ_TEXT,
            reply_markup=main_keyboard(),
        )

    elif text == "👥 Поделиться с другом":
        share_url = f"https://t.me/{BOT_USERNAME}?start=ref"
        await update.message.reply_text(
            "🤝 *Поделись с другом!*\n\n"
            "Отправь другу эту ссылку — и если он отработает месяц, ты получишь *+100$*.\n"
            "А если сделает 3k+ — *+200$* 🔥\n\n"
            f"👉 {share_url}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📤 Поделиться",
                    switch_inline_query=f"Присоединяйся к команде Allstars! {share_url}",
                )
            ]]),
        )

    elif text == "❌ Отменить заполнение":
        return await cancel(update, context)


async def duplicate_decision_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "dup_update_yes":
        context.user_data["update_row_number"] = context.user_data.get("duplicate_row")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Отлично, обновим вашу предыдущую заявку новыми данными.")
    else:
        context.user_data.pop("update_row_number", None)
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Ок, создадим новую заявку.")

    return await start_form_flow(update, context)


async def nda_menu_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(NDA_TEXT, parse_mode="Markdown", reply_markup=main_keyboard())


# ─────────────────────────────────────────────
#  INLINE CALLBACKS (Об агентстве / Инструменты)
#  Логика: редактируем то же самое сообщение,
#  не плодим новые — кнопка «← Назад» возвращает
#  к меню раздела без скролла.
# ─────────────────────────────────────────────
async def about_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "about_menu":
        # Редактируем само сообщение обратно к меню
        await q.edit_message_caption(
            caption=(
                "🏢 *Об агентстве*\n\n"
                "_3 года на рынке · 16 моделей · Системный подход_\n\n"
                "Выбери тему 👇"
            ),
            parse_mode="Markdown",
            reply_markup=about_inline_keyboard(),
        )
    elif q.data == "about_agency":
        await q.edit_message_caption(
            caption=ABOUT_AGENCY_TEXT,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="about_menu")]]),
        )
    elif q.data == "about_models":
        await q.edit_message_caption(
            caption=ABOUT_MODELS_TEXT,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="about_menu")]]),
        )


async def tools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="tool_menu")]])

    if q.data == "tool_menu":
        await q.edit_message_caption(
            caption=(
                "🛠 *Инструменты и экосистема*\n\n"
                "_OnlyMonster · CRM · AI-технологии · Контент_\n\n"
                "Выбери тему 👇"
            ),
            parse_mode="Markdown",
            reply_markup=tools_inline_keyboard(),
        )
    elif q.data == "tool_onlymonster":
        await q.edit_message_caption(caption=TOOLS_ONLYMONSTER_TEXT, parse_mode="Markdown", reply_markup=back_btn)
    elif q.data == "tool_crm":
        await q.edit_message_caption(caption=TOOLS_CRM_TEXT, parse_mode="Markdown", reply_markup=back_btn)
    elif q.data == "tool_ai":
        await q.edit_message_caption(caption=TOOLS_AI_TEXT, parse_mode="Markdown", reply_markup=back_btn)
    elif q.data == "tool_content":
        await q.edit_message_caption(caption=TOOLS_CONTENT_TEXT, parse_mode="Markdown", reply_markup=back_btn)


# ─────────────────────────────────────────────
#  HR Invite Callback
# ─────────────────────────────────────────────
async def handle_hr_invite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    user = query.from_user

    if data.startswith("interview_confirm:"):
        row_number = data.split(":", 1)[1]

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Отлично! 🙌\n"
            "Спасибо за подтверждение. Тогда ждём вас в назначенное время 😊"
        )

        if HR_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=HR_CHAT_ID,
                    text=(
                        f"✅ Кандидат подтвердил собеседование\n\n"
                        f"Пользователь: @{user.username if user.username else 'без username'}\n"
                        f"TG ID: {user.id}\n"
                        f"Row: {row_number}"
                    )
                )
            except Exception as e:
                logger.error(f"HR notify confirm error: {type(e).__name__}: {e}", exc_info=True)

    elif data.startswith("interview_reschedule:"):
        row_number = data.split(":", 1)[1]

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Хорошо 🙌\n"
            "Спасибо! Напишите, пожалуйста, в какое время вам было бы удобнее созвониться, "
            "и HR свяжется с вами."
        )

        if HR_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=HR_CHAT_ID,
                    text=(
                        f"🕒 Кандидату нужно другое время\n\n"
                        f"Пользователь: @{user.username if user.username else 'без username'}\n"
                        f"TG ID: {user.id}\n"
                        f"Row: {row_number}"
                    )
                )
            except Exception as e:
                logger.error(f"HR notify reschedule error: {type(e).__name__}: {e}", exc_info=True)


# ─────────────────────────────────────────────
#  АНКЕТА
# ─────────────────────────────────────────────
async def q1_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    text = update.message.text.strip()
    if context.user_data.get("awaiting_custom_source"):
        context.user_data["source"] = text
        context.user_data.pop("awaiting_custom_source", None)
    elif text == "✍️ Другое":
        context.user_data["awaiting_custom_source"] = True
        await update.message.reply_text(
            "Напишите, пожалуйста, откуда вы о нас узнали:",
            reply_markup=cancel_keyboard(),
        )
        return Q1_SOURCE
    else:
        context.user_data["source"] = text

    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q2_NAME")
    await update.message.reply_text(
        f"{question_header(1, 17)}*Вопрос 2 из 17:*\nКак вас зовут?",
        parse_mode="Markdown", reply_markup=cancel_keyboard(),
    )
    return Q2_NAME


async def q2_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)
    context.user_data["name"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q3_AGE")
    await update.message.reply_text(
        f"{question_header(2, 17)}*Вопрос 3 из 17:*\nСколько вам лет?",
        parse_mode="Markdown", reply_markup=cancel_keyboard(),
    )
    return Q3_AGE


async def q3_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ Пожалуйста, введите возраст *числом* (например: 23):",
            parse_mode="Markdown",
        )
        return Q3_AGE
    age = int(text)
    if age < 18:
        await update.message.reply_text(
            "🔞 *К сожалению, мы берём на работу только с 18 лет.*\n\nЕсли есть вопросы — используйте меню ниже.",
            parse_mode="Markdown", reply_markup=main_keyboard(),
        )
        return ConversationHandler.END
    if age > 65:
        await update.message.reply_text("⚠️ Пожалуйста, введите реальный возраст:", parse_mode="Markdown")
        return Q3_AGE
    context.user_data["age"] = str(age)
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q5_ENGLISH")
    await update.message.reply_text(
        f"{question_header(3, 17)}*Вопрос 4 из 17:*\nКакой у вас уровень английского языка?",
        parse_mode="Markdown", reply_markup=english_keyboard(),
    )
    return Q5_ENGLISH


async def q5_english_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    level = q.data.replace("eng_", "")
    context.user_data["english"] = level
    await q.edit_message_text(f"🌐 Английский: *{level}* ✅", parse_mode="Markdown")
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q6_PLATFORM")
    await q.message.reply_text(
        f"{question_header(4, 17)}*Вопрос 5 из 17:*\nКакая платформа вас интересует?",
        parse_mode="Markdown", reply_markup=platform_keyboard(),
    )
    return Q6_PLATFORM


async def q6_platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mapping = {"plat_onlyfans": "OnlyFans", "plat_fansly": "Fansly", "plat_both": "Обе платформы"}
    platform = mapping[q.data]
    context.user_data["platform"]    = platform
    context.user_data["open_shifts"] = get_open_shifts_for(platform)
    await q.edit_message_text(f"📱 Платформа: *{platform}* ✅", parse_mode="Markdown")
    context.user_data["shifts"] = []

    open_shifts = context.user_data["open_shifts"]
    shift_names = {
        "00-06": "🌙 00:00–06:00", "06-12": "🌅 06:00–12:00",
        "12-18": "☀️ 12:00–18:00", "18-00": "🌆 18:00–00:00",
    }
    open_list = " · ".join(shift_names[s] for s in open_shifts) if open_shifts else "нет открытых смен"

    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q7_SHIFT")
    await q.message.reply_text(
        f"{question_header(5, 17)}*Вопрос 6 из 17:*\nКакая смена вам подходит?\n\n"
        f"🟢 *Сейчас открыт набор ({platform}):* {open_list}\n\n"
        "_Можно выбрать несколько, затем нажмите «Подтвердить»._",
        parse_mode="Markdown",
        reply_markup=shift_keyboard(open_shifts=open_shifts),
    )
    return Q7_SHIFT


async def q7_shift_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    open_shifts = context.user_data.get("open_shifts", [])

    if q.data == "shift_done":
        if not context.user_data.get("shifts"):
            await q.answer("⚠️ Выберите хотя бы одну смену!", show_alert=True)
            return Q7_SHIFT

        shifts_str = ", ".join(context.user_data["shifts"])
        await q.edit_message_text(f"🕐 Смены: *{shifts_str}* ✅", parse_mode="Markdown")
        set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q8_EXPERIENCE")
        await q.message.reply_text(
            f"{question_header(6, 17)}*Вопрос 7 из 17:*\nКакой у вас стаж работы оператором/чаттером?\n(например: 6 месяцев / 1 год)",
            parse_mode="Markdown", reply_markup=cancel_keyboard(),
        )
        return Q8_EXPERIENCE

    shift = q.data.replace("shift_", "")
    shifts = context.user_data.get("shifts", [])
    if shift in shifts:
        shifts.remove(shift)
    else:
        shifts.append(shift)
    context.user_data["shifts"] = shifts
    await q.edit_message_reply_markup(reply_markup=shift_keyboard(shifts, open_shifts=open_shifts))
    return Q7_SHIFT


async def waitlist_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "waitlist_yes":
        save_waitlist(context.user_data)
        cancel_form_reminders(context, update.effective_user.id)
        context.user_data["form_active"] = False
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(1.0)
        d = context.user_data
        card = (
            "╔══════════════════════════════╗\n"
            "║   ✅  ДОБАВЛЕН В ОЖИДАНИЕ  ║\n"
            "╚══════════════════════════════╝\n\n"
            f"👤 *Имя:* {d.get('name', '—')}\n"
            f"🎂 *Возраст:* {d.get('age', '—')}\n"
            f"🌐 *Английский:* {d.get('english', '—')}\n"
            f"📱 *Платформа:* {d.get('platform', '—')}\n"
            f"🕐 *Смены:* {d.get('shifts', '—')}\n"
            f"💼 *Стаж:* {d.get('experience', '—')}\n"
            f"📊 *Топ страниц:* {d.get('top_pages', '—')}\n"
            f"📈 *Конверсия:* {d.get('conversion', '—')}\n"
            f"👤 *Типаж моделей:* {d.get('model_types', '—')}\n"
            f"🌐 *Платформы (опыт):* {d.get('worked_platforms', '—')}\n"
            f"💵 *Ср чек:* {d.get('avg_check', '—')}\n"
            f"📚 *Основная деятельность/учеба:* {d.get('main_activity', '—')}\n"
            f"📅 *График:* {d.get('work_schedule', '—')}\n"
            f"💸 *Финансовые ожидания:* {d.get('financial_expectations', '—')}\n"
            f"✉️ *Mail:* {d.get('email', '—')}\n"
            f"🪪 *Верификация:* {d.get('verification', '—')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Отлично! Мы сохранили твою кандидатуру. 🤝\n\n"
            "Как только нужная смена откроется — HR-менеджер *лично напишет тебе* в Telegram.\n\n"
            "_Пока ждёшь — можешь изучить разделы «🏢 Об агентстве» и «🛠 Инструменты» 👇_"
        )
        await q.message.reply_text(card, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        track_dropoff(context, "waitlist_declined")
        cancel_form_reminders(context, update.effective_user.id)
        context.user_data["form_active"] = False
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            "Понял! Если передумаешь — возвращайся, мы всегда рады. 👋",
            reply_markup=main_keyboard(),
        )
    return ConversationHandler.END


async def q8_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)
    context.user_data["experience"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q9_TOP_PAGES")
    await update.message.reply_text(
        f"{question_header(7, 17)}*Вопрос 8 из 17:*\nС каким топом страниц вы работали?",
        parse_mode="Markdown", reply_markup=cancel_keyboard(),
    )
    return Q9_TOP_PAGES


async def q9_top_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["top_pages"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q10_CONVERSION")

    await update.message.reply_text(
        f"{question_header(8, 17)}*Вопрос 9 из 17:*\nКакую конверсию вы обычно показывали?",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q10_CONVERSION


async def q10_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["conversion"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q11_MODEL_TYPES")

    await update.message.reply_text(
        f"{question_header(9, 17)}*Вопрос 10 из 17:*\n"
        "С каким типажом моделей работали?",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q11_MODEL_TYPES


async def q11_model_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["model_types"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q12_WORKED_PLATFORMS")

    await update.message.reply_text(
        f"{question_header(10, 17)}*Вопрос 11 из 17:*\n"
        "На каких платформах у вас уже был практический опыт?",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q12_WORKED_PLATFORMS


async def q12_worked_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["worked_platforms"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q13_FINANCIAL")

    await update.message.reply_text(
        f"{question_header(11, 17)}*Вопрос 12 из 17:*\n"
        "Какой уровень дохода рассматриваешь на старте и через 3 месяца?",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q13_FINANCIAL


async def q13_financial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["financial_expectations"] = update.message.text
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q14_GMAIL")

    await update.message.reply_text(
        f"{question_header(12, 17)}*Вопрос 13 из 17:*\n"
        "Напишите, пожалуйста, ваш Gmail для отправки обучающего гайда:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q14_GMAIL


async def q14_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    email = update.message.text.strip().lower()
    if not is_valid_gmail(email):
        await update.message.reply_text(
            "Укажите корректный Gmail в формате `example@gmail.com`.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
        return Q14_GMAIL

    context.user_data["email"] = email
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q15_AVG_CHECK")

    await update.message.reply_text(
        f"{question_header(13, 17)}*Вопрос 14 из 17:*\n"
        "Какой средний чек на анкетах, с которыми вы работали?",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return Q15_AVG_CHECK


async def q15_avg_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["avg_check"] = update.message.text.strip()
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q16_MAIN_ACTIVITY")

    await update.message.reply_text(
        f"{question_header(14, 17)}*Вопрос 15 из 17:*\n"
        "Есть ли у вас основная деятельность или учеба?",
        parse_mode="Markdown",
        reply_markup=main_activity_keyboard(),
    )
    return Q16_MAIN_ACTIVITY


async def q16_main_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["main_activity"] = update.message.text.strip()
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q17_SCHEDULE")

    await update.message.reply_text(
        f"{question_header(15, 17)}*Вопрос 16 из 17:*\n"
        "Какой график вам подходит: 5/2 или 6/1?",
        parse_mode="Markdown",
        reply_markup=schedule_keyboard(),
    )
    return Q17_SCHEDULE


async def q17_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    schedule = update.message.text.strip()
    if schedule not in {"5/2", "6/1"}:
        await update.message.reply_text(
            "Пожалуйста, выберите только один из вариантов: 5/2 или 6/1.",
            reply_markup=schedule_keyboard(),
        )
        return Q17_SCHEDULE

    context.user_data["work_schedule"] = schedule
    set_form_step(context, update.effective_user.id, update.effective_chat.id, "Q18_VERIFICATION")

    await update.message.reply_text(
        f"{question_header(16, 17)}*Вопрос 17 из 17:*\n\n"
        "*Верификация и NDA (кратко):*\n"
        "• Документы только после тест-смены\n"
        "• Данные защищены NDA\n"
        "• Нужно для безопасности команды и моделей\n\n"
        "Нажмите «Показать подробнее», если хотите полную версию.\n\n"
        "*Готовы пройти верификацию после тест-смены?*",
        parse_mode="Markdown",
        reply_markup=verification_keyboard(),
    )
    return Q18_VERIFICATION


async def q18_verification_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "nda_more":
        await q.message.reply_text(
            "📖 *Подробно про верификацию и NDA:*\n\n"
            "1) Верификация только после тест-смены\n"
            "2) Подходит паспорт/ID/права/ВНЖ\n"
            "3) Данные не публикуются и защищены договором\n"
            "4) Это обязательный стандарт безопасности в агентстве",
            parse_mode="Markdown",
        )
        return Q18_VERIFICATION

    if q.data == "verif_no":
        await q.edit_message_text("🪪 Верификация: *❌ Нет*", parse_mode="Markdown")
        context.user_data["user_id"]  = update.effective_user.id
        context.user_data["username"] = update.effective_user.username or update.effective_user.full_name
        track_dropoff(context, f"verification_declined_at_{context.user_data.get('current_step', 'unknown')}")
        cancel_form_reminders(context, update.effective_user.id)
        context.user_data["form_active"] = False
        save_rejection(context.user_data)
        await q.message.chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(1.2)
        await q.message.reply_text(
            "╔══════════════════════════════╗\n"
            "║   😔  ЗАЯВКА ОТКЛОНЕНА     ║\n"
            "╚══════════════════════════════╝\n\n"
            "К сожалению, верификация личности является *обязательным условием* для работы в Allstars.\n\n"
            "Это не прихоть — это стандарт безопасности, который защищает как моделей, так и всю команду.\n\n"
            "Без верификации мы не можем допустить оператора к работе с реальными страницами и данными.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_Если ты передумаешь — всегда можешь вернуться и заполнить анкету заново. Мы будем рады видеть тебя в команде! 🤝_",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    # Верификация — Да
    context.user_data["verification"] = "✅ Да"
    context.user_data["user_id"]      = update.effective_user.id
    context.user_data["username"]     = update.effective_user.username or update.effective_user.full_name
    context.user_data["shifts"]       = ", ".join(context.user_data.get("shifts", []))
    context.user_data["screening"], context.user_data["auto_tag"] = score_candidate(context.user_data)
    cancel_form_reminders(context, update.effective_user.id)
    context.user_data["form_active"] = False

    await q.edit_message_text("🪪 Верификация: *✅ Да*", parse_mode="Markdown")

    # Проверяем — выбранные смены открыты?
    open_shifts     = context.user_data.get("open_shifts", [])
    selected_shifts = [s.strip() for s in context.user_data.get("shifts", "").split(",") if s.strip()]
    has_open        = any(s in open_shifts for s in selected_shifts)

    if not has_open:
        # Смены закрыты — предлагаем лист ожидания
        shift_names = {
            "00-06": "🌙 00:00–06:00", "06-12": "🌅 06:00–12:00",
            "12-18": "☀️ 12:00–18:00", "18-00": "🌆 18:00–00:00",
        }
        platform  = context.user_data.get("platform", "")
        open_list = " · ".join(shift_names[s] for s in open_shifts) if open_shifts else "пока нет открытых смен"

        await q.message.chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(1.0)
        await q.message.reply_text(
            "╔══════════════════════════════╗\n"
            "║   ⏳  СМЕНЫ ПЕРЕПОЛНЕНЫ     ║\n"
            "╚══════════════════════════════╝\n\n"
            f"Анкета заполнена отлично! Но выбранные смены сейчас *закрыты для набора* на *{platform}*.\n\n"
            f"🟢 *Сейчас открыт набор на:* {open_list}\n\n"
            "Мы можем добавить тебя в *лист ожидания* — как только смена откроется, HR-менеджер напишет тебе лично.\n\n"
            "*Хочешь попасть в лист ожидания?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, добавьте меня", callback_data="waitlist_yes")],
                [InlineKeyboardButton("❌ Нет, спасибо",      callback_data="waitlist_no")],
            ]),
        )
        return Q_WAITLIST

    # Смены открыты — сохраняем в основную таблицу
    saved = save_to_sheet(context.user_data)

    if saved:
        await notify_hr(context, context.user_data)
        d = context.user_data
        card = (
            f"{progress(17, 17)}\n\n"
            "╔══════════════════════════════╗\n"
            "║     ✅  АНКЕТА ОТПРАВЛЕНА!  ║\n"
            "╚══════════════════════════════╝\n\n"
            f"👤 *Имя:* {d.get('name', '—')}\n"
            f"🎂 *Возраст:* {d.get('age', '—')}\n"
            f"🌐 *Английский:* {d.get('english', '—')}\n"
            f"📱 *Платформа:* {d.get('platform', '—')}\n"
            f"🕐 *Смены:* {d.get('shifts', '—')}\n"
            f"🎯 *Скрининг:* {d.get('screening', '—')}\n"
            f"🏷 *Автотег:* {d.get('auto_tag', '—')}\n"
            f"💼 *Стаж:* {d.get('experience', '—')}\n"
            f"📊 *Топ страниц:* {d.get('top_pages', '—')}\n"
            f"📈 *Конверсия:* {d.get('conversion', '—')}\n"
            f"👤 *Типаж моделей:* {d.get('model_types', '—')}\n"
            f"🌐 *Платформы (опыт):* {d.get('worked_platforms', '—')}\n"
            f"💵 *Ср чек:* {d.get('avg_check', '—')}\n"
            f"📚 *Основная деятельность/учеба:* {d.get('main_activity', '—')}\n"
            f"📅 *График:* {d.get('work_schedule', '—')}\n"
            f"💸 *Финансовые ожидания:* {d.get('financial_expectations', '—')}\n"
            f"✉️ *Mail:* {d.get('email', '—')}\n"
            f"🪪 *Верификация:* {d.get('verification', '—')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "1) Проверка анкеты\n"
            "2) Контакт HR\n"
            "3) Тест-смена\n\n"
            "⏱ HR пишет обычно в течение *1 часа*.\n\n"
            "*FAQ кратко:* выплаты - каждый вторник, NDA обязателен, верификация после тест-смены.\n\n"
            "_Пока ждёшь — изучи раздел «🏢 Об агентстве» и «💰 Условия работы» 👇_"
        )
        await q.message.chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(1.0)
        await q.message.reply_text(card, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await q.message.reply_text(
            "⚠️ *Произошла техническая ошибка при сохранении данных.*\n\n"
            "Пожалуйста, попробуйте заполнить анкету ещё раз через несколько минут.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
        )
    return ConversationHandler.END


async def guide_apply_now_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    await q.edit_message_reply_markup(reply_markup=None)
    return await start_form_flow(update, context)


async def guide_ack_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Отлично! Открываю ссылки на гайды.")

    await q.edit_message_text(
        "📘 *Гайд агентства Allstars*\n\n"
        "✅ Условие зафиксировано: доступ к материалам выдает HR после анкеты.\n\n"
        "Ссылки на гайды:\n"
        "• OnlyFans\n"
        "• Fansly\n\n"
        "Если Notion сейчас показывает отказ в доступе - это нормально до выдачи прав HR-менеджером.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📙 Гайд на OnlyFans", url="https://www.notion.so/28d845f0c6ed818698bbe00ebf69ffda")],
            [InlineKeyboardButton("📗 Гайд на Fansly",   url="https://www.notion.so/1dc845f0c6ed8029a3dbd32f34c9a0b5")],
            [InlineKeyboardButton("✅ Понял(а), заполнить анкету", callback_data="guide_apply_now")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="guide_back_menu")],
        ]),
    )


async def guide_back_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(
        "Возвращаю вас в главное меню 👇",
        reply_markup=main_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("current_step", "unknown")
    track_dropoff(context, f"cancel_at_{step}")
    cancel_form_reminders(context, update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text(
        "❌ *Заполнение анкеты отменено.*\n\nВы можете вернуться в любое время — нажмите «📝 Заполнить анкету».",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.job_queue.run_repeating(
        interview_reminder_job,
        interval=300,   # каждые 5 минут
        first=20
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📝 Заполнить анкету$"), handle_menu),
            CallbackQueryHandler(guide_apply_now_cb, pattern="^guide_apply_now$"),
        ],
        states={
            Q_DUPLICATE: [CallbackQueryHandler(duplicate_decision_cb, pattern="^dup_update_")],
            Q1_SOURCE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, q1_source)],
            Q2_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, q2_name)],
            Q3_AGE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, q3_age)],
            Q5_ENGLISH:       [CallbackQueryHandler(q5_english_cb, pattern="^eng_")],
            Q6_PLATFORM:      [CallbackQueryHandler(q6_platform_cb, pattern="^plat_")],
            Q7_SHIFT:         [CallbackQueryHandler(q7_shift_cb, pattern="^shift_")],
            Q8_EXPERIENCE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q8_experience)],
            Q9_TOP_PAGES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, q9_top_pages)],
            Q10_CONVERSION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q10_conversion)],
            Q11_MODEL_TYPES: [MessageHandler(filters.TEXT & ~filters.COMMAND, q11_model_types)],
            Q12_WORKED_PLATFORMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, q12_worked_platforms)],
            Q13_FINANCIAL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q13_financial)],
            Q14_GMAIL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, q14_gmail)],
            Q15_AVG_CHECK:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q15_avg_check)],
            Q16_MAIN_ACTIVITY:[MessageHandler(filters.TEXT & ~filters.COMMAND, q16_main_activity)],
            Q17_SCHEDULE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, q17_schedule)],
            Q18_VERIFICATION: [CallbackQueryHandler(q18_verification_cb, pattern="^(verif_|nda_more)")],
            Q_WAITLIST:       [CallbackQueryHandler(waitlist_cb, pattern="^waitlist_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^❌ Отменить заполнение$"), cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # Inline-кнопки подразделов + навигация «Назад»
    app.add_handler(CallbackQueryHandler(about_callback, pattern="^about_"))
    app.add_handler(CallbackQueryHandler(tools_callback, pattern="^tool_"))
    app.add_handler(CallbackQueryHandler(nda_menu_more_cb, pattern="^nda_menu_more$"))
    app.add_handler(CallbackQueryHandler(guide_ack_cb, pattern="^guide_ack$"))
    app.add_handler(CallbackQueryHandler(guide_back_menu_cb, pattern="^guide_back_menu$"))
    app.add_handler(CallbackQueryHandler(handle_hr_invite_callback, pattern=r"^interview_(confirm|reschedule):"))

    # Меню
    app.add_handler(MessageHandler(
        filters.Regex(
            "^(📘 Гайд агентства Allstars|🏢 Об агентстве|🛠 Инструменты|💰 Условия работы"
            "|🎓 Обучение|📋 NDA и верификация|❓ FAQ|👥 Поделиться с другом)$"
        ),
        handle_menu,
    ))

    logger.info("🚀 AllStars Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()