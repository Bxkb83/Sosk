# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
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
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "").strip()
STAR_TOPUP_TIERS_RAW = os.getenv("STAR_TOPUP_TIERS", "50:25,100:50,250:125,500:250").strip()
VIP_PRICE_TL = os.getenv("VIP_PRICE_TL", "500").strip() # Railway'deki 500 TL ayarını çeker
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
class PromoFlow(StatesGroup):
    code = State()
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
class AdminServiceAddFlow(StatesGroup):
    name = State()
    price = State()
    description = State()
class AdminHazineFlow(StatesGroup):
    total_amount = State()
    user_limit = State()
    duration = State()

# ==========================================
# YARDIMCI FONKSİYONLAR VE DİNAMİK ÇEMBER
# ==========================================
ORDER_ANIMATIONS = {}
ORDER_MESSAGE_REFS = {}
ORDER_PHONE_CACHE = {}

def generate_circle(text):
    length = len(text)
    if length <= 4: width, height = 9, 3
    elif length <= 8: width, height = 13, 3
    elif length <= 12: width, height = 17, 5
    elif length <= 16: width, height = 21, 5
    else: width, height = length + 6, 5

    top = "  " + "•" * (width - 2) + "  "
    empty = "•" + " " * (width - 2) + "•"
    mid = "•" + text.center(width - 2) + "•"
    
    if height == 3: return f"<pre>{top}\n{mid}\n{top}</pre>"
    else: return f"<pre>{top}\n{empty}\n{mid}\n{empty}\n{top}</pre>"

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
        return f"{row['first_name']}"
    return f"Bilinmeyen Kullanıcı"

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
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance_cents INTEGER DEFAULT 0, points INTEGER DEFAULT 0, vip_until TEXT, last_daily_at TEXT, last_seen_at TEXT, is_blocked INTEGER DEFAULT 0, created_at TEXT);
            CREATE TABLE IF NOT EXISTS services (service_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, price_cents INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_id INTEGER, service_name TEXT, amount_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS transactions (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, kind TEXT, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS referrals (referral_id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_user_id INTEGER UNIQUE, reward_cents INTEGER, rewarded_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS star_payment_intents (payload TEXT PRIMARY KEY, user_id INTEGER, stars_amount INTEGER, credit_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS support_tickets (ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, reward_cents INTEGER, channel_id TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS user_tasks (user_id INTEGER, task_id INTEGER, claimed_at TEXT, PRIMARY KEY(user_id, task_id));
            
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY, 
                bonus_cents INTEGER, 
                max_uses INTEGER, 
                current_uses INTEGER DEFAULT 0, 
                expires_at TEXT, 
                active INTEGER DEFAULT 1,
                message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS promo_uses (code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
            
            CREATE TABLE IF NOT EXISTS hazine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_cents INTEGER,
                remaining_cents INTEGER,
                max_users INTEGER,
                current_users INTEGER DEFAULT 0,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS hazine_claims (hazine_id INTEGER, user_id INTEGER, amount_cents INTEGER, PRIMARY KEY(hazine_id, user_id));

            INSERT OR IGNORE INTO settings (key, value) VALUES 
            ('vip_enabled', '1'), ('coupon_enabled', '1'), ('rewards_enabled', '1'), 
            ('vip_price_tl', '500'), ('vip_duration_days', '30'), ('ref_require_order', '1');
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
        await db.execute("INSERT INTO users (user_id, username, first_name, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, username = excluded.username, is_blocked = 0",
                         (user.id, user.username or "", user.first_name or "User", timestamp(), timestamp()))
        await db.commit()

async def add_transaction(db, user_id, amount, kind, note):
    await db.execute("INSERT INTO transactions (user_id, amount_cents, kind, note, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, amount, kind, note, timestamp()))

async def notify_admins(bot, text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except: pass

async def handle_referral_penalty(bot, user_id, reason):
    async with db_session() as db:
        cursor = await db.execute("SELECT referral_id, referrer_id, reward_cents, rewarded_at, created_at FROM referrals WHERE referred_user_id = ?", (user_id,))
        ref = await cursor.fetchone()
        if ref and ref['rewarded_at']:
            referrer_id, reward = ref['referrer_id'], ref['reward_cents']
            await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (reward, referrer_id))
            await db.execute("DELETE FROM referrals WHERE referral_id = ?", (ref['referral_id'],))
            await db.commit()
            uname = await get_display_name(user_id)
            msg = f"⚠️ **Referans İptal Bildirimi**\n\nDavet ettiğiniz **{uname}** isimli kullanıcı {reason}.\nKazandığınız `{format_money(reward)}` ödül kurallar gereği geri alınmıştır."
            try: await bot.send_message(referrer_id, msg, parse_mode="Markdown")
            except: pass

# ==========================================
# MIDDLEWARE
# ==========================================
class JoinChannelMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id): return await handler(event, data)
        
        if isinstance(event, Message) and event.text and event.text.startswith("/start"): await ensure_user(user)
        if not FORCE_JOIN_CHANNEL: return await handler(event, data)
        
        try:
            member = await event.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user.id)
            is_member = member.status in ("creator", "administrator", "member")
        except: is_member = None

        if is_member is False:
            await handle_referral_penalty(event.bot, user.id, "kanaldan çıktığı tespit edildi")
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
        [InlineKeyboardButton(text="🛍️ Hizmet Kataloğu", callback_data="menu:catalog", style="primary"), InlineKeyboardButton(text="💰 Bakiyem", callback_data="menu:balance", style="success")],
        [InlineKeyboardButton(text="📦 Siparişlerim", callback_data="menu:orders", style="primary"), InlineKeyboardButton(text="⭐ Bakiye Yükle", callback_data="menu:topup", style="success")],
        [InlineKeyboardButton(text="💎 VIP Üyelik", callback_data="menu:vip", style="success"), InlineKeyboardButton(text="🎟️ İndirim Kuponu", callback_data="menu:coupon", style="primary")],
        [InlineKeyboardButton(text="👥 Arkadaşını Davet Et", callback_data="menu:referral", style="primary"), InlineKeyboardButton(text="🏆 Davet Liderleri", callback_data="menu:leaderboard", style="success")],
        [InlineKeyboardButton(text="🎁 Ödüller ve Görevler", callback_data="menu:rewards", style="primary"), InlineKeyboardButton(text="💬 Destek", callback_data="menu:support", style="success")],
        [InlineKeyboardButton(text="🌐 Dil Seçimi", callback_data="menu:language", style="primary"), InlineKeyboardButton(text="ℹ️ Yardım", callback_data="menu:help", style="primary")]
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
        [InlineKeyboardButton(text="🎁 Promo Ekle", callback_data="admin:promo_add", style="primary"), InlineKeyboardButton(text="💰 Hazine Dağıt", callback_data="admin:hazine_add", style="success")],
        [InlineKeyboardButton(text="📦 Hizmet Ekle", callback_data="admin:service_add", style="primary"), InlineKeyboardButton(text="👥 Toplam Kullanıcı (.txt)", callback_data="admin:total_users", style="success")],
        [InlineKeyboardButton(text="⚙️ Özellik Ayarları", callback_data="admin:settings", style="primary"), InlineKeyboardButton(text="📢 Duyuru Gönder", callback_data="admin:broadcast", style="danger")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")]
    ])

def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VIP Sistemi (Aç/Kapat)", callback_data="toggle:vip_enabled", style="primary")],
        [InlineKeyboardButton(text="Kupon Sistemi (Aç/Kapat)", callback_data="toggle:coupon_enabled", style="primary")],
        [InlineKeyboardButton(text="Görevler (Aç/Kapat)", callback_data="toggle:rewards_enabled", style="primary")],
        [InlineKeyboardButton(text="Referans: İlk Sipariş Şartı (Aç/Kapat)", callback_data="toggle:ref_require_order", style="danger")],
        [InlineKeyboardButton(text="🔙 Admin Paneli", callback_data="menu:admin", style="success")]
    ])

# ==========================================
# START VE ANA MENÜ
# ==========================================
async def delete_after_delay(chat_id, message_id, delay, bot):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError: pass

async def build_welcome_text(user_id, first_name):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents, vip_until, points FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'", (user_id,))
        order_count = (await cursor.fetchone())['cnt']
        
    vip_status = "Aktif Değil ❌"
    if user and user['vip_until']:
        try:
            if datetime.fromisoformat(user['vip_until']) > now_utc(): vip_status = f"Aktif ✅ ({user['vip_until'][:10]}'e kadar)"
        except: pass

    return (f"🌟 **Hoş Geldin, {first_name}!**\n\n"
            f"💳 **Cüzdan Bilgileriniz:**\n"
            f"├ 💰 Bakiye: `{format_money(user['balance_cents'] if user else 0)}`\n"
            f"├ 🎯 Puan: `{user['points'] if user else 0}`\n"
            f"├ 📦 Sipariş: `{order_count}`\n"
            f"└ 💎 VIP: `{vip_status}`\n\n"
            f"🚀 *Sistemimiz 7/24 otomatik teslimat ile kesintisiz hizmet vermektedir.*\n"
            f"⚡ *VIP üyelerimiz öncelikli işlem ve %20 indirim avantajına sahiptir.*\n\n"
            f"👇 İşlem yapmak için aşağıdaki menüyü kullanın:")

@router.message(CommandStart())
async def start_handler(message: Message):
    if not message.from_user: return
    loading_msg = await message.answer("🔄 Ana menü hazırlanıyor...")
    asyncio.create_task(delete_after_delay(message.chat.id, loading_msg.message_id, 30, message.bot))
    
    async with db_session() as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (message.from_user.id,))
        is_new = await cursor.fetchone() is None
    
    if is_new:
        await ensure_user(message.from_user)
        args = message.text.split()
        if len(args) > 1 and args[1].startswith("ref_"):
            referrer_id = int(args[1][4:])
            if referrer_id != message.from_user.id:
                async with db_session() as db:
                    await db.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_user_id, reward_cents, created_at) VALUES (?, ?, ?, ?)", (referrer_id, message.from_user.id, REFERRAL_BONUS_CENTS, timestamp()))
                    await db.commit()
                
                require_order = await get_setting('ref_require_order')
                if require_order == '0':
                    async with db_session() as db:
                        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (REFERRAL_BONUS_CENTS, referrer_id))
                        await db.execute("UPDATE referrals SET rewarded_at = ? WHERE referred_user_id = ?", (timestamp(), message.from_user.id))
                        await add_transaction(db, referrer_id, REFERRAL_BONUS_CENTS, "referral", "Davet ödülü (Anında)")
                        await db.commit()
                    try: await message.bot.send_message(referrer_id, f"🎉 Tebrikler! Davet ettiğiniz kişi bota katıldı ve `{format_money(REFERRAL_BONUS_CENTS)}` kazandınız.", parse_mode="Markdown")
                    except: pass

    text = await build_welcome_text(message.from_user.id, message.from_user.first_name)
    await message.answer(text, reply_markup=main_keyboard(message.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "menu:back")
async def back_to_menu(cb: CallbackQuery):
    text = await build_welcome_text(cb.from_user.id, cb.from_user.first_name)
    await cb.message.edit_text(text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents, points, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,))
        row = await cursor.fetchone()
    vip_status = "Aktif Değil ❌" if not row or not row['vip_until'] or datetime.fromisoformat(row['vip_until']) <= now_utc() else f"Aktif ✅"
    text = (f"💰 **Cüzdan Detayları**\n\n🔹 Bakiye: `{format_money(row['balance_cents'] if row else 0)}`\n🔹 Puan: `{row['points'] if row else 0}`\n🔹 VIP Üyelik: `{vip_status}`")
    await cb.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

