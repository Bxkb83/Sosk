# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
JOIN_CHANNEL_URL = os.getenv("JOIN_CHANNEL_URL", "").strip()
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "").strip()
STAR_TOPUP_TIERS_RAW = os.getenv("STAR_TOPUP_TIERS", "50:25,100:50,250:125,500:250").strip()
VIP_PRICE_TL = os.getenv("VIP_PRICE_TL", "500").strip()
VIP_DURATION_DAYS = os.getenv("VIP_DURATION_DAYS", "30").strip()
CANCEL_SECONDS = 6 * 60
MAX_AMOUNT_CENTS = 100_000_000

router = Router()
fallback_router = Router(name="store-fallback")
logger = logging.getLogger("service-order-bot")

def channel_join_url():
    return JOIN_CHANNEL_URL or (f"https://t.me/{FORCE_JOIN_CHANNEL[1:]}" if FORCE_JOIN_CHANNEL.startswith("@") else "")

def parse_admin_ids():
    return {int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()}

ADMIN_IDS = parse_admin_ids()

# ==========================================
# STATES
# ==========================================
class AdminEnterNumberFlow(StatesGroup):
    country_code = State()
    phone = State()
    code = State()
class SupportFlow(StatesGroup):
    body = State()
class AdminBroadcastFlow(StatesGroup):
    body = State()
class AdminBalanceFlow(StatesGroup):
    user_query = State()
    amount = State()
class AdminPromoAddFlow(StatesGroup):
    code = State()
    bonus = State()
    max_uses = State()
    duration = State()
class AdminHazineFlow(StatesGroup):
    total_amount = State()
    user_limit = State()
    duration = State()

class AdminServiceFlow(StatesGroup):
    name = State()

class AdminProductFlow(StatesGroup):
    service_id = State()
    country_code = State()
    price = State()

class AdminSpecialServiceFlow(StatesGroup):
    name = State()
    price = State()
    max_users = State()
    duration = State()

# ==========================================
# YARDIMCI FONKSİYONLAR VE DİNAMİK ÇEMBER
# ==========================================
ORDER_ANIMATIONS = {}
ORDER_MESSAGE_REFS = {}
ORDER_PHONE_CACHE = {}

def generate_circle(text):
    lines = text.split('\n')
    max_len = max(len(line) for line in lines)
    
    if max_len <= 4: width = 13
    elif max_len <= 10: width = 17
    elif max_len <= 16: width = 23
    elif max_len <= 22: width = 29
    else: width = max_len + 8

    top = "  " + "•" * (width - 2) + "  "
    empty = "•" + " " * (width - 2) + "•"
    
    body_lines = []
    for line in lines:
        body_lines.append("•" + line.center(width - 2) + "•")
        
    return f"<pre>{top}\n{empty}\n" + "\n".join(body_lines) + f"\n{empty}\n{top}</pre>"

async def animate_order_status(bot, chat_id, message_id, order_id):
    step = 0
    try:
        while order_id in ORDER_ANIMATIONS:
            try:
                frame = "• • • • •\n•       •\n•  ---  •\n•       •\n• • • • •"
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"<pre>{frame}</pre>", parse_mode="HTML")
            except TelegramAPIError: pass
            step += 1
            await asyncio.sleep(0.8)
    except asyncio.CancelledError: return

def stop_order_animation(order_id): ORDER_ANIMATIONS.pop(order_id, None)
def now_utc(): return datetime.now(timezone.utc)
def timestamp(): return now_utc().isoformat(timespec="seconds")

def format_money(cents):
    whole, fraction = divmod(int(cents), 100)
    return f"{whole}.{str(fraction).zfill(2)} TL"

