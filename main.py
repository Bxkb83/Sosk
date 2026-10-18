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
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, ReplyKeyboardRemove
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
    phone = State()
    code = State()
class SupportFlow(StatesGroup):
    body = State()
class AdminBroadcastFlow(StatesGroup):
    body = State()
class AdminAddTaskFlow(StatesGroup):
    title = State()
    reward = State()
    channel = State()

# ==========================================
# YARDIMCI FONKSİYONLAR
# ==========================================
ORDER_ANIMATIONS = {}
ORDER_MESSAGE_REFS = {}
ORDER_PHONE_CACHE = {}
BULLET_CHAR = "\u2022"

SPINNER_POSITIONS = [(0,0),(0,1),(0,2),(0,3),(0,4),(1,4),(2,4),(3,4),(4,4),(4,3),(4,2),(4,1),(4,0),(3,0),(2,0),(1,0)]

def render_spinner_frame(step, center_text):
    missing = SPINNER_POSITIONS[step % 16]
    lines = []
    for r in range(5):
        row_chars = []
        for c in range(5):
            is_border = (r == 0 or r == 4 or c == 0 or c == 4)
            if is_border:
                row_chars.append(" " if (r, c) == missing else BULLET_CHAR)
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
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=render_spinner_frame(step, "-----"), parse_mode="HTML")
            except TelegramAPIError: pass
            step += 1
            await asyncio.sleep(0.6)
    except asyncio.CancelledError: return

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
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance_cents INTEGER DEFAULT 0, language TEXT DEFAULT 'tr', last_seen_at TEXT, is_banned INTEGER DEFAULT 0, created_at TEXT);
            CREATE TABLE IF NOT EXISTS services (service_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, price_cents INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_id INTEGER, service_name TEXT, amount_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT, expires_at TEXT);
            CREATE TABLE IF NOT EXISTS transactions (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount_cents INTEGER, kind TEXT, note TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS referrals (referral_id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER, referred_user_id INTEGER UNIQUE, reward_cents INTEGER, rewarded_at TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS star_payment_intents (payload TEXT PRIMARY KEY, user_id INTEGER, stars_amount INTEGER, credit_cents INTEGER, status TEXT DEFAULT 'pending', created_at TEXT);
            CREATE TABLE IF NOT EXISTS support_tickets (ticket_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, body TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, reward_cents INTEGER, channel_id TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS user_tasks (user_id INTEGER, task_id INTEGER, claimed_at TEXT, PRIMARY KEY(user_id, task_id));
            
            INSERT OR IGNORE INTO settings (key, value) VALUES ('vip_enabled', '0'), ('coupon_enabled', '0'), ('rewards_enabled', '1');
        """)
        await db.commit()

async def is_feature_enabled(feature):
    async with db_session() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (feature,))
        row = await cursor.fetchone()
        return row['value'] == '1' if row else False

async def ensure_user(user):
    async with db_session() as db:
        await db.execute("INSERT INTO users (user_id, username, first_name, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, username = excluded.username",
                         (user.id, user.username or "", user.first_name or "User", timestamp(), timestamp()))
        await db.commit()

async def add_transaction(db, user_id, amount, kind, note):
    await db.execute("INSERT INTO transactions (user_id, amount_cents, kind, note, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, amount, kind, note, timestamp()))

async def notify_admins(bot, text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except: pass

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
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠️ Admin Paneli", callback_data="menu:admin", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_to_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Ana Menüye Dön", callback_data="menu:back", style="primary")]])

def topup_keyboard():
    rows = [[InlineKeyboardButton(text=f"{s} ⭐ Yıldız -> {format_money(c)}", callback_data=f"star:{s}", style="success")] for s, c in STAR_TOPUP_TIERS]
    rows.append([InlineKeyboardButton(text="🔙 Geri Dön", callback_data="menu:back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Özellik Ayarları", callback_data="admin:settings", style="primary")],
        [InlineKeyboardButton(text="📝 Görev Ekle", callback_data="admin:addtask", style="success")],
        [InlineKeyboardButton(text="📢 Görevleri Kanala Duyur", callback_data="admin:notify_tasks", style="danger")],
        [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")]
    ])

def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VIP Üyelik (Aç/Kapat)", callback_data="toggle:vip_enabled", style="primary")],
        [InlineKeyboardButton(text="Kupon Sistemi (Aç/Kapat)", callback_data="toggle:coupon_enabled", style="primary")],
        [InlineKeyboardButton(text="Görevler (Aç/Kapat)", callback_data="toggle:rewards_enabled", style="primary")],
        [InlineKeyboardButton(text="🔙 Admin Paneli", callback_data="menu:admin", style="success")]
    ])

# ==========================================
# CALLBACK YAKALAYICILARI
# ==========================================
@router.message(CommandStart())
async def start_handler(message: Message):
    if not message.from_user: return
    await message.answer("🔄 Ana menü hazırlanıyor...", reply_markup=ReplyKeyboardRemove())
    await ensure_user(message.from_user)
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (message.from_user.id,))
        row = await cursor.fetchone()
    text = f"✨ **Hoş geldin, {message.from_user.first_name}!**\n\n💰 **Bakiyeniz:** `{format_money(row['balance_cents'] if row else 0)}`\n\n⚡ *Siparişleriniz otomatik teslim edilir.*\n💎 *VIP üyeler önceliklidir ve %20 indirim alır.*\n\n👇 Lütfen bir işlem seçin:"
    await message.answer(text, reply_markup=main_keyboard(message.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "menu:back")
async def back_to_menu(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (cb.from_user.id,))
        row = await cursor.fetchone()
    text = f"✨ **Ana Menü**\n\n💰 **Bakiyeniz:** `{format_money(row['balance_cents'] if row else 0)}`\n\n👇 Lütfen bir işlem seçin:"
    await cb.message.edit_text(text, reply_markup=main_keyboard(cb.from_user.id), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(cb: CallbackQuery):
    if not await is_feature_enabled('rewards_enabled'):
        return await cb.message.edit_text("🚧 **Görev sistemi geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    
    async with db_session() as db:
        cursor = await db.execute("SELECT task_id, title, reward_cents FROM tasks WHERE active = 1")
        tasks = await cursor.fetchall()
        cursor = await db.execute("SELECT task_id FROM user_tasks WHERE user_id = ?", (cb.from_user.id,))
        claimed = {r['task_id'] for r in await cursor.fetchall()}
        
    if not tasks:
        return await cb.message.edit_text("🎁 Şu an aktif görev bulunmuyor.", reply_markup=back_to_menu_kb())
        
    text = "🎁 **Ödüllü Görevler**\n\nAşağıdaki görevleri tamamlayarak bakiye kazanabilirsiniz:\n\n"
    kb = []
    for t in tasks:
        status = "✅ Tamamlandı" if t['task_id'] in claimed else f"Ödül: {format_money(t['reward_cents'])}"
        text += f"🔹 **{t['title']}** - {status}\n"
        if t['task_id'] not in claimed:
            kb.append([InlineKeyboardButton(text=f"Görevi Yap: {t['title']}", callback_data=f"task:{t['task_id']}", style="success")])
    kb.append([InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("task:"))
async def claim_task(cb: CallbackQuery):
    tid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT title, reward_cents, channel_id FROM tasks WHERE task_id = ?", (tid,))
        task = await cursor.fetchone()
        cursor = await db.execute("SELECT 1 FROM user_tasks WHERE user_id = ? AND task_id = ?", (cb.from_user.id, tid))
        if await cursor.fetchone(): return await cb.answer("Bu görevi zaten yaptınız.", show_alert=True)
        
    channel = task['channel_id']
    if channel:
        try:
            member = await cb.bot.get_chat_member(chat_id=channel, user_id=cb.from_user.id)
            if member.status not in ("creator", "administrator", "member"):
                return await cb.answer(f"Lütfen önce {channel} kanalına katılın!", show_alert=True)
        except:
            return await cb.answer("Kanal kontrol edilemedi. Yöneticiye bildirin.", show_alert=True)
            
    async with db_session() as db:
        await db.execute("INSERT INTO user_tasks (user_id, task_id, claimed_at) VALUES (?, ?, ?)", (cb.from_user.id, tid, timestamp()))
        await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (task['reward_cents'], cb.from_user.id))
        await add_transaction(db, cb.from_user.id, task['reward_cents'], "task", f"Görev Ödülü: {task['title']}")
        await db.commit()
    await cb.answer(f"Tebrikler! {format_money(task['reward_cents'])} kazandınız.", show_alert=True)
    await menu_rewards(cb)

# ==========================================
# ADMİN PANELİ VE AYARLAR
# ==========================================
@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("Yetkiniz yok.", show_alert=True)
    text = "🛠️ **Admin Paneli**\n\nİşlem yapmak istediğiniz seçeneği belirleyin:"
    await cb.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "admin:settings")
async def admin_settings(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    text = "⚙️ **Özellik Ayarları**\n\nAşağıdaki butonlara basarak özellikleri açıp kapatabilirsiniz:"
    await cb.message.edit_text(text, reply_markup=settings_kb(), parse_mode="Markdown")

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

# ==========================================
# GÖREV EKLEME (ADMİN)
# ==========================================
@router.callback_query(F.data == "admin:addtask")
async def admin_add_task_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminAddTaskFlow.title)
    await cb.message.edit_text("📝 **Yeni Görev Ekle**\n\nAdım 1: Lütfen görevin başlığını yazın (Örn: `Kanala Katıl`):")
    await cb.answer()

@router.message(AdminAddTaskFlow.title)
async def task_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AdminAddTaskFlow.reward)
    await message.answer("Adım 2: Bu görevin ödülü ne kadar olsun? (TL cinsinden, Örn: `5`)")

@router.message(AdminAddTaskFlow.reward)
async def task_reward(message: Message, state: FSMContext):
    try:
        reward = parse_money(message.text)
        await state.update_data(reward=reward)
        await state.set_state(AdminAddTaskFlow.channel)
        await message.answer("Adım 3: Kullanıcının katılması gereken kanalın linkini veya ID'sini yazın (Örn: `@kanaladi`). Eğer bir kanala katılma şartı yoksa `-` yazın.")
    except:
        await message.answer("Lütfen geçerli bir sayı girin.")

@router.message(AdminAddTaskFlow.channel)
async def task_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    channel = message.text if message.text != "-" else None
    async with db_session() as db:
        await db.execute("INSERT INTO tasks (title, reward_cents, channel_id) VALUES (?, ?, ?)", (data['title'], data['reward'], channel))
        await db.commit()
    await state.clear()
    await message.answer("✅ Görev başarıyla oluşturuldu!", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "admin:notify_tasks")
async def admin_notify_tasks(cb: CallbackQuery):
    if not is_admin(cb.from_user.id) or not PROMO_CHANNEL_ID: return await cb.answer("Promo kanalı ayarlanmamış.", show_alert=True)
    text = "🎉 **YENİ GÖREVLER EKLENDİ!**\n\nBota gidip '🎁 Ödüller ve Görevler' menüsünden yeni görevleri tamamlayarak hemen bakiye kazanabilirsiniz. 🚀"
    try:
        await cb.bot.send_message(PROMO_CHANNEL_ID, text, parse_mode="Markdown")
        await cb.answer("Duyuru gönderildi!", show_alert=True)
    except:
        await cb.answer("Kanala mesaj gönderilemedi. Botu kanala admin yapın.", show_alert=True)

# ==========================================
# STANDART SİPARİŞ VE YILDIZ İŞLEMLERİ
# ==========================================
@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT service_id, name FROM services WHERE active = 1")
        services = await cursor.fetchall()
    if not services: return await cb.message.edit_text("🛒 Şu an aktif hizmet bulunmuyor.", reply_markup=back_to_menu_kb())
    kb = [[InlineKeyboardButton(text=s['name'], callback_data=f"s:{s['service_id']}", style="primary")] for s in services]
    kb.append([InlineKeyboardButton(text="🔙 Ana Menü", callback_data="menu:back", style="primary")])
    await cb.message.edit_text("🛍️ Lütfen bir hizmet seçin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@router.callback_query(F.data.startswith("s:"))
async def service_detail(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name, description, price_cents FROM services WHERE service_id = ?", (sid,))
        s = await cursor.fetchone()
    text = f"📦 **{s['name']}**\n\n{s['description']}\n\n💵 Fiyat: `{format_money(s['price_cents'])}`"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Sipariş Oluştur", callback_data=f"b:{sid}", style="success")], [InlineKeyboardButton(text="🔙 Geri", callback_data="menu:catalog", style="primary")]]), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("b:"))
async def buy_service(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with db_session() as db:
        cursor = await db.execute("SELECT name, price_cents FROM services WHERE service_id = ?", (sid,))
        s = await cursor.fetchone()
        cursor = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (cb.from_user.id,))
        u = await cursor.fetchone()
    if u['balance_cents'] < s['price_cents']: return await cb.answer("Yetersiz bakiye.", show_alert=True)
    
    async with db_session() as db:
        await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (s['price_cents'], cb.from_user.id))
        cursor = await db.execute("INSERT INTO orders (user_id, service_id, service_name, amount_cents, status, created_at, expires_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                                 (cb.from_user.id, sid, s['name'], s['price_cents'], timestamp(), (now_utc()+timedelta(seconds=CANCEL_SECONDS)).isoformat()))
        oid = cursor.lastrowid
        await add_transaction(db, cb.from_user.id, -s['price_cents'], "order", f"Sipariş #{oid}")
        await db.commit()
        
    await cb.message.edit_text(f"✅ Sipariş #{oid} alındı. Lütfen bekleyin.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ İptal", callback_data=f"o:cancel:{oid}", style="danger")]]))
    
    try:
        msg = await cb.bot.send_message(cb.from_user.id, render_spinner_frame(0, "-----"), parse_mode="HTML")
        ORDER_ANIMATIONS[oid] = True
        ORDER_MESSAGE_REFS[oid] = (cb.from_user.id, msg.message_id)
        asyncio.create_task(animate_order_status(cb.bot, cb.from_user.id, msg.message_id, oid))
    except: pass
    
    uname = await get_display_name(cb.from_user.id)
    await notify_admins(cb.bot, f"🔔 **Yeni Sipariş #{oid}**\n👤 Kullanıcı: {uname}\n📦 {s['name']}\n💵 {format_money(s['price_cents'])}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Numara Gir", callback_data=f"n:{oid}", style="success")],[InlineKeyboardButton(text="❌ İade", callback_data=f"o:refund:{oid}", style="danger")]]))
    await cb.answer("Sipariş verildi.")

# ==========================================
# YILDIZ, REFERANS VE DESTEK
# ==========================================
@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await cb.message.edit_text("⭐ **Bakiye Yükle**\n\nPaket seçin:", reply_markup=topup_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("star:"))
async def create_star_invoice(cb: CallbackQuery):
    stars = int(cb.data.split(":")[1])
    tier = next((t for t in STAR_TOPUP_TIERS if t[0] == stars), None)
    payload = f"topup:{uuid4().hex}"
    async with db_session() as db:
        await db.execute("INSERT INTO star_payment_intents (payload, user_id, stars_amount, credit_cents, created_at) VALUES (?, ?, ?, ?, ?)", (payload, cb.from_user.id, stars, tier[1], timestamp()))
        await db.commit()
    try:
        await cb.bot.send_invoice(cb.from_user.id, "Bakiye Yükleme", f"{format_money(tier[1])} bakiye", payload, "", "XTR", [LabeledPrice("Bakiye", stars)])
        await cb.answer()
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
            await add_transaction(db, message.from_user.id, row['credit_cents'], "stars", "Yıldız ile yükleme")
            await db.commit()
            await message.answer(f"✅ Ödeme başarılı. `{format_money(row['credit_cents'])}` eklendi.", parse_mode="Markdown")

@router.callback_query(F.data == "menu:referral")
async def menu_referral(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    async with db_session() as db:
        cursor = await db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN rewarded_at IS NOT NULL THEN reward_cents ELSE 0 END) as earned FROM referrals WHERE referrer_id = ?", (cb.from_user.id,))
        row = await cursor.fetchone()
    text = f"👥 **Referans Sistemi**\n\n🔗 **Davet Linkiniz:**\n`{link}`\n\n📊 **İstatistikleriniz:**\n• Davet Edilen: `{row['total'] or 0}` kişi\n• Kazanılan: `{format_money(row['earned'] or 0)}`\n\n💡 *Davet ettiğiniz kişi ilk siparişini tamamladığında kazancınız bakiyenize eklenir.*"
    await cb.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "menu:support")
async def menu_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFlow.body)
    await cb.message.edit_text("💬 Sorununuzu tek mesaj halinde yazın. İptal için `/cancel`", parse_mode="Markdown")

@router.message(SupportFlow.body)
async def support_body(message: Message, state: FSMContext):
    await state.clear()
    async with db_session() as db:
        cursor = await db.execute("INSERT INTO support_tickets (user_id, body, created_at) VALUES (?, ?, ?)", (message.from_user.id, message.text, timestamp()))
        tid = cursor.lastrowid
        await db.commit()
    await message.answer("✅ Talebiniz alındı.", reply_markup=back_to_menu_kb())
    uname = await get_display_name(message.from_user.id)
    await notify_admins(message.bot, f"🚨 **Destek #{tid}**\n👤 {uname}\n💬 {message.text}")

@router.callback_query(F.data == "menu:orders")
async def menu_orders(cb: CallbackQuery):
    async with db_session() as db:
        cursor = await db.execute("SELECT order_id, service_name, amount_cents, status FROM orders WHERE user_id = ? ORDER BY order_id DESC LIMIT 10", (cb.from_user.id,))
        rows = await cursor.fetchall()
    if not rows: return await cb.message.edit_text("📦 Sipariş yok.", reply_markup=back_to_menu_kb())
    statuses = {"pending": "⏳ Bekliyor", "completed": "✅ Tamamlandı", "refunded": "❌ İade"}
    text = "📦 **Siparişleriniz:**\n\n" + "".join([f"🔹 #{r['order_id']} - {r['service_name']} - {statuses.get(r['status'])}\n" for r in rows])
    await cb.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode="Markdown")

# ==========================================
# DİĞER MENÜLER VE ADMİN NUMARA GİRİŞİ
# ==========================================
@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    if not await is_feature_enabled('vip_enabled'): return await cb.message.edit_text("🚧 **VIP üyelik geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.message.edit_text("💎 VIP sistemimiz aktif! (Admin panelinden yönetebilirsiniz)", reply_markup=back_to_menu_kb())

@router.callback_query(F.data == "menu:coupon")
async def menu_coupon(cb: CallbackQuery):
    if not await is_feature_enabled('coupon_enabled'): return await cb.message.edit_text("🚧 **Kupon sistemi geçici olarak kapalıdır.**", reply_markup=back_to_menu_kb(), parse_mode="Markdown")
    await cb.message.edit_text("🎟️ Kupon sistemimiz aktif! (Admin panelinden yönetebilirsiniz)", reply_markup=back_to_menu_kb())

@router.callback_query(F.data.startswith("n:"))
async def enter_number(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminEnterNumberFlow.phone)
    await state.update_data(order_id=int(cb.data.split(":")[1]))
    await cb.message.answer("Telefon numarasını girin:")
    await cb.answer()

@router.message(AdminEnterNumberFlow.phone)
async def receive_phone(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    oid = data['order_id']
    await state.clear()
    ORDER_PHONE_CACHE[oid] = message.text
    stop_order_animation(oid)
    if oid in ORDER_MESSAGE_REFS:
        try: await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], render_spinner_frame(0, f"##{message.text}##"), parse_mode="HTML")
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
    data = await state.get_data()
    oid = data['order_id']
    await state.clear()
    phone = ORDER_PHONE_CACHE.pop(oid, "")
    if oid in ORDER_MESSAGE_REFS:
        try: await message.bot.edit_message_text(ORDER_MESSAGE_REFS[oid][0], ORDER_MESSAGE_REFS[oid][1], render_spinner_frame(0, f"##{message.text}##"), parse_mode="HTML")
        except: pass
    async with db_session() as db:
        await db.execute("UPDATE orders SET status = 'completed' WHERE order_id = ?", (oid,))
        await db.commit()
        cursor = await db.execute("SELECT user_id FROM orders WHERE order_id = ?", (oid,))
        uid = (await cursor.fetchone())['user_id']
    await message.bot.send_message(uid, f"✅ **Siparişiniz Tamamlandı!**\n\n📱 Numara: `{phone}`\n🔑 Kod: `{message.text}`", parse_mode="Markdown")
    await message.answer("Sipariş başarıyla kapatıldı.")

@router.callback_query(F.data.startswith("o:"))
async def cancel_order(cb: CallbackQuery):
    action, oid = cb.data.split(":")[1], int(cb.data.split(":")[2])
    if action == "refund" and not is_admin(cb.from_user.id): return
    async with db_session() as db:
        cursor = await db.execute("SELECT user_id, amount_cents FROM orders WHERE order_id = ? AND status = 'pending'", (oid,))
        order = await cursor.fetchone()
        if order:
            await db.execute("UPDATE orders SET status = 'refunded' WHERE order_id = ?", (oid,))
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (order['amount_cents'], order['user_id']))
            await add_transaction(db, order['user_id'], order['amount_cents'], "refund", f"#{oid} İade")
            await db.commit()
            stop_order_animation(oid)
            ORDER_MESSAGE_REFS.pop(oid, None)
            await cb.bot.send_message(order['user_id'], "❌ Sipariş iptal edildi, bakiye iade edildi.")
            await cb.answer("İade yapıldı.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("İptal edildi.", reply_markup=back_to_menu_kb())

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