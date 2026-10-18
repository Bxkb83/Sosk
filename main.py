# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
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
class AdminAddTaskFlow(StatesGroup):
    title = State()
    reward = State()
    channel = State()

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
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance_cents INTEGER DEFAULT 0, points INTEGER DEFAULT 0, vip_until TEXT, last_daily_at TEXT, is_blocked INTEGER DEFAULT 0, joined_at TEXT);
            CREATE TABLE IF NOT EXISTS services (service_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, price_cents INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_id INTEGER, service_name TEXT, amount_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS transactions (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, kind TEXT, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS referrals (referral_id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_user_id INTEGER UNIQUE, reward_cents INTEGER, rewarded_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS star_payment_intents (payload TEXT PRIMARY KEY, user_id INTEGER, stars_amount INTEGER, credit_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS support_tickets (ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS promos (promo_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, bonus_cents INTEGER, max_uses INTEGER, current_uses INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS promo_uses (promo_id INTEGER, user_id INTEGER, used_at TEXT, PRIMARY KEY(promo_id, user_id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, reward_cents INTEGER, channel_id TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS user_tasks (user_id INTEGER, task_id INTEGER, claimed_at TEXT, PRIMARY KEY(user_id, task_id));
            
            INSERT OR IGNORE INTO settings (key, value) VALUES 
            ('vip_enabled', '1'), ('coupon_enabled', '1'), ('rewards_enabled', '1'), 
            ('vip_price_tl', '50'), ('vip_duration_days', '30'), ('ref_require_order', '1');
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
        await db.execute("INSERT INTO users (user_id, username, first_name, created_at, last_seen_at, joined_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, username = excluded.username, is_blocked = 0",
                         (user.id, user.username or "", user.first_name or "User", timestamp(), timestamp(), timestamp()))
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
# MIDDLEWARE (Kanal Kontrolü)
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
        [InlineKeyboardButton(text="🎁 Promo Ekle", callback_data="admin:promo_add", style="primary"), InlineKeyboardButton(text="📦 Hizmet Ekle", callback_data="admin:service_add", style="primary")],
        [InlineKeyboardButton(text="👥 Toplam Kullanıcı (.txt)", callback_data="admin:total_users", style="success")],
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
# DETAYLI ANA MENÜ METNİ
# ==========================================
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
    await message.answer("🔄 Ana menü hazırlanıyor...", reply_markup=ReplyKeyboardRemove())
    
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

# ==========================================
# GÜNLÜK ÖDÜL VE GÖREVLER MENÜSÜ
# ==========================================
@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(cb: CallbackQuery):
    if not await is_feature_enabled('rewards_enabled'): 
        return await cb.message.edit_text("🚧 **Görev sistemi geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
        
    async with db_session() as db:
        cursor = await db.execute("SELECT task_id, title, reward_cents FROM tasks WHERE active = 1")
        tasks = await cursor.fetchall()
        cursor = await db.execute("SELECT task_id FROM user_tasks WHERE user_id = ?", (cb.from_user.id,))
        claimed = {r['task_id'] for r in await cursor.fetchall()}
        
    text = "🎁 **Ödüller ve Görevler**\n\nAşağıdaki görevleri tamamlayarak ekstra bakiye kazanabilirsiniz:\n\n"
    kb = []
    
    # Günlük Bonus Butonu
    kb.append([InlineKeyboardButton(text="🎁 Günlük Ödülünü Al (+1 TL)", callback_data="daily_bonus", style="success")])
    
    for t in tasks:
        status = "✅ Tamamlandı" if t['task_id'] in claimed else f"Ödül: {format_money(t['reward_cents'])}"
        text += f"🔹 **{t['title']}** - {status}\n"
        if t['task_id'] not in claimed:
            kb.append([InlineKeyboardButton(text=f"Görevi Yap: {t['title']}", callback_data=f"task:{t['task_id']}", style="primary")])
            
    kb.append([InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "daily_bonus")
async def claim_daily_bonus(cb: CallbackQuery):
    user_id = cb.from_user.id
    async with db_session() as db:
        cursor = await db.execute("SELECT last_daily_at, balance_cents FROM users WHERE user_id = ?", (user_id,))
        user = await cursor.fetchone()
        
        now = now_utc()
        if user['last_daily_at']:
            last_claim = datetime.fromisoformat(user['last_daily_at'])
            # 86400 saniye = 24 saat
            elapsed = (now - last_claim).total_seconds()
            if elapsed < 86400:
                remaining = 86400 - elapsed
                hours = int(remaining // 3600)
                mins = int((remaining % 3600) // 60)
                return await cb.answer(f"⏳ Günlük ödülünüzü zaten aldınız!\n\nKalan süre: {hours} saat {mins} dakika.", show_alert=True)

        # Ödül ver (1 TL)
        reward_cents = 100
        new_balance = user['balance_cents'] + reward_cents
        await db.execute("UPDATE users SET balance_cents = ?, last_daily_at = ? WHERE user_id = ?", (new_balance, timestamp(), user_id))
        await add_transaction(db, user_id, reward_cents, "daily", "Günlük Ödül")
        await db.commit()

    await cb.answer(f"🎉 Başarılar!\n\n✅ Günlük ödülünüz olan {format_money(reward_cents)} hesabınıza eklendi.", show_alert=True)
    await menu_rewards(cb)

@router.callback_query(F.data.startswith("task:"))
async def claim_task(cb: CallbackQuery):
    tid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT title, reward_cents, channel_id FROM tasks WHERE task_id = ?", (tid,)); task = await cursor.fetchone()
        cursor = await db.execute("SELECT 1 FROM user_tasks WHERE user_id = ? AND task_id = ?", (cb.from_user.id, tid))
        if await cursor.fetchone(): return await cb.answer("Bu görevi zaten yaptınız.", show_alert=True)
    if task['channel_id']:
        try:
            member = await cb.bot.get_chat_member(chat_id=task['channel_id'], user_id=cb.from_user.id)
            if member.status not in ("creator", "administrator", "member"): return await cb.answer(f"Lütfen önce {task['channel_id']} kanalına katılın!", show_alert=True)
        except: return await cb.answer("Kanal kontrol edilemedi.", show_alert=True)
    async with db_session() as db:
        await db.execute("INSERT INTO user_tasks (user_id, task_id, claimed_at) VALUES (?, ?, ?)", (cb.from_user.id, tid, timestamp()))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (task['reward_cents'], cb.from_user.id))
        await add_transaction(db, cb.from_user.id, task['reward_cents'], "task", f"Görev: {task['title']}"); await db.commit()
    await cb.answer(f"🎉 Başarılar! {format_money(task['reward_cents'])} kazandınız.", show_alert=True); 
    await menu_rewards(cb)

# ==========================================
# YILDIZ YÜKLEME VE BAŞARILI ÖDEME MESAJI
# ==========================================
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
            
            cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
            new_bal = (await cursor.fetchone())['balance_cents']
            
            await message.answer(f"🎉 **Tebrikler, Başarılar!**\n\n✅ Hesabınıza başarıyla `{format_money(row['credit_cents'])}` değerinde bakiye yüklemesi yapıldı.\n\n💰 Güncel Bakiyeniz: `{format_money(new_bal)}`", parse_mode="Markdown")

# ==========================================
# SİPARİŞ OLUŞTURMA VE NUMARA GİRİŞİ
# ==========================================
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
    is_vip = u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now_utc()
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
    is_vip = u['vip_until'] and datetime.fromisoformat(u['vip_until']) > now_utc()
    final_price = int(s['price_cents'] * 0.8) if is_vip else s['price_cents']
    if u['balance_cents'] < final_price: return await cb.answer("Yetersiz bakiye.", show_alert=True)
    
    async with db_session() as db:
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (final_price, cb.from_user.id))
        cursor = await db.execute("INSERT INTO orders (user_id, service_id, service_name, amount_cents, status, created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)", (cb.from_user.id, sid, s['name'], final_price, timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat()))
        oid = cursor.lastrowid; await add_transaction(db, cb.from_user.id, -final_price, "order", f"Sipariş #{oid}"); await db.commit()
    
    # BAŞARILAR MESAJI (Sipariş Verilince)
    await cb.message.answer(f"🎉 **Başarılar!**\n\n✅ Siparişiniz başarıyla oluşturuldu ve yönetici onayına düştü.\n⏳ Lütfen numaranızın size iletilmesini bekleyin.", parse_mode="Markdown")
    
    try:
        msg = await cb.bot.send_message(cb.from_user.id, generate_circle("Yükleniyor"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True; ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass
    
    uname = await get_display_name(cb.from_user.id)
    await notify_admins(cb.bot, f"🔔 **Yeni Sipariş #{oid}**\n👤 Kullanıcı: {uname}\n📦 {s['name']}\n💵 {format_money(final_price)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara Gir", callback_data=f"n:{oid}", style="success")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}", style="danger")]]))
    await cb.answer("Sipariş verildi.")

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
            cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ?", (oid,))
            uid = (await cursor.fetchone())['user_id']
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders WHERE user_id = ? AND status = 'completed'", (uid,))
            order_count = (await cursor.fetchone())['cnt']
            
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

    # BAŞARILAR MESAJI (Numara Verilince)
    await message.bot.send_message(uid, f"🎉 **Tebrikler, Başarılar!**\n\n✅ Siparişiniz tamamlandı ve numaranız hazır.\n\n📱 Numara: `{phone}`\n🔑 Kod: `{code}`", parse_mode="Markdown")
    await message.answer("Sipariş başarıyla kapatıldı.")

# ==========================================
# DİĞER YARDIMCI FONKSİYONLAR (İptal, Admin vs.)
# ==========================================
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