def parse_money(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
        if amount <= 0: raise ValueError
        return int(amount * 100)
    except: raise ValueError("Gecerli bir tutar girin.")

def parse_star_topup_tiers(raw):
    tiers = []
    for item in raw.split(","):
        pair = item.strip().split(":", 1)
        if len(pair) == 2 and pair[0].isdigit() and pair[1].replace(".", "").isdigit():
            tiers.append((int(pair[0]), int(float(pair[1]) * 100)))
    return sorted(tiers)

STAR_TOPUP_TIERS = parse_star_topup_tiers(STAR_TOPUP_TIERS_RAW)
REFERRAL_BONUS_CENTS = parse_money(os.getenv("REFERRAL_BONUS_TL", "5"))

def is_admin(user_id): return user_id in ADMIN_IDS

async def get_display_name(user_id):
    async with db_session() as db:
        cursor = await db.execute("SELECT username, first_name FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
    if row:
        if row['username']: return f"@{row['username']}"
        return f"{row['first_name']} (ID: {user_id})"
    return f"ID: {user_id}"

async def safe_edit(cb: CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass

# ==========================================
# VERİTABANI
# ==========================================
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

async def init_db():
    async with db_session() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance_cents INTEGER DEFAULT 0, vip_until TEXT, is_blocked INTEGER DEFAULT 0, created_at TEXT);
            
            CREATE TABLE IF NOT EXISTS services (
                service_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                name TEXT, 
                active INTEGER DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER,
                country_code TEXT,
                price_cents INTEGER,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS special_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                price_cents INTEGER,
                max_users INTEGER,
                current_users INTEGER DEFAULT 0,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                msg_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_type TEXT, item_id INTEGER, item_name TEXT, amount_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS transactions (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, kind TEXT, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS referrals (referral_id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_user_id INTEGER UNIQUE, reward_cents INTEGER, rewarded_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS star_payment_intents (payload TEXT PRIMARY KEY, user_id INTEGER, stars_amount INTEGER, credit_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS support_tickets (ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS promos (code TEXT PRIMARY KEY, bonus_cents INTEGER, max_uses INTEGER, current_uses INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS promo_uses (code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
            CREATE TABLE IF NOT EXISTS hazine (id INTEGER PRIMARY KEY AUTOINCREMENT, total_cents INTEGER, remaining_cents INTEGER, max_users INTEGER, current_users INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1, message_id INTEGER);
            CREATE TABLE IF NOT EXISTS hazine_claims (hazine_id INTEGER, user_id INTEGER, amount_cents INTEGER, PRIMARY KEY(hazine_id, user_id));
            
            INSERT OR IGNORE INTO settings (key, value) VALUES ('vip_enabled', '1'), ('vip_price_tl', '500'), ('vip_duration_days', '30'), ('ref_require_order', '1');
        """)
        await db.commit()

async def is_feature_enabled(feature):
    async with db_session() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (feature,))
        row = await cursor.fetchone()
        return row['value'] == '1' if row else False

async def get_setting(key):
    async with db_session() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row['value'] if row else None

async def ensure_user(user):
    async with db_session() as db:
        try:
            await db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user.id, timestamp()))
            await db.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", (user.username or "", user.first_name or "User", user.id))
            await db.commit()
        except: pass

async def add_transaction(db, user_id, amount, kind, note):
    await db.execute("INSERT INTO transactions (user_id, amount_cents, kind, note, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, amount, kind, note, timestamp()))

async def log_to_admins(bot, title, user_id, details):
    uname = await get_display_name(user_id)
    text = f"📝 **{title}**\n\n👤 Kullanıcı: {uname}\n📋 Detay: {details}\n📅 Tarih: {timestamp()[:16].replace('T', ' ')}"
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, text, parse_mode="Markdown")
        except: pass

async def notify_admins(bot, text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except: pass

# ==========================================
# MIDDLEWARE
# ==========================================
class JoinChannelMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id): return await handler(event, data)
        
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
        if isinstance(event, PreCheckoutQuery):
            return await handler(event, data)
        
        if isinstance(event, Message) and event.text and event.text.startswith("/start"): 
            await ensure_user(user)
            
        if not FORCE_JOIN_CHANNEL: return await handler(event, data)
        
        try:
            member = await event.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user.id)
            is_member = member.status in ("creator", "administrator", "member")
        except: is_member = None

        if is_member is False:
            text = "👋 Botu kullanmadan önce kanalımıza katılman gerekiyor."
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📣 Kanala Katıl", url=channel_join_url(), style="primary")], [InlineKeyboardButton(text="✅ Katıldım, Kontrol Et", callback_data="check_join", style="success")]])
            if isinstance(event, Message): await event.answer(text, reply_markup=kb)
            elif isinstance(event, CallbackQuery): await event.answer("Önce kanala katıl.", show_alert=True)
            return None
        return await handler(event, data)

router.message.outer_middleware(JoinChannelMiddleware())
router.callback_query.outer_middleware(JoinChannelMiddleware())

# ==========================================
# KLAVYELER
# ==========================================
def main_keyboard(user_id):
    rows = [
        [InlineKeyboardButton(text="🛍️ Market", callback_data="menu:catalog", style="primary"), InlineKeyboardButton(text="💰 Bakiyem", callback_data="menu:balance", style="success")],
        [InlineKeyboardButton(text="📦 Siparişlerim", callback_data="menu:orders", style="primary"), InlineKeyboardButton(text="⭐ Yıldız ile Yükle", callback_data="menu:topup", style="success")],
        [InlineKeyboardButton(text="💎 VIP Üyelik", callback_data="menu:vip", style="success"), InlineKeyboardButton(text="👥 Arkadaşını Davet Et", callback_data="menu:referral", style="primary")],
        [InlineKeyboardButton(text="🏆 Liderlik", callback_data="menu:leaderboard", style="success"), InlineKeyboardButton(text="💬 Destek", callback_data="menu:support", style="primary")],
        [InlineKeyboardButton(text="ℹ️ Yardım", callback_data="menu:help", style="primary")]
    ]
    if is_admin(user_id): rows.append([InlineKeyboardButton(text="🛠️ Admin Paneli", callback_data="menu:admin", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_to_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Ana Menüye Dön", callback_data="menu:back", style="primary")]])

def topup_keyboard():
    rows = [[InlineKeyboardButton(text=f"{s} ⭐ Yıldız -> {format_money(c)}", callback_data=f"star:{s}", style="success")] for s, c in STAR_TOPUP_TIERS]
    rows.append([InlineKeyboardButton(text="🔙 Geri Dön", callback_data="menu:back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Bakiye Ekle", callback_data="admin:bal_add", style="success"), InlineKeyboardButton(text="➖ Bakiye Sil", callback_data="admin:bal_rem", style="danger")],
        [InlineKeyboardButton(text="📦 Hizmet Ekle", callback_data="admin:service_add", style="primary"), InlineKeyboardButton(text="🛒 Ürün Ekle", callback_data="admin:product_add", style="success")],
        [InlineKeyboardButton(text="✨ Özel Hizmet Aç", callback_data="admin:special_add", style="danger")],
        [InlineKeyboardButton(text="🎁 Promo Ekle", callback_data="admin:promo_add", style="success"), InlineKeyboardButton(text="💰 Hazine Dağıt", callback_data="admin:hazine_add", style="primary")],
        [InlineKeyboardButton(text="👥 Toplam Kullanıcı (.txt)", callback_data="admin:total_users", style="success")],
        [InlineKeyboardButton(text="⚙️ Özellik Ayarları", callback_data="admin:settings", style="primary"), InlineKeyboardButton(text="📢 Duyuru Gönder", callback_data="admin:broadcast", style="danger")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")]
    ])

def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VIP Sistemi (Aç/Kapat)", callback_data="toggle:vip_enabled", style="primary")],
        [InlineKeyboardButton(text="Referans: İlk Sipariş Şartı (Aç/Kapat)", callback_data="toggle:ref_require_order", style="danger")],
        [InlineKeyboardButton(text="🔙 Admin Paneli", callback_data="menu:admin", style="success")]
    ])

# ==========================================
# START VE ANA MENÜ
# ==========================================
async def build_welcome_text(user_id, first_name):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'", (user_id,))
        order_count = (await cursor.fetchone())['cnt']
    vip_status = "Aktif Değil ❌"
    if user and user['vip_until']:
        try:
            if datetime.fromisoformat(user['vip_until']) > now_utc(): vip_status = f"Aktif ✅ ({user['vip_until'][:10]}'e kadar)"
        except: pass
    return (f"🌟 **Hoş Geldin, {first_name}!**\n\n💳 **Cüzdan Bilgileriniz:**\n├ 💰 Bakiye: `{format_money(user['balance_cents'] if user else 0)}`\n├ 📦 Sipariş: `{order_count}`\n└ 💎 VIP: `{vip_status}`\n\n🚀 *Sistemimiz 7/24 otomatik teslimat ile kesintisiz hizmet vermektedir.*\n⚡ *VIP üyelerimiz öncelikli işlem ve %20 indirim avantajına sahiptir.*\n\n👇 İşlem yapmak için aşağıdaki menüyü kullanın:")

@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    if not message.from_user: return
    uid = message.from_user.id
    
    await state.clear()
    
    try: await message.answer("🔄 Ana menü hazırlanıyor...", reply_markup=ReplyKeyboardRemove())
    except: pass

    async with db_session() as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,))
        is_new = await cursor.fetchone() is None
    
    if is_new:
        await ensure_user(message.from_user)

    args = message.text.split()
    if len(args) > 1:
        param = args[1]
        if param.startswith("ref_"):
            referrer_id = int(param[4:])
            if referrer_id != uid and is_new:
                async with db_session() as db:
                    await db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_user_id, reward_cents, created_at) VALUES (?, ?, ?, ?)", (referrer_id, uid, REFERRAL_BONUS_CENTS, timestamp()))
                    await db.commit()
                require_order = await get_setting('ref_require_order')
                if require_order == '0':
                    async with db_session() as db:
                        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (REFERRAL_BONUS_CENTS, referrer_id))
                        await db.execute("UPDATE referrals SET rewarded_at = ? WHERE referred_user_id = ?", (timestamp(), uid))
                        await add_transaction(db, referrer_id, REFERRAL_BONUS_CENTS, "referral", "Davet ödülü (Anında)")
                        await db.commit()
                    await log_to_admins(message.bot, "Yeni Referans", referrer_id, f"{uid} davet etti. +{format_money(REFERRAL_BONUS_CENTS)} verildi.")

        elif param.startswith("promo_"):
            code = param[6:].upper()
            await process_promo_claim(message, code)
            
        elif param.startswith("hazine_"):
            hz_id = int(param[7:])
            await process_hazine_claim(message, hz_id)

        elif param.startswith("special_"):
            sp_id = int(param[8:])
            await process_special_claim(message, sp_id, message.from_user)

    text = await build_welcome_text(uid, message.from_user.first_name)
    await message.answer(text, reply_markup=main_keyboard(uid), parse_mode="Markdown")

@router.callback_query(F.data == "menu:back")
async def back_to_menu(cb: CallbackQuery):
    text = await build_welcome_text(cb.from_user.id, cb.from_user.first_name)
    await safe_edit(cb, text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        row = await cursor.fetchone()
    vip_status = "Aktif Değil ❌" if not row or not row['vip_until'] or datetime.fromisoformat(row['vip_until']) <= now_utc() else f"Aktif ✅"
    text = (f"💰 **Cüzdan Detayları**\n\n🔹 Bakiye: `{format_money(row['balance_cents'] if row else 0)}`\n🔹 VIP Üyelik: `{vip_status}`")
    await safe_edit(cb, text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

# ==========================================
# VIP, YILDIZ VE DİĞER KULLANICI İŞLEMLERİ
# ==========================================
@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    if not await is_feature_enabled('vip_enabled'): 
        return await safe_edit(cb, "🚧 **VIP üyelik geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    
    text = f"💎 **VIP Üyelik**\n\nVIP üyelerimiz tüm siparişlerde **%20 indirim** kazanır ve siparişleri **öncelikli** işleme alınır.\n\n💰 **Fiyat:** `{VIP_PRICE_TL} TL`\n⏳ **Süre:** `{VIP_DURATION_DAYS} Gün`"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Satın Al", callback_data="vip_buy", style="success")], 
        [InlineKeyboardButton(text="🔙 Geri", callback_data="menu:back", style="primary")]
    ])
    await safe_edit(cb, text, reply_markup=kb, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "vip_buy")
async def vip_buy(cb: CallbackQuery):
    try: price_cents = int(float(VIP_PRICE_TL) * 100)
    except: price_cents = 50000
    try: days = int(VIP_DURATION_DAYS)
    except: days = 30
    
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (cb.from_user.id,))
        user = await cursor.fetchone()
        if not user: return await cb.answer("Kullanıcı bulunamadı, /start yazın.", show_alert=True)
        if user['balance_cents'] < price_cents: return await cb.answer("❌ Yetersiz bakiye.", show_alert=True)
        
        new_vip = (now_utc() + timedelta(days=days)).isoformat()
        await db.execute("UPDATE users SET balance_cents = balance_cents - ?, vip_until = ? WHERE user_id = ?", (price_cents, new_vip, cb.from_user.id))
        await add_transaction(db, cb.from_user.id, -price_cents, "vip", "VIP Satın Alma"); await db.commit()
        
    await safe_edit(cb, f"✅ Tebrikler! VIP üyeliğiniz başladı. Bitiş: {new_vip[:10]}", reply_markup=back_to_menu_kb())
    await cb.answer("VIP Aktif!", show_alert=True)

@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await safe_edit(cb, "⭐ **Yıldız ile Bakiye Yükleme**\n\nPaket seçin:", reply_markup=topup_keyboard(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("star:"))
async def create_star_invoice(cb: CallbackQuery):
    try:
        stars = int(cb.data.split(":")[1])
        tier = next((t for t in STAR_TOPUP_TIERS if t[0] == stars), None)
        if not tier: return await cb.answer("Geçersiz paket.", show_alert=True)
        
        payload = f"topup:{uuid4().hex}"
        async with db_session() as db:
            await db.execute("INSERT INTO star_payment_intents (payload, user_id, stars_amount, credit_cents, created_at) VALUES (?, ?, ?, ?, ?)", (payload, cb.from_user.id, stars, tier[1], timestamp()))
            await db.commit()
        
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id, title="Bakiye Yükleme", description=f"{format_money(tier[1])} bakiye", payload=payload, provider_token="", currency="XTR", prices=[LabeledPrice(label="Bakiye", amount=stars)]
        )
        await cb.answer()
    except Exception:
        await cb.answer(f"Ödeme başlatılamadı. Lütfen sonra deneyin.", show_alert=True)

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery): 
    await query.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(message: Message):
    payment = message.successful_payment
    async with db_session() as db:
        cursor = await db.execute("SELECT credit_cents, status FROM star_payment_intents WHERE payload = ?", (payment.invoice_payload,))
        row = await cursor.fetchone()
        if not row or row['status'] == 'paid':
            return
        credit_cents = row['credit_cents']
        await db.execute("UPDATE star_payment_intents SET status = 'paid' WHERE payload = ?", (payment.invoice_payload,))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (credit_cents, message.from_user.id))
        await add_transaction(db, message.from_user.id, credit_cents, "stars", "Yıldız ile yükleme")
        await db.commit()
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
        new_bal = (await cursor.fetchone())['balance_cents']
    await message.answer(f"🎉 **Tebrikler!**\n\n✅ Hesabınıza `{format_money(credit_cents)}` yüklendi.\n💰 Güncel Bakiyeniz: `{format_money(new_bal)}`", parse_mode="Markdown")

# ==========================================
# MARKET SİSTEMİ (Hizmet -> Ürün)
# ==========================================
@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        services = await cursor.fetchall()
    
    if not services:
        return await safe_edit(cb, "🛒 Şu an markette aktif hizmet bulunmuyor.", reply_markup=back_to_menu_kb())
    
    kb = [[InlineKeyboardButton(text=s['name'], callback_data=f"srv:{s['service_id']}", style="primary")] for s in services]
    kb.append([InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")])
    await safe_edit(cb, "🛍️ Lütfen bir hizmet seçin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("srv:"))
async def service_products(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name FROM services WHERE service_id = ?", (sid,)); srv = await cursor.fetchone()
        cursor = await db.execute("SELECT product_id, country_code, price_cents FROM products WHERE service_id = ? AND active = 1", (sid,)); products = await cursor.fetchall()
    
    if not products:
        return await cb.answer("Bu hizmetin aktif ürünü yok.", show_alert=True)
    
    text = f"📦 **{srv['name']}**\n\nLütfen ülke / numara türü seçin:"
    kb = []
    for p in products:
        cc = p['country_code'] if p['country_code'] else "Özel"
        kb.append([InlineKeyboardButton(text=f"{cc} - {format_money(p['price_cents'])}", callback_data=f"buyprod:{p['product_id']}", style="success")])
    
    kb.append([InlineKeyboardButton(text="🔙 Geri", callback_data="menu:catalog", style="primary")])
    await safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("buyprod:"))
async def buy_product(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT p.price_cents, p.country_code, s.name FROM products p JOIN services s ON p.service_id = s.service_id WHERE p.product_id = ?", (pid,))
        p = await cursor.fetchone()
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,)); u = await cursor.fetchone()
    
    is_vip = u and u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now_utc()
    final_price = int(p['price_cents'] * 0.8) if is_vip else p['price_cents']
    full_name = f"{p['name']} ({p['country_code']})"

    if u['balance_cents'] < final_price: return await cb.answer("Yetersiz bakiye.", show_alert=True)
    
    async with db_session() as db:
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (final_price, cb.from_user.id))
        cursor = await db.execute("INSERT INTO orders (user_id, item_type, item_id, item_name, amount_cents, status, created_at, expires_at) VALUES (?, 'product', ?, ?, ?, 'pending', ?, ?)", 
                                 (cb.from_user.id, pid, full_name, final_price, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat()))
        oid = cursor.lastrowid; await add_transaction(db, cb.from_user.id, -final_price, "order", f"Sipariş #{oid}"); await db.commit()
    
    await cb.message.answer(f"🎉 **Başarılar!**\n\n✅ Siparişiniz ({full_name}) alındı.\n⏳ Yönetici numaranızı iletecek.", parse_mode="Markdown")
    
    try:
        msg = await cb.bot.send_message(cb.from_user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True; ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass
    
    uname = await get_display_name(cb.from_user.id)
    await notify_admins(cb.bot, f"🔔 **Yeni Sipariş #{oid}**\n👤 {uname}\n📦 {full_name}\n💵 {format_money(final_price)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara Gir", callback_data=f"n:{oid}", style="success")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}", style="danger")]]))
    await cb.answer("Sipariş verildi.")

# ==========================================
# ÖZEL HİZMET KULLANIM MANTIĞI
# ==========================================
async def process_special_claim(message, sp_id, user):
    async with db_session() as db:
        cursor = await db.execute("SELECT id, name, price_cents, max_users, current_users, expires_at, active FROM special_services WHERE id = ?", (sp_id,))
        sp = await cursor.fetchone()
        
        if not sp or not sp['active'] or datetime.fromisoformat(sp['expires_at']) < now_utc():
            return await message.answer("❌ Bu özel hizmetin süresi dolmuş veya kapanmış.")
        if sp['current_users'] >= sp['max_users']:
            return await message.answer("❌ Bu özel hizmet için kontenjan dolmuş.")
            
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (user.id,)); u = await cursor.fetchone()
        
        price = sp['price_cents']
        if u['balance_cents'] < price: return await message.answer("❌ Yetersiz bakiye.")
        
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (price, user.id))
        await db.execute("UPDATE special_services SET current_users = current_users + 1 WHERE id = ?", (sp_id,))
        
        new_users = sp['current_users'] + 1
        if new_users >= sp['max_users']:
            await db.execute("UPDATE special_services SET active = 0 WHERE id = ?", (sp_id,))
            
        cursor = await db.execute("INSERT INTO orders (user_id, item_type, item_id, item_name, amount_cents, status, created_at, expires_at) VALUES (?, 'special', ?, ?, ?, 'pending', ?, ?)", 
                                 (user.id, sp_id, sp['name'], price, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat()))
        oid = cursor.lastrowid; await add_transaction(db, user.id, -price, "order", f"Sipariş #{oid}"); await db.commit()
        
        if new_users >= sp['max_users'] and sp['msg_id'] and PROMO_CHANNEL_ID:
            try: await message.bot.delete_message(PROMO_CHANNEL_ID, sp['msg_id'])
            except: pass

    await message.answer(f"🎉 **Başarılar!**\n\n✅ `{sp['name']}` özel hizmetini satın aldınız.\n⏳ Lütfen yöneticinin numara göndermesini bekleyin.")
    
    try:
        msg = await message.bot.send_message(user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True; ORDER_MESSAGE_REFS[oid] = (user.id, msg.message_id)
        asyncio.create_task(animate_order_status(message.bot, user.id, msg.message_id, oid))
    except: pass
    
    uname = await get_display_name(user.id)
    await notify_admins(message.bot, f"🔔 **Özel Hizmet Siparişi #{oid}**\n👤 {uname}\n📦 {sp['name']}\n💵 {format_money(price)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara Gir", callback_data=f"n:{oid}", style="success")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}", style="danger")]]))

# ==========================================
# ADMİN: HİZMET VE ÜRÜN EKLEME
# ==========================================
@router.callback_query(F.data == "admin:service_add")
async def admin_service_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminServiceFlow.name)
    await safe_edit(cb, "📦 **Hizmet Ekle**\n\nLütfen hizmet adını yazın (Örn: `WhatsApp`, `Telegram`):")
    await cb.answer()

@router.message(AdminServiceFlow.name)
async def admin_service_name(message: Message, state: FSMContext):
    name = message.text.strip()
    async with db_session() as db:
        await db.execute("INSERT INTO services (name) VALUES (?)", (name,))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ **{name}** hizmeti başarıyla eklendi.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:product_add")
async def admin_product_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    async with db_session() as db:
        cursor = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        services = await cursor.fetchall()
    
    if not services:
        return await safe_edit(cb, "⚠️ Önce bir hizmet eklemelisiniz.", reply_markup=admin_panel_kb())
    
    await state.set_state(AdminProductFlow.service_id)
    kb = [[InlineKeyboardButton(text=s['name'], callback_data=f"sel_srv:{s['service_id']}", style="primary")] for s in services]
    kb.append([InlineKeyboardButton(text="🔙 İptal", callback_data="menu:admin", style="danger")])
    await safe_edit(cb, "🛒 **Ürün Ekle**\n\nHangi hizmete ürün ekleyeceksiniz?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(AdminProductFlow.service_id, F.data.startswith("sel_srv:"))
async def admin_product_srv_selected(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    await state.update_data(service_id=sid)
    await state.set_state(AdminProductFlow.country_code)
    await safe_edit(cb, "🌍 Lütfen ülke kodunu veya özel adı yazın (Örn: `+90`, `+27`, `Global`):")
    await cb.answer()

@router.message(AdminProductFlow.country_code)
async def admin_product_cc(message: Message, state: FSMContext):
    await state.update_data(country_code=message.text.strip())
    await state.set_state(AdminProductFlow.price)
    await message.answer("💵 Lütfen bu ürünün fiyatını TL olarak yazın (Örn: `150`):")

@router.message(AdminProductFlow.price)
async def admin_product_price(message: Message, state: FSMContext):
    try: price = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz fiyat.")
    data = await state.get_data()
    async with db_session() as db:
        await db.execute("INSERT INTO products (service_id, country_code, price_cents) VALUES (?, ?, ?)", (data['service_id'], data['country_code'], price))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Ürün başarıyla eklendi.\nÜlke: `{data['country_code']}`\nFiyat: {format_money(price)}", reply_markup=back_to_menu_kb())

# ==========================================
# ADMİN: ÖZEL HİZMET AÇMA
# ==========================================
@router.callback_query(F.data == "admin:special_add")
async def admin_special_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminSpecialServiceFlow.name)
    await safe_edit(cb, "✨ **Özel Hizmet Aç**\n\nLütfen hizmetin adını yazın (Örn: `VIP Numara`, `Premium Hesap`):")
    await cb.answer()

@router.message(AdminSpecialServiceFlow.name)
async def admin_special_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminSpecialServiceFlow.price)
    await message.answer("💵 Bu hizmetin fiyatı ne kadar? (TL):")

@router.message(AdminSpecialServiceFlow.price)
async def admin_special_price(message: Message, state: FSMContext):
    try: price = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz fiyat.")
    await state.update_data(price=price)
    await state.set_state(AdminSpecialServiceFlow.max_users)
    await message.answer("👥 Bu hizmetten toplam kaç kişi faydalanabilsin? (Örn: `5`):")

@router.message(AdminSpecialServiceFlow.max_users)
async def admin_special_users(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    await state.update_data(max_users=int(message.text))
    await state.set_state(AdminSpecialServiceFlow.duration)
    await message.answer("⏳ Bu hizmet kaç dakika aktif kalsın? (Örn: `60`):")

@router.message(AdminSpecialServiceFlow.duration)
async def admin_special_duration(message: Message, state: FSMContext, bot: Bot):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    duration = int(message.text)
    data = await state.get_data(); await state.clear()
    
    expires_at = (now_utc() + timedelta(minutes=duration)).isoformat()
    
    async with db_session() as db:
        cursor = await db.execute("INSERT INTO special_services (name, price_cents, max_users, expires_at) VALUES (?, ?, ?, ?)", (data['name'], data['price'], data['max_users'], expires_at))
        sp_id = cursor.lastrowid
        await db.commit()

    if PROMO_CHANNEL_ID:
        bot_me = await bot.get_me()
        text = (f"✨ **ÖZEL HİZMET AÇILDI!** ✨\n"
                f"╭──────────────────────────╮\n"
                f"   📦 **{data['name']}**\n"
                f"╰──────────────────────────╯\n\n"
                f"💰 **Fiyat:** `{format_money(data['price'])}`\n"
                f"👥 **Kontenjan:** `{data['max_users']}` Kişi\n"
                f"⏳ **Süre:** `{duration}` Dakika\n\n"
                f"⚡ *Kontenjan dolduğunda veya süre bittiğinde kapanır!*\n"
                f"👇 Hemen kapmak için bota gidin:")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Satın Al", url=f"https://t.me/{bot_me.username}?start=special_{sp_id}", style="success")]])
        try:
            msg = await bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="Markdown")
            async with db_session() as db:
                await db.execute("UPDATE special_services SET msg_id = ? WHERE id = ?", (msg.message_id, sp_id))
                await db.commit()
        except Exception: pass
        
    await message.answer(f"✅ **{data['name']}** özel hizmeti kanala duyuruldu.", reply_markup=back_to_menu_kb())

# ==========================================
# ADMİN PANELİ VE DİĞER İŞLEMLER
# ==========================================
@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    await safe_edit(cb, "🛠️ **Admin Paneli**\n\nİşlem seçin:", reply_markup=admin_panel_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "admin:settings")
async def admin_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await safe_edit(cb, "⚙️ **Özellik Ayarları**\n\nAçıp kapatmak istediğiniz özelliği seçin:", reply_markup=settings_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("toggle:"))
async def toggle_setting(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    key = cb.data.split(":")[1]
    async with db_session() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        new_val = '0' if row['value'] == '1' else '1'
        await db.execute("UPDATE settings SET value = ? WHERE key = ?", (new_val, key))
        await db.commit()
    await cb.answer(f"Özellik {'Açıldı' if new_val == '1' else 'Kapatıldı'}!", show_alert=True)

@router.callback_query(F.data.in_({"admin:bal_add", "admin:bal_rem"}))
async def admin_bal_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    action = "eklemek" if "add" in cb.data else "silmek"
    await state.set_state(AdminBalanceFlow.user_query)
    await state.update_data(action=cb.data)
    await safe_edit(cb, f"💰 **Bakiye {action.capitalize()}**\n\nLütfen kullanıcının `@kullanıcıadı` veya `Telegram ID`'sini yazın:", parse_mode="Markdown")
    await cb.answer()

@router.message(AdminBalanceFlow.user_query)
async def admin_bal_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    query = message.text.strip()
    async with db_session() as db:
        if query.startswith("@"):
            cursor = await db.execute("SELECT user_id, first_name FROM users WHERE lower(username) = lower(?)", (query[1:],))
        else:
            cursor = await db.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (query,))
        user = await cursor.fetchone()
    if not user: return await message.answer("❌ **Böyle bir kullanıcı yok!**")
    await state.update_data(target_id=user['user_id'], target_name=user['first_name'])
    await state.set_state(AdminBalanceFlow.amount)
    await message.answer(f"✅ Kullanıcı: **{user['first_name']}**\n\nLütfen tutarı yazın (Örn: `50`):", parse_mode="Markdown")

@router.message(AdminBalanceFlow.amount)
async def admin_bal_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: amount = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz tutar.")
    data = await state.get_data(); await state.clear()
    is_add = "add" in data['action']
    async with db_session() as db:
        if is_add: await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amount, data['target_id']))
        else: await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (amount, data['target_id']))
        await add_transaction(db, data['target_id'], amount if is_add else -amount, "admin", f"Admin bakiye {'ekledi' if is_add else 'sildi'}")
        await db.commit()
    await message.answer(f"✅ **{data['target_name']}** bakiyesi {format_money(amount)} {'artırıldı' if is_add else 'azaltıldı'}.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:total_users")
async def admin_total_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await safe_edit(cb, "⏳ Rapor hazırlanıyor...")
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id, username, first_name, balance_cents, vip_until, created_at FROM users ORDER BY created_at DESC")
        users = await cursor.fetchall()
    total = len(users); now = now_utc()
    report = f"=== TOPLAM KULLANICI RAPORU ===\nToplam: {total}\n=================================\n\n"
    report += f"{'ID':<12} | {'Kullanıcı Adı':<15} | {'Adı':<15} | {'Kayıt':<10} | {'Bakiye':<10} | {'VIP'}\n"
    report += "-" * 90 + "\n"
    for u in users:
        vip = "Var" if u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now else "Yok"
        uname = f"@{u['username']}" if u['username'] else "-"
        fname = u['first_name'] or "-"
        reg_date = u['created_at'][:10] if u['created_at'] else "-"
        report += f"{u['user_id']:<12} | {uname:<15} | {fname:<15} | {reg_date:<10} | {format_money(u['balance_cents']):<10} | {vip}\n"
    document = BufferedInputFile(report.encode('utf-8'), filename="kullanicilar.txt")
    await safe_edit(cb, f"📊 **Toplam Kullanıcı:** `{total}`\n\nDetaylı rapor aşağıda:", parse_mode="Markdown", reply_markup=back_to_menu_kb())
    await cb.message.answer_document(document)
    await cb.answer()

@router.callback_query(F.data == "admin:promo_add")
async def admin_promo_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminPromoAddFlow.code)
    await safe_edit(cb, "🎁 **Promo Kod Ekle**\n\nAdım 1: Promo kodunu yazın (Örn: `YENIYIL`):")
    await cb.answer()

@router.message(AdminPromoAddFlow.code)
async def admin_promo_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.upper()); await state.set_state(AdminPromoAddFlow.bonus); await message.answer("Adım 2: Ödül miktarı? (TL, Örn: `20`)")

@router.message(AdminPromoAddFlow.bonus)
async def admin_promo_bonus(message: Message, state: FSMContext):
    try: bonus = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz tutar.")
    await state.update_data(bonus=bonus); await state.set_state(AdminPromoAddFlow.max_uses); await message.answer("Adım 3: Toplam kaç kişi kullanabilir? (Örn: `100`)")

@router.message(AdminPromoAddFlow.max_uses)
async def admin_promo_uses(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    await state.update_data(max_uses=int(message.text)); await state.set_state(AdminPromoAddFlow.duration); await message.answer("Adım 4: Geçerlilik süresi kaç dakika? (Örn: `1440` - 1 Gün)")

@router.message(AdminPromoAddFlow.duration)
async def admin_promo_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    data = await state.get_data(); await state.clear()
    expires_at = (now_utc() + timedelta(minutes=int(message.text))).isoformat()
    
    async with db_session() as db:
        cursor = await db.execute("SELECT 1 FROM promos WHERE code = ?", (data['code'],))
        if await cursor.fetchone(): return await message.answer("❌ Bu kod zaten var.")
        
        if PROMO_CHANNEL_ID:
            bot_me = await message.bot.get_me()
            text = (f"╭──────────────────────────╮\n"
                    f"   🎁 **PROMO KOD GELDİ**\n"
                    f"╰──────────────────────────╯\n\n"
                    f"🔑 **Kod:** `{data['code']}`\n"
                    f"💰 **Ödül:** `{format_money(data['bonus'])}`\n"
                    f"👥 **Kalan Kullanım:** `{data['max_uses']}`\n"
                    f"⏳ **Süre:** `{message.text}` dakika\n\n"
                    f"👇 Kodu almak için bota gidin:")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Bota Gir (Kullan)", url=f"https://t.me/{bot_me.username}?start=promo_{data['code']}", style="success")]])
            try:
                msg = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="Markdown")
                await db.execute("INSERT INTO promos (code, bonus_cents, max_uses, expires_at, message_id) VALUES (?, ?, ?, ?, ?)", (data['code'], data['bonus'], data['max_uses'], expires_at, msg.message_id))
                await db.commit()
                await message.answer(f"✅ **{data['code']}** kodu kanala gönderildi.", reply_markup=back_to_menu_kb())
                return
            except Exception: pass
            
        await db.execute("INSERT INTO promos (code, bonus_cents, max_uses, expires_at) VALUES (?, ?, ?, ?)", (data['code'], data['bonus'], data['max_uses'], expires_at))
        await db.commit()
    await message.answer(f"✅ **{data['code']}** kodu oluşturuldu.", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:hazine_add")
async def admin_hazine_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminHazineFlow.total_amount)
    await safe_edit(cb, "💰 **Hazine Patlatma**\n\nAdım 1: Dağıtılacak toplam bakiye ne kadar? (TL, Örn: `100`)")
    await cb.answer()

@router.message(AdminHazineFlow.total_amount)
async def admin_hazine_amount(message: Message, state: FSMContext):
    try: amount = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz tutar.")
    await state.update_data(total_amount=amount); await state.set_state(AdminHazineFlow.user_limit); await message.answer("Adım 2: Bu hazineden kaç kişi faydalanabilsin? (Örn: `50`)")

@router.message(AdminHazineFlow.user_limit)
async def admin_hazine_limit(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    await state.update_data(user_limit=int(message.text)); await state.set_state(AdminHazineFlow.duration); await message.answer("Adım 3: Hazinenin süresi kaç dakika? (Örn: `30`)")

@router.message(AdminHazineFlow.duration)
async def admin_hazine_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("❌ Lütfen sayı girin.")
    data = await state.get_data(); await state.clear()
    expires_at = (now_utc() + timedelta(minutes=int(message.text))).isoformat()
    
    async with db_session() as db:
        cursor = await db.execute("INSERT INTO hazine (total_cents, remaining_cents, max_users, expires_at) VALUES (?, ?, ?, ?)", (data['total_amount'], data['total_amount'], data['user_limit'], expires_at))
        hz_id = cursor.lastrowid
        await db.commit()

    if PROMO_CHANNEL_ID:
        bot_me = await message.bot.get_me()
        text = (f"╭──────────────────────────╮\n"
                f"   💰 **HAZİNE PATLADI!**\n"
                f"╰──────────────────────────╯\n\n"
                f"💎 **Toplam Hazine:** `{format_money(data['total_amount'])}`\n"
                f"👥 **Kalan Kişi:** `{data['user_limit']}`\n"
                f"⏳ **Süre:** `{message.text}` dakika\n\n"
                f"⚡ *Her kullanıcıya rastgele miktarda bakiye verilir!*\n"
                f"👇 Hemen kapmak için bota gidin:")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Hazineden Kap", url=f"https://t.me/{bot_me.username}?start=hazine_{hz_id}", style="success")]])
        try:
            msg = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="Markdown")
            async with db_session() as db:
                await db.execute("UPDATE hazine SET message_id = ? WHERE id = ?", (msg.message_id, hz_id))
                await db.commit()
            await message.answer("✅ Hazine kanala gönderildi.", reply_markup=back_to_menu_kb())
            return
        except Exception: pass
    await message.answer("✅ Hazine oluşturuldu.", reply_markup=back_to_menu_kb())

# ==========================================
# ADMİN: NUMARA VE KOD GİRİŞİ (KARE İÇİNDE)
# ==========================================
@router.callback_query(F.data.startswith("n:"))
async def enter_number(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminEnterNumberFlow.country_code)
    await state.update_data(order_id=int(cb.data.split(":")[1]))
    await cb.message.answer("Ülke kodunu girin (Örn: +90):")
    await cb.answer()

@router.message(AdminEnterNumberFlow.country_code)
async def receive_country_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(country_code=message.text.strip())
    await state.set_state(AdminEnterNumberFlow.phone)
    await message.answer("Telefon numarasını girin (Örn: 500 500 50 50):")

@router.message(AdminEnterNumberFlow.phone)
async def receive_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data(); oid = data['order_id']; cc = data['country_code']; phone = message.text.strip()
    await state.clear()
    full_phone = f"{cc} {phone}"
    ORDER_PHONE_CACHE[oid] = full_phone
    stop_order_animation(oid)
    
    if oid in ORDER_MESSAGE_REFS:
        try: 
            await message.bot.edit_message_text(
                ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(f"Numara:\n{full_phone}"), parse_mode="HTML"
            )
        except: pass
        
    await message.answer(f"Numara iletildi. Şimdi Kod Gir butonuna basın.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Kod Gir", callback_data=f"c:{oid}", style="success")]]))

@router.callback_query(F.data.startswith("c:"))
async def enter_code(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminEnterNumberFlow.code)
    await state.update_data(order_id=int(cb.data.split(":")[1]))
    await cb.message.answer("SMS kodunu girin:")
    await cb.answer()

@router.message(AdminEnterNumberFlow.code)
async def receive_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data(); oid = data['order_id']; code = message.text.strip()
    await state.clear()
    phone = ORDER_PHONE_CACHE.pop(oid, "")
    
    if oid in ORDER_MESSAGE_REFS:
        try: 
            await message.bot.edit_message_text(
                ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(f"Numara:\n{phone}\nKod:\n{code}"), parse_mode="HTML"
            )
        except: pass
        
    async with db_session() as db:
        await db.execute("UPDATE orders SET status = 'completed' WHERE order_id = ?", (oid,))
        require_order = await get_setting('ref_require_order')
        if require_order == '1':
            cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ?", (oid,)); uid = (await cursor.fetchone())['user_id']
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'", (uid,)); order_count = (await cursor.fetchone())['cnt']
            if order_count == 1: 
                cursor = await db.execute("SELECT referral_id, referrer_id, reward_cents FROM referrals WHERE referred_user_id = ? AND rewarded_at IS NULL", (uid,))
                ref = await cursor.fetchone()
                if ref:
                    await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (ref['reward_cents'], ref['referrer_id']))
                    await db.execute("UPDATE referrals SET rewarded_at = ? WHERE referral_id = ?", (timestamp(), ref['referral_id']))
                    await add_transaction(db, ref['referrer_id'], ref['reward_cents'], "referral", "Davet ödülü (İlk Sipariş)")
                    try: await message.bot.send_message(ref['referrer_id'], f"🎉 Tebrikler! Davet ettiğiniz kişi ilk siparişini tamamladı ve `{format_money(ref['reward_cents'])}` kazandınız.", parse_mode="Markdown")
                    except: pass
        await db.commit()
        cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ?", (oid,)); uid = (await cursor.fetchone())['user_id']
        
    await message.bot.send_message(uid, f"🎉 **Tebrikler, Başarılar!**\n\n✅ Siparişiniz tamamlandı ve numaranız hazır.\n\n📱 Numara: `{phone}`\n🔑 Kod: `{code}`", parse_mode="Markdown")
    await message.answer("Sipariş başarıyla kapatıldı.")

@router.callback_query(F.data.startswith("o:"))
async def cancel_order(cb: CallbackQuery):
    action, oid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    if action == "refund" and not is_admin(cb.from_user.id): return
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id, amount_cents FROM orders WHERE order_id = ? AND status = 'pending'", (oid,)); order = await cursor.fetchone()
        if order:
            await db.execute("UPDATE orders SET status = 'refunded' WHERE order_id = ?", (oid,))
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (order['amount_cents'], order['user_id']))
            await add_transaction(db, order['user_id'], order['amount_cents'], "refund", f"#{oid} İade"); await db.commit()
            stop_order_animation(oid); ORDER_MESSAGE_REFS.pop(oid, None)
            await cb.bot.send_message(order['user_id'], "❌ Sipariş iptal edildi, bakiye iade edildi."); await cb.answer("İade yapıldı.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear(); await message.answer("İptal edildi.", reply_markup=back_to_menu_kb())

@fallback_router.message()
async def fallback(message: Message):
    await message.answer("Komut anlaşılamadı. Lütfen /start yazarak menüyü açın.", reply_markup=back_to_menu_kb())

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(fallback_router)
    await init_db()
    print("Bot calisiyor...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())