# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "").strip()
BANK_TRANSFER_ENABLED = os.getenv("BANK_TRANSFER_ENABLED", "").strip().lower() in ("1", "true", "yes")
STAR_TOPUP_TIERS_RAW = os.getenv("STAR_TOPUP_TIERS", "").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
JOIN_CHANNEL_URL = os.getenv("JOIN_CHANNEL_URL", "").strip()
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "").strip()
CANCEL_SECONDS = 6 * 60
DAILY_REWARD_CENTS = 100
MAX_AMOUNT_CENTS = 100_000_000

router = Router()
fallback_router = Router(name="store-fallback")
logger = logging.getLogger("service-order-bot")

def channel_join_url():
    if JOIN_CHANNEL_URL:
        return JOIN_CHANNEL_URL
    if FORCE_JOIN_CHANNEL.startswith("@"):
        return "https://t.me/" + FORCE_JOIN_CHANNEL[1:]
    return ""

def parse_admin_ids():
    result = set()
    for item in os.getenv("ADMIN_IDS", "").split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            result.add(int(item))
    return result

ADMIN_IDS = parse_admin_ids()

# ==========================================
# STATES (Durum Yönetimi)
# ==========================================
class DepositFlow(StatesGroup):
    amount = State()
    reference = State()

class PromoFlow(StatesGroup):
    code = State()

class AdminPromoFlow(StatesGroup):
    code = State()
    bonus = State()
    uses = State()
    duration_minutes = State()

class AdminPromoDeleteFlow(StatesGroup):
    code = State()

class AdminBalanceFlow(StatesGroup):
    target = State()
    amount = State()

class AdminServiceFlow(StatesGroup):
    name = State()
    price = State()
    description = State()

class AdminDeleteServiceFlow(StatesGroup):
    service_id = State()

class AdminEnterNumberFlow(StatesGroup):
    phone = State()
    code = State()

class SupportFlow(StatesGroup):
    body = State()

class AdminBroadcastFlow(StatesGroup):
    body = State()
    confirmation = State()

# ==========================================
# YARDIMCI FONKSİYONLAR
# ==========================================
ORDER_ANIMATIONS = {}
ORDER_MESSAGE_REFS = {}
ORDER_PHONE_CACHE = {}
BULLET_CHAR = "\u2022"

SPINNER_POSITIONS = [
    (0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (1, 4), (2, 4), (3, 4),
    (4, 4), (4, 3), (4, 2), (4, 1), (4, 0), (3, 0), (2, 0), (1, 0),
]

def render_spinner_frame(step, center_text):
    missing = SPINNER_POSITIONS[step % 16]
    lines = []
    for r in range(5):
        row_chars = []
        for c in range(5):
            is_border = (r == 0 or r == 4 or c == 0 or c == 4)
            if is_border:
                if (r, c) == missing:
                    row_chars.append(" ")
                else:
                    row_chars.append(BULLET_CHAR)
            else:
                row_chars.append(" ")
        lines.append(" ".join(row_chars))
    lines[2] = "  " + center_text
    return "<pre>" + "\n".join(lines) + "</pre>"

def stop_order_animation(order_id):
    ORDER_ANIMATIONS.pop(order_id, None)

async def animate_order_status(bot, chat_id, message_id, order_id):
    step = 0
    try:
        while order_id in ORDER_ANIMATIONS:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=render_spinner_frame(step, "-----"), parse_mode="HTML"
                )
            except TelegramAPIError:
                pass
            step += 1
            await asyncio.sleep(0.6)
    except asyncio.CancelledError:
        return

def now_utc():
    return datetime.now(timezone.utc)

def timestamp():
    return now_utc().isoformat(timespec="seconds")

def format_money(cents):
    whole, fraction = divmod(int(cents), 100)
    return str(whole) + "." + str(fraction).zfill(2) + " TL"

def parse_money(value):
    raw = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Tutar sayi olmali.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Tutar sifirdan buyuk olmali.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("En fazla iki ondalik basamak kullanin.")
    cents = int(amount * 100)
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("Tutar izin verilen sinirin uzerinde.")
    return cents

def parse_star_topup_tiers(raw=STAR_TOPUP_TIERS_RAW):
    tiers = []
    if not raw:
        return tiers
    seen = set()
    for item in raw.split(","):
        pair = item.strip().split(":", 1)
        if len(pair) != 2 or not pair[0].strip().isdigit():
            raise ValueError("STAR_TOPUP_TIERS must use stars:TL pairs.")
        stars = int(pair[0].strip())
        if stars < 1 or stars > 100_000:
            raise ValueError("Stars amount must be between 1 and 100000.")
        try:
            credit_cents = parse_money(pair[1].strip())
        except ValueError as exc:
            raise ValueError("Credit must be a positive TL amount.") from exc
        if stars in seen:
            raise ValueError("Duplicate Stars amount.")
        seen.add(stars)
        tiers.append((stars, credit_cents))
    return sorted(tiers)

