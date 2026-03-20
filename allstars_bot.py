import asyncio
import json
import logging
import os
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
import gspread
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
HR_CHAT_ID       = int(os.getenv("HR_CHAT_ID", "0"))
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

GOOGLE_CREDS = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
SPREADSHEET_NAME = os.environ.get("GOOGLE_SPREADSHEET_NAME", "AllStarsLeads")

# ─────────────────────────────────────────────
#  СОСТОЯНИЯ ДИАЛОГА
# ─────────────────────────────────────────────
Q1_SOURCE, Q2_NAME, Q3_AGE, Q5_ENGLISH, Q6_PLATFORM, Q7_SHIFT, Q8_EXPERIENCE, Q9_PROFILES, Q10_VERIFICATION, Q_WAITLIST = range(10)

# ─────────────────────────────────────────────
#  GOOGLE SHEETS — кэшированный клиент
# ─────────────────────────────────────────────
_gs_client = None
_gs_sheet  = None
_gs_rejections = None  # Лист с отказами от верификации

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
        _gs_sheet  = _gs_client.open(SPREADSHEET_NAME).sheet1
        logger.info("Spreadsheet opened successfully!")

        if not _gs_sheet.row_values(1):
            _gs_sheet.append_row([
                "Дата", "TG Username", "TG ID",
                "Источник", "Имя", "Возраст",
                "Английский", "Платформа", "Смены",
                "Опыт", "Анкеты", "Верификация",
            ])
        return _gs_sheet
    except Exception as e:
        logger.error(f"Failed to connect: {type(e).__name__}: {e}", exc_info=True)
        raise


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
    try:
        logger.info("Saving to Google Sheets...")
        sheet = get_sheet()
        sheet.append_row([
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            data.get("username", ""), data.get("user_id", ""),
            data.get("source", ""),   data.get("name", ""),
            data.get("age", ""),      data.get("english", ""),
            data.get("platform", ""), data.get("shifts", ""),
            data.get("experience", ""), data.get("profiles", ""),
            data.get("verification", ""),
        ])
        logger.info("Saved to Google Sheets successfully!")
        return True
    except Exception as e:
        logger.error(f"Sheets error: {type(e).__name__}: {e}", exc_info=True)
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
            sheet = spreadsheet.add_worksheet(title="Ожидание", rows=1000, cols=13)
            sheet.append_row([
                "Дата", "TG Username", "TG ID",
                "Откуда узнали", "Имя", "Возраст",
                "Английский", "Платформа", "Смена",
                "Опыт", "Анкеты (топ, %)", "Верификация",
            ])
            return sheet
    except Exception as e:
        logger.error(f"Waitlist sheet error: {type(e).__name__}: {e}", exc_info=True)
        raise


def save_waitlist(data: dict) -> bool:
    """Сохраняет кандидата в лист ожидания — полная анкета."""
    try:
        sheet = get_waitlist_sheet()
        sheet.append_row([
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
            data.get("profiles", ""),
            data.get("verification", ""),
        ])
        logger.info("Waitlist entry saved.")
        return True
    except Exception as e:
        logger.error(f"Waitlist save error: {type(e).__name__}: {e}", exc_info=True)
        return False


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
        [InlineKeyboardButton("✅ Да, согласен(а)", callback_data="verif_yes")],
        [InlineKeyboardButton("❌ Нет, не готов(а)", callback_data="verif_no")],
    ])

