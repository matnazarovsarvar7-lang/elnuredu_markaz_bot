from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup, WebAppInfo)

from app.config import settings

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
B_SCAN = "📷 Skaner"
B_STOCK = "📦 Ombor"
B_RATING = "🏆 Reyting"
B_APPEAL = "✉️ Murojaat"
B_APPEALS = "📨 Murojaatlar"
B_PRINT = "🖨 Chop etish"
B_LIBRARY = "📚 Kutubxona"
B_MOC = "🎓 Imtihonlar"
B_MOC_OLD = "🎓 MOC imtihonlar"          # eski klaviaturalar uchun
# 🎓 Imtihonlar bo'limi — har biri to'g'ridan-to'g'ri yaratishni boshlaydi
B_X_MOC = "🎓 MOC (markazda)"
B_X_ONL = "📲 Online MOC"
B_X_OLP = "🏆 Olimpiada"
B_X_ATT = "👩‍🏫 Attestatsiya"
B_X_PDF = "📄 PDF nazorat"
B_X_EXT = "🌐 Tashqi test"
B_X_LIST = "📋 Imtihonlar ro'yxati"
B_X_CAL = "📐 Ballni moslash"
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
# rahbar menyusi bo'limlari (pastki menyu almashadi; «⬅️ Asosiy menyu» — qaytish)
B_SEC_EDU = "🎓 Ta'lim bo'limi"
B_SEC_FIN = "💰 Moliya bo'limi"
B_SEC_ADM = "🏢 Boshqaruv bo'limi"
B_BACK = "⬅️ Asosiy menyu"


def guest_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_APPLY)],
                                         [KeyboardButton(text=B_CONTACT, request_contact=True)]],
                               resize_keyboard=True, is_persistent=True)


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CONTACT, request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    def btn(t: str) -> KeyboardButton:
        if t == B_SCAN and settings.scanner_url:          # jonli kamera (Telegram Mini App)
            return KeyboardButton(text=t, web_app=WebAppInfo(url=settings.scanner_url))
        return KeyboardButton(text=t)
    return ReplyKeyboardMarkup(keyboard=[[btn(t) for t in r] for r in rows], resize_keyboard=True, is_persistent=True)


def staff_menu(role: str, multi: bool) -> ReplyKeyboardMarkup:
    tail = [B_HELP] + ([B_SWITCH] if multi else [])
    if role == "rahbar":                 # eng ko'p ishlatiladiganlar + 3 ta bo'lim (32 tugma 17 qatorda edi)
        return _kb([[B_PENDING, B_NOW],
                    [B_PAY_IN, B_DEBTS],
                    [B_STUDENT, B_NEW_STUDENT],
                    [B_TODAY, B_SCAN],
                    [B_SEC_EDU, B_SEC_FIN],
                    [B_SEC_ADM] + tail])
    rows: list[list[str]] = []
    if role in ("oqituvchi", "yordamchi"):           # testlar — «Nazoratlar» ichida, kutubxona — «Chop etish» ichida
        rows.append([B_TODAY, B_EXAMS])
        rows.append([B_SCAN, B_MOC])
        rows.append([B_RATING, B_APPEALS])
        rows.append([B_PRINT] + ([B_ACHIEVE] if role == "oqituvchi" else []))
    if role == "oqituvchi":
        rows.append([B_DELEGATE])
    if role == "moliya":
        rows.append([B_PAY_IN, B_DEBTS])
        rows.append([B_STUDENT, B_GROUPS])
        rows.append([B_NEW_STUDENT, B_MARKET])
        rows.append([B_CASH_CLOSE, B_HANDOVER])
        rows.append([B_EXPENSE, B_SERVICES])
        rows.append([B_STOCK, B_PRINT])
        rows.append([B_MOC])
    if role == "admin":
        rows.append([B_STUDENT, B_DEBTS])
        rows.append([B_NEW_STUDENT, B_LEADS])
        rows.append([B_SERVICES, B_STOCK])
        rows.append([B_ACHIEVE, B_PRINT])
        rows.append([B_MOC])
    rows.append([B_MY_TASKS, B_MY_SALARY])
    rows.append(tail)
    return _kb(rows)