try:
    WELCOME_BONUS_CENTS = parse_money(os.getenv("WELCOME_BONUS_TL", "1").strip())
except ValueError as exc:
    raise RuntimeError("WELCOME_BONUS_TL must be a positive TL amount.") from exc

try:
    STAR_TOPUP_TIERS = parse_star_topup_tiers()
except ValueError as exc:
    raise RuntimeError(str(exc)) from exc

try:
    REFERRAL_BONUS_CENTS = parse_money(os.getenv("REFERRAL_BONUS_TL", "5").strip())
except ValueError as exc:
    raise RuntimeError("REFERRAL_BONUS_TL must be a positive TL amount.") from exc

def clean_text(value, max_length=180):
    return " ".join(str(value).strip().split())[:max_length]

def is_admin(user_id):
    return user_id in ADMIN_IDS

def referral_id_from_start(text):
    parts = str(text or "").split(maxsplit=1)
    if len(parts) != 2:
        return None
    match = re.fullmatch(r"ref_(\d+)", parts[1].strip())
    return int(match.group(1)) if match else None

async def check_channel_membership(bot, user_id):
    if not FORCE_JOIN_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
    except TelegramAPIError:
        return None
    if member.status in ("creator", "administrator", "member"):
        return True
    return member.status == "restricted" and bool(member.is_member)

def join_channel_keyboard():
    url = channel_join_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Kanala katıl", url=url, style="primary")],
        [InlineKeyboardButton(text="✅ Katıldım, kontrol et", callback_data="check_join", style="success")]
    ])

# ==========================================
# VERİTABANI İŞLEMLERİ
# ==========================================
async def open_db():
    db = await aiosqlite.connect(DB_PATH, timeout=10)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 10000")
    return db

@asynccontextmanager
async def db_session():
    db = await open_db()
    try:
        yield db
    finally:
        await db.close()

async def get_language(user_id):
    try:
        async with db_session() as db:
            cursor = await db.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row["language"]:
                return row["language"]
    except Exception:
        pass
    return "tr"

async def user_is_banned(user_id):
    try:
        async with db_session() as db:
            cursor = await db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row["is_banned"]:
                return True
    except Exception:
        pass
    return False

async def init_db():
    if DB_PATH != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    async with db_session() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT NOT NULL,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                last_daily_at TEXT, language TEXT DEFAULT 'tr',
                vip_until TEXT, last_seen_at TEXT,
                is_banned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS services (
                service_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, service_id INTEGER NOT NULL,
                service_name TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
                kind TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deposit_requests (
                deposit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
                reference TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promos (
                promo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE, bonus_cents INTEGER NOT NULL,
                max_uses INTEGER NOT NULL, current_uses INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promo_uses (
                promo_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                used_at TEXT NOT NULL, PRIMARY KEY(promo_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL, referred_user_id INTEGER NOT NULL UNIQUE,
                reward_cents INTEGER NOT NULL DEFAULT 0, rewarded_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS star_payment_intents (
                payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                stars_amount INTEGER NOT NULL, credit_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', telegram_charge_id TEXT UNIQUE,
                created_at TEXT NOT NULL, paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS message_rate_limits (
                user_id INTEGER PRIMARY KEY, last_message_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', closed_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_user_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_ratings (
                rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL, created_at TEXT NOT NULL
            );
        """)
        await db.commit()

async def ensure_user(user, referrer_id=None):
    async with db_session() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user.id,))
        is_new = await cursor.fetchone() is None
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username, first_name = excluded.first_name, last_seen_at = excluded.last_seen_at
        """, (user.id, clean_text(user.username or "", 64), clean_text(user.first_name or "User", 100), timestamp(), timestamp()))
        if is_new and WELCOME_BONUS_CENTS > 0:
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (WELCOME_BONUS_CENTS, user.id))
            await add_transaction(db, user.id, WELCOME_BONUS_CENTS, "welcome_bonus", "Hoş geldin bonusu")
        if is_new and isinstance(referrer_id, int) and referrer_id > 0 and referrer_id != user.id:
            cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,))
            if await cursor.fetchone():
                await db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_user_id, reward_cents, created_at) VALUES (?, ?, ?, ?)",
                                 (referrer_id, user.id, REFERRAL_BONUS_CENTS, timestamp()))
        await db.commit()