# ==========================================
# REFERANS SİSTEMİ
# ==========================================
@router.callback_query(F.data == "menu:referral")
async def menu_referral(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    require_order = await get_setting('ref_require_order')
    rule_text = "ℹ️ *Kural:* Davet ettiğiniz kişi ilk numarasını aldığında ödülünüz hesabınıza yatacaktır." if require_order == '1' else "ℹ️ *Kural:* Davet ettiğiniz kişi bota katıldığı an ödülünüz hesabınıza yatacaktır."
    async with db_session() as db:
        cursor = await db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN rewarded_at IS NOT NULL THEN reward_cents ELSE 0 END) as earned FROM referrals WHERE referrer_id = ?", (cb.from_user.id,))
        row = await cursor.fetchone()
    text = (f"👥 **Referans Sistemi**\n\n🔗 **Davet Linkiniz:**\n`{link}`\n\n📊 **İstatistikleriniz:**\n• Davet Edilen: `{row['total'] or 0}` kişi\n• Kazanılan Ödül: `{format_money(row['earned'] or 0)}`\n\n{rule_text}")
    await cb.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

# ==========================================
# ADMİN PANELİ VE AYARLAR
# ==========================================
@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    await cb.message.edit_text("🛠️ **Admin Paneli**\n\nİşlem yapmak istediğiniz seçeneği belirleyin:", reply_markup=admin_panel_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "admin:settings")
async def admin_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await cb.message.edit_text("⚙️ **Özellik Ayarları**\n\nAçıp kapatmak istediğiniz özelliği seçin:", reply_markup=settings_kb(), parse_mode="Markdown")

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