# ─────────────────────────────────────────────
#  ПРОГРЕСС-БАР
# ─────────────────────────────────────────────
def progress(step: int, total: int = 9) -> str:
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
        "🔔 *Новая заявка AllStars!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Имя:* {e(data.get('name', '—'))}\n"
        f"🪪 *TG:* @{e(data.get('username', '—'))} (`{data.get('user_id', '—')}`)\n"
        f"🎂 *Возраст:* {e(data.get('age', '—'))}\n"
        f"🌐 *Английский:* {e(data.get('english', '—'))}\n"
        f"📱 *Платформа:* {e(data.get('platform', '—'))}\n"
        f"🕐 *Смены:* {e(data.get('shifts', '—'))}\n"
        f"💼 *Опыт:* {e(data.get('experience', '—'))}\n"
        f"📊 *Анкеты:* {e(data.get('profiles', '—'))}\n"
        f"🪪 *Верификация:* {e(data.get('verification', '—'))}\n"
        f"📡 *Источник:* {e(data.get('source', '—'))}\n"
        f"🕒 *Время:* {datetime.now().strftime('%d.%m.%Y %H:%M')}"
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


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📝 Заполнить анкету":
        # Фото → потом вопросы
        await send_section_photo(
            update,
            gdrive_id=SECTION_IMAGES["form"],
            cache_key="form",
            caption=(
                "📋 *Анкета Allstars*\n\n"
                "8 вопросов · ~2 минуты\n\n"
                "_Нажми кнопку ниже, чтобы начать 👇_"
            ),
        )
        await asyncio.sleep(0.4)
        await update.message.reply_text(
            f"{progress(0)}\n\n"
            "*Вопрос 1 из 8:*\n"
            "Откуда вы о нас узнали? Напишите имя/ник в TG друга или другой источник:",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
        return Q1_SOURCE

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
            caption=NDA_TEXT,
            reply_markup=main_keyboard(),
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
#  АНКЕТА
# ─────────────────────────────────────────────
async def q1_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)
    context.user_data["source"] = update.message.text
    await update.message.reply_text(
        f"{progress(1)}\n\n*Вопрос 2 из 9:*\nКак вас зовут?",
        parse_mode="Markdown", reply_markup=cancel_keyboard(),
    )
    return Q2_NAME