async def add_transaction(db, user_id, amount_cents, kind, note):
    await db.execute("INSERT INTO transactions (user_id, amount_cents, kind, note, created_at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, amount_cents, kind, clean_text(note, 180), timestamp()))

async def resolve_user_id(identifier):
    value = str(identifier or "").strip()
    if value.isdigit() and int(value) > 0:
        async with db_session() as db:
            cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(value),))
            row = await cursor.fetchone()
            return int(row["user_id"]) if row else None
    username = value[1:] if value.startswith("@") else value
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE lower(username) = lower(?)", (username,))
        row = await cursor.fetchone()
        return int(row["user_id"]) if row else None

async def notify_admins(bot, text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            pass

async def notify_user(bot, user_id, text):
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        pass

async def log_event(bot, text):
    logger.info("bot_event: %s", clean_text(text, 1800))
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(LOG_CHANNEL_ID, "📋 Bot kaydı:\n" + clean_text(text, 1800))
        except TelegramAPIError:
            pass

# ==========================================
# MENÜ & KLAVYELER
# ==========================================
def main_keyboard(user_id, language="tr"):
    english = language == "en"
    labels = {
        "catalog": "🛍️ Catalog" if english else "🛍️ Hizmet kataloğu",
        "balance": "💰 Balance" if english else "💰 Bakiyem",
        "orders": "📦 Orders" if english else "📦 Siparişlerim",
        "topup": "⭐ Add balance" if english else "⭐ Bakiye yükle",
        "vip": "💎 VIP membership" if english else "💎 VIP üyelik",
        "coupon": "🎟️ Discount coupon" if english else "🎟️ İndirim kuponu",
        "referral": "👥 Invite friends" if english else "👥 Arkadaşını davet et",
        "leaderboard": "🏆 Invite leaders" if english else "🏆 Davet liderleri",
        "rewards": "🎁 Rewards & tasks" if english else "🎁 Ödüller ve görevler",
        "support": "💬 Support" if english else "💬 Destek",
        "language": "🌐 Language" if english else "🌐 Dil seçimi",
        "help": "ℹ️ Help" if english else "ℹ️ Yardım",
        "announcement": "📢 Announcement" if english else "📢 Duyuru gönder",
        "admin": "🛠️ Admin panel" if english else "🛠️ Admin paneli",
    }
    rows = [
        [InlineKeyboardButton(text=labels["catalog"], callback_data="menu:catalog", style="primary"),
         InlineKeyboardButton(text=labels["balance"], callback_data="menu:balance", style="success")],
        [InlineKeyboardButton(text=labels["orders"], callback_data="menu:orders", style="primary"),
         InlineKeyboardButton(text=labels["topup"], callback_data="menu:topup", style="success")],
        [InlineKeyboardButton(text=labels["vip"], callback_data="menu:vip", style="success"),
         InlineKeyboardButton(text=labels["coupon"], callback_data="menu:coupon", style="primary")],
        [InlineKeyboardButton(text=labels["referral"], callback_data="menu:referral", style="primary"),
         InlineKeyboardButton(text=labels["leaderboard"], callback_data="menu:leaderboard", style="success")],
        [InlineKeyboardButton(text=labels["rewards"], callback_data="menu:rewards", style="primary"),
         InlineKeyboardButton(text=labels["support"], callback_data="menu:support", style="success")],
        [InlineKeyboardButton(text=labels["language"], callback_data="menu:language", style="primary"),
         InlineKeyboardButton(text=labels["help"], callback_data="menu:help", style="primary")]
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text=labels["announcement"], callback_data="menu:broadcast", style="danger"),
                     InlineKeyboardButton(text=labels["admin"], callback_data="menu:admin", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_keyboard(services):
    rows = []
    for s in services:
        rows.append([InlineKeyboardButton(text=clean_text(s["name"], 45), callback_data="s:" + str(s["service_id"]))])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def service_detail_keyboard(service_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Sipariş Oluştur", callback_data="b:" + str(service_id), style="success")]])

def order_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ İptal Et ve İade Al", callback_data="o:cancel:" + str(order_id), style="danger")]])

def admin_order_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Numara Gir", callback_data="n:" + str(order_id), style="success")],
        [InlineKeyboardButton(text="❌ İptal Et ve İade", callback_data="o:refund:" + str(order_id), style="danger")]
    ])

def admin_enter_code_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Kod Gir", callback_data="c:" + str(order_id), style="success")],
        [InlineKeyboardButton(text="❌ İptal Et ve İade", callback_data="o:refund:" + str(order_id), style="danger")]
    ])

def deposit_keyboard(deposit_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Onayla", callback_data="d:approve:" + str(deposit_id), style="success"),
        InlineKeyboardButton(text="Reddet", callback_data="d:reject:" + str(deposit_id), style="danger")
    ]])

