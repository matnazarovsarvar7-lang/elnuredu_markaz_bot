from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

from app.services import ROLE_TITLES

B_CONTACT = "📱 Raqamni yuborish"
B_SWITCH = "🔄 Rolni almashtirish"
B_TODAY = "📋 Bugungi darslar"
B_CHILD = "👦 Farzandim"
B_ME = "📊 Mening holatim"
B_HOMEWORK = "📚 Uy vazifasi"
B_PAY_IN = "💵 To'lov kiritish"
B_DEBTS = "📒 Qarzdorlar"
B_PENDING = "⏳ Kutilayotganlar"
B_HELP = "ℹ️ Yordam"
B_GROUPS = "⚙️ Guruhlar"
B_STUDENT = "👤 O'quvchi"
B_STAFF = "👥 Xodimlar"
B_SETTINGS = "🛠 Sozlamalar"
B_TASK_NEW = "📝 Vazifa berish"
B_TASKS_ALL = "📋 Vazifalar holati"
B_MY_TASKS = "📝 Vazifalarim"
B_LEADS = "🧲 Arizalar"
B_NOW = "📊 Hozirgi holat"
B_APPLY = "✍️ Kursga yozilish"
B_NEW_STUDENT = "➕ Yangi o'quvchi"
B_EXAMS = "📝 Nazoratlar"
B_TESTS = "📚 Testlar"
B_CASH_CLOSE = "🔐 Kassani yopish"
B_HANDOVER = "💼 Kassani topshirish"
B_CASH = "💼 Kassa"
B_EXPENSE = "🧾 Xarajat"
B_SERVICES = "🛒 Xizmatlar"
B_MY_SALARY = "💰 Oyligim"
B_REPORT = "📈 Hisobot"
B_ACHIEVE = "🏆 Yutuq kiritish"
B_HISTORY = "📜 Markaz tarixi"
B_MARKET = "🛍 Market"
B_DELEGATE = "🤝 Yordamchi"
B_LEAVE = "🛂 Ruxsat"
B_PARENT_PAY = "💳 To'lov chekini yuborish"


def guest_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_APPLY)],
                                         [KeyboardButton(text=B_CONTACT, request_contact=True)]],
                               resize_keyboard=True, is_persistent=True)


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CONTACT, request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


def staff_menu(role: str, multi: bool) -> ReplyKeyboardMarkup:
    rows: list[list[str]] = []
    if role in ("oqituvchi", "yordamchi", "rahbar"):
        rows.append([B_TODAY, B_EXAMS])
        rows.append([B_TESTS])
    if role == "oqituvchi":
        rows.append([B_DELEGATE, B_ACHIEVE])
    if role in ("moliya", "rahbar"):
        rows.append([B_PAY_IN, B_DEBTS])
        rows.append([B_STUDENT, B_GROUPS])
        rows.append([B_NEW_STUDENT, B_MARKET])
    if role == "moliya":
        rows.append([B_CASH_CLOSE, B_HANDOVER])
    if role in ("moliya", "rahbar"):
        rows.append([B_EXPENSE, B_SERVICES])
    if role == "admin":
        rows.append([B_STUDENT, B_DEBTS])
        rows.append([B_NEW_STUDENT, B_LEADS])
        rows.append([B_SERVICES, B_ACHIEVE])
    if role == "rahbar":
        rows.append([B_PENDING, B_STAFF])
        rows.append([B_TASK_NEW, B_TASKS_ALL])
        rows.append([B_NOW, B_LEADS])
        rows.append([B_DELEGATE, B_LEAVE])
        rows.append([B_CASH, B_REPORT])
        rows.append([B_ACHIEVE, B_HISTORY])
        rows.append([B_SETTINGS])
    else:
        rows.append([B_MY_TASKS, B_MY_SALARY])
    rows.append([B_HELP] + ([B_SWITCH] if multi else []))
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True, is_persistent=True)


def parent_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CHILD), KeyboardButton(text=B_HOMEWORK)],
                                         [KeyboardButton(text=B_PARENT_PAY), KeyboardButton(text=B_MARKET)],
                                         [KeyboardButton(text=B_HELP)]], resize_keyboard=True, is_persistent=True)


def student_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_ME), KeyboardButton(text=B_HOMEWORK)],
                                         [KeyboardButton(text=B_MARKET), KeyboardButton(text=B_HELP)]],
                               resize_keyboard=True, is_persistent=True)


def roles_kb(roles: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ROLE_TITLES[r], callback_data=f"role:{r}")]
                                                 for r in roles])


MARK_ICON = {"k": "✅", "l": "🕒", "y": "❌"}
HW_ICON = {"done": "✅", "part": "➗", "none": "❌"}


def attendance_kb(lesson_id: int, rows: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """rows: (enrollment_id, ism, belgi). Bosilsa: keldi → kelmadi → kechikdi → keldi."""
    kb = [[InlineKeyboardButton(text=f"{MARK_ICON[m]} {name}", callback_data=f"att:{lesson_id}:{eid}")]
          for eid, name, m in rows]
    kb.append([InlineKeyboardButton(text="💾 Tasdiqlash", callback_data=f"attok:{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def homework_kb(lesson_id: int, rows: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(text=f"{HW_ICON[m]} {name}", callback_data=f"hw:{lesson_id}:{eid}")]
          for eid, name, m in rows]
    kb.append([InlineKeyboardButton(text="💾 Tasdiqlash", callback_data=f"hwok:{lesson_id}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Roziman", callback_data="consent")]])


def menu_texts() -> set[str]:
    """Pastki menyudagi barcha tugma matnlari (bosilsa — boshlangan jarayon yopiladi)."""
    return {v for k, v in globals().items() if k.startswith("B_") and isinstance(v, str) and k != "B_CONTACT"}