# --- BAKİYE EKLE / SİL ---
@router.callback_query(F.data.in_({"admin:bal_add", "admin:bal_rem"}))
async def admin_bal_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    action = "eklemek" if "add" in cb.data else "silmek"
    await state.set_state(AdminBalanceFlow.user_query)
    await state.update_data(action=cb.data)
    await cb.message.edit_text(f"💰 **Bakiye {action.capitalize()}**\n\nLütfen kullanıcının `@kullanıcıadı` veya `Telegram ID`'sini yazın:", parse_mode="Markdown")

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
    if not user: return await message.answer("❌ **Böyle bir kullanıcı yok!**\nLütfen geçerli bir kullanıcı adı veya ID girin.")
    await state.update_data(target_id=user['user_id'], target_name=user['first_name'])
    await state.set_state(AdminBalanceFlow.amount)
    await message.answer(f"✅ Kullanıcı bulundu: **{user['first_name']}**\n\nLütfen işlem yapılacak TL tutarını yazın (Örn: `50`):", parse_mode="Markdown")

@router.message(AdminBalanceFlow.amount)
async def admin_bal_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: amount = parse_money(message.text)
    except: return await message.answer("❌ Geçersiz tutar. Lütfen sayı girin.")
    data = await state.get_data(); await state.clear()
    is_add = "add" in data['action']
    async with db_session() as db:
        if is_add: await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amount, data['target_id']))
        else: await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (amount, data['target_id']))
        await add_transaction(db, data['target_id'], amount if is_add else -amount, "admin", f"Admin bakiye {'ekledi' if is_add else 'sildi'}")
        await db.commit()
    await message.answer(f"✅ **{data['target_name']}** adlı kullanıcının bakiyesi {format_money(amount)} {'artırıldı' if is_add else 'azaltıldı'}.", reply_markup=back_to_menu_kb())