def order_rating_keyboard(order_id):
    buttons = [InlineKeyboardButton(text=str(i) + " ⭐", callback_data="rate:" + str(order_id) + ":" + str(i), style="success") for i in range(1, 6)]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

async def welcome_screen_text(user_id, first_name, language):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
    balance = int(user["balance_cents"]) if user else 0
    vip_until = user["vip_until"] if user else None
    try:
        vip_active = bool(vip_until and datetime.fromisoformat(vip_until) > now_utc())
    except ValueError:
        vip_active = False
    name = clean_text(first_name or "", 60) or "Arkadaşım"
    if language == "en":
        vip_status = "Active until " + str(vip_until) if vip_active else "Not active"
        return "✨ Welcome, " + name + "!\n💰 Your balance: " + format_money(balance) + "\n💎 VIP: " + vip_status + "\n\n⚡ Orders are delivered automatically.\n💎 VIP members get priority and 20% discount.\n👇 Choose an option:"
    vip_status = vip_until + " tarihine kadar aktif" if vip_active else "Aktif değil"
    return "✨ Hoş geldin, " + name + "!\n💰 Bakiyeniz: " + format_money(balance) + "\n💎 VIP: " + vip_status + "\n\n⚡ Siparişler otomatik teslim edilir.\n💎 VIP üyeler önceliklidir ve %20 indirim alır.\n👇 Bir seçenek belirle:"

# ==========================================
# MIDDLEWARES
# ==========================================
class JoinChannelMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None and not is_admin(user.id):
            if await user_is_banned(user.id):
                if isinstance(event, Message):
                    await event.answer("🚫 Bu hesap botu kullanamıyor.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 Bu hesap botu kullanamıyor.", show_alert=True)
                return None
            async with db_session() as db:
                await db.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?", (timestamp(), user.id))
                await db.commit()
        
        if not FORCE_JOIN_CHANNEL or user is None or is_admin(user.id):
            return await handler(event, data)
            
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
            
        if isinstance(event, Message):
            await ensure_user(user, referrer_id=referral_id_from_start(event.text))
            
        if isinstance(event, CallbackQuery) and event.data == "check_join":
            return await handler(event, data)

        joined = await check_channel_membership(event.bot, user.id)
        if joined is True:
            return await handler(event, data)

        text = "⚠️ Kanal üyeliğini kontrol edemiyorum." if joined is None else "👋 Hoş geldin!\n\nBotu kullanmadan önce kanalımıza katılman gerekiyor."
        keyboard = None if joined is None else join_channel_keyboard()

        if isinstance(event, Message):
            await event.answer(text, reply_markup=keyboard)
        elif isinstance(event, CallbackQuery):
            if event.message:
                await event.message.answer(text, reply_markup=keyboard)
            await event.answer("📣 Önce duyuru kanalına katıl.", show_alert=True)
        return None

class RateLimitMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or event.from_user is None or is_admin(event.from_user.id) or event.successful_payment or (event.text or "").strip() == "/cancel":
            return await handler(event, data)
        state = data.get("state")
        if state is not None:
            try:
                if await state.get_state(): return await handler(event, data)
            except Exception: pass
        
        user_id = event.from_user.id
        current = now_utc()
        db = await open_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT last_message_at FROM message_rate_limits WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                remaining = 30 - int((current - datetime.fromisoformat(row["last_message_at"])).total_seconds())
                if remaining > 0:
                    await db.rollback()
                    await event.answer("Guvenlik siniri: yeni mesaj icin " + str(remaining) + " saniye bekleyin.")
                    return None
            await db.execute("INSERT INTO message_rate_limits (user_id, last_message_at) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET last_message_at = excluded.last_message_at",
                             (user_id, current.isoformat(timespec="seconds")))
            await db.commit()
        except Exception:
            await db.rollback()
        finally:
            await db.close()
        return await handler(event, data)

router.message.outer_middleware(JoinChannelMiddleware())
router.callback_query.outer_middleware(JoinChannelMiddleware())
router.message.outer_middleware(RateLimitMiddleware())

# ==========================================
# KOMUTLAR & MENÜ BUTONLARI (CALLBACKS)
# ==========================================
@router.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user is None: return
    try: await message.answer("🔄 Ana menü hazırlanıyor...", reply_markup=ReplyKeyboardRemove())
    except Exception: pass
    await ensure_user(message.from_user, referrer_id=referral_id_from_start(message.text))
    language = await get_language(message.from_user.id)
    text = await welcome_screen_text(message.from_user.id, message.from_user.first_name, language)
    await message.answer(text, reply_markup=main_keyboard(message.from_user.id, language))

@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        services = await cursor.fetchall()
    if not services:
        await cb.message.edit_text("Su an aktif hizmet yok.")
    else:
        await cb.message.edit_text("Bir hizmet secin:", reply_markup=service_keyboard(services))
    await cb.answer()