SECTIONS = {
    B_SEC_EDU: ("🎓 <b>Ta'lim bo'limi</b>", [[B_EXAMS, B_MOC], [B_RATING, B_APPEALS], [B_PRINT, B_ACHIEVE],
                                            [B_BACK]]),
    B_SEC_FIN: ("💰 <b>Moliya bo'limi</b>", [[B_CASH, B_REPORT], [B_EXPENSE, B_SERVICES], [B_MARKET, B_STOCK],
                                            [B_BACK]]),
    B_SEC_ADM: ("🏢 <b>Boshqaruv bo'limi</b>", [[B_STAFF, B_GROUPS], [B_TASK_NEW, B_TASKS_ALL], [B_LEADS, B_DELEGATE],
                                               [B_LEAVE, B_HISTORY], [B_SETTINGS, B_BACK]]),
}


def exam_menu(boss: bool) -> ReplyKeyboardMarkup:
    """🎓 Imtihonlar: yangi imtihon turini bir bosishda boshlash + ro'yxat."""
    return _kb([[B_X_MOC, B_X_ONL], [B_X_OLP, B_X_ATT], [B_X_PDF, B_X_EXT],
                [B_X_LIST] + ([B_X_CAL] if boss else []), [B_BACK]])


def section_menu(key: str) -> ReplyKeyboardMarkup:
    return _kb(SECTIONS[key][1])


def parent_menu(switch: bool = False) -> ReplyKeyboardMarkup:
    last = [KeyboardButton(text=B_HELP)] + ([KeyboardButton(text=B_SWITCH)] if switch else [])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_CHILD), KeyboardButton(text=B_RATING)],
                                         [KeyboardButton(text=B_HOMEWORK), KeyboardButton(text=B_MARKET)],
                                         [KeyboardButton(text=B_PARENT_PAY), KeyboardButton(text=B_APPEAL)],
                                         last], resize_keyboard=True, is_persistent=True)


def student_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_ME), KeyboardButton(text=B_RATING)],
                                         [KeyboardButton(text=B_HOMEWORK), KeyboardButton(text=B_MARKET)],
                                         [KeyboardButton(text=B_APPEAL), KeyboardButton(text=B_HELP)]],
                               resize_keyboard=True, is_persistent=True)


def roles_kb(roles: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ROLE_TITLES[r], callback_data=f"role:{r}")]
                                                 for r in roles])


MARK_ICON = {"k": "✅", "l": "🕒", "y": "❌"}
HW_ICON = {"done": "✅", "part": "➗", "none": "❌"}


def attendance_kb(lesson_id: int, rows: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """rows: (enrollment_id, ism, belgi). Bosilsa: keldi → kelmadi → kechikdi → keldi."""
    ok = [InlineKeyboardButton(text="💾 Tasdiqlash va davom etish ➡️", callback_data=f"attok:{lesson_id}")]
    kb = [[InlineKeyboardButton(text=f"{MARK_ICON[m]} {name}", callback_data=f"att:{lesson_id}:{eid}")]
          for eid, name, m in rows]
    # ro'yxat uzun bo'lsa tugma pastda ko'rinmay qoladi — tepada ham turadi
    return InlineKeyboardMarkup(inline_keyboard=([ok] if len(rows) > 6 else []) + kb + [ok])


def homework_kb(lesson_id: int, rows: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    ok = [InlineKeyboardButton(text="💾 Tasdiqlash va davom etish ➡️", callback_data=f"hwok:{lesson_id}")]
    kb = [[InlineKeyboardButton(text=f"{HW_ICON[m]} {name}", callback_data=f"hw:{lesson_id}:{eid}")]
          for eid, name, m in rows]
    return InlineKeyboardMarkup(inline_keyboard=([ok] if len(rows) > 6 else []) + kb + [ok])


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Roziman", callback_data="consent")]])


def menu_texts() -> set[str]:
    """Pastki menyudagi barcha tugma matnlari (bosilsa — boshlangan jarayon yopiladi)."""
    return {v for k, v in globals().items() if k.startswith("B_") and isinstance(v, str) and k != "B_CONTACT"}