# --- TOPLAM KULLANICI ---
@router.callback_query(F.data == "admin:total_users")
async def admin_total_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await cb.message.edit_text("⏳ Rapor hazırlanıyor...")
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id, username, first_name, balance_cents, vip_until, created_at FROM users ORDER BY created_at DESC")
        users = await cursor.fetchall()
    total = len(users); now = now_utc()
    report = f"=== TOPLAM KULLANICI RAPORU ===\nToplam: {total}\n=================================\n\n"
    report += f"{'ID':<12} | {'Kullanıcı Adı':<15} | {'Adı':<15} | {'Katılım Tarihi':<20} | {'Bakiye':<10} | {'VIP'}\n"
    report += "-" * 90 + "\n"
    for u in users:
        vip = "Var" if u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now else "Yok"
        uname = f"@{u['username']}" if u['username'] else "-"
        fname = u['first_name'] or "-"
        reg_date = u['created_at'][:10] if u['created_at'] else "-"
        report += f"{u['user_id']:<12} | {uname:<15} | {fname:<15} | {reg_date:<20} | {format_money(u['balance_cents']):<10} | {vip}\n"
    document = BufferedInputFile(report.encode('utf-8'), filename="kullanicilar.txt")
    await cb.message.edit_text(f"📊 **Toplam Kullanıcı:** `{total}`\n\nDetaylı rapor aşağıda:", parse_mode="Markdown", reply_markup=back_to_menu_kb())
    await cb.message.answer_document(document)