@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery):
    await balance_handler(cb.message, user_id=cb.from_user.id)
    await cb.answer()

@router.callback_query(F.data == "menu:orders")
async def menu_orders(cb: CallbackQuery):
    await history_handler(cb.message, user_id=cb.from_user.id)
    await cb.answer()

@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await show_topup_methods(cb.message)
    await cb.answer()

@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    await cb.message.answer("💎 VIP üyelik sistemi yakında!")
    await cb.answer()

@router.callback_query(F.data == "menu:coupon")
async def menu_coupon(cb: CallbackQuery):
    await cb.message.answer("🎟️ İndirim kuponu sistemi yakında!")
    await cb.answer()

@router.callback_query(F.data == "menu:referral")
async def menu_referral(cb: CallbackQuery):
    await referral_handler(cb.message, user_id=cb.from_user.id)
    await cb.answer()

@router.callback_query(F.data == "menu:leaderboard")
async def menu_leaderboard(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT u.username, u.first_name, COUNT(r.referral_id) as cnt FROM users u JOIN referrals r ON r.referrer_id = u.user_id GROUP BY u.user_id ORDER BY cnt DESC LIMIT 10")
        rows = await cursor.fetchall()
    text = "🏆 Liderlik Tablosu:\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. {r['username'] or r['first_name']} - {r['cnt']} davet\n"
    if not rows: text += "Henüz kimse yok."
    await cb.message.edit_text(text)
    await cb.answer()

@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(cb: CallbackQuery):
    await cb.message.answer("🎁 Ödüller ve görevler yakında!")
    await cb.answer()

@router.callback_query(F.data == "menu:support")
async def menu_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFlow.body)
    await cb.message.edit_text("💬 Destek için sorununuzu tek mesaj olarak yazın:")
    await cb.answer()

@router.callback_query(F.data == "menu:language")
async def menu_language(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="language:tr"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="language:en"),
    ]])
    await cb.message.edit_text("Dil seçimi / Language selection:", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("language:"))
async def set_language(cb: CallbackQuery):
    lang = (cb.data or "").split(":", 1)[-1]
    async with db_session() as db:
        await db.execute("UPDATE users SET language = ? WHERE user_id = ?", (lang, cb.from_user.id))
        await db.commit()
    await cb.message.edit_text("Dil güncellendi: " + ("Türkçe" if lang == "tr" else "English"))
    await cb.answer()

@router.callback_query(F.data == "menu:help")
async def menu_help(cb: CallbackQuery):
    await help_handler(cb.message)
    await cb.answer()

@router.callback_query(F.data == "menu:broadcast")
async def menu_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    await state.set_state(AdminBroadcastFlow.body)
    await cb.message.answer("📢 Göndermek istediğiniz duyuru metnini yazın:")
    await cb.answer()

@router.message(AdminBroadcastFlow.body)
async def broadcast_body(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    text = message.text.strip()
    if not text: return await message.answer("Mesaj boş olamaz.")
    await state.clear()
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
    count = 0
    for r in rows:
        try:
            await message.bot.send_message(r["user_id"], text)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"Duyuru {count} kişiye gönderildi.")

@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    await admin_handler(cb.message)
    await cb.answer()

@router.message(SupportFlow.body)
async def support_body(message: Message, state: FSMContext):
    await state.clear()
    async with db_session() as db:
        cursor = await db.execute("INSERT INTO support_tickets (user_id, body, created_at) VALUES (?, ?, ?)", (message.from_user.id, message.text, timestamp()))
        tid = cursor.lastrowid
        await db.commit()
    await message.answer(f"Destek talebiniz alındı. (#{tid})")
    await notify_admins(message.bot, f"Yeni destek talebi #{tid}\nKullanıcı: {message.from_user.id}\nMesaj: {message.text}")

@router.message(Command("help"))
async def help_handler(message: Message):
    lines = ["Kullanici komutlari:", "/start - Ana menu", "/balance - Bakiye", "/history - Gecmis", "/daily - Gunluk odul", "/promo KOD", "/referral", "/stars", "\nVIP uyeler %20 indirim alir."]
    await message.answer("\n".join(lines))

@router.message(Command("cancel"))
async def cancel_flow_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Islem iptal edildi.")

@router.message(Command("balance"))
async def balance_handler(message: Message, user_id=None):
    uid = user_id or (message.from_user.id if message.from_user else 0)
    if message.from_user: await ensure_user(message.from_user)
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (uid,))
        row = await cursor.fetchone()
    await message.answer("Bakiyeniz: " + format_money(int(row["balance_cents"]) if row else 0))