async def q2_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)
    context.user_data["name"] = update.message.text
    await update.message.reply_text(
        f"{progress(2)}\n\n*Вопрос 3 из 9:*\nСколько вам лет?",
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
    await update.message.reply_text(
        f"{progress(3)}\n\n*Вопрос 4 из 9:*\nКакой у вас уровень английского языка?",
        parse_mode="Markdown", reply_markup=english_keyboard(),
    )
    return Q5_ENGLISH


async def q5_english_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    level = q.data.replace("eng_", "")
    context.user_data["english"] = level
    await q.edit_message_text(f"🌐 Английский: *{level}* ✅", parse_mode="Markdown")
    await q.message.reply_text(
        f"{progress(4)}\n\n*Вопрос 5 из 9:*\nКакая платформа вас интересует?",
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

    await q.message.reply_text(
        f"{progress(5)}\n\n*Вопрос 7 из 9:*\nКакая смена вам подходит?\n\n"
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
        await q.message.reply_text(
            f"{progress(6)}\n\n*Вопрос 8 из 9:*\nЕсть ли у вас опыт работы оператором/чаттером?\nЕсли да — укажите, сколько по времени:",
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
            f"💼 *Опыт:* {d.get('experience', '—')}\n"
            f"📊 *Анкеты:* {d.get('profiles', '—')}\n"
            f"🪪 *Верификация:* {d.get('verification', '—')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Отлично! Мы сохранили твою кандидатуру. 🤝\n\n"
            "Как только нужная смена откроется — HR-менеджер *лично напишет тебе* в Telegram.\n\n"
            "_Пока ждёшь — можешь изучить разделы «🏢 Об агентстве» и «🛠 Инструменты» 👇_"
        )
        await q.message.reply_text(card, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
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
    await update.message.reply_text(
        f"{progress(7)}\n\n*Вопрос 9 из 9:*\nС какими анкетами работали? Укажите топ и примерный % конверсии.",
        parse_mode="Markdown", reply_markup=cancel_keyboard(),
    )
    return Q9_PROFILES


async def q9_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отменить заполнение":
        return await cancel(update, context)

    context.user_data["profiles"] = update.message.text

    await update.message.reply_text(
        f"{progress(8)}\n\n"
        "🔴 *Вопрос 10 из 9:*\n\n"
        "╔══════════════════════════════╗\n"
        "║  🪪  ВЕРИФИКАЦИЯ И NDA      ║\n"
        "╚══════════════════════════════╝\n\n"
        "Ты уже почти в команде — осталось разобраться с одним важным моментом. Читай внимательно, это честно 👇\n\n"
        "📖 *Почему мы вообще это ввели?*\n"
        "Мы работаем 3+ года. За это время, к сожалению, сталкивались со скамом — "
        "людьми которые получали доступ к страницам и пропадали. "
        "Именно поэтому мы были вынуждены ввести верификацию. "
        "Это не прихоть — это урок, который мы усвоили чтобы защитить и моделей, и команду.\n\n"
        "🛡 *Зачем это нужно нам?*\n"
        "Мы работаем со страницами с хорошим топом. "
        "Это реальные люди, реальные деньги и реальная ответственность. "
        "Агентство обязано обеспечить моделям безопасность — "
        "а значит знать каждого, кому даёт доступ к их данным.\n\n"
        "🙅 *Это не деанон и не слежка*\n"
        "Нам нужно только одно: подтвердить личность и возраст. "
        "Фото + имя — всё. Подойдёт *любой действующий документ* где есть эти данные.\n\n"
        "🔐 *Безопасность твоих документов прописана в договоре.* "
        "NDA подписывают обе стороны — мы берём на себя такую же ответственность перед тобой. "
        "Твои данные не передаются третьим лицам и нигде не публикуются.\n\n"
        "💬 *Есть отзывы от наших сотрудников* — если хочешь познакомиться с командой поближе перед тем как принять решение, спроси HR-менеджера.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Верификация — это не про недоверие к тебе. Это про то, что мы строим команду всерьёз и надолго. 🤝_\n\n"
        "*Ты готов(-а) пройти верификацию после тест-смены?*",
        parse_mode="Markdown",
        reply_markup=verification_keyboard(),
    )
    return Q10_VERIFICATION


async def q10_verification_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "verif_no":
        await q.edit_message_text("🪪 Верификация: *❌ Нет*", parse_mode="Markdown")
        context.user_data["user_id"]  = update.effective_user.id
        context.user_data["username"] = update.effective_user.username or update.effective_user.full_name
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
            f"{progress(9)}\n\n"
            "╔══════════════════════════════╗\n"
            "║     ✅  АНКЕТА ОТПРАВЛЕНА!  ║\n"
            "╚══════════════════════════════╝\n\n"
            f"👤 *Имя:* {d.get('name', '—')}\n"
            f"🎂 *Возраст:* {d.get('age', '—')}\n"
            f"🌐 *Английский:* {d.get('english', '—')}\n"
            f"📱 *Платформа:* {d.get('platform', '—')}\n"
            f"🕐 *Смены:* {d.get('shifts', '—')}\n"
            f"💼 *Опыт:* {d.get('experience', '—')}\n"
            f"📊 *Анкеты:* {d.get('profiles', '—')}\n"
            f"🪪 *Верификация:* {d.get('verification', '—')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎉 Отлично! Наш HR-менеджер свяжется с тобой в ближайшее время для согласования даты созвона.\n\n"
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📝 Заполнить анкету$"), handle_menu)],
        states={
            Q1_SOURCE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, q1_source)],
            Q2_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, q2_name)],
            Q3_AGE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, q3_age)],
            Q5_ENGLISH:       [CallbackQueryHandler(q5_english_cb, pattern="^eng_")],
            Q6_PLATFORM:      [CallbackQueryHandler(q6_platform_cb, pattern="^plat_")],
            Q7_SHIFT:         [CallbackQueryHandler(q7_shift_cb, pattern="^shift_")],
            Q8_EXPERIENCE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, q8_experience)],
            Q9_PROFILES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, q9_profiles)],
            Q10_VERIFICATION: [CallbackQueryHandler(q10_verification_cb, pattern="^verif_")],
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

    # Меню
    app.add_handler(MessageHandler(
        filters.Regex(
            "^(🏢 Об агентстве|🛠 Инструменты|💰 Условия работы"
            "|🎓 Обучение|📋 NDA и верификация|❓ FAQ|👥 Поделиться с другом)$"
        ),
        handle_menu,
    ))

    logger.info("🚀 AllStars Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()