# ==========================================
# ADMİN: PROMO VE HAZİNE SİSTEMİ
# ==========================================
@router.callback_query(F.data == "admin:promo_add")
async def admin_promo_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminPromoAddFlow.code)
    await cb.message.edit_text("🎁 **Promo Kod Ekle**\n\nAdım 1: Promo kodunu yazın (Örn: `YENIYIL`):")

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
            text = (f"╭──────────────────────────╮\n"
                    f"   🎁 **PROMO KOD GELDİ**\n"
                    f"╰──────────────────────────╯\n\n"
                    f"🔑 **Kod:** `{data['code']}`\n"
                    f"💰 **Ödül:** `{format_money(data['bonus'])}`\n"
                    f"👥 **Kalan Kullanım:** `{data['max_uses']}`\n"
                    f"⏳ **Süre:** `{message.text}` dakika\n\n"
                    f"👇 Kodu almak için bota gidin:")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Bota Gir", url=f"https://t.me/{(await message.bot.get_me()).username}", style="success")]])
            try:
                msg = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="Markdown")
                await db.execute("INSERT INTO promos (code, bonus_cents, max_uses, expires_at, message_id) VALUES (?, ?, ?, ?, ?)", (data['code'], data['bonus'], data['max_uses'], expires_at, msg.message_id))
                await db.commit()
                await message.answer(f"✅ **{data['code']}** kodu kanala gönderildi.", reply_markup=back_to_menu_kb())
                return
            except: pass
            
        await db.execute("INSERT INTO promos (code, bonus_cents, max_uses, expires_at) VALUES (?, ?, ?, ?)", (data['code'], data['bonus'], data['max_uses'], expires_at))
        await db.commit()
    await message.answer(f"✅ **{data['code']}** kodu oluşturuldu (Kanal ayarlanmadığı için gönderilemedi).", reply_markup=back_to_menu_kb())

# --- HAZİNE DAĞITMA SİSTEMİ ---
@router.callback_query(F.data == "admin:hazine_add")
async def admin_hazine_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminHazineFlow.total_amount)
    await cb.message.edit_text("💰 **Hazine Patlatma**\n\nAdım 1: Dağıtılacak toplam bakiye ne kadar? (TL, Örn: `100`)")

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
    
    if PROMO_CHANNEL_ID:
        text = (f"╭──────────────────────────╮\n"
                f"   💰 **HAZİNE PATLADI!**\n"
                f"╰──────────────────────────╯\n\n"
                f"💎 **Toplam Hazine:** `{format_money(data['total_amount'])}`\n"
                f"👥 **Kalan Kişi:** `{data['user_limit']}`\n"
                f"⏳ **Süre:** `{message.text}` dakika\n\n"
                f"⚡ *Her kullanıcıya rastgele miktarda bakiye verilir!*\n"
                f"👇 Hemen kapmak için bota gidin:")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Hazineden Kap", url=f"https://t.me/{(await message.bot.get_me()).username}", style="success")]])
        try:
            msg = await message.bot.send_message(PROMO_CHANNEL_ID, text, reply_markup=kb, parse_mode="Markdown")
            async with db_session() as db:
                await db.execute("INSERT INTO hazine (total_cents, remaining_cents, max_users, expires_at, message_id) VALUES (?, ?, ?, ?, ?)", (data['total_amount'], data['total_amount'], data['user_limit'], expires_at, msg.message_id))
                await db.commit()
            await message.answer("✅ Hazine kanala gönderildi.", reply_markup=back_to_menu_kb())
            return
        except: pass
    await message.answer("❌ Kanal ayarlanmadığı için hazine gönderilemedi.", reply_markup=back_to_menu_kb())