@router.message(Command("history"))
async def history_handler(message: Message, user_id=None):
    uid = user_id or (message.from_user.id if message.from_user else 0)
    async with db_session() as db:
        cursor = await db.execute("SELECT order_id, service_name, amount_cents, status FROM orders WHERE user_id = ? ORDER BY order_id DESC LIMIT 10", (uid,))
        rows = await cursor.fetchall()
    if not rows: return await message.answer("Henuz siparisiniz yok.")
    statuses = {"pending": "Bekliyor", "completed": "Tamamlandi", "refunded": "Iade"}
    lines = ["Son siparisleriniz:"]
    for r in rows:
        lines.append(f"#{r['order_id']} {clean_text(r['service_name'], 50)} - {format_money(r['amount_cents'])} - {statuses.get(r['status'], r['status'])}")
    await message.answer("\n".join(lines))

@router.message(Command("referral"))
async def referral_handler(message: Message, user_id=None):
    uid = user_id or (message.from_user.id if message.from_user else 0)
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    await message.answer("Referans linkiniz:\n" + link)

@router.message(Command("daily"))
async def daily_handler(message: Message):
    await ensure_user(message.from_user)
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute("SELECT last_daily_at FROM users WHERE user_id = ?", (message.from_user.id,))
        row = await cursor.fetchone()
        current = now_utc()
        if row and row["last_daily_at"]:
            wait_until = datetime.fromisoformat(row["last_daily_at"]) + timedelta(hours=24)
            if current < wait_until:
                await db.rollback()
                return await message.answer(f"Gunluk odul icin {int((wait_until - current).total_seconds() / 60)} dk bekleyin.")
        await db.execute("UPDATE users SET balance_cents = balance_cents + ?, last_daily_at = ? WHERE user_id = ?", (DAILY_REWARD_CENTS, current.isoformat(), message.from_user.id))
        await add_transaction(db, message.from_user.id, DAILY_REWARD_CENTS, "daily", "Gunluk odul")
        await db.commit()
    except Exception:
        await db.rollback()
    finally:
        await db.close()
    await message.answer("Gunluk odul eklendi: " + format_money(DAILY_REWARD_CENTS))

@router.message(Command("promo"))
async def promo_command(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        await process_promo(message, parts[1])
    else:
        await state.set_state(PromoFlow.code)
        await message.answer("Promo kodunu gonderin.")

@router.message(PromoFlow.code)
async def promo_code_input(message: Message, state: FSMContext):
    await state.clear()
    await process_promo(message, message.text)

@router.message(Command("admin"))
async def admin_handler(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(admin_help_text())
    await send_pending_orders(message)

# ==========================================
# SİPARİŞ & HİZMETLER
# ==========================================
async def show_topup_methods(message):
    keyboard = topup_keyboard()
    if not keyboard:
        await message.answer("Bakiye yukleme kapali.")
        return
    await message.answer("Bakiye yukleme secin:", reply_markup=keyboard)

def topup_keyboard():
    rows = [[InlineKeyboardButton(text=f"{s} ⭐", callback_data=f"star:{s}")] for s, c in STAR_TOPUP_TIERS]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

async def create_order(user_id, service_id, vip_discount=False):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute("SELECT name, price_cents FROM services WHERE service_id = ? AND active = 1", (service_id,))
        service = await cursor.fetchone()
        if not service:
            await db.rollback()
            return "missing", None
        amount = int(service["price_cents"])
        if vip_discount: amount = int(amount * 80 // 100)
        
        cursor = await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ? AND balance_cents >= ?", (amount, user_id, amount))
        if cursor.rowcount != 1:
            await db.rollback()
            return "balance", None
            
        created, expires = now_utc(), now_utc() + timedelta(seconds=CANCEL_SECONDS)
        cursor = await db.execute("INSERT INTO orders (user_id, service_id, service_name, amount_cents, status, created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                                 (user_id, service_id, clean_text(service["name"], 80), amount, created.isoformat(), expires.isoformat()))
        order_id = cursor.lastrowid
        await add_transaction(db, user_id, -amount, "order_reserve", f"Order #{order_id}")
        await db.commit()
        return "created", {"order_id": order_id, "service_name": service["name"], "amount_cents": amount}
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

@router.callback_query(F.data.startswith("s:"))
async def service_detail_handler(cb: CallbackQuery):
    sid = int((cb.data or "").split(":", 1)[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name, description, price_cents FROM services WHERE service_id = ?", (sid,))
        service = await cursor.fetchone()
        cursor = await db.execute("SELECT vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        user = await cursor.fetchone()
    if not service: return await cb.answer("Hizmet yok.", show_alert=True)
    is_vip = user and user["vip_until"] and datetime.fromisoformat(user["vip_until"]) > now_utc()
    price = int(service["price_cents"])
    
    text = f"{clean_text(service['name'])}\n{clean_text(service['description'])}"
    if is_vip:
        text += f"\nNormal: {format_money(price)}\n💎 VIP: {format_money(int(price*0.8))} (%20 indirim)"
    else:
        text += f"\nFiyat: {format_money(price)}"
    await cb.message.answer(text, reply_markup=service_detail_keyboard(sid))
    await cb.answer()

@router.callback_query(F.data.startswith("b:"))
async def buy_service_handler(cb: CallbackQuery):
    sid = int((cb.data or "").split(":", 1)[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        user = await cursor.fetchone()
    is_vip = user and user["vip_until"] and datetime.fromisoformat(user["vip_until"]) > now_utc()
    
    res, order = await create_order(cb.from_user.id, sid, vip_discount=is_vip)
    if res == "balance": return await cb.answer("Yetersiz bakiye.", show_alert=True)
    if res == "missing": return await cb.answer("Hizmet yok.", show_alert=True)
    
    oid = order["order_id"]
    await cb.message.answer(f"Sipariş #{oid} alındı.\nOtomatik teslimat başlatıldı.", reply_markup=order_keyboard(oid))
    
    try:
        msg = await cb.bot.send_message(cb.from_user.id, render_spinner_frame(0, "-----"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True
        ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass

    await notify_admins(cb.bot, f"{'⭐ VIP ' if is_vip else ''}Yeni Sipariş #{oid}\nKullanıcı: {cb.from_user.id}\nTutar: {format_money(order['amount_cents'])}", reply_markup=admin_order_keyboard(oid))
    await cb.answer("Sipariş alındı.")

# ==========================================
# MANUEL NUMARA VE KOD GİRİŞİ (ADMIN)
# ==========================================
@router.callback_query(F.data.startswith("n:"))
async def enter_number_handler(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    oid = int((cb.data or "").split(":", 1)[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT order_id FROM orders WHERE order_id = ? AND status = 'pending'", (oid,))
        order = await cursor.fetchone()
    if not order: return await cb.answer("Sipariş beklemiyor.", show_alert=True)
    
    await state.set_state(AdminEnterNumberFlow.phone)
    await state.update_data(order_id=oid)
    await cb.message.answer(f"Sipariş #{oid}\n\nTelefon numarasını yazın (örn: +905551234567). İptal için /cancel.")
    await cb.answer()

@router.message(AdminEnterNumberFlow.phone)
async def receive_phone_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    phone = clean_text(message.text or "", 32)
    if len(phone) < 5: return await message.answer("Geçerli bir numara yazın.")
    
    data = await state.get_data()
    oid = int(data.get("order_id", 0))
    await state.clear()
    
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ? AND status = 'pending'", (oid,))
        order = await cursor.fetchone()
    if not order: return await message.answer("Sipariş artık beklemiyor.")
    
    ORDER_PHONE_CACHE[oid] = phone
    stop_order_animation(oid)
    
    if oid in ORDER_MESSAGE_REFS:
        chat_id, msg_id = ORDER_MESSAGE_REFS[oid]
        try:
            await message.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=render_spinner_frame(0, f"##{phone}##"), parse_mode="HTML")
        except: pass

    await message.answer(f"Numara kullanıcıya iletildi:\n##{phone}##\n\nSMS kodu geldiğinde '🔑 Kod Gir' butonuna basın.", reply_markup=admin_enter_code_keyboard(oid))

@router.callback_query(F.data.startswith("c:"))
async def enter_code_start_handler(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    oid = int((cb.data or "").split(":", 1)[1])
    await state.set_state(AdminEnterNumberFlow.code)
    await state.update_data(order_id=oid)
    await cb.message.answer(f"Sipariş #{oid}\n\nSMS kodunu yazın (örn: 123456). İptal için /cancel.")
    await cb.answer()

@router.message(AdminEnterNumberFlow.code)
async def receive_code_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    code = clean_text(message.text or "", 16)
    if len(code) < 3: return await message.answer("Geçerli bir kod yazın.")
    
    data = await state.get_data()
    oid = int(data.get("order_id", 0))
    await state.clear()
    
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ? AND status = 'pending'", (oid,))
        order = await cursor.fetchone()
    if not order: return await message.answer("Sipariş artık beklemiyor.")
    
    phone = ORDER_PHONE_CACHE.pop(oid, "")
    
    if oid in ORDER_MESSAGE_REFS:
        chat_id, msg_id = ORDER_MESSAGE_REFS.pop(oid)
        try:
            await message.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=render_spinner_frame(0, f"##{code}##"), parse_mode="HTML")
        except: pass
    
    db = await open_db()
    await db.execute("UPDATE orders SET status = 'completed', completed_at = ? WHERE order_id = ?", (timestamp(), oid))
    await db.commit()
    await db.close()
    
    await notify_user(message.bot, order["user_id"], f"Sipariş #{oid} tamamlandı.\nNumara: ##{phone}##\nKod: ##{code}##")
    await message.answer(f"Sipariş #{oid} tamamlandı.\nNumara: {phone}\nKod: {code}")

# ==========================================
# İPTAL & İADE
# ==========================================
@router.callback_query(F.data.startswith("o:"))
async def order_action_handler(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    action, oid = parts[1], int(parts[2])
    
    if action in ("cancel", "refund"):
        if action == "refund" and not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
        
        db = await open_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT user_id, amount_cents, status FROM orders WHERE order_id = ?", (oid,))
            order = await cursor.fetchone()
            if not order or order["status"] != "pending": return await cb.answer("Sipariş beklemiyor.", show_alert=True)
            if action == "cancel" and order["user_id"] != cb.from_user.id: return await cb.answer("Sizin siparişiniz değil.", show_alert=True)
            
            await db.execute("UPDATE orders SET status = 'refunded' WHERE order_id = ?", (oid,))
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (order["amount_cents"], order["user_id"]))
            await add_transaction(db, order["user_id"], order["amount_cents"], "refund", f"Order #{oid} iptal")
            await db.commit()
        except Exception:
            await db.rollback()
            return await cb.answer("Hata oluştu.")
        finally:
            await db.close()
            
        stop_order_animation(oid)
        ORDER_MESSAGE_REFS.pop(oid, None)
        ORDER_PHONE_CACHE.pop(oid, None)
        
        await cb.message.answer("Sipariş iptal edildi ve iade yapıldı.")
        if action == "refund":
            await notify_user(cb.bot, order["user_id"], f"Sipariş #{oid} iptal edildi. Bakiye iade edildi.")
        await cb.answer("İşlem tamam.")
    else:
        await cb.answer("Geçersiz işlem.")

# ==========================================
# BAKİYE, STAR ÖDEME VE SİPARİŞ SÜRESİ DOLMA
# ==========================================
async def process_promo(message, code):
    async with db_session() as db:
        cursor = await db.execute("SELECT promo_id, bonus_cents, max_uses, current_uses, expires_at FROM promos WHERE code = ? AND active = 1", (code.upper(),))
        promo = await cursor.fetchone()
        if not promo: return await message.answer("Promo bulunamadı.")
        if promo["current_uses"] >= promo["max_uses"]: return await message.answer("Promo limiti doldu.")
        
        cursor = await db.execute("SELECT 1 FROM promo_uses WHERE promo_id = ? AND user_id = ?", (promo["promo_id"], message.from_user.id))
        if await cursor.fetchone(): return await message.answer("Bu promoyu zaten kullandınız.")
        
        await db.execute("INSERT INTO promo_uses (promo_id, user_id, used_at) VALUES (?, ?, ?)", (promo["promo_id"], message.from_user.id, timestamp()))
        await db.execute("UPDATE promos SET current_uses = current_uses + 1 WHERE promo_id = ?", (promo["promo_id"],))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (promo["bonus_cents"], message.from_user.id))
        await add_transaction(db, message.from_user.id, promo["bonus_cents"], "promo", f"Promo {code.upper()}")
        await db.commit()
    await message.answer(f"Promo kullanıldı. Bakiyenize {format_money(promo['bonus_cents'])} eklendi.")

async def expire_orders(bot):
    while True:
        try:
            async with db_session() as db:
                cursor = await db.execute("SELECT order_id, user_id, amount_cents FROM orders WHERE status = 'pending' AND expires_at <= ?", (timestamp(),))
                rows = await cursor.fetchall()
            for r in rows:
                db = await open_db()
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await db.execute("UPDATE orders SET status = 'refunded' WHERE order_id = ?", (r["order_id"],))
                    await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (r["amount_cents"], r["user_id"]))
                    await db.commit()
                except: await db.rollback()
                finally: await db.close()
                
                stop_order_animation(r["order_id"])
                ORDER_MESSAGE_REFS.pop(r["order_id"], None)
                ORDER_PHONE_CACHE.pop(r["order_id"], None)
                try: await bot.send_message(r["user_id"], f"Sipariş #{r['order_id']} süresi doldu. İade edildi.")
                except: pass
        except Exception as e:
            logger.error(f"Expire error: {e}")
        await asyncio.sleep(15)

async def main():
    if not BOT_TOKEN or not ADMIN_IDS: raise RuntimeError("Env eksik.")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(fallback_router)
    await init_db()
    asyncio.create_task(expire_orders(bot))
    print("Bot calisiyor...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())