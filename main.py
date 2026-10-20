# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import html
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, ReplyKeyboardRemove, BufferedInputFile
)

# ================= ENV =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
JOIN_CHANNEL_URL = os.getenv("JOIN_CHANNEL_URL", "").strip()
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "").strip()
STAR_TOPUP_TIERS_RAW = os.getenv("STAR_TOPUP_TIERS", "50:25,100:50,250:125,500:250").strip()
VIP_PRICE_TL = os.getenv("VIP_PRICE_TL", "500").strip()
VIP_DURATION_DAYS = os.getenv("VIP_DURATION_DAYS", "30").strip()
CANCEL_SECONDS = 6 * 60

router = Router()
fallback_router = Router(name="store-fallback")
logger = logging.getLogger("service-order-bot")
ADMIN_IDS = {int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()}

def channel_join_url():
    return JOIN_CHANNEL_URL or (f"https://t.me/{FORCE_JOIN_CHANNEL[1:]}" if FORCE_JOIN_CHANNEL.startswith("@") else "")

def is_admin(uid): return uid in ADMIN_IDS

# ================= HELPERS =================
ORDER_ANIMATIONS = {}
ORDER_MESSAGE_REFS = {}
ORDER_PHONE_CACHE = {}
_bot_username_cache = {"value": ""}

def now_utc(): return datetime.now(timezone.utc)
def timestamp(): return now_utc().isoformat(timespec="seconds")
def today_str(): return now_utc().strftime("%Y-%m-%d")
def esc(s): return html.escape(str(s), quote=False)

def is_expired(t):
    if not t: return True
    try: return datetime.fromisoformat(t) <= now_utc()
    except: return True

def is_vip_user(row):
    if not row: return False
    try: v = row["vip_until"]
    except: return False
    return bool(v) and not is_expired(v)

def time_remaining_str(t):
    try:
        d = int((datetime.fromisoformat(t) - now_utc()).total_seconds())
        if d <= 0: return "⏱️ Doldu"
        m, s = divmod(d, 60); h, m = divmod(m, 60); dy, h = divmod(h, 24)
        if dy > 0: return f"{dy}g {h}sa"
        if h > 0: return f"{h}sa {m}dk"
        if m > 0: return f"{m}dk {s}sn"
        return f"{s}sn"
    except: return "-"

def progress_bar(cur, tot, length=10):
    try:
        if tot <= 0: return "▱" * length
        cur = max(0, min(int(cur), int(tot)))
        f = int(cur / tot * length)
        return "▰" * f + "▱" * (length - f)
    except: return "▱" * length

def format_money(c):
    w, f = divmod(int(c), 100)
    return f"{w}.{str(f).zfill(2)} TL"

def parse_money(v):
    try:
        a = Decimal(str(v).strip().replace(",", "."))
        if a <= 0: raise ValueError
        return int(a * 100)
    except: raise ValueError("Geçerli tutar gir.")

def parse_star_tiers(raw):
    """Sıfır/negatif ve duplicate paketleri filtreler."""
    t = []
    for it in raw.split(","):
        p = it.strip().split(":", 1)
        if len(p) == 2 and p[0].isdigit() and p[1].replace(".", "").isdigit():
            stars = int(p[0]); cents = int(float(p[1]) * 100)
            if stars > 0 and cents > 0:
                t.append((stars, cents))
    return sorted(set(t))

STAR_TOPUP_TIERS = parse_star_tiers(STAR_TOPUP_TIERS_RAW) or [(50, 2500), (100, 5000)]
try: REFERRAL_BONUS_CENTS = parse_money(os.getenv("REFERRAL_BONUS_TL", "5"))
except: REFERRAL_BONUS_CENTS = 500

def generate_circle(text):
    lines = text.split("\n"); ml = max(len(l) for l in lines)
    w = 13 if ml <= 4 else 17 if ml <= 10 else 23 if ml <= 16 else 29 if ml <= 22 else ml + 8
    top = "  " + "•" * (w - 2) + "  "
    empty = "•" + " " * (w - 2) + "•"
    body = ["•" + l.center(w - 2) + "•" for l in lines]
    return f"<pre>{top}\n{empty}\n" + "\n".join(body) + f"\n{empty}\n{top}</pre>"

async def animate_order_status(bot, chat_id, mid, oid):
    try:
        while oid in ORDER_ANIMATIONS:
            try:
                frame = "• • • • •\n•       •\n•  ---  •\n•       •\n• • • • •"
                await bot.edit_message_text(chat_id=chat_id, message_id=mid, text=f"<pre>{frame}</pre>", parse_mode="HTML")
            except TelegramAPIError: pass
            await asyncio.sleep(0.8)
    except asyncio.CancelledError: return

def stop_order_animation(oid): ORDER_ANIMATIONS.pop(oid, None)

async def get_bot_username(bot):
    if _bot_username_cache["value"]: return _bot_username_cache["value"]
    try:
        me = await bot.get_me(); _bot_username_cache["value"] = me.username or ""
    except: pass
    return _bot_username_cache["value"]

def xp_for_level(lv): return lv * 100

async def add_xp(db, uid, xp_amount):
    await db.execute("UPDATE users SET xp = xp + ? WHERE user_id = ?", (xp_amount, uid))
    cur = await db.execute("SELECT xp, level FROM users WHERE user_id = ?", (uid,))
    row = await cur.fetchone()
    if not row: return 0, 0
    xp, lv = row["xp"], row["level"] or 1
    leveled = 0
    while xp >= xp_for_level(lv):
        xp -= xp_for_level(lv); lv += 1; leveled += 1
    await db.execute("UPDATE users SET xp = ?, level = ? WHERE user_id = ?", (xp, lv, uid))
    return leveled, lv

ACHIEVEMENTS = [
    ("first_order", "🛒 İlk Sipariş", "İlk siparişini tamamla"),
    ("ten_orders", "📦 10 Sipariş", "10 sipariş tamamla"),
    ("vip_member", "💎 VIP Üye", "VIP üyelik satın al"),
    ("level_5", "⭐ Seviye 5", "Seviye 5'e ulaş"),
    ("level_10", "🌟 Seviye 10", "Seviye 10'a ulaş"),
    ("streak_7", "🔥 7 Günlük Seri", "7 gün üst üste ödül al"),
]

QUESTS = [
    ("play_1_game", "🎮 1 oyun oyna", 500, "game"),
    ("order_1", "📦 1 sipariş ver", 1000, "order"),
    ("spend_100", "💸 100 TL harca", 2000, "spend"),
    ("invite_1", "👥 1 arkadaş davet et", 1500, "referral"),
]

# ================= STATES =================
class AdminEnterNumberFlow(StatesGroup):
    country_code = State(); phone = State(); code = State()
class SupportFlow(StatesGroup):
    body = State()
class AdminBroadcastFlow(StatesGroup):
    body = State()
class AdminBalanceFlow(StatesGroup):
    user_query = State(); amount = State()
class AdminPromoAddFlow(StatesGroup):
    code = State(); bonus = State(); max_uses = State(); duration = State()
class AdminHazineFlow(StatesGroup):
    total_amount = State(); user_limit = State(); duration = State()
class AdminServiceFlow(StatesGroup):
    name = State()
class AdminProductFlow(StatesGroup):
    service_id = State(); country_code = State(); price = State()
class AdminSpecialServiceFlow(StatesGroup):
    name = State(); price = State(); max_users = State(); duration = State()
class TransferFlow(StatesGroup):
    target = State(); amount = State()
class BankDepositFlow(StatesGroup):
    amount = State(); duration = State()
class BetFlow(StatesGroup):
    amount = State()
class DuelFlow(StatesGroup):
    target = State(); amount = State()
class OrderTrackFlow(StatesGroup):
    order_id = State()

# ================= DB =================
async def open_db():
    db = await aiosqlite.connect(DB_PATH, timeout=10)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db

@asynccontextmanager
async def db_session():
    db = await open_db()
    try: yield db
    finally: await db.close()

async def _ensure_columns(db):
    """Eski DB'ye yeni kolonları ekler (varsa atlar)."""
    migrations = [
        ("users", "xp", "INTEGER DEFAULT 0"),
        ("users", "level", "INTEGER DEFAULT 1"),
        ("users", "daily_streak", "INTEGER DEFAULT 0"),
        ("users", "last_daily", "TEXT"),
        ("users", "total_won", "INTEGER DEFAULT 0"),
        ("users", "total_lost", "INTEGER DEFAULT 0"),
        ("users", "games_played", "INTEGER DEFAULT 0"),
        ("users", "games_won", "INTEGER DEFAULT 0"),
        ("users", "total_spent", "INTEGER DEFAULT 0"),
        ("users", "duels_won", "INTEGER DEFAULT 0"),
        ("users", "spins_total", "INTEGER DEFAULT 0"),
        ("orders", "completed_at", "TEXT"),
        ("orders", "phone", "TEXT"),
        ("orders", "code", "TEXT"),
        ("orders", "service_name", "TEXT"),
        ("orders", "country_code", "TEXT"),
    ]
    for table, col, typedef in migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
        except Exception: pass
    await db.commit()

async def init_db():
    async with db_session() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                balance_cents INTEGER DEFAULT 0, vip_until TEXT, is_blocked INTEGER DEFAULT 0,
                created_at TEXT, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
                daily_streak INTEGER DEFAULT 0, last_daily TEXT,
                total_won INTEGER DEFAULT 0, total_lost INTEGER DEFAULT 0,
                games_played INTEGER DEFAULT 0, games_won INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0, duels_won INTEGER DEFAULT 0,
                spins_total INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS services (service_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS products (product_id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER, country_code TEXT, price_cents INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS special_services (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price_cents INTEGER, max_users INTEGER, current_users INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, msg_id INTEGER);
            CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_type TEXT, item_id INTEGER, item_name TEXT, amount_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, expires_at TEXT, completed_at TEXT, phone TEXT, code TEXT, service_name TEXT, country_code TEXT);
            CREATE TABLE IF NOT EXISTS transactions (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, kind TEXT, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS referrals (referral_id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_user_id INTEGER UNIQUE, reward_cents INTEGER, rewarded_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS star_payment_intents (payload TEXT PRIMARY KEY, user_id INTEGER, stars_amount INTEGER, credit_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS support_tickets (ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, bonus_cents INTEGER, max_uses INTEGER, current_uses INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS promo_uses (code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
            CREATE TABLE IF NOT EXISTS hazine (id INTEGER PRIMARY KEY AUTOINCREMENT, total_cents INTEGER, remaining_cents INTEGER, max_users INTEGER, current_users INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS hazine_claims (hazine_id INTEGER, user_id INTEGER, amount_cents INTEGER, PRIMARY KEY(hazine_id, user_id));
            CREATE TABLE IF NOT EXISTS bank_deposits (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, interest_pct INTEGER, deposited_at TEXT, matures_at TEXT, withdrawn INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS achievements (user_id INTEGER, achievement_id TEXT, unlocked_at TEXT, PRIMARY KEY(user_id, achievement_id));
            CREATE TABLE IF NOT EXISTS quests_progress (user_id INTEGER, quest_id TEXT, date TEXT, progress INTEGER DEFAULT 0, completed INTEGER DEFAULT 0, claimed INTEGER DEFAULT 0, PRIMARY KEY(user_id, quest_id, date));
            CREATE TABLE IF NOT EXISTS game_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT, bet_cents INTEGER, win_cents INTEGER, result TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS favorites (user_id INTEGER, product_id INTEGER, PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS duels (id INTEGER PRIMARY KEY AUTOINCREMENT, challenger_id INTEGER, opponent_id INTEGER, bet_cents INTEGER, status TEXT DEFAULT 'pending', winner_id INTEGER, created_at TEXT);
            CREATE TABLE IF NOT EXISTS cart (user_id INTEGER, product_id INTEGER, PRIMARY KEY(user_id, product_id));
            INSERT OR IGNORE INTO settings (key, value) VALUES 
                ('vip_enabled', '1'), ('vip_price_tl', '500'), ('vip_duration_days', '30'), ('ref_require_order', '1');
        """)
        await db.commit()
    async with db_session() as db:
        await _ensure_columns(db)

async def get_setting(key, db=None):
    if db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        r = await cur.fetchone(); return r["value"] if r else None
    async with db_session() as d:
        cur = await d.execute("SELECT value FROM settings WHERE key = ?", (key,))
        r = await cur.fetchone(); return r["value"] if r else None

async def is_feature_enabled(f): return (await get_setting(f)) == "1"

async def ensure_user(user):
    async with db_session() as db:
        try:
            await db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user.id, timestamp()))
            await db.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", (user.username or "", user.first_name or "User", user.id))
            await db.commit()
        except: pass

async def add_transaction(db, uid, amt, kind, note):
    await db.execute("INSERT INTO transactions (user_id, amount_cents, kind, note, created_at) VALUES (?, ?, ?, ?, ?)", (uid, amt, kind, note, timestamp()))

async def get_display_name(uid):
    async with db_session() as db:
        cur = await db.execute("SELECT username, first_name FROM users WHERE user_id = ?", (uid,))
        r = await cur.fetchone()
    if r:
        if r["username"]: return f"@{r['username']}"
        return f"{r['first_name']} (ID: {uid})"
    return f"ID: {uid}"

async def notify_admins(bot, text, kb=None):
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, text, reply_markup=kb, parse_mode="Markdown")
        except: pass

async def safe_edit(cb, text, kb=None, parse_mode=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except TelegramAPIError:
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode=parse_mode)
        except: pass

async def unlock_achievement(bot, uid, aid):
    if not uid: return False
    async with db_session() as db:
        cur = await db.execute("SELECT 1 FROM achievements WHERE user_id = ? AND achievement_id = ?", (uid, aid))
        if await cur.fetchone(): return False
        await db.execute("INSERT INTO achievements (user_id, achievement_id, unlocked_at) VALUES (?, ?, ?)", (uid, aid, timestamp()))
        await db.commit()
    a = next((x for x in ACHIEVEMENTS if x[0] == aid), None)
    if a:
        try: await bot.send_message(uid, f"🏆 **Başarım!**\n\n{a[1]}\n_{a[2]}_", parse_mode="Markdown")
        except: pass
    return True

async def progress_quest(bot, uid, qtype, amount=1):
    qdate = today_str()
    async with db_session() as db:
        for qid, qlabel, qreward, qtype_ in QUESTS:
            if qtype_ != qtype: continue
            target = {"play_1_game": 1, "order_1": 1, "spend_100": 10000, "invite_1": 1}[qid]
            cur = await db.execute("SELECT progress, completed FROM quests_progress WHERE user_id = ? AND quest_id = ? AND date = ?", (uid, qid, qdate))
            r = await cur.fetchone()
            if r:
                if r["completed"]: continue
                np = r["progress"] + amount
                comp = 1 if np >= target else 0
                await db.execute("UPDATE quests_progress SET progress = ?, completed = ? WHERE user_id = ? AND quest_id = ? AND date = ?", (np, comp, uid, qid, qdate))
            else:
                comp = 1 if amount >= target else 0
                await db.execute("INSERT INTO quests_progress (user_id, quest_id, date, progress, completed) VALUES (?, ?, ?, ?, ?)", (uid, qid, qdate, amount, comp))
        await db.commit()

async def award_xp(bot, db, uid, xp_amount):
    leveled, lv = await add_xp(db, uid, xp_amount)
    if leveled > 0:
        await db.commit()
        try: await bot.send_message(uid, f"⬆️ **Seviye Atladın!** Yeni: **{lv}**", parse_mode="Markdown")
        except: pass
        if lv >= 5: await unlock_achievement(bot, uid, "level_5")
        if lv >= 10: await unlock_achievement(bot, uid, "level_10")

# ================= KEYBOARDS =================
def main_keyboard(uid):
    rows = [
        [InlineKeyboardButton(text="🛍️ Market", callback_data="menu:catalog"), InlineKeyboardButton(text="💰 Bakiyem", callback_data="menu:balance")],
        [InlineKeyboardButton(text="📦 Siparişlerim", callback_data="menu:orders"), InlineKeyboardButton(text="🎯 Günlük", callback_data="menu:daily")],
        [InlineKeyboardButton(text="🎮 Oyunlar", callback_data="menu:games"), InlineKeyboardButton(text="🏆 Liderlik", callback_data="menu:leaderboard")],
        [InlineKeyboardButton(text="👤 Profilim", callback_data="menu:profile"), InlineKeyboardButton(text="📊 İstatistikler", callback_data="menu:stats")],
        [InlineKeyboardButton(text="⭐ Yıldız Yükle", callback_data="menu:topup"), InlineKeyboardButton(text="💎 VIP", callback_data="menu:vip")],
        [InlineKeyboardButton(text="🎯 Görevler", callback_data="menu:quests"), InlineKeyboardButton(text="🏆 Başarımlar", callback_data="menu:achievements")],
        [InlineKeyboardButton(text="💼 Banka", callback_data="menu:bank"), InlineKeyboardButton(text="🤝 Transfer", callback_data="menu:transfer")],
        [InlineKeyboardButton(text="👥 Davet", callback_data="menu:referral"), InlineKeyboardButton(text="🎁 Fırsatlar", callback_data="menu:active")],
        [InlineKeyboardButton(text="🎫 Kod Takip", callback_data="menu:track"), InlineKeyboardButton(text="📞 Kod Geçmişi", callback_data="menu:code_history")],
        [InlineKeyboardButton(text="🛒 Sepetim", callback_data="menu:cart"), InlineKeyboardButton(text="⭐ Favoriler", callback_data="menu:favorites")],
        [InlineKeyboardButton(text="📜 İşlem Geçmişi", callback_data="menu:history"), InlineKeyboardButton(text="💬 Destek", callback_data="menu:support")],
        [InlineKeyboardButton(text="ℹ️ Yardım", callback_data="menu:help")]
    ]
    if is_admin(uid): rows.append([InlineKeyboardButton(text="🛠️ Admin Paneli", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_to_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back")]])

def topup_keyboard():
    rows = [[InlineKeyboardButton(text=f"{s} ⭐ → {format_money(c)}", callback_data=f"star:{s}")] for s, c in STAR_TOPUP_TIERS]
    rows.append([InlineKeyboardButton(text="🔙 Geri", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def games_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Çark", callback_data="game:wheel"), InlineKeyboardButton(text="🎰 Slot", callback_data="game:slot")],
        [InlineKeyboardButton(text="🎲 Zar", callback_data="game:dice"), InlineKeyboardButton(text="🪙 Yazı Tura", callback_data="game:coin")],
        [InlineKeyboardButton(text="🎁 Sandık", callback_data="game:chest"), InlineKeyboardButton(text="⚔️ Düello", callback_data="game:duel")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back")]
    ])

def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Dashboard", callback_data="admin:dashboard"), InlineKeyboardButton(text="👥 Kullanıcılar", callback_data="admin:total_users")],
        [InlineKeyboardButton(text="➕ Bakiye Ekle", callback_data="admin:bal_add"), InlineKeyboardButton(text="➖ Bakiye Sil", callback_data="admin:bal_rem")],
        [InlineKeyboardButton(text="📦 Hizmet Ekle", callback_data="admin:service_add"), InlineKeyboardButton(text="🛒 Ürün Ekle", callback_data="admin:product_add")],
        [InlineKeyboardButton(text="✨ Özel Hizmet Aç", callback_data="admin:special_add")],
        [InlineKeyboardButton(text="🎁 Promo Ekle", callback_data="admin:promo_add"), InlineKeyboardButton(text="💰 Hazine Dağıt", callback_data="admin:hazine_add")],
        [InlineKeyboardButton(text="📋 Promolar", callback_data="admin:promos_list"), InlineKeyboardButton(text="📋 Hazineler", callback_data="admin:hazines_list")],
        [InlineKeyboardButton(text="📋 Özel Hizmetler", callback_data="admin:specials_list"), InlineKeyboardButton(text="🔄 Kanal Sync", callback_data="admin:sync_channels")],
        [InlineKeyboardButton(text="⚙️ Ayarlar", callback_data="admin:settings"), InlineKeyboardButton(text="📢 Duyuru", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back")]
    ])

def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VIP Sistemi (Aç/Kapat)", callback_data="toggle:vip_enabled")],
        [InlineKeyboardButton(text="Referans: İlk Sipariş Şartı", callback_data="toggle:ref_require_order")],
        [InlineKeyboardButton(text="🔙 Admin", callback_data="menu:admin")]
    ])

# ================= PROMO/HAZINE KANAL =================
def build_promo_text(p):
    used = p["current_uses"] or 0; tot = p["max_uses"] or 1
    rem = max(0, tot - used); tl = time_remaining_str(p["expires_at"])
    bar = progress_bar(used, tot)
    dep = used >= tot; exp = is_expired(p["expires_at"])
    head = "❌ <b>TÜKENDİ</b>" if dep else ("⏱️ <b>SÜRE DOLDU</b>" if exp else "🎁 <b>PROMO — AKTİF</b>")
    text = (f"╭──────────────────────────────╮\n   {head}\n╰──────────────────────────────╯\n\n"
            f"🔑 <b>Kod:</b> <code>{esc(p['code'])}</code>\n💰 <b>Ödül:</b> <code>{format_money(p['bonus_cents'])}</code>\n\n"
            f"📊 <b>Kullanım</b>\n├ Kullanıldı: <b>{used}</b>/{tot}\n├ Kalan: <b>{rem}</b>\n└ {bar}\n\n"
            f"⏳ <b>Kalan:</b> {tl}\n\n")
    text += "🔒 <i>Kontenjan doldu.</i>" if dep else ("🔒 <i>Süre doldu.</i>" if exp else "⚡ <i>Hemen al!</i>")
    return text, dep, exp

def build_hazine_text(h):
    ta = h["total_cents"]; ra = max(0, h["remaining_cents"])
    used = h["current_users"] or 0; mu = h["max_users"] or 1
    ru = max(0, mu - used); tl = time_remaining_str(h["expires_at"])
    bar = progress_bar(used, mu)
    dep = used >= mu or ra <= 0; exp = is_expired(h["expires_at"])
    head = "❌ <b>TÜKENDİ</b>" if dep else ("⏱️ <b>SÜRE DOLDU</b>" if exp else "💰 <b>HAZİNE — AÇIK</b>")
    text = (f"╭──────────────────────────────╮\n   {head}\n╰──────────────────────────────╯\n\n"
            f"💎 <b>Toplam:</b> <code>{format_money(ta)}</code>\n💵 <b>Kalan:</b> <code>{format_money(ra)}</code>\n\n"
            f"👥 <b>Kontenjan</b>\n├ Kullanan: <b>{used}</b>/{mu}\n├ Kalan: <b>{ru}</b>\n└ {bar}\n\n"
            f"⏳ <b>Kalan:</b> {tl}\n\n")
    text += "🔒 <i>Tükendi.</i>" if dep else ("🔒 <i>Süre doldu.</i>" if exp else "⚡ <i>Rastgele pay kap!</i>")
    return text, dep, exp

async def refresh_promo(bot, code):
    if not PROMO_CHANNEL_ID: return
    try:
        async with db_session() as db:
            cur = await db.execute("SELECT * FROM promos WHERE code = ?", (code,))
            p = await cur.fetchone()
        if not p or not p["message_id"]: return
        text, dep, exp = build_promo_text(p)
        kb = None
        if not dep and not exp:
            u = await get_bot_username(bot)
            if u: kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Kullan", url=f"https://t.me/{u}?start=promo_{code}")]])
        try: await bot.edit_message_text(PROMO_CHANNEL_ID, p["message_id"], text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest: pass
        if (dep or exp) and p["active"]:
            async with db_session() as db:
                await db.execute("UPDATE promos SET active = 0 WHERE code = ?", (code,))
                await db.commit()
    except Exception as e: logger.error(f"refresh promo: {e}")

async def refresh_hazine(bot, hid):
    if not PROMO_CHANNEL_ID: return
    try:
        async with db_session() as db:
            cur = await db.execute("SELECT * FROM hazine WHERE id = ?", (hid,))
            h = await cur.fetchone()
        if not h or not h["message_id"]: return
        text, dep, exp = build_hazine_text(h)
        kb = None
        if not dep and not exp:
            u = await get_bot_username(bot)
            if u: kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Kap", url=f"https://t.me/{u}?start=hazine_{hid}")]])
        try: await bot.edit_message_text(PROMO_CHANNEL_ID, h["message_id"], text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest: pass
        if (dep or exp) and h["active"]:
            async with db_session() as db:
                await db.execute("UPDATE hazine SET active = 0 WHERE id = ?", (hid,))
                await db.commit()
    except Exception as e: logger.error(f"refresh hazine: {e}")

async def channel_updater(bot):
    await asyncio.sleep(15)
    while True:
        try:
            if PROMO_CHANNEL_ID:
                async with db_session() as db:
                    cur = await db.execute("SELECT code FROM promos WHERE active = 1 AND message_id IS NOT NULL")
                    for r in await cur.fetchall():
                        await refresh_promo(bot, r["code"]); await asyncio.sleep(1.2)
                async with db_session() as db:
                    cur = await db.execute("SELECT id FROM hazine WHERE active = 1 AND message_id IS NOT NULL")
                    for r in await cur.fetchall():
                        await refresh_hazine(bot, r["id"]); await asyncio.sleep(1.2)
        except Exception as e: logger.error(f"channel_updater: {e}")
        await asyncio.sleep(60)

# ================= AUTO CLEANUP =================
async def background_cleanup(bot):
    """Süresi dolmuş sipariş/hazine/promo/özel hizmetleri kapat + iade."""
    await asyncio.sleep(20)
    while True:
        try:
            expired_orders = []
            async with db_session() as db:
                cur = await db.execute(
                    "SELECT order_id, user_id, amount_cents FROM orders WHERE status = 'pending' AND expires_at < ?",
                    (timestamp(),)
                )
                expired_orders = await cur.fetchall()
                for o in expired_orders:
                    cur2 = await db.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE order_id = ? AND status = 'pending'",
                        (o["order_id"],)
                    )
                    if cur2.rowcount > 0:
                        await db.execute(
                            "UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?",
                            (o["amount_cents"], o["user_id"])
                        )
                        await add_transaction(db, o["user_id"], o["amount_cents"], "auto_refund", f"#{o['order_id']} süre doldu")
                        try:
                            await bot.send_message(
                                o["user_id"],
                                f"⏱️ **Sipariş #{o['order_id']} süresi doldu**\n\n💰 `{format_money(o['amount_cents'])}` iade edildi.",
                                parse_mode="Markdown"
                            )
                        except: pass
                        stop_order_animation(o["order_id"])
                await db.execute("UPDATE hazine SET active = 0 WHERE active = 1 AND expires_at < ?", (timestamp(),))
                await db.execute("UPDATE promos SET active = 0 WHERE active = 1 AND expires_at < ?", (timestamp(),))
                await db.execute("UPDATE special_services SET active = 0 WHERE active = 1 AND expires_at < ?", (timestamp(),))
                await db.commit()
            if expired_orders:
                logger.info(f"Auto-refund: {len(expired_orders)} sipariş")
        except Exception as e:
            logger.error(f"background_cleanup: {e}")
        await asyncio.sleep(30)

# ================= MIDDLEWARE =================
class JoinMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id): return await handler(event, data)
        if isinstance(event, Message) and event.successful_payment: return await handler(event, data)
        if isinstance(event, PreCheckoutQuery): return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_join": return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"): await ensure_user(user)
        if not FORCE_JOIN_CHANNEL: return await handler(event, data)
        try:
            m = await event.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user.id)
            ok = m.status in ("creator", "administrator", "member")
        except: ok = None
        if ok is False:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📣 Katıl", url=channel_join_url())],
                [InlineKeyboardButton(text="✅ Kontrol Et", callback_data="check_join")]
            ])
            if isinstance(event, Message): await event.answer("👋 Önce kanala katıl.", reply_markup=kb)
            elif isinstance(event, CallbackQuery): await event.answer("Önce kanala katıl.", show_alert=True)
            return None
        return await handler(event, data)

router.message.outer_middleware(JoinMiddleware())
router.callback_query.outer_middleware(JoinMiddleware())

# ================= WELCOME =================
async def build_welcome(uid, fname):
    async with db_session() as db:
        cur = await db.execute("SELECT balance_cents, vip_until, level, xp, daily_streak FROM users WHERE user_id = ?", (uid,))
        u = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE user_id = ? AND status = 'completed'", (uid,))
        oc = (await cur.fetchone())["c"]
    vip = "Aktif ✅" if is_vip_user(u) else "Yok ❌"
    xpn = xp_for_level(u["level"] if u else 1)
    return (f"🌟 **Hoş Geldin, {fname}!**\n\n"
            f"💳 **Profil:**\n"
            f"├ 💰 Bakiye: `{format_money(u['balance_cents'] if u else 0)}`\n"
            f"├ 📦 Sipariş: `{oc}`\n"
            f"├ ⭐ Seviye: `{u['level'] if u else 1}` ({u['xp'] if u else 0}/{xpn} XP)\n"
            f"├ 🔥 Seri: `{u['daily_streak'] if u else 0} gün`\n"
            f"└ 💎 VIP: `{vip}`\n\n"
            f"🚀 *7/24 otomatik teslimat.*\n\n"
            f"👇 Menüden işlem seç:")

@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    if not message.from_user: return
    uid = message.from_user.id
    await state.clear()
    try: await message.answer("🔄 Hazırlanıyor...", reply_markup=ReplyKeyboardRemove())
    except: pass
    async with db_session() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,))
        is_new = await cur.fetchone() is None
    if is_new: await ensure_user(message.from_user)
    args = message.text.split()
    if len(args) > 1:
        param = args[1]
        if param.startswith("ref_"):
            try:
                rid = int(param[4:])
                if rid != uid and is_new:
                    async with db_session() as db:
                        cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (rid,))
                        if await cur.fetchone():
                            await db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_user_id, reward_cents, created_at) VALUES (?, ?, ?, ?)", (rid, uid, REFERRAL_BONUS_CENTS, timestamp()))
                            await db.commit()
                            await progress_quest(message.bot, rid, "referral", 1)
                    if (await get_setting("ref_require_order")) == "0":
                        async with db_session() as db:
                            cur = await db.execute("SELECT 1 FROM referrals WHERE referred_user_id = ? AND rewarded_at IS NULL", (uid,))
                            if await cur.fetchone():
                                await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (REFERRAL_BONUS_CENTS, rid))
                                await db.execute("UPDATE referrals SET rewarded_at = ? WHERE referred_user_id = ?", (timestamp(), uid))
                                await add_transaction(db, rid, REFERRAL_BONUS_CENTS, "referral", "Davet ödülü")
                                await db.commit()
            except: pass
        elif param.startswith("promo_"): await process_promo_claim(message, param[6:].upper())
        elif param.startswith("hazine_"):
            try: await process_hazine_claim(message, int(param[7:]))
            except: pass
        elif param.startswith("special_"):
            try: await process_special_claim(message, int(param[8:]), message.from_user)
            except: pass
    text = await build_welcome(uid, message.from_user.first_name)
    await message.answer(text, reply_markup=main_keyboard(uid), parse_mode="Markdown")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("İptal edildi.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "menu:back")
async def back_to_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text = await build_welcome(cb.from_user.id, cb.from_user.first_name)
    await safe_edit(cb, text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
    await cb.answer()

# ================= CLAIM =================
async def process_promo_claim(message, code):
    async with db_session() as db:
        cur = await db.execute(
            "UPDATE promos SET current_uses = current_uses + 1 WHERE code = ? AND active = 1 AND current_uses < max_uses AND expires_at > ?",
            (code, timestamp())
        )
        if cur.rowcount == 0:
            cur = await db.execute("SELECT * FROM promos WHERE code = ?", (code,))
            p = await cur.fetchone()
            if not p: return await message.answer("❌ Geçersiz kod.")
            if not p["active"] or p["current_uses"] >= p["max_uses"]: return await message.answer("❌ Limit doldu.")
            if is_expired(p["expires_at"]): return await message.answer("❌ Süre doldu.")
            return await message.answer("❌ Kullanılamıyor.")
        try:
            await db.execute("INSERT INTO promo_uses (code, user_id) VALUES (?, ?)", (code, message.from_user.id))
        except:
            await db.execute("UPDATE promos SET current_uses = current_uses - 1 WHERE code = ?", (code,))
            await db.commit()
            return await message.answer("❌ Bu kodu zaten kullandın.")
        cur = await db.execute("SELECT bonus_cents FROM promos WHERE code = ?", (code,))
        bonus = (await cur.fetchone())["bonus_cents"]
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (bonus, message.from_user.id))
        await add_transaction(db, message.from_user.id, bonus, "promo", f"Promo: {code}")
        await award_xp(message.bot, db, message.from_user.id, 5)
        await db.commit()
    await message.answer(f"🎁 **Promo!**\n\n✅ +`{format_money(bonus)}`", parse_mode="Markdown")
    await refresh_promo(message.bot, code)

async def process_hazine_claim(message, hid):
    async with db_session() as db:
        cur = await db.execute(
            "UPDATE hazine SET current_users = current_users + 1 WHERE id = ? AND active = 1 AND current_users < max_users AND remaining_cents > 0 AND expires_at > ?",
            (hid, timestamp())
        )
        if cur.rowcount == 0:
            return await message.answer("❌ Hazine tükendi veya süresi doldu.")
        try:
            await db.execute("INSERT INTO hazine_claims (hazine_id, user_id, amount_cents) VALUES (?, ?, 0)", (hid, message.from_user.id))
        except:
            await db.execute("UPDATE hazine SET current_users = current_users - 1 WHERE id = ?", (hid,))
            await db.commit()
            return await message.answer("❌ Bu hazineden zaten pay aldın.")
        cur = await db.execute("SELECT remaining_cents, max_users, current_users FROM hazine WHERE id = ?", (hid,))
        h = await cur.fetchone()
        slots = max(1, h["max_users"] - h["current_users"] + 1)
        avg = h["remaining_cents"] // slots
        if avg <= 0:
            await db.execute("UPDATE hazine SET current_users = current_users - 1 WHERE id = ?", (hid,))
            await db.execute("DELETE FROM hazine_claims WHERE hazine_id = ? AND user_id = ?", (hid, message.from_user.id))
            await db.commit()
            return await message.answer("❌ Hazine tükendi.")
        low = max(1, avg // 2); high = min(max(low + 1, avg * 3 // 2), h["remaining_cents"])
        amt = random.randint(low, high)
        await db.execute("UPDATE hazine SET remaining_cents = remaining_cents - ? WHERE id = ?", (amt, hid))
        await db.execute("UPDATE hazine_claims SET amount_cents = ? WHERE hazine_id = ? AND user_id = ?", (amt, hid, message.from_user.id))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amt, message.from_user.id))
        await add_transaction(db, message.from_user.id, amt, "hazine", f"Hazine #{hid}")
        cur = await db.execute("SELECT current_users, max_users, remaining_cents FROM hazine WHERE id = ?", (hid,))
        h2 = await cur.fetchone()
        if h2["current_users"] >= h2["max_users"] or h2["remaining_cents"] <= 0:
            await db.execute("UPDATE hazine SET active = 0 WHERE id = ?", (hid,))
        await db.commit()
    await message.answer(f"💰 **Hazine Payı!**\n\n✅ +`{format_money(amt)}`", parse_mode="Markdown")
    await refresh_hazine(message.bot, hid)

async def process_special_claim(message, spid, user):
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM special_services WHERE id = ?", (spid,))
        sp = await cur.fetchone()
        if not sp or not sp["active"] or is_expired(sp["expires_at"]): return await message.answer("❌ Süresi dolmuş.")
        cur = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (user.id,))
        u = await cur.fetchone()
        price = int(sp["price_cents"] * 0.8) if is_vip_user(u) else sp["price_cents"]
        cur = await db.execute(
            "UPDATE special_services SET current_users = current_users + 1 WHERE id = ? AND active = 1 AND current_users < max_users AND expires_at > ?",
            (spid, timestamp())
        )
        if cur.rowcount == 0:
            return await message.answer("❌ Kontenjan doldu.")
        cur = await db.execute(
            "UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ? AND balance_cents >= ?",
            (price, user.id, price)
        )
        if cur.rowcount == 0:
            await db.execute("UPDATE special_services SET current_users = current_users - 1 WHERE id = ?", (spid,))
            await db.commit()
            return await message.answer("❌ Yetersiz bakiye.")
        cur = await db.execute(
            "INSERT INTO orders (user_id, item_type, item_id, item_name, amount_cents, status, created_at, expires_at, service_name) VALUES (?, 'special', ?, ?, ?, 'pending', ?, ?, ?)",
            (user.id, spid, sp["name"], price, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat(), sp["name"])
        )
        oid = cur.lastrowid
        await add_transaction(db, user.id, -price, "order", f"Sipariş #{oid}")
        cur = await db.execute("SELECT current_users, max_users, msg_id FROM special_services WHERE id = ?", (spid,))
        sp_after = await cur.fetchone()
        if sp_after["current_users"] >= sp_after["max_users"]:
            await db.execute("UPDATE special_services SET active = 0 WHERE id = ?", (spid,))
            if sp_after["msg_id"] and PROMO_CHANNEL_ID:
                try: await message.bot.delete_message(PROMO_CHANNEL_ID, sp_after["msg_id"])
                except: pass
        await db.commit()
    await message.answer(f"🎉 **Satın Alındı!**\n\n✅ {sp['name']}\n⏳ Admin numara iletecek.")
    try:
        msg = await message.bot.send_message(user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True
        ORDER_MESSAGE_REFS[oid] = (user.id, msg.message_id)
        asyncio.create_task(animate_order_status(message.bot, user.id, msg.message_id, oid))
    except: pass
    await notify_admins(message.bot, f"🔔 **Sipariş #{oid}**\n👤 {await get_display_name(user.id)}\n📦 {sp['name']}\n💵 {format_money(price)}",
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara", callback_data=f"n:{oid}")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}")]]))

@router.callback_query(F.data == "check_join")
async def check_join(cb: CallbackQuery):
    if not FORCE_JOIN_CHANNEL:
        text = await build_welcome(cb.from_user.id, cb.from_user.first_name)
        await safe_edit(cb, text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
        return await cb.answer("Kanal yok.")
    try:
        m = await cb.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=cb.from_user.id)
        ok = m.status in ("creator", "administrator", "member")
    except: ok = False
    if ok:
        text = await build_welcome(cb.from_user.id, cb.from_user.first_name)
        await safe_edit(cb, text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
        await cb.answer("✅ Onaylandı!", show_alert=True)
    else: await cb.answer("❌ Katılmadın.", show_alert=True)

# ================= BALANCE / HISTORY / STATS / PROFILE / ORDERS =================
@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        r = await cur.fetchone()
    vip = "Aktif ✅" if is_vip_user(r) else "Yok ❌"
    text = f"💰 **Cüzdan**\n\n🔹 Bakiye: `{format_money(r['balance_cents'] if r else 0)}`\n🔹 VIP: `{vip}`"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:history")
async def menu_history(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT amount_cents, kind, note, created_at FROM transactions WHERE user_id = ? ORDER BY transaction_id DESC LIMIT 20", (cb.from_user.id,))
        txs = await cur.fetchall()
    if not txs: return await safe_edit(cb, "📜 İşlem yok.", reply_markup=back_to_menu_kb())
    text = "📜 **İşlem Geçmişi** (Son 20)\n\n"
    for t in txs:
        s = "➕" if t["amount_cents"] > 0 else "➖"
        text += f"{s} `{format_money(abs(t['amount_cents']))}` — {t['kind']}\n   _{t['note']}_ • {t['created_at'][:16]}\n"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:stats")
async def menu_stats(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT games_played, games_won, total_won, total_lost, total_spent, spins_total FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE user_id = ?", (cb.from_user.id,))
        ot = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE user_id = ? AND status = 'completed'", (cb.from_user.id,))
        od = (await cur.fetchone())["c"]
    wr = (u["games_won"] / u["games_played"] * 100) if u["games_played"] else 0
    net = (u["total_won"] or 0) - (u["total_lost"] or 0)
    text = (f"📊 **İstatistikler**\n\n"
            f"📦 **Siparişler**\n├ Toplam: `{ot}`\n├ Tamamlanan: `{od}`\n└ Başarı: `%{(od/ot*100 if ot else 0):.0f}`\n\n"
            f"🎮 **Oyunlar**\n├ Oynanan: `{u['games_played'] or 0}`\n├ Kazanılan: `{u['games_won'] or 0}` (%{wr:.0f})\n└ Net: `{format_money(net)}`\n\n"
            f"🎡 Çark: `{u['spins_total'] or 0}`\n"
            f"🛒 Harcama: `{format_money(u['total_spent'] or 0)}`")
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:profile")
async def menu_profile(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) as c FROM achievements WHERE user_id = ?", (cb.from_user.id,))
        ac = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE user_id = ? AND status = 'completed'", (cb.from_user.id,))
        oc = (await cur.fetchone())["c"]
    xpn = xp_for_level(u["level"] or 1)
    bar = progress_bar(u["xp"] or 0, xpn, 15)
    text = (f"👤 **Profil**\n\n"
            f"🆔 `{cb.from_user.id}`\n"
            f"🏅 Lv. **{u['level'] or 1}** {bar} ({u['xp'] or 0}/{xpn})\n"
            f"🎖️ Başarım: `{ac}/{len(ACHIEVEMENTS)}`\n"
            f"📦 Sipariş: `{oc}`\n"
            f"🔥 Seri: `{u['daily_streak'] or 0} gün`")
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:orders")
async def menu_orders(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT order_id, item_name, amount_cents, status FROM orders WHERE user_id = ? ORDER BY order_id DESC LIMIT 15", (cb.from_user.id,))
        orders = await cur.fetchall()
    if not orders: return await safe_edit(cb, "📦 Henüz sipariş yok.", reply_markup=back_to_menu_kb())
    smap = {"pending": "⏳", "completed": "✅", "refunded": "❌", "cancelled": "❌"}
    text = "📦 **Siparişlerim** (Son 15)\n\n"
    for o in orders:
        text += f"`#{o['order_id']}` • {o['item_name']} • `{format_money(o['amount_cents'])}` • {smap.get(o['status'], o['status'])}\n"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:track")
async def menu_track(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(OrderTrackFlow.order_id)
    await safe_edit(cb, "🎫 **Sipariş Takip**\n\nSipariş ID'sini yaz (örn: `12`):\n\n❌ İptal: /cancel", reply_markup=back_to_menu_kb()); await cb.answer()

@router.message(OrderTrackFlow.order_id)
async def track_order(message: Message, state: FSMContext):
    if not message.text.strip().isdigit(): return await message.answer("❌ Sadece sayı.")
    oid = int(message.text.strip()); await state.clear()
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ?", (oid, message.from_user.id))
        o = await cur.fetchone()
    if not o: return await message.answer("❌ Sipariş bulunamadı.", reply_markup=back_to_menu_kb())
    smap = {"pending": "⏳ Beklemede", "completed": "✅ Tamamlandı", "refunded": "❌ İade", "cancelled": "❌ İptal"}
    text = f"🎫 **Sipariş #{oid}**\n\n📦 {o['item_name']}\n💵 `{format_money(o['amount_cents'])}`\n📊 {smap.get(o['status'], o['status'])}\n"
    if o["phone"]: text += f"\n📱 Numara: `{o['phone']}`"
    if o["code"]: text += f"\n🔑 Kod: `{o['code']}`"
    if o["created_at"]: text += f"\n📅 {o['created_at'][:16]}"
    await message.answer(text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "menu:code_history")
async def menu_code_history(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT order_id, item_name, phone, code, completed_at FROM orders WHERE user_id = ? AND code IS NOT NULL ORDER BY order_id DESC LIMIT 10", (cb.from_user.id,))
        orders = await cur.fetchall()
    if not orders: return await safe_edit(cb, "📞 Henüz tamamlanan sipariş yok.", reply_markup=back_to_menu_kb())
    text = "📞 **Kod Geçmişi**\n\n"
    for o in orders:
        text += f"**#{o['order_id']}** {o['item_name']}\n├ 📱 `{o['phone'] or '-'}`\n├ 🔑 `{o['code']}`\n└ {o['completed_at'][:16] if o['completed_at'] else '-'}\n\n"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

# ================= DAILY =================
DAILY_REWARDS = [500, 1000, 2000, 3500, 5000, 7500, 15000]

@router.callback_query(F.data == "menu:daily")
async def menu_daily(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT daily_streak, last_daily FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
    streak = u["daily_streak"] or 0
    can = True
    if u["last_daily"]:
        try:
            if (now_utc() - datetime.fromisoformat(u["last_daily"])).total_seconds() < 20 * 3600: can = False
        except: pass
    if not can:
        return await safe_edit(cb, f"🎯 **Günlük**\n\n✅ Bugün aldın!\n🔥 Seri: `{streak} gün`\n\n⏳ 20 saat sonra.", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    nxt = min(streak, 6); rw = DAILY_REWARDS[nxt]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 {format_money(rw)} Al", callback_data="daily:claim")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back")]
    ])
    await safe_edit(cb, f"🎯 **Günlük**\n\n🔥 Seri: `{streak} gün`\n🎁 Bugün: `{format_money(rw)}`\n📅 Yarın: `{format_money(DAILY_REWARDS[min(streak+1, 6)])}`\n\n20 saatte bir al!", reply_markup=kb, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "daily:claim")
async def daily_claim(cb: CallbackQuery):
    uid = cb.from_user.id
    async with db_session() as db:
        cur = await db.execute("SELECT daily_streak, last_daily FROM users WHERE user_id = ?", (uid,))
        u = await cur.fetchone()
        if u["last_daily"]:
            try:
                if (now_utc() - datetime.fromisoformat(u["last_daily"])).total_seconds() < 20 * 3600:
                    return await cb.answer("⏳ Henüz alamazsın!", show_alert=True)
            except: pass
        streak = u["daily_streak"] or 0
        cont = False
        if u["last_daily"]:
            try:
                if (now_utc() - datetime.fromisoformat(u["last_daily"])).total_seconds() < 48 * 3600: cont = True
            except: pass
        ns = (streak + 1) if cont else 1
        idx = min(ns - 1, 6); rw = DAILY_REWARDS[idx]
        await db.execute("UPDATE users SET balance_cents = balance_cents + ?, daily_streak = ?, last_daily = ? WHERE user_id = ?", (rw, ns, timestamp(), uid))
        await add_transaction(db, uid, rw, "daily", f"Streak {ns}")
        await award_xp(cb.bot, db, uid, 10)
        await db.commit()
        if ns >= 7: await unlock_achievement(cb.bot, uid, "streak_7")
    await safe_edit(cb, f"🎉 **Alındı!**\n\n🎁 `{format_money(rw)}`\n🔥 Seri: `{ns} gün`\n⭐ +10 XP", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer("Alındı!")

# ================= GAMES =================
@router.callback_query(F.data == "menu:games")
async def menu_games(cb: CallbackQuery):
    await safe_edit(cb, "🎮 **Oyunlar**\n\nŞansını dene!", reply_markup=games_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.in_({"game:wheel", "game:slot", "game:dice", "game:coin", "game:chest"}))
async def game_start(cb: CallbackQuery, state: FSMContext):
    game = cb.data.split(":")[1]
    title = {"wheel": "🎡 Çark", "slot": "🎰 Slot", "dice": "🎲 Zar", "coin": "🪙 Yazı Tura", "chest": "🎁 Sandık"}[game]
    await state.clear(); await state.set_state(BetFlow.amount); await state.update_data(game=game)
    mn = 5 if game == "chest" else 1
    await safe_edit(cb, f"🎮 **{title}**\n\n💰 Bahis (TL):\n📌 Min: `{mn}` Max: `5000`\n\n❌ /cancel", reply_markup=back_to_menu_kb(), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "game:duel")
async def game_duel(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(DuelFlow.target)
    await safe_edit(cb, "⚔️ **Düello**\n\nRakibin @kullanıcıadı veya ID:\n\n❌ /cancel", reply_markup=back_to_menu_kb()); await cb.answer()

@router.message(BetFlow.amount)
async def bet_handler(message: Message, state: FSMContext):
    data = await state.get_data(); game = data.get("game")
    try: bet = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz tutar.")
    async with db_session() as db:
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
        u = await cur.fetchone()
    if not u or u["balance_cents"] < bet: return await message.answer("❌ Yetersiz bakiye.")
    await state.clear()
    uid = message.from_user.id
    if game == "wheel": await play_wheel(message, uid, bet)
    elif game == "slot": await play_slot(message, uid, bet)
    elif game == "dice": await play_dice(message, uid, bet)
    elif game == "coin": await play_coin(message, uid, bet)
    elif game == "chest": await play_chest(message, uid, bet)

async def _charge(bot, db, uid, bet, game):
    await db.execute("UPDATE users SET balance_cents = balance_cents - ?, games_played = games_played + 1 WHERE user_id = ?", (bet, uid))
    await add_transaction(db, uid, -bet, f"game_{game}", "Bahis")
    await db.commit()

async def _pay(bot, db, uid, win, game, bet, result):
    await db.execute("UPDATE users SET balance_cents = balance_cents + ?, games_won = games_won + 1, total_won = total_won + ? WHERE user_id = ?", (win, max(0, win - bet), uid))
    await db.execute("INSERT INTO game_history (user_id, game, bet_cents, win_cents, result, created_at) VALUES (?, ?, ?, ?, ?, ?)", (uid, game, bet, win, result, timestamp()))
    await add_transaction(db, uid, win, f"game_{game}_win", result)
    await award_xp(bot, db, uid, 5)
    await progress_quest(bot, uid, "game", 1)
    await db.commit()

async def _lose(bot, db, uid, bet, game, result):
    await db.execute("UPDATE users SET total_lost = total_lost + ? WHERE user_id = ?", (bet, uid))
    await db.execute("INSERT INTO game_history (user_id, game, bet_cents, win_cents, result, created_at) VALUES (?, ?, ?, ?, ?, ?)", (uid, game, bet, 0, result, timestamp()))
    await progress_quest(bot, uid, "game", 1)
    await db.commit()

async def play_wheel(message, uid, bet):
    mults = [(0, 35), (0.5, 20), (1.0, 20), (1.5, 15), (2.0, 7), (3.0, 2), (5.0, 0.8), (10.0, 0.2)]
    r = random.uniform(0, 100); acc = 0; mult = 0
    for m, w in mults:
        acc += w
        if r <= acc: mult = m; break
    win = int(bet * mult)
    async with db_session() as db:
        await _charge(message.bot, db, uid, bet, "wheel")
        await db.execute("UPDATE users SET spins_total = spins_total + 1 WHERE user_id = ?", (uid,))
        if win > 0: await _pay(message.bot, db, uid, win, "wheel", bet, f"{mult}x")
        else: await _lose(message.bot, db, uid, bet, "wheel", "kaybetti")
        await db.commit()
    if win == 0: txt = f"🎡 **Çark**\n\n❌ Kazanamadın!\n💸 `{format_money(bet)}`"
    elif win < bet: txt = f"🎡 **Çark**\n\n⚠️ {mult}x\n💵 `{format_money(win)}`"
    elif win == bet: txt = f"🎡 **Çark**\n\n🟡 {mult}x Berabere\n💵 `{format_money(win)}`"
    else: txt = f"🎡 **Çark**\n\n🎉 {mult}x KAZANDIN!\n💵 `{format_money(win)}`"
    await message.answer(txt, reply_markup=games_kb(), parse_mode="Markdown")

SLOT_SY = ["🍒", "🍋", "🍊", "🍇", "🔔", "💎", "7️⃣"]

async def play_slot(message, uid, bet):
    w = [40, 25, 15, 10, 6, 3, 1]; idx = []
    for _ in range(3): idx.append(random.choices(range(7), weights=w, k=1)[0])
    s = [SLOT_SY[i] for i in idx]
    mult = 0
    if idx[0] == idx[1] == idx[2]: mult = {0: 2, 1: 2, 2: 3, 3: 3, 4: 4, 5: 8, 6: 15}.get(idx[0], 2)
    elif idx[0] == idx[1] or idx[1] == idx[2] or idx[0] == idx[2]: mult = 1.2
    win = int(bet * mult)
    async with db_session() as db:
        await _charge(message.bot, db, uid, bet, "slot")
        if win > 0: await _pay(message.bot, db, uid, win, "slot", bet, f"{s[0]}{s[1]}{s[2]}")
        else: await _lose(message.bot, db, uid, bet, "slot", f"{s[0]}{s[1]}{s[2]}")
        await db.commit()
    spin = f"┌─────────┐\n│ {s[0]} {s[1]} {s[2]} │\n└─────────┘\n\n"
    res = "❌ Kaybettin!" if win == 0 else ("⚠️ Az kazandın" if win < bet else "🎉 KAZANDIN!")
    await message.answer(f"🎰 **Slot**\n\n{spin}{res}\n" + (f"💵 `{format_money(win)}`" if win > 0 else f"💸 `{format_money(bet)}`"), reply_markup=games_kb(), parse_mode="Markdown")

DICE_E = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]

async def play_dice(message, uid, bet):
    ur = random.randint(1, 6); hr = random.randint(1, 6); win = 0
    if ur > hr: win = int(bet * 1.9); out = "🎉 Kazandın!"
    elif ur == hr: win = bet; out = "🟡 Berabere"
    else: out = "❌ Kaybettin!"
    res = f"Sen: {DICE_E[ur-1]} ({ur}) vs Bot: {DICE_E[hr-1]} ({hr})"
    async with db_session() as db:
        await _charge(message.bot, db, uid, bet, "dice")
        if win > 0: await _pay(message.bot, db, uid, win, "dice", bet, res)
        else: await _lose(message.bot, db, uid, bet, "dice", res)
        await db.commit()
    await message.answer(f"🎲 **Zar**\n\n{res}\n\n{out}\n" + (f"💵 `{format_money(win)}`" if win > 0 else f"💸 `{format_money(bet)}`"), reply_markup=games_kb(), parse_mode="Markdown")

async def play_coin(message, uid, bet):
    c = random.choice(["yazı", "tura"]); g = random.choice(["yazı", "tura"])
    win = int(bet * 1.95) if c == g else 0
    async with db_session() as db:
        await _charge(message.bot, db, uid, bet, "coin")
        if win > 0: await _pay(message.bot, db, uid, win, "coin", bet, c)
        else: await _lose(message.bot, db, uid, bet, "coin", c)
        await db.commit()
    em = "🪙" if c == "yazı" else "🟡"
    await message.answer(f"🪙 **Yazı Tura**\n\n{em} Sonuç: **{c.upper()}**\n\n" + (f"🎉 Kazandın!\n💵 `{format_money(win)}`" if win > 0 else f"❌ Kaybettin!\n💸 `{format_money(bet)}`"), reply_markup=games_kb(), parse_mode="Markdown")

CHEST_P = [(0, 50), (0.5, 20), (1, 15), (1.5, 8), (2, 4), (3, 2), (5, 0.9), (10, 0.1)]

async def play_chest(message, uid, bet):
    r = random.uniform(0, 100); acc = 0; mult = 0
    for m, w in CHEST_P:
        acc += w
        if r <= acc: mult = m; break
    win = int(bet * mult)
    async with db_session() as db:
        await _charge(message.bot, db, uid, bet, "chest")
        if win > 0: await _pay(message.bot, db, uid, win, "chest", bet, f"{mult}x")
        else: await _lose(message.bot, db, uid, bet, "chest", "boş")
        await db.commit()
    if win == 0: txt = f"🎁 **Sandık**\n\n💨 Boş!\n💸 `{format_money(bet)}`"
    else: txt = f"🎁 **Sandık**\n\n🎉 {mult}x!\n💵 `{format_money(win)}`"
    await message.answer(txt, reply_markup=games_kb(), parse_mode="Markdown")

# ================= DUEL =================
@router.message(DuelFlow.target)
async def duel_target(message: Message, state: FSMContext):
    q = message.text.strip()
    async with db_session() as db:
        if q.startswith("@"):
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE lower(username) = lower(?)", (q[1:],))
        else:
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (q,))
        u = await cur.fetchone()
    if not u or u["user_id"] == message.from_user.id: return await message.answer("❌ Geçersiz rakip.")
    await state.update_data(opp=u["user_id"], opp_name=u["first_name"])
    await state.set_state(DuelFlow.amount)
    await message.answer(f"⚔️ Rakip: **{u['first_name']}**\n\nBahis:", parse_mode="Markdown")

@router.message(DuelFlow.amount)
async def duel_amount(message: Message, state: FSMContext):
    try: bet = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    data = await state.get_data(); await state.clear()
    opp = data["opp"]
    async with db_session() as db:
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
        me = await cur.fetchone()
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (opp,))
        o = await cur.fetchone()
    if me["balance_cents"] < bet: return await message.answer("❌ Yetersiz bakiye.")
    if not o or o["balance_cents"] < bet: return await message.answer("❌ Rakibin yetersiz.")
    async with db_session() as db:
        cur = await db.execute("INSERT INTO duels (challenger_id, opponent_id, bet_cents, created_at) VALUES (?, ?, ?, ?)", (message.from_user.id, opp, bet, timestamp()))
        did = cur.lastrowid; await db.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Kabul", callback_data=f"duel:accept:{did}"), InlineKeyboardButton(text="❌ Reddet", callback_data=f"duel:reject:{did}")]
    ])
    try: await message.bot.send_message(opp, f"⚔️ **Düello!**\n\n👤 {message.from_user.first_name} seni çağırdı!\n💰 `{format_money(bet)}`", reply_markup=kb, parse_mode="Markdown")
    except: return await message.answer("❌ Rakip mesaj alamıyor.")
    await message.answer("✅ Teklif gönderildi.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data.startswith("duel:accept:"))
async def duel_accept(cb: CallbackQuery):
    try: did = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM duels WHERE id = ?", (did,))
        d = await cur.fetchone()
        if not d or d["status"] != "pending" or d["opponent_id"] != cb.from_user.id: return await cb.answer("Geçersiz.", show_alert=True)
        bet = d["bet_cents"]
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (d["challenger_id"],))
        c = await cur.fetchone()
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (d["opponent_id"],))
        o = await cur.fetchone()
        if not c or c["balance_cents"] < bet or not o or o["balance_cents"] < bet:
            await db.execute("UPDATE duels SET status = 'cancel' WHERE id = ?", (did,)); await db.commit()
            return await cb.answer("Yetersiz bakiye.", show_alert=True)
        cr = random.randint(1, 6); or_ = random.randint(1, 6)
        while cr == or_: cr = random.randint(1, 6); or_ = random.randint(1, 6)
        winner = d["challenger_id"] if cr > or_ else d["opponent_id"]
        payout = int(bet * 2 * 0.9)
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id IN (?, ?)", (bet, d["challenger_id"], d["opponent_id"]))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ?, duels_won = duels_won + 1 WHERE user_id = ?", (payout, winner))
        await db.execute("UPDATE duels SET status = 'done', winner_id = ? WHERE id = ?", (winner, did))
        await add_transaction(db, winner, payout, "duel_win", f"Düello #{did}")
        await db.commit()
    ce = DICE_E[cr-1]; oe = DICE_E[or_-1]
    txt = f"⚔️ **Düello**\n\nŞallenger: {ce} ({cr})\nRakip: {oe} ({or_})\n\n🏆 **{'Şallenger' if cr > or_ else 'Rakip'} Kazandı!**\n💵 `{format_money(payout)}`"
    for uid in (d["challenger_id"], d["opponent_id"]):
        try: await cb.bot.send_message(uid, txt, parse_mode="Markdown")
        except: pass
    await safe_edit(cb, txt, parse_mode="Markdown")
    await cb.answer("Düello bitti!")

@router.callback_query(F.data.startswith("duel:reject:"))
async def duel_reject(cb: CallbackQuery):
    try: did = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT challenger_id, opponent_id FROM duels WHERE id = ?", (did,))
        d = await cur.fetchone()
        if not d or d["opponent_id"] != cb.from_user.id: return await cb.answer("Geçersiz.")
        await db.execute("UPDATE duels SET status = 'rejected' WHERE id = ?", (did,)); await db.commit()
    try: await cb.bot.send_message(d["challenger_id"], "❌ Düellon reddedildi.")
    except: pass
    await safe_edit(cb, "❌ Reddettin.")
    await cb.answer("Reddedildi.")

# ================= BANK =================
BANK_PLANS = [("24 Saat", 24, 5), ("7 Gün", 24*7, 15), ("30 Gün", 24*30, 30)]

@router.callback_query(F.data == "menu:bank")
async def menu_bank(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT id, amount_cents, interest_pct, matures_at FROM bank_deposits WHERE user_id = ? AND withdrawn = 0", (cb.from_user.id,))
        deps = await cur.fetchall()
    text = "💼 **Banka**\n\nFaizle para kazan!\n\n"
    for d in deps:
        amt = d["amount_cents"]; intr = int(amt * d["interest_pct"] / 100)
        ready = is_expired(d["matures_at"])
        text += f"├ `#{d['id']}` {format_money(amt)} → {format_money(amt+intr)} (%{d['interest_pct']})"
        text += " ✅\n" if ready else f" ⏳ {time_remaining_str(d['matures_at'])}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Yatır", callback_data="bank:deposit")],
        [InlineKeyboardButton(text="💸 Çek", callback_data="bank:withdraw")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back")]
    ])
    await safe_edit(cb, text, reply_markup=kb, parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "bank:deposit")
async def bank_dep(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(BankDepositFlow.amount)
    await safe_edit(cb, "💼 **Yatırım**\n\nMiktar (min 50 TL):\n\n❌ /cancel", reply_markup=back_to_menu_kb()); await cb.answer()

@router.message(BankDepositFlow.amount)
async def bank_amt(message: Message, state: FSMContext):
    try: amt = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    if amt < 5000: return await message.answer("❌ Min 50 TL.")
    async with db_session() as db:
        cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
        u = await cur.fetchone()
    if u["balance_cents"] < amt: return await message.answer("❌ Yetersiz.")
    await state.update_data(amount=amt); await state.set_state(BankDepositFlow.duration)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{n} — %{p}", callback_data=f"bank:plan:{i}")] for i, (n, h, p) in enumerate(BANK_PLANS)])
    await message.answer(f"💰 `{format_money(amt)}`\n\nPlan:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("bank:plan:"))
async def bank_plan(cb: CallbackQuery, state: FSMContext):
    try: i = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    if i < 0 or i >= len(BANK_PLANS): return await cb.answer("Geçersiz.", show_alert=True)
    n, h, p = BANK_PLANS[i]
    data = await state.get_data(); amt = data.get("amount")
    if not amt: return await cb.answer("Hata.", show_alert=True)
    await state.clear()
    matures = (now_utc() + timedelta(hours=h)).isoformat()
    async with db_session() as db:
        cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ? AND balance_cents >= ?", (amt, cb.from_user.id, amt))
        if cur.rowcount == 0:
            return await cb.answer("Yetersiz.", show_alert=True)
        await db.execute("INSERT INTO bank_deposits (user_id, amount_cents, interest_pct, deposited_at, matures_at) VALUES (?, ?, ?, ?, ?)", (cb.from_user.id, amt, p, timestamp(), matures))
        await add_transaction(db, cb.from_user.id, -amt, "bank_dep", n)
        await award_xp(cb.bot, db, cb.from_user.id, 10)
        await db.commit()
    intr = int(amt * p / 100)
    await safe_edit(cb, f"✅ **Yatırıldı**\n\n💰 `{format_money(amt)}`\n📈 %{p}\n💵 Vade: `{format_money(amt+intr)}`", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer("OK!")

@router.callback_query(F.data == "bank:withdraw")
async def bank_wd(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT id, amount_cents, interest_pct, matures_at FROM bank_deposits WHERE user_id = ? AND withdrawn = 0", (cb.from_user.id,))
        deps = await cur.fetchall()
    kb = []
    for d in deps:
        if is_expired(d["matures_at"]):
            intr = int(d["amount_cents"] * d["interest_pct"] / 100)
            tot = d["amount_cents"] + intr
            kb.append([InlineKeyboardButton(text=f"💵 {format_money(tot)}", callback_data=f"bank:do:{d['id']}")])
    if not kb: return await cb.answer("Vadesi dolan yok.", show_alert=True)
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:bank")])
    await safe_edit(cb, "💸 **Çekilebilir:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)); await cb.answer()

@router.callback_query(F.data.startswith("bank:do:"))
async def bank_do(cb: CallbackQuery):
    try: did = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM bank_deposits WHERE id = ? AND user_id = ?", (did, cb.from_user.id))
        d = await cur.fetchone()
        if not d or d["withdrawn"] or not is_expired(d["matures_at"]): return await cb.answer("Geçersiz.", show_alert=True)
        intr = int(d["amount_cents"] * d["interest_pct"] / 100)
        tot = d["amount_cents"] + intr
        await db.execute("UPDATE bank_deposits SET withdrawn = 1 WHERE id = ?", (did,))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (tot, cb.from_user.id))
        await add_transaction(db, cb.from_user.id, tot, "bank_wd", f"Çekim #{did}")
        await db.commit()
    await safe_edit(cb, f"✅ **Çekildi**\n\n💰 `{format_money(tot)}`\n📈 Kar: `{format_money(intr)}`", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer("OK!")

# ================= LEADERBOARD / QUESTS / ACHIEVEMENTS =================
@router.callback_query(F.data == "menu:leaderboard")
async def menu_lb(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT first_name, username, balance_cents FROM users ORDER BY balance_cents DESC LIMIT 10")
        top = await cur.fetchall()
    med = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    text = "🏆 **Liderlik**\n\n💎 **En Zengin:**\n"
    for i, u in enumerate(top):
        n = f"@{u['username']}" if u["username"] else (u["first_name"] or "?")
        text += f"{med[i]} {n} — `{format_money(u['balance_cents'])}`\n"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "menu:quests")
async def menu_quests(cb: CallbackQuery):
    qd = today_str()
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM quests_progress WHERE user_id = ? AND date = ?", (cb.from_user.id, qd))
        progs = {r["quest_id"]: r for r in await cur.fetchall()}
    tg = {"play_1_game": 1, "order_1": 1, "spend_100": 10000, "invite_1": 1}
    text = "🎯 **Günlük Görevler**\n\n"; kb = []
    for qid, lbl, rw, qt in QUESTS:
        p = progs.get(qid); prog = p["progress"] if p else 0; target = tg[qid]
        done = p and p["completed"] if p else False
        claimed = p and p["claimed"] if p else False
        bar = progress_bar(prog, target, 10)
        st = "✅ Alındı" if claimed else ("🎁 Hazır" if done else f"{prog}/{target}")
        text += f"{lbl}\n├ {bar}\n├ {st}\n└ `{format_money(rw)}`\n\n"
        if done and not claimed: kb.append([InlineKeyboardButton(text=f"🎁 {lbl[:20]}", callback_data=f"quest:claim:{qid}")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:back")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("quest:claim:"))
async def quest_claim(cb: CallbackQuery):
    qid = cb.data.split(":")[2]; qd = today_str()
    q = next((x for x in QUESTS if x[0] == qid), None)
    if not q: return await cb.answer("Hata.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT completed, claimed FROM quests_progress WHERE user_id = ? AND quest_id = ? AND date = ?", (cb.from_user.id, qid, qd))
        r = await cur.fetchone()
        if not r or not r["completed"]: return await cb.answer("Tamamlanmadı.", show_alert=True)
        if r["claimed"]: return await cb.answer("Alındı.", show_alert=True)
        await db.execute("UPDATE quests_progress SET claimed = 1 WHERE user_id = ? AND quest_id = ? AND date = ?", (cb.from_user.id, qid, qd))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (q[2], cb.from_user.id))
        await add_transaction(db, cb.from_user.id, q[2], "quest", q[1])
        await award_xp(cb.bot, db, cb.from_user.id, 15)
        await db.commit()
    await cb.answer(f"✅ +{format_money(q[2])}!", show_alert=True)
    await menu_quests(cb)

@router.callback_query(F.data == "menu:achievements")
async def menu_ach(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT achievement_id FROM achievements WHERE user_id = ?", (cb.from_user.id,))
        un = {r["achievement_id"] for r in await cur.fetchall()}
    text = f"🏆 **Başarımlar** ({len(un)}/{len(ACHIEVEMENTS)})\n\n"
    for aid, t, d in ACHIEVEMENTS:
        icon = "✅" if aid in un else "🔒"
        text += f"{icon} **{t}**\n   _{d}_\n\n"
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown"); await cb.answer()

# ================= TRANSFER =================
@router.callback_query(F.data == "menu:transfer")
async def menu_transfer(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(TransferFlow.target)
    await safe_edit(cb, "🤝 **Transfer**\n\nAlıcı @username veya ID:\n\n💸 Kom: %2\n\n❌ /cancel", reply_markup=back_to_menu_kb()); await cb.answer()

@router.message(TransferFlow.target)
async def tr_target(message: Message, state: FSMContext):
    q = message.text.strip()
    async with db_session() as db:
        if q.startswith("@"):
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE lower(username) = lower(?)", (q[1:],))
        else:
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (q,))
        u = await cur.fetchone()
    if not u or u["user_id"] == message.from_user.id: return await message.answer("❌ Geçersiz.")
    await state.update_data(tid=u["user_id"], tname=u["first_name"])
    await state.set_state(TransferFlow.amount)
    await message.answer(f"👤 **{u['first_name']}**\n\nMiktar:", parse_mode="Markdown")

@router.message(TransferFlow.amount)
async def tr_amt(message: Message, state: FSMContext):
    try: amt = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    if amt < 100: return await message.answer("❌ Min 1 TL.")
    data = await state.get_data(); await state.clear()
    fee = int(amt * 0.02); tot = amt + fee
    async with db_session() as db:
        cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ? AND balance_cents >= ?", (tot, message.from_user.id, tot))
        if cur.rowcount == 0: return await message.answer("❌ Yetersiz.")
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amt, data["tid"]))
        await add_transaction(db, message.from_user.id, -tot, "transfer_out", f"→ {data['tname']}")
        await add_transaction(db, data["tid"], amt, "transfer_in", f"← {message.from_user.first_name}")
        await db.commit()
    try: await message.bot.send_message(data["tid"], f"💸 **Para aldın!**\n\n👤 {message.from_user.first_name}\n💰 `{format_money(amt)}`", parse_mode="Markdown")
    except: pass
    await message.answer(f"✅ **Transfer OK**\n\n👤 {data['tname']}\n💵 `{format_money(amt)}`\n💸 Kom: `{format_money(fee)}`", reply_markup=back_to_menu_kb(), parse_mode="Markdown")

# ================= FAVORITES =================
@router.callback_query(F.data == "menu:favorites")
async def menu_fav(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT p.product_id, p.country_code, p.price_cents, s.name as sn FROM favorites f JOIN products p ON f.product_id = p.product_id JOIN services s ON p.service_id = s.service_id WHERE f.user_id = ?", (cb.from_user.id,))
        favs = await cur.fetchall()
    if not favs: return await safe_edit(cb, "⭐ Favori yok.", reply_markup=back_to_menu_kb())
    text = "⭐ **Favoriler**\n\n"; kb = []
    for f in favs:
        text += f"• {f['sn']} ({f['country_code']}) — `{format_money(f['price_cents'])}`\n"
        kb.append([InlineKeyboardButton(text=f"🛒 {f['sn'][:20]}", callback_data=f"buyprod:{f['product_id']}"), InlineKeyboardButton(text="❌", callback_data=f"fav:rm:{f['product_id']}")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:back")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("fav:add:"))
async def fav_add(cb: CallbackQuery):
    try: pid = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        await db.execute("INSERT OR IGNORE INTO favorites (user_id, product_id) VALUES (?, ?)", (cb.from_user.id, pid))
        await db.commit()
    await cb.answer("⭐ Eklendi!")

@router.callback_query(F.data.startswith("fav:rm:"))
async def fav_rm(cb: CallbackQuery):
    try: pid = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        await db.execute("DELETE FROM favorites WHERE user_id = ? AND product_id = ?", (cb.from_user.id, pid))
        await db.commit()
    await cb.answer("❌ Çıkarıldı.")
    await menu_fav(cb)

# ================= ACTIVE / REFERRAL =================
@router.callback_query(F.data == "menu:active")
async def menu_active(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM promos WHERE active = 1"); ps = await cur.fetchall()
        cur = await db.execute("SELECT * FROM hazine WHERE active = 1"); hs = await cur.fetchall()
        cur = await db.execute("SELECT * FROM special_services WHERE active = 1"); ss = await cur.fetchall()
    u = await get_bot_username(cb.bot)
    text = "🎯 <b>AKTİF FIRSATLAR</b>\n\n"; kb = []; any_ = False
    for p in ps:
        if is_expired(p["expires_at"]) or p["current_uses"] >= p["max_uses"]: continue
        any_ = True
        text += f"🎁 <b>{esc(p['code'])}</b> • 💰 {format_money(p['bonus_cents'])} • ⏳ {time_remaining_str(p['expires_at'])}\n"
        if u: kb.append([InlineKeyboardButton(text=f"🎁 {p['code']}", url=f"https://t.me/{u}?start=promo_{p['code']}")])
    for h in hs:
        if is_expired(h["expires_at"]) or h["current_users"] >= h["max_users"]: continue
        any_ = True
        text += f"💰 Hazine: {format_money(h['remaining_cents'])} • ⏳ {time_remaining_str(h['expires_at'])}\n"
        if u: kb.append([InlineKeyboardButton(text=f"💰 Hazine Kap", url=f"https://t.me/{u}?start=hazine_{h['id']}")])
    for s in ss:
        if is_expired(s["expires_at"]) or s["current_users"] >= s["max_users"]: continue
        any_ = True
        text += f"✨ {esc(s['name'])} • {format_money(s['price_cents'])}\n"
        if u: kb.append([InlineKeyboardButton(text=f"✨ {esc(s['name'][:20])}", url=f"https://t.me/{u}?start=special_{s['id']}")])
    if not any_: return await safe_edit(cb, "🎯 Aktif fırsat yok.", reply_markup=back_to_menu_kb())
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:back")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML"); await cb.answer()

@router.callback_query(F.data == "menu:referral")
async def menu_ref(cb: CallbackQuery):
    u = await get_bot_username(cb.bot)
    link = f"https://t.me/{u}?start=ref_{cb.from_user.id}" if u else "-"
    async with db_session() as db:
        cur = await db.execute("SELECT COUNT(*) as t FROM referrals WHERE referrer_id = ?", (cb.from_user.id,)); tot = (await cur.fetchone())["t"] or 0
        cur = await db.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id = ? AND rewarded_at IS NOT NULL", (cb.from_user.id,)); rw = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COALESCE(SUM(reward_cents), 0) as e FROM referrals WHERE referrer_id = ? AND rewarded_at IS NOT NULL", (cb.from_user.id,)); e = (await cur.fetchone())["e"] or 0
    req = await get_setting("ref_require_order")
    c = "İlk sipariş sonrası" if req == "1" else "Anında"
    text = (f"👥 **Davet**\n\n🔗 `{link}`\n\n📊 Toplam: `{tot}` • Ödül: `{rw}` • Kazanç: `{format_money(e)}`\n💰 Ödül: `{format_money(REFERRAL_BONUS_CENTS)}` ({c})")
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown"); await cb.answer()

# ================= SUPPORT / HELP =================
@router.callback_query(F.data == "menu:support")
async def menu_support(cb: CallbackQuery, state: FSMContext):
    await state.clear(); await state.set_state(SupportFlow.body)
    await safe_edit(cb, "💬 **Destek**\n\nSorununu yaz.\n\n❌ /cancel", parse_mode="Markdown"); await cb.answer()

@router.message(SupportFlow.body)
async def support_body(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 5: return await message.answer("❌ Min 5 karakter.")
    body = message.text.strip(); await state.clear()
    async with db_session() as db:
        cur = await db.execute("INSERT INTO support_tickets (user_id, body, created_at) VALUES (?, ?, ?)", (message.from_user.id, body, timestamp()))
        tid = cur.lastrowid; await db.commit()
    await notify_admins(message.bot, f"💬 **Destek #{tid}**\n👤 {await get_display_name(message.from_user.id)}\n📝 {body}")
    await message.answer(f"✅ Alındı! (Talep #{tid})", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "menu:help")
async def menu_help(cb: CallbackQuery):
    text = ("ℹ️ **Yardım**\n\n"
            "🛍️ Market • 💰 Bakiye • 📦 Siparişler\n"
            "🎮 Oyunlar • 🎯 Günlük • 🏆 Liderlik\n"
            "🎫 Kod Takip • 📞 Kod Geçmişi\n"
            "💼 Banka • 🤝 Transfer\n"
            "⭐ Favoriler • 🛒 Sepet • 💬 Destek\n\n"
            "❓ /start = Menü\n🔄 /cancel = İptal")
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown"); await cb.answer()

# ================= VIP / TOPUP =================
@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    if not await is_feature_enabled("vip_enabled"): return await safe_edit(cb, "🚧 VIP kapalı.", reply_markup=back_to_menu_kb())
    # DB'den oku
    price_str = await get_setting("vip_price_tl") or VIP_PRICE_TL
    days_str = await get_setting("vip_duration_days") or VIP_DURATION_DAYS
    text = f"💎 **VIP**\n\n✅ %20 indirim\n✅ Öncelikli\n\n💰 `{price_str} TL` • `{days_str} gün`"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Satın Al", callback_data="vip_buy")],[InlineKeyboardButton(text="🔙", callback_data="menu:back")]])
    await safe_edit(cb, text, reply_markup=kb, parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "vip_buy")
async def vip_buy(cb: CallbackQuery):
    # DB'den oku
    price_str = await get_setting("vip_price_tl") or VIP_PRICE_TL
    days_str = await get_setting("vip_duration_days") or VIP_DURATION_DAYS
    try:
        price = int(float(price_str) * 100)
        days = int(days_str)
    except:
        price, days = 50000, 30
    async with db_session() as db:
        cur = await db.execute("SELECT vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
        if not u: return await cb.answer("Önce /start.")
        base = now_utc()
        if u["vip_until"] and not is_expired(u["vip_until"]):
            try: base = datetime.fromisoformat(u["vip_until"])
            except: pass
        nv = (base + timedelta(days=days)).isoformat()
        cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ?, vip_until = ? WHERE user_id = ? AND balance_cents >= ?", (price, nv, cb.from_user.id, price))
        if cur.rowcount == 0:
            return await cb.answer("❌ Yetersiz bakiye.", show_alert=True)
        await add_transaction(db, cb.from_user.id, -price, "vip", f"{days} gün VIP")
        await db.commit()
    await unlock_achievement(cb.bot, cb.from_user.id, "vip_member")
    await safe_edit(cb, f"✅ VIP! Bitiş: {nv[:10]}", reply_markup=back_to_menu_kb())
    await cb.answer("VIP Aktif!", show_alert=True)

@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await safe_edit(cb, "⭐ **Yıldız Yükle**\n\nPaket:", reply_markup=topup_keyboard(), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("star:"))
async def star_inv(cb: CallbackQuery):
    try:
        s = int(cb.data.split(":")[1])
        t = next((x for x in STAR_TOPUP_TIERS if x[0] == s), None)
        if not t: return await cb.answer("Hata.", show_alert=True)
        pl = f"topup:{uuid4().hex}"
        async with db_session() as db:
            await db.execute("INSERT INTO star_payment_intents (payload, user_id, stars_amount, credit_cents, created_at) VALUES (?, ?, ?, ?, ?)", (pl, cb.from_user.id, s, t[1], timestamp()))
            await db.commit()
        await cb.bot.send_invoice(chat_id=cb.from_user.id, title="Bakiye", description=f"{format_money(t[1])}", payload=pl, provider_token="", currency="XTR", prices=[LabeledPrice(label="Bakiye", amount=s)])
        await cb.answer()
    except: await cb.answer("Hata.", show_alert=True)

@router.pre_checkout_query()
async def pcq(q: PreCheckoutQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT user_id, stars_amount, credit_cents, status FROM star_payment_intents WHERE payload = ?", (q.invoice_payload,))
        row = await cur.fetchone()
    if not row:
        return await q.answer(ok=False, error_message="Geçersiz ödeme.")
    if row["user_id"] != q.from_user.id:
        return await q.answer(ok=False, error_message="Yetkisiz.")
    if row["status"] == "paid":
        return await q.answer(ok=False, error_message="Zaten tamamlandı.")
    if q.total_amount != row["stars_amount"]:
        return await q.answer(ok=False, error_message="Tutar uyuşmuyor.")
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def on_pay(message: Message):
    p = message.successful_payment
    async with db_session() as db:
        cur = await db.execute("SELECT user_id, credit_cents, status FROM star_payment_intents WHERE payload = ?", (p.invoice_payload,))
        r = await cur.fetchone()
        if not r: return
        if r["user_id"] != message.from_user.id: return
        if r["status"] == "paid": return
        cur = await db.execute("UPDATE star_payment_intents SET status = 'paid' WHERE payload = ? AND status = 'pending'", (p.invoice_payload,))
        if cur.rowcount == 0: return
        c = r["credit_cents"]
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (c, r["user_id"]))
        await add_transaction(db, r["user_id"], c, "stars", "Yıldız")
        await db.commit()
    await message.answer(f"🎉 **Yüklendi!**\n\n✅ +`{format_money(c)}`", parse_mode="Markdown")

# ================= MARKET / CART / BUY =================
@router.callback_query(F.data == "menu:catalog")
async def menu_cat(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    async with db_session() as db:
        cur = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        srvs = await cur.fetchall()
    if not srvs: return await safe_edit(cb, "🛒 Hizmet yok.", reply_markup=back_to_menu_kb())
    kb = [[InlineKeyboardButton(text=s["name"], callback_data=f"srv:{s['service_id']}")] for s in srvs]
    kb.append([InlineKeyboardButton(text="🛒 Sepetim", callback_data="menu:cart"), InlineKeyboardButton(text="🔙", callback_data="menu:back")])
    await safe_edit(cb, "🛍️ **Market**\n\nHizmet:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("srv:"))
async def srv_prods(cb: CallbackQuery):
    try: sid = int(cb.data.split(":")[1])
    except: return await cb.answer("Geçersiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT name FROM services WHERE service_id = ? AND active = 1", (sid,)); srv = await cur.fetchone()
        if not srv: return await cb.answer("Hizmet yok.", show_alert=True)
        cur = await db.execute("SELECT product_id, country_code, price_cents FROM products WHERE service_id = ? AND active = 1", (sid,)); prods = await cur.fetchall()
    if not prods: return await cb.answer("Ürün yok.", show_alert=True)
    kb = []
    for p in prods:
        cc = p["country_code"] if p["country_code"] else "Özel"
        kb.append([
            InlineKeyboardButton(text=f"{cc} - {format_money(p['price_cents'])}", callback_data=f"buyprod:{p['product_id']}"),
            InlineKeyboardButton(text="⭐", callback_data=f"fav:add:{p['product_id']}"),
            InlineKeyboardButton(text="🛒", callback_data=f"cart:add:{p['product_id']}")
        ])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:catalog")])
    await safe_edit(cb, f"📦 **{srv['name']}**\n\nÜlke:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("buyprod:"))
async def buy_prod(cb: CallbackQuery):
    try: pid = int(cb.data.split(":")[1])
    except: return await cb.answer("Geçersiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT p.price_cents, p.country_code, p.active as pactive, s.name, s.active as sactive FROM products p JOIN services s ON p.service_id = s.service_id WHERE p.product_id = ?", (pid,))
        p = await cur.fetchone()
        if not p or not p["pactive"] or not p["sactive"]:
            return await cb.answer("Ürün artık mevcut değil.", show_alert=True)
        cur = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,)); u = await cur.fetchone()
        fp = int(p["price_cents"] * 0.8) if is_vip_user(u) else p["price_cents"]
        fname = f"{p['name']} ({p['country_code']})"
        cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ?, total_spent = total_spent + ? WHERE user_id = ? AND balance_cents >= ?", (fp, fp, cb.from_user.id, fp))
        if cur.rowcount == 0:
            return await cb.answer("❌ Yetersiz bakiye.", show_alert=True)
        cur = await db.execute("INSERT INTO orders (user_id, item_type, item_id, item_name, amount_cents, status, created_at, expires_at, service_name, country_code) VALUES (?, 'product', ?, ?, ?, 'pending', ?, ?, ?, ?)",
                             (cb.from_user.id, pid, fname, fp, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat(), p["name"], p["country_code"]))
        oid = cur.lastrowid
        await add_transaction(db, cb.from_user.id, -fp, "order", f"Sipariş #{oid}")
        await award_xp(cb.bot, db, cb.from_user.id, 20)
        await progress_quest(cb.bot, cb.from_user.id, "order", 1)
        await progress_quest(cb.bot, cb.from_user.id, "spend", fp)
        await db.commit()
    await cb.message.answer(f"🎉 **Sipariş #{oid}**\n\n✅ {fname}\n💵 {format_money(fp)}", parse_mode="Markdown")
    try:
        msg = await cb.bot.send_message(cb.from_user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True
        ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass
    await notify_admins(cb.bot, f"🔔 **Sipariş #{oid}**\n👤 {await get_display_name(cb.from_user.id)}\n📦 {fname}\n💵 {format_money(fp)}",
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara", callback_data=f"n:{oid}")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}")]]))
    await cb.answer("Sipariş verildi!")

@router.callback_query(F.data.startswith("cart:add:"))
async def cart_add(cb: CallbackQuery):
    try: pid = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        await db.execute("INSERT OR IGNORE INTO cart (user_id, product_id) VALUES (?, ?)", (cb.from_user.id, pid))
        await db.commit()
    await cb.answer("🛒 Sepete eklendi!")

@router.callback_query(F.data.startswith("cart:rm:"))
async def cart_rm(cb: CallbackQuery):
    try: pid = int(cb.data.split(":")[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        await db.execute("DELETE FROM cart WHERE user_id = ? AND product_id = ?", (cb.from_user.id, pid))
        await db.commit()
    await cb.answer("Çıkarıldı.")
    await menu_cart(cb)

@router.callback_query(F.data == "menu:cart")
async def menu_cart(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT c.product_id, p.price_cents, p.country_code, s.name as sn FROM cart c JOIN products p ON c.product_id = p.product_id JOIN services s ON p.service_id = s.service_id WHERE c.user_id = ?", (cb.from_user.id,))
        items = await cur.fetchall()
        cur = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
    if not items:
        return await safe_edit(cb, "🛒 **Sepet boş.**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍️ Market", callback_data="menu:catalog")],[InlineKeyboardButton(text="🔙", callback_data="menu:back")]
        ]))
    is_v = is_vip_user(u)
    total = 0; text = "🛒 **Sepetim**\n\n"; kb = []
    for it in items:
        p = int(it["price_cents"] * 0.8) if is_v else it["price_cents"]
        total += p
        text += f"• {it['sn']} ({it['country_code']}) — `{format_money(p)}`\n"
        kb.append([InlineKeyboardButton(text=f"❌ {it['sn'][:15]}", callback_data=f"cart:rm:{it['product_id']}")])
    text += f"\n💰 **Toplam:** `{format_money(total)}`\n"
    if u["balance_cents"] < total: text += f"⚠️ Yetersiz!"
    else:
        text += f"✅ Yeterli"
        kb.insert(0, [InlineKeyboardButton(text=f"💳 Öde ({format_money(total)})", callback_data="cart:checkout")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:back")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "cart:checkout")
async def cart_checkout(cb: CallbackQuery):
    async with db_session() as db:
        cur = await db.execute("SELECT c.product_id, p.price_cents, p.country_code, p.active as pactive, s.name as sn, s.active as sactive FROM cart c JOIN products p ON c.product_id = p.product_id JOIN services s ON p.service_id = s.service_id WHERE c.user_id = ?", (cb.from_user.id,))
        items = await cur.fetchall()
        if not items: return await cb.answer("Sepet boş.", show_alert=True)
        for it in items:
            if not it["pactive"] or not it["sactive"]:
                return await cb.answer(f"⚠️ '{it['sn']}' artık satışta değil.", show_alert=True)
        cur = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cur.fetchone()
        is_v = is_vip_user(u)
        total = sum(int(it["price_cents"] * 0.8) if is_v else it["price_cents"] for it in items)
        cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ?, total_spent = total_spent + ? WHERE user_id = ? AND balance_cents >= ?", (total, total, cb.from_user.id, total))
        if cur.rowcount == 0: return await cb.answer("Yetersiz bakiye.", show_alert=True)
        oids = []
        for it in items:
            p = int(it["price_cents"] * 0.8) if is_v else it["price_cents"]
            fname = f"{it['sn']} ({it['country_code']})"
            cur = await db.execute("INSERT INTO orders (user_id, item_type, item_id, item_name, amount_cents, status, created_at, expires_at, service_name, country_code) VALUES (?, 'product', ?, ?, ?, 'pending', ?, ?, ?, ?)",
                                 (cb.from_user.id, it["product_id"], fname, p, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat(), it["sn"], it["country_code"]))
            oids.append(cur.lastrowid)
        await db.execute("DELETE FROM cart WHERE user_id = ?", (cb.from_user.id,))
        await add_transaction(db, cb.from_user.id, -total, "order", f"Sepet ({len(items)})")
        await award_xp(cb.bot, db, cb.from_user.id, 20 * len(items))
        await db.commit()
    await safe_edit(cb, f"🎉 **Sepet Sipariş!**\n\n📦 {len(items)} ürün\n💵 `{format_money(total)}`\n\nSiparişler: #{', #'.join(map(str, oids))}", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer("Tamam!")

# ================= ADMIN PANEL =================
@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await safe_edit(cb, "🛠️ **Admin**", reply_markup=admin_panel_kb(), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "admin:settings")
async def admin_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    await safe_edit(cb, "⚙️ **Ayarlar**", reply_markup=settings_kb(), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data.startswith("toggle:"))
async def toggle_setting(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    key = cb.data.split(":")[1]
    async with db_session() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        r = await cur.fetchone()
        nv = "0" if r and r["value"] == "1" else "1"
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, nv))
        await db.commit()
    await cb.answer(f"{'Açık' if nv=='1' else 'Kapalı'}!", show_alert=False)
    await admin_settings(cb)

@router.callback_query(F.data == "admin:dashboard")
async def admin_dash(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT COUNT(*) as c, COALESCE(SUM(balance_cents),0) as b FROM users"); u = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) as c FROM users WHERE vip_until > ?", (timestamp(),)); vip = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE status = 'completed'"); oc = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE status = 'pending'"); op = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COALESCE(SUM(amount_cents),0) as s FROM orders WHERE status = 'completed'"); rev = (await cur.fetchone())["s"]
        cur = await db.execute("SELECT COUNT(*) as c FROM promos WHERE active = 1"); pr = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) as c FROM hazine WHERE active = 1"); hz = (await cur.fetchone())["c"]
    text = (f"📈 **DASHBOARD**\n\n"
            f"👥 Kullanıcı: `{u['c']}` (VIP: `{vip}`)\n"
            f"💰 Bakiye: `{format_money(u['b'])}`\n\n"
            f"📦 Siparişler\n├ Tamamlanan: `{oc}`\n├ Bekleyen: `{op}`\n└ Ciro: `{format_money(rev)}`\n\n"
            f"🎁 Promo: `{pr}` • 💰 Hazine: `{hz}`")
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Yenile", callback_data="admin:dashboard")],
        [InlineKeyboardButton(text="🔙 Admin", callback_data="menu:admin")]
    ]), parse_mode="Markdown"); await cb.answer()

@router.callback_query(F.data == "admin:promos_list")
async def admin_promos_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM promos ORDER BY active DESC, code")
        ps = await cur.fetchall()
    if not ps: return await safe_edit(cb, "📋 Promo yok.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Promo Ekle", callback_data="admin:promo_add")],[InlineKeyboardButton(text="🔙", callback_data="menu:admin")]
    ]))
    text = "📋 **Promolar**\n\n"; kb = []
    for p in ps:
        st = "✅" if p["active"] else "❌"
        left = time_remaining_str(p["expires_at"]) if p["active"] else "-"
        text += f"{st} `{esc(p['code'])}`\n   💰 {format_money(p['bonus_cents'])} • 👥 {p['current_uses']}/{p['max_uses']} • ⏳ {left}\n\n"
        if p["active"]: kb.append([InlineKeyboardButton(text=f"❌ İptal: {p['code']}", callback_data=f"admin:promo_cancel:{p['code']}")])
    kb.append([InlineKeyboardButton(text="🔄", callback_data="admin:promos_list"), InlineKeyboardButton(text="🔙", callback_data="menu:admin")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML"); await cb.answer()

@router.callback_query(F.data.startswith("admin:promo_cancel:"))
async def admin_promo_cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    code = cb.data.split(":", 2)[2]
    async with db_session() as db:
        await db.execute("UPDATE promos SET active = 0 WHERE code = ?", (code,))
        await db.commit()
    await refresh_promo(cb.bot, code)
    await cb.answer(f"✅ {code} iptal.", show_alert=True)
    await admin_promos_list(cb)

@router.callback_query(F.data == "admin:hazines_list")
async def admin_hazines_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM hazine ORDER BY active DESC, id DESC")
        hs = await cur.fetchall()
    if not hs: return await safe_edit(cb, "📋 Hazine yok.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Hazine Ekle", callback_data="admin:hazine_add")],[InlineKeyboardButton(text="🔙", callback_data="menu:admin")]
    ]))
    text = "📋 **Hazineler**\n\n"; kb = []
    for h in hs:
        st = "✅" if h["active"] else "❌"
        left = time_remaining_str(h["expires_at"]) if h["active"] else "-"
        text += f"{st} `#{h['id']}`\n   💵 {format_money(h['remaining_cents'])}/{format_money(h['total_cents'])} • 👥 {h['current_users']}/{h['max_users']} • ⏳ {left}\n\n"
        if h["active"]: kb.append([InlineKeyboardButton(text=f"❌ İptal #{h['id']}", callback_data=f"admin:hazine_cancel:{h['id']}")])
    kb.append([InlineKeyboardButton(text="🔄", callback_data="admin:hazines_list"), InlineKeyboardButton(text="🔙", callback_data="menu:admin")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML"); await cb.answer()

@router.callback_query(F.data.startswith("admin:hazine_cancel:"))
async def admin_hazine_cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    try: hid = int(cb.data.split(":", 2)[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        await db.execute("UPDATE hazine SET active = 0 WHERE id = ?", (hid,))
        await db.commit()
    await refresh_hazine(cb.bot, hid)
    await cb.answer(f"✅ Hazine #{hid} iptal.", show_alert=True)
    await admin_hazines_list(cb)

@router.callback_query(F.data == "admin:specials_list")
async def admin_specials_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT * FROM special_services ORDER BY active DESC, id DESC")
        ss = await cur.fetchall()
    if not ss: return await safe_edit(cb, "📋 Özel hizmet yok.", reply_markup=back_to_menu_kb())
    text = "📋 **Özel Hizmetler**\n\n"; kb = []
    for s in ss:
        st = "✅" if s["active"] else "❌"
        left = time_remaining_str(s["expires_at"]) if s["active"] else "-"
        text += f"{st} **{esc(s['name'])}**\n   💰 {format_money(s['price_cents'])} • 👥 {s['current_users']}/{s['max_users']} • ⏳ {left}\n\n"
        if s["active"]: kb.append([InlineKeyboardButton(text=f"❌ İptal: {s['name'][:20]}", callback_data=f"admin:special_cancel:{s['id']}")])
    kb.append([InlineKeyboardButton(text="🔄", callback_data="admin:specials_list"), InlineKeyboardButton(text="🔙", callback_data="menu:admin")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML"); await cb.answer()

@router.callback_query(F.data.startswith("admin:special_cancel:"))
async def admin_special_cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    try: spid = int(cb.data.split(":", 2)[2])
    except: return await cb.answer("Geçersiz.")
    async with db_session() as db:
        cur = await db.execute("SELECT msg_id FROM special_services WHERE id = ?", (spid,))
        row = await cur.fetchone()
        await db.execute("UPDATE special_services SET active = 0 WHERE id = ?", (spid,))
        await db.commit()
    if row and row["msg_id"] and PROMO_CHANNEL_ID:
        try: await cb.bot.delete_message(PROMO_CHANNEL_ID, row["msg_id"])
        except: pass
    await cb.answer("İptal.", show_alert=True)
    await admin_specials_list(cb)

@router.callback_query(F.data == "admin:sync_channels")
async def admin_sync(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    if not PROMO_CHANNEL_ID: return await cb.answer("Kanal ayarlı değil.", show_alert=True)
    n = 0
    async with db_session() as db:
        cur = await db.execute("SELECT code FROM promos WHERE active = 1 AND message_id IS NOT NULL")
        codes = await cur.fetchall()
        cur = await db.execute("SELECT id FROM hazine WHERE active = 1 AND message_id IS NOT NULL")
        hzs = await cur.fetchall()
    for r in codes:
        try: await refresh_promo(cb.bot, r["code"]); n += 1
        except: pass
        await asyncio.sleep(0.4)
    for r in hzs:
        try: await refresh_hazine(cb.bot, r["id"]); n += 1
        except: pass
        await asyncio.sleep(0.4)
    await cb.answer(f"✅ {n} güncellendi.", show_alert=True)

# ================= ADMIN BALANCE =================
@router.callback_query(F.data.in_({"admin:bal_add", "admin:bal_rem"}))
async def admin_bal_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.")
    action = "eklemek" if "add" in cb.data else "silmek"
    await state.clear(); await state.set_state(AdminBalanceFlow.user_query); await state.update_data(action=cb.data)
    await safe_edit(cb, f"💰 **Bakiye {action}**\n\nKullanıcı @username veya ID:\n\n❌ /cancel", parse_mode="Markdown"); await cb.answer()

@router.message(AdminBalanceFlow.user_query)
async def admin_bal_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    q = message.text.strip()
    async with db_session() as db:
        if q.startswith("@"):
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE lower(username) = lower(?)", (q[1:],))
        else:
            cur = await db.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (q,))
        u = await cur.fetchone()
    if not u: return await message.answer("❌ Yok.")
    await state.update_data(target_id=u["user_id"], target_name=u["first_name"])
    await state.set_state(AdminBalanceFlow.amount)
    await message.answer(f"✅ **{u['first_name']}**\n\nMiktar:", parse_mode="Markdown")

@router.message(AdminBalanceFlow.amount)
async def admin_bal_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: amt = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    data = await state.get_data(); await state.clear()
    is_add = "add" in data["action"]
    tid = data["target_id"]
    async with db_session() as db:
        if is_add:
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amt, tid))
        else:
            cur = await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ? AND balance_cents >= ?", (amt, tid, amt))
            if cur.rowcount == 0:
                return await message.answer("❌ Kullanıcının yeterli bakiyesi yok.")
        await add_transaction(db, tid, amt if is_add else -amt, "admin", f"Admin {'+' if is_add else '-'}")
        await db.commit()
    await message.answer(f"✅ **{data['target_name']}** bakiyesi {format_money(amt)} {'artırıldı' if is_add else 'azaltıldı'}.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:total_users")
async def admin_total_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await safe_edit(cb, "⏳ Rapor hazırlanıyor...")
    async with db_session() as db:
        cur = await db.execute("SELECT user_id, username, first_name, balance_cents, vip_until, created_at FROM users ORDER BY created_at DESC")
        users = await cur.fetchall()
    total = len(users); now = now_utc()
    report = f"=== TOPLAM KULLANICI RAPORU ===\nToplam: {total}\n=================================\n\n"
    report += f"{'ID':<12} | {'Kullanıcı Adı':<15} | {'Adı':<15} | {'Kayıt':<10} | {'Bakiye':<10} | {'VIP'}\n"
    report += "-" * 90 + "\n"
    for u in users:
        try: vip = "Var" if u["vip_until"] and datetime.fromisoformat(u["vip_until"]) > now else "Yok"
        except: vip = "Hatalı"
        try: reg_date = (u["created_at"][:10]) if u["created_at"] else "-"
        except: reg_date = "Hatalı"
        uname = f"@{u['username']}" if u["username"] else "-"
        fname = u["first_name"] or "-"
        try: bal_str = format_money(u["balance_cents"] or 0)
        except: bal_str = "0.00 TL"
        report += f"{u['user_id']:<12} | {uname:<15} | {fname:<15} | {reg_date:<10} | {bal_str:<10} | {vip}\n"
    document = BufferedInputFile(report.encode("utf-8"), filename="kullanicilar.txt")
    await safe_edit(cb, f"📊 **Toplam Kullanıcı:** `{total}`\n\nDetaylı rapor:", parse_mode="Markdown", reply_markup=back_to_menu_kb())
    await cb.message.answer_document(document)
    await cb.answer()

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await state.clear(); await state.set_state(AdminBroadcastFlow.body)
    await safe_edit(cb, "📢 **Duyuru**\n\nGönderilecek mesajı yaz.\n\n❌ /cancel")
    await cb.answer()

@router.message(AdminBroadcastFlow.body)
async def admin_broadcast_body(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return
    body = (message.text or "").strip()
    if not body: return await message.answer("❌ Boş olamaz.")
    await state.clear()
    async with db_session() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_blocked = 0")
        users = await cur.fetchall()
    total = len(users)
    if total == 0: return await message.answer("❌ Hiç kullanıcı yok.")
    status_msg = await message.answer(f"⏳ Gönderiliyor... (0/{total})")
    sent = 0; failed = 0; blocked_now = 0
    for i, u in enumerate(users, 1):
        try:
            await bot.send_message(u["user_id"], body)
            sent += 1
        except Exception as e:
            failed += 1
            err = str(e).lower()
            if any(x in err for x in ("blocked", "deactivated", "chat not found", "user not found", "kicked")):
                async with db_session() as db:
                    await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (u["user_id"],))
                    await db.commit()
                blocked_now += 1
        if i % 25 == 0:
            try: await status_msg.edit_text(f"⏳ Gönderiliyor... ({i}/{total})")
            except: pass
        await asyncio.sleep(0.04)
    await message.answer(f"✅ **Tamamlandı**\n\n✅ Başarılı: `{sent}`\n❌ Başarısız: `{failed}`\n🚫 Yeni blok: `{blocked_now}`", reply_markup=back_to_menu_kb(), parse_mode="Markdown")

# ================= ADMIN: SERVICE / PRODUCT / SPECIAL / PROMO / HAZINE =================
@router.callback_query(F.data == "admin:service_add")
async def admin_service_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await state.clear(); await state.set_state(AdminServiceFlow.name)
    await safe_edit(cb, "📦 **Hizmet Ekle**\n\nHizmet adı:\n\n❌ /cancel", parse_mode="Markdown"); await cb.answer()

@router.message(AdminServiceFlow.name)
async def admin_service_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2: return await message.answer("❌ En az 2 karakter.")
    async with db_session() as db:
        await db.execute("INSERT INTO services (name) VALUES (?)", (name,))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ **{name}** eklendi.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:product_add")
async def admin_product_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        services = await cur.fetchall()
    if not services: return await safe_edit(cb, "⚠️ Önce hizmet ekle.", reply_markup=admin_panel_kb())
    await state.clear(); await state.set_state(AdminProductFlow.service_id)
    kb = [[InlineKeyboardButton(text=s["name"], callback_data=f"sel_srv:{s['service_id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu:admin")])
    await safe_edit(cb, "🛒 **Ürün Ekle**\n\nHizmet:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown"); await cb.answer()

@router.callback_query(AdminProductFlow.service_id, F.data.startswith("sel_srv:"))
async def admin_product_srv_selected(cb: CallbackQuery, state: FSMContext):
    try: sid = int(cb.data.split(":")[1])
    except: return await cb.answer("Geçersiz.")
    await state.update_data(service_id=sid)
    await state.set_state(AdminProductFlow.country_code)
    await safe_edit(cb, "🌍 Ülke kodu (örn: `+90`, `Global`):\n\n❌ /cancel"); await cb.answer()

@router.message(AdminProductFlow.country_code)
async def admin_product_cc(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 1: return await message.answer("❌ Geçersiz.")
    await state.update_data(country_code=message.text.strip())
    await state.set_state(AdminProductFlow.price)
    await message.answer("💵 Fiyat (TL):")

@router.message(AdminProductFlow.price)
async def admin_product_price(message: Message, state: FSMContext):
    try: price = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    data = await state.get_data(); await state.clear()
    async with db_session() as db:
        await db.execute("INSERT INTO products (service_id, country_code, price_cents) VALUES (?, ?, ?)", (data["service_id"], data["country_code"], price))
        await db.commit()
    await message.answer(f"✅ Ürün eklendi.\n{data['country_code']} — {format_money(price)}", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:special_add")
async def admin_special_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await state.clear(); await state.set_state(AdminSpecialServiceFlow.name)
    await safe_edit(cb, "✨ **Özel Hizmet**\n\nAd:\n\n❌ /cancel"); await cb.answer()

@router.message(AdminSpecialServiceFlow.name)
async def admin_special_name(message: Message, state: FSMContext):
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(AdminSpecialServiceFlow.price)
    await message.answer("💵 Fiyat (TL):")

@router.message(AdminSpecialServiceFlow.price)
async def admin_special_price(message: Message, state: FSMContext):
    try: price = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    await state.update_data(price=price)
    await state.set_state(AdminSpecialServiceFlow.max_users)
    await message.answer("👥 Kontenjan (1-10000):")

@router.message(AdminSpecialServiceFlow.max_users)
async def admin_special_users(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Sadece sayı.")
    val = int(message.text)
    if val < 1 or val > 10000: return await message.answer("❌ 1-10000 arası.")
    await state.update_data(max_users=val)
    await state.set_state(AdminSpecialServiceFlow.duration)
    await message.answer("⏳ Süre (dakika, 1-10080):")

@router.message(AdminSpecialServiceFlow.duration)
async def admin_special_duration(message: Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit(): return await message.answer("❌ Sadece sayı.")
    val = int(message.text)
    if val < 1 or val > 10080: return await message.answer("❌ 1-10080 dk arası.")
    data = await state.get_data(); await state.clear()
    expires_at = (now_utc() + timedelta(minutes=val)).isoformat()
    async with db_session() as db:
        cur = await db.execute("INSERT INTO special_services (name, price_cents, max_users, expires_at) VALUES (?, ?, ?, ?)", (data["name"], data["price"], data["max_users"], expires_at))
        spid = cur.lastrowid; await db.commit()
    if PROMO_CHANNEL_ID:
        username = await get_bot_username(bot)
        text = (f"✨ <b>ÖZEL HİZMET!</b>\n\n📦 <b>{esc(data['name'])}</b>\n💰 {format_money(data['price'])}\n"
                f"👥 {data['max_users']} kişi • ⏳ {val} dk\n\n⚡ Hemen kap!")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Satın Al", url=f"https://t.me/{username}?start=special_{spid}")]]) if username else None
        try:
            msg = await bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="HTML")
            async with db_session() as db:
                await db.execute("UPDATE special_services SET msg_id = ? WHERE id = ?", (msg.message_id, spid))
                await db.commit()
        except Exception as e: logger.error(f"Special channel: {e}")
    await message.answer(f"✅ **{data['name']}** oluşturuldu.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:promo_add")
async def admin_promo_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await state.clear(); await state.set_state(AdminPromoAddFlow.code)
    await safe_edit(cb, "🎁 **Promo Ekle**\n\nAdım 1: Kod:\n\n❌ /cancel"); await cb.answer()

@router.message(AdminPromoAddFlow.code)
async def admin_promo_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if len(code) < 2 or len(code) > 25: return await message.answer("❌ 2-25 karakter.")
    if not code.replace("_", "").replace("-", "").isalnum(): return await message.answer("❌ Harf/rakam/_/- kullan.")
    await state.update_data(code=code)
    await state.set_state(AdminPromoAddFlow.bonus)
    await message.answer("Adım 2: Ödül (TL):")

@router.message(AdminPromoAddFlow.bonus)
async def admin_promo_bonus(message: Message, state: FSMContext):
    try: bonus = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    await state.update_data(bonus=bonus)
    await state.set_state(AdminPromoAddFlow.max_uses)
    await message.answer("Adım 3: Max kullanım (1-100000):")

@router.message(AdminPromoAddFlow.max_uses)
async def admin_promo_uses(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Sayı gir.")
    val = int(message.text)
    if val < 1 or val > 100000: return await message.answer("❌ 1-100000 arası.")
    await state.update_data(max_uses=val)
    await state.set_state(AdminPromoAddFlow.duration)
    await message.answer("Adım 4: Süre (dakika, 1-10080):")

@router.message(AdminPromoAddFlow.duration)
async def admin_promo_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Sayı gir.")
    minutes = int(message.text)
    if minutes < 1 or minutes > 10080: return await message.answer("❌ 1-10080 dk arası.")
    data = await state.get_data()
    code = data.get("code")
    async with db_session() as db:
        cur = await db.execute("SELECT 1 FROM promos WHERE code = ?", (code,))
        if await cur.fetchone():
            return await message.answer(f"❌ **{code}** zaten var.")
    await state.clear()
    bonus = data["bonus"]; max_uses = data["max_uses"]
    expires_at = (now_utc() + timedelta(minutes=minutes)).isoformat()
    async with db_session() as db:
        await db.execute("INSERT INTO promos (code, bonus_cents, max_uses, expires_at) VALUES (?, ?, ?, ?)", (code, bonus, max_uses, expires_at))
        await db.commit()
    channel_msg = "⚠️ Kanal ayarlı değil."
    if PROMO_CHANNEL_ID:
        promo_like = {"code": code, "bonus_cents": bonus, "max_uses": max_uses, "current_uses": 0, "expires_at": expires_at}
        text, _, _ = build_promo_text(promo_like)
        username = await get_bot_username(message.bot)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Kodu Kullan", url=f"https://t.me/{username}?start=promo_{code}")]]) if username else None
        try:
            sent = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="HTML")
            async with db_session() as db:
                await db.execute("UPDATE promos SET message_id = ? WHERE code = ?", (sent.message_id, code))
                await db.commit()
            channel_msg = "✅ Kanala gönderildi!"
        except Exception as e:
            logger.error(f"Promo channel: {e}")
            channel_msg = f"⚠️ Kanala gönderilemedi: {e}"
    await message.answer(f"✅ **{esc(code)}** oluşturuldu.\n{channel_msg}", parse_mode="HTML", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:hazine_add")
async def admin_hazine_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    await state.clear(); await state.set_state(AdminHazineFlow.total_amount)
    await safe_edit(cb, "💰 **Hazine**\n\nAdım 1: Toplam tutar (TL):\n\n❌ /cancel"); await cb.answer()

@router.message(AdminHazineFlow.total_amount)
async def admin_hazine_amount(message: Message, state: FSMContext):
    try: amount = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz.")
    await state.update_data(total_amount=amount)
    await state.set_state(AdminHazineFlow.user_limit)
    await message.answer("Adım 2: Kişi sayısı (1-10000):")

@router.message(AdminHazineFlow.user_limit)
async def admin_hazine_limit(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Sayı gir.")
    val = int(message.text)
    if val < 1 or val > 10000: return await message.answer("❌ 1-10000 arası.")
    await state.update_data(user_limit=val)
    await state.set_state(AdminHazineFlow.duration)
    await message.answer("Adım 3: Süre (dakika, 1-10080):")

@router.message(AdminHazineFlow.duration)
async def admin_hazine_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Sayı gir.")
    minutes = int(message.text)
    if minutes < 1 or minutes > 10080: return await message.answer("❌ 1-10080 dk arası.")
    data = await state.get_data(); await state.clear()
    expires_at = (now_utc() + timedelta(minutes=minutes)).isoformat()
    async with db_session() as db:
        cur = await db.execute("INSERT INTO hazine (total_cents, remaining_cents, max_users, expires_at) VALUES (?, ?, ?, ?)", (data["total_amount"], data["total_amount"], data["user_limit"], expires_at))
        hid = cur.lastrowid; await db.commit()
    channel_msg = "⚠️ Kanal ayarlı değil."
    if PROMO_CHANNEL_ID:
        hz_like = {"total_cents": data["total_amount"], "remaining_cents": data["total_amount"], "max_users": data["user_limit"], "current_users": 0, "expires_at": expires_at}
        text, _, _ = build_hazine_text(hz_like)
        username = await get_bot_username(message.bot)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Kap", url=f"https://t.me/{username}?start=hazine_{hid}")]]) if username else None
        try:
            sent = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="HTML")
            async with db_session() as db:
                await db.execute("UPDATE hazine SET message_id = ? WHERE id = ?", (sent.message_id, hid))
                await db.commit()
            channel_msg = "✅ Kanala gönderildi!"
        except Exception as e:
            logger.error(f"Hazine channel: {e}")
            channel_msg = f"⚠️ Kanala gönderilemedi: {e}"
    await message.answer(f"✅ Hazine oluşturuldu.\n{channel_msg}", parse_mode="HTML", reply_markup=back_to_menu_kb())

# ================= ADMIN: NUMBER / CODE =================
@router.callback_query(F.data.startswith("n:"))
async def enter_number(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    try: oid = int(cb.data.split(":")[1])
    except: return await cb.answer("Geçersiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT status FROM orders WHERE order_id = ?", (oid,))
        o = await cur.fetchone()
        if not o: return await cb.answer("Sipariş yok.", show_alert=True)
        if o["status"] != "pending": return await cb.answer(f"Sipariş zaten {o['status']}.", show_alert=True)
    await state.clear(); await state.set_state(AdminEnterNumberFlow.country_code); await state.update_data(order_id=oid)
    await cb.message.answer("Ülke kodunu girin (örn: +90):\n\n❌ /cancel"); await cb.answer()

@router.message(AdminEnterNumberFlow.country_code)
async def receive_country_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(country_code=message.text.strip())
    await state.set_state(AdminEnterNumberFlow.phone)
    await message.answer("Telefon numarasını girin:")

@router.message(AdminEnterNumberFlow.phone)
async def receive_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data(); oid = data["order_id"]; cc = data["country_code"]
    phone = message.text.strip()
    if not phone: return await message.answer("❌ Numara boş olamaz.")
    await state.clear()
    async with db_session() as db:
        cur = await db.execute("SELECT status FROM orders WHERE order_id = ?", (oid,))
        o = await cur.fetchone()
        if not o or o["status"] != "pending": return await message.answer(f"❌ Sipariş #{oid} artık pending değil.")
    full_phone = f"{cc} {phone}"
    ORDER_PHONE_CACHE[oid] = full_phone
    stop_order_animation(oid)
    if oid in ORDER_MESSAGE_REFS:
        try:
            await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(f"Numara:\n{full_phone}"), parse_mode="HTML")
        except: pass
    await message.answer("Numara kaydedildi. Şimdi Kod Gir:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Kod Gir", callback_data=f"c:{oid}")]]))

@router.callback_query(F.data.startswith("c:"))
async def enter_code(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkisiz.", show_alert=True)
    try: oid = int(cb.data.split(":")[1])
    except: return await cb.answer("Geçersiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT status FROM orders WHERE order_id = ?", (oid,))
        o = await cur.fetchone()
        if not o: return await cb.answer("Sipariş yok.", show_alert=True)
        if o["status"] != "pending": return await cb.answer(f"Sipariş zaten {o['status']}.", show_alert=True)
    await state.clear(); await state.set_state(AdminEnterNumberFlow.code); await state.update_data(order_id=oid)
    await cb.message.answer("SMS kodunu girin:\n\n❌ /cancel"); await cb.answer()

@router.message(AdminEnterNumberFlow.code)
async def receive_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data(); oid = data["order_id"]; code = message.text.strip()
    await state.clear()
    phone = ORDER_PHONE_CACHE.pop(oid, "")
    if not code: return await message.answer("❌ Kod boş olamaz.")
    uid = None
    async with db_session() as db:
        cur = await db.execute("SELECT status, user_id FROM orders WHERE order_id = ?", (oid,))
        o = await cur.fetchone()
        if not o: return await message.answer(f"❌ Sipariş #{oid} bulunamadı.")
        if o["status"] != "pending": return await message.answer(f"❌ Sipariş #{oid} zaten **{o['status']}**.", parse_mode="Markdown")
        uid = o["user_id"]
        cur = await db.execute("UPDATE orders SET status = 'completed', phone = ?, code = ?, completed_at = ? WHERE order_id = ? AND status = 'pending'", (phone, code, timestamp(), oid))
        if cur.rowcount == 0: return await message.answer("❌ Sipariş zaten işlenmiş.")
        require = await get_setting("ref_require_order", db)
        if require == "1" and uid:
            cur = await db.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'", (uid,))
            order_count = (await cur.fetchone())["cnt"]
            if order_count == 1:
                cur = await db.execute("SELECT referral_id, referrer_id, reward_cents FROM referrals WHERE referred_user_id = ? AND rewarded_at IS NULL", (uid,))
                ref = await cur.fetchone()
                if ref:
                    await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (ref["reward_cents"], ref["referrer_id"]))
                    await db.execute("UPDATE referrals SET rewarded_at = ? WHERE referral_id = ?", (timestamp(), ref["referral_id"]))
                    await add_transaction(db, ref["referrer_id"], ref["reward_cents"], "referral", "Davet ödülü (ilk sipariş)")
                    try:
                        await message.bot.send_message(ref["referrer_id"], f"🎉 Davet ettiğin kişi ilk siparişini tamamladı!\n💰 +`{format_money(ref['reward_cents'])}`", parse_mode="Markdown")
                    except: pass
        await db.commit()
    if oid in ORDER_MESSAGE_REFS:
        try:
            await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(f"Numara:\n{phone}\nKod:\n{code}"), parse_mode="HTML")
        except: pass
    if uid:
        try:
            await message.bot.send_message(uid, f"🎉 **Tebrikler!**\n\n✅ Sipariş #{oid} tamamlandı.\n\n📱 Numara: `{phone}`\n🔑 Kod: `{code}`", parse_mode="Markdown")
        except: pass
        await unlock_achievement(message.bot, uid, "first_order")
        async with db_session() as db:
            cur = await db.execute("SELECT COUNT(*) as c FROM orders WHERE user_id = ? AND status = 'completed'", (uid,))
            if (await cur.fetchone())["c"] >= 10:
                await unlock_achievement(message.bot, uid, "ten_orders")
    await message.answer("✅ Sipariş kapatıldı.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data.startswith("o:"))
async def cancel_order(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) != 3: return await cb.answer("Geçersiz.", show_alert=True)
    action = parts[1]
    try: oid = int(parts[2])
    except: return await cb.answer("Geçersiz.", show_alert=True)
    if action != "refund" or not is_admin(cb.from_user.id):
        return await cb.answer("Yetkisiz.", show_alert=True)
    async with db_session() as db:
        cur = await db.execute("SELECT user_id, amount_cents FROM orders WHERE order_id = ? AND status = 'pending'", (oid,))
        order = await cur.fetchone()
        if not order: return await cb.answer("Sipariş zaten işlenmiş.", show_alert=True)
        cur = await db.execute("UPDATE orders SET status = 'refunded' WHERE order_id = ? AND status = 'pending'", (oid,))
        if cur.rowcount == 0: return await cb.answer("İade başarısız.", show_alert=True)
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (order["amount_cents"], order["user_id"]))
        await add_transaction(db, order["user_id"], order["amount_cents"], "refund", f"#{oid} İade")
        await db.commit()
    stop_order_animation(oid); ORDER_MESSAGE_REFS.pop(oid, None)
    try: await cb.bot.send_message(order["user_id"], "❌ Sipariş iptal edildi, bakiye iade edildi.")
    except: pass
    await cb.answer("İade yapıldı.", show_alert=True)

# ================= FALLBACK =================
@fallback_router.message()
async def fallback(message: Message):
    await message.answer("Komut anlaşılamadı. Lütfen /start yazarak menüyü açın.", reply_markup=back_to_menu_kb())

@fallback_router.callback_query()
async def fallback_callback(cb: CallbackQuery):
    await cb.answer("⚠️ Bu buton artık geçerli değil. /start yazıp menüyü yenileyin.", show_alert=True)

# ================= MAIN =================
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        print("HATA: BOT_TOKEN eksik!"); return
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(fallback_router)
    await init_db()
    try:
        me = await bot.get_me()
        _bot_username_cache["value"] = me.username or ""
    except: pass
    asyncio.create_task(channel_updater(bot))
    asyncio.create_task(background_cleanup(bot))
    print("Bot calisiyor...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())