# ==========================================
# KULLANICI MENÜLERİ VE SİPARİŞ SİSTEMİ
# ==========================================
@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    if not await is_feature_enabled('vip_enabled'): return await cb.message.edit_text("🚧 **VIP üyelik geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    text = f"💎 **VIP Üyelik**\n\nVIP üyelerimiz tüm siparişlerde **%20 indirim** kazanır ve siparişleri **öncelikli** işleme alınır.\n\n💰 **Fiyat:** `{VIP_PRICE_TL} TL`\n⏳ **Süre:** `{VIP_DURATION_DAYS} Gün`"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Satın Al", callback_data="vip_buy", style="success")], [InlineKeyboardButton(text="🔙 Geri", callback_data="menu:back", style="primary")]])
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

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
    await cb.message.edit_text(f"✅ Tebrikler! VIP üyeliğiniz başladı. Bitiş: {new_vip[:10]}", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await cb.message.edit_text("⭐ **Bakiye Yükleme**\n\nPaket seçin:", reply_markup=topup_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("star:"))
async def create_star_invoice(cb: CallbackQuery):
    stars = int(cb.data.split(":")[1]); tier = next((t for t in STAR_TOPUP_TIERS if t[0] == stars), None)
    if not tier: return await cb.answer("Geçersiz paket.", show_alert=True)
    payload = f"topup:{uuid4().hex}"
    async with db_session() as db:
        await db.execute("INSERT INTO star_payment_intents (payload, user_id, stars_amount, credit_cents, created_at) VALUES (?, ?, ?, ?, ?)", (payload, cb.from_user.id, stars, tier[1], timestamp())); await db.commit()
    try:
        await cb.bot.send_invoice(cb.from_user.id, "Bakiye Yükleme", f"{format_money(tier[1])} bakiye", payload, "", "XTR", [LabeledPrice("Bakiye", stars)]); await cb.answer()
    except: await cb.answer("Ödeme başlatılamadı.", show_alert=True)

@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery): await query.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(message: Message):
    async with db_session() as db:
        cursor = await db.execute("SELECT credit_cents FROM star_payment_intents WHERE payload = ? AND status = 'pending'", (message.successful_payment.invoice_payload,))
        row = await cursor.fetchone()
        if row:
            await db.execute("UPDATE star_payment_intents SET status = 'paid' WHERE payload = ?", (message.successful_payment.invoice_payload,))
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (row['credit_cents'], message.from_user.id))
            await add_transaction(db, message.from_user.id, row['credit_cents'], "stars", "Yıldız ile yükleme"); await db.commit()
            cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,)); new_bal = (await cursor.fetchone())['balance_cents']
            await message.answer(f"🎉 **Tebrikler, Başarılar!**\n\n✅ Hesabınıza başarıyla `{format_money(row['credit_cents'])}` değerinde bakiye yüklemesi yapıldı.\n\n💰 Güncel Bakiyeniz: `{format_money(new_bal)}`", parse_mode="Markdown")

@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT service_id, name FROM services WHERE active = 1"); services = await cursor.fetchall()
    if not services: return await cb.message.edit_text("🛒 Şu an aktif hizmet bulunmuyor.", reply_markup=back_to_menu_kb())
    kb = [[InlineKeyboardButton(text=s['name'], callback_data=f"s:{s['service_id']}", style="primary")] for s in services]
    kb.append([InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")])
    await cb.message.edit_text("🛍️ Lütfen bir hizmet seçin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("s:"))
async def service_detail(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name, description, price_cents FROM services WHERE service_id = ?", (sid,)); s = await cursor.fetchone()
        cursor = await db.execute("SELECT vip_until FROM users WHERE user_id = ?", (cb.from_user.id,)); u = await cursor.fetchone()
    is_vip = u and u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now_utc()
    price = s['price_cents']; text = f"📦 **{s['name']}**\n\n{s['description']}\n\n"
    if is_vip: text += f"💵 Normal Fiyat: `{format_money(price)}`\n💎 VIP Fiyat: `{format_money(int(price*0.8))}`"
    else: text += f"💵 Fiyat: `{format_money(price)}`"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Sipariş Oluştur", callback_data=f"b:{sid}", style="success")], [InlineKeyboardButton(text="🔙 Geri", callback_data="menu:catalog", style="primary")]]), parse_mode="Markdown")

@router.callback_query(F.data.startswith("b:"))
async def buy_service(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name, price_cents FROM services WHERE service_id = ?", (sid,)); s = await cursor.fetchone()
        cursor = await db.execute("SELECT balance_cents, vip_until FROM users WHERE user_id = ?", (cb.from_user.id,)); u = await cursor.fetchone()
    is_vip = u and u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now_utc()
    final_price = int(s['price_cents'] * 0.8) if is_vip else s['price_cents']
    if u['balance_cents'] < final_price: return await cb.answer("Yetersiz bakiye.", show_alert=True)
    async with db_session() as db:
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (final_price, cb.from_user.id))
        cursor = await db.execute("INSERT INTO orders (user_id, service_id, service_name, amount_cents, status, created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)", (cb.from_user.id, sid, s['name'], final_price, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat()))
        oid = cursor.lastrowid; await add_transaction(db, cb.from_user.id, -final_price, "order", f"Sipariş #{oid}"); await db.commit()
    await cb.message.answer(f"🎉 **Başarılar!**\n\n✅ Siparişiniz başarıyla oluşturuldu ve yönetici onayına düştü.\n⏳ Lütfen numaranızın size iletilmesini bekleyin.", parse_mode="Markdown")
    try:
        msg = await cb.bot.send_message(cb.from_user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True; ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass
    uname = await get_display_name(cb.from_user.id)
    await notify_admins(cb.bot, f"🔔 **Yeni Sipariş #{oid}**\n👤 Kullanıcı: {uname}\n📦 {s['name']}\n💵 {format_money(final_price)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara Gir", callback_data=f"n:{oid}", style="success")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}", style="danger")]]))
    await cb.answer("Sipariş verildi.")

# --- ADMIN NUMARA GİRİŞİ ---
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
        try: await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(full_phone), parse_mode="HTML")
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
        try: await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], generate_circle(code), parse_mode="HTML")
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

# ==========================================
# PROMO VE HAZİNE KULLANIM MANTIĞI
# ==========================================
@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(cb: CallbackQuery):
    # Buraya istersen Promo Kodu Gir ve Hazine Katıl butonlarını ekleyebiliriz ama genelde kanaldan link ile gelinir.
    text = "🎁 **Ödüller ve Görevler**\n\n- Bakiye Yükle kısmından yıldız ile bakiye yükleyebilirsiniz.\n- Kanaldaki Hazine ve Promo etkinliklerini takip edin!"
    await cb.message.edit_text(text, reply_markup=back_to_menu_kb())

@router.message(Command("promo"))
async def promo_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2: return await message.answer("Kullanım: `/promo KOD`", parse_mode="Markdown")
    code = parts[1].upper()
    
    async with db_session() as db:
        cursor = await db.execute("SELECT bonus_cents, max_uses, current_uses, expires_at, active, message_id FROM promos WHERE code = ?", (code,))
        promo = await cursor.fetchone()
        if not promo or not promo['active'] or datetime.fromisoformat(promo['expires_at']) < now_utc():
            return await message.answer("❌ Bu kod geçersiz veya süresi dolmuş.")
        if promo['current_uses'] >= promo['max_uses']:
            return await message.answer("❌ Bu kodun kullanım limiti dolmuş.")
            
        cursor = await db.execute("SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", (code, message.from_user.id))
        if await cursor.fetchone(): return await message.answer("❌ Bu kodu zaten kullandınız.")
        
        await db.execute("INSERT INTO promo_uses (code, user_id) VALUES (?, ?)", (code, message.from_user.id))
        await db.execute("UPDATE promos SET current_uses = current_uses + 1 WHERE code = ?", (code,))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (promo['bonus_cents'], message.from_user.id))
        await add_transaction(db, message.from_user.id, promo['bonus_cents'], "promo", f"Promo: {code}")
        
        new_uses = promo['current_uses'] + 1
        await db.commit()
        
        # Kanaldaki mesajı güncelle
        if promo['message_id'] and PROMO_CHANNEL_ID:
            rem_uses = promo['max_uses'] - new_uses
            rem_time = int((datetime.fromisoformat(promo['expires_at']) - now_utc()).total_seconds() / 60)
            text = (f"╭──────────────────────────╮\n"
                    f"   🎁 **PROMO KOD GELDİ**\n"
                    f"╰──────────────────────────╯\n\n"
                    f"🔑 **Kod:** `{code}`\n"
                    f"💰 **Ödül:** `{format_money(promo['bonus_cents'])}`\n"
                    f"👥 **Kalan Kullanım:** `{rem_uses}`\n"
                    f"⏳ **Süre:** `{rem_time}` dakika\n\n"
                    f"👇 Kodu almak için bota gidin:")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Bota Gir", url=f"https://t.me/{(await message.bot.get_me()).username}", style="success")]])
            try: await message.bot.edit_message_text(text, chat_id=PROMO_CHANNEL_ID, message_id=promo['message_id'], reply_markup=kb, parse_mode="Markdown")
            except: pass
            
    await message.answer(f"🎉 Tebrikler! Promo kodu kullanıldı. Hesabınıza `{format_money(promo['bonus_cents'])}` eklendi.")

@router.message(Command("hazine"))
async def hazine_command(message: Message):
    async with db_session() as db:
        cursor = await db.execute("SELECT id, total_cents, remaining_cents, max_users, current_users, expires_at, active, message_id FROM hazine WHERE active = 1 ORDER BY id DESC LIMIT 1")
        hz = await cursor.fetchone()
        if not hz or datetime.fromisoformat(hz['expires_at']) < now_utc() or hz['current_users'] >= hz['max_users'] or hz['remaining_cents'] <= 0:
            return await message.answer("❌ Şu an aktif bir hazine bulunmuyor veya tükendi.")
            
        cursor = await db.execute("SELECT 1 FROM hazine_claims WHERE hazine_id = ? AND user_id = ?", (hz['id'], message.from_user.id))
        if await cursor.fetchone(): return await message.answer("❌ Bu hazineden zaten payınızı aldınız.")
        
        # Rastgele bakiye dağıtma algoritması (En fazla kalanın yarısı)
        max_claim = hz['remaining_cents'] // 2
        if max_claim < 1: max_claim = 1
        reward = random.randint(1, max_claim)
        
        # Eğer son kişiyse kalanı ver
        if hz['current_users'] + 1 == hz['max_users']:
            reward = hz['remaining_cents']
            
        new_remaining = hz['remaining_cents'] - reward
        new_users = hz['current_users'] + 1
        
        await db.execute("INSERT INTO hazine_claims (hazine_id, user_id, amount_cents) VALUES (?, ?, ?)", (hz['id'], message.from_user.id, reward))
        await db.execute("UPDATE hazine SET remaining_cents = ?, current_users = ? WHERE id = ?", (new_remaining, new_users, hz['id']))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (reward, message.from_user.id))
        await add_transaction(db, message.from_user.id, reward, "hazine", f"Hazine Ödülü")
        
        # Kanaldaki mesajı güncelle
        if hz['message_id'] and PROMO_CHANNEL_ID:
            rem_time = int((datetime.fromisoformat(hz['expires_at']) - now_utc()).total_seconds() / 60)
            rem_users = hz['max_users'] - new_users
            text = (f"╭──────────────────────────╮\n"
                    f"   💰 **HAZİNE PATLADI!**\n"
                    f"╰──────────────────────────╯\n\n"
                    f"💎 **Kalan Hazine:** `{format_money(new_remaining)}`\n"
                    f"👥 **Kalan Kişi:** `{rem_users}`\n"
                    f"⏳ **Süre:** `{rem_time}` dakika\n\n"
                    f"⚡ *Her kullanıcıya rastgele miktarda bakiye verilir!*\n"
                    f"👇 Hemen kapmak için bota gidin:")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Hazineden Kap", url=f"https://t.me/{(await message.bot.get_me()).username}", style="success")]])
            try: await message.bot.edit_message_text(text, chat_id=PROMO_CHANNEL_ID, message_id=hz['message_id'], reply_markup=kb, parse_mode="Markdown")
            except: pass
            
        await db.commit()
        
    await message.answer(f"🎉 **Başarılar!**\n\nHazine patlamasından `{format_money(reward)}` kazandınız!\nGüncel bakiyenizi görmek için /start yazabilirsiniz.")

# ==========================================
# DİĞER KOMUTLAR
# ==========================================
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