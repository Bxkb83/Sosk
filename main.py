# -*- coding: utf-8 -*-
import asyncio
import logging
import aiosqlite

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice
)
from aiogram.client.default import DefaultBotProperties

BOT_TOKEN = "8296234950:AAF0zJu5WgUzMrct9dxdjuL0bzH4lFel814"
ADMIN_IDS = [8806248358]
CHANNEL = "@MythAX"
CHANNEL_LINK = "https://t.me/MythAX"
CANCEL_MINUTES = 6
REFERRAL_POINT = 1
IBAN = "TR00 0000 0000 0000 0000 0000 00"
IBAN_NAME = "Ad Soyad"
DB_PATH = "bot.db"

DEFAULT_MARKET = [
    ("whatsapp", "WhatsApp", 15),
    ("telegram", "Telegram", 12),
    ("uber", "Uber", 20),
    ("trendyol", "Trendyol", 18),
    ("instagram", "Instagram", 25),
    ("google", "Google", 22),
]

COUNTRIES = [("+90", "TR"), ("+1", "USA")]


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "user_id INTEGER PRIMARY KEY,"
            "username TEXT, first_name TEXT,"
            "balance INTEGER DEFAULT 0,"
            "points INTEGER DEFAULT 0,"
            "referrer_id INTEGER,"
            "was_member INTEGER DEFAULT 0,"
            "reset_count INTEGER DEFAULT 0,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS transactions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER, amount INTEGER, type TEXT,"
            "description TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS requests ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER, app_name TEXT, country_code TEXT,"
            "price INTEGER, status TEXT DEFAULT 'pending',"
            "phone TEXT, code TEXT, admin_msg_ids TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS promos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "code TEXT UNIQUE, max_uses INTEGER,"
            "current_uses INTEGER DEFAULT 0,"
            "remaining_hours INTEGER, bonus INTEGER,"
            "channel_message_id INTEGER,"
            "active INTEGER DEFAULT 1,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS promo_uses ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "promo_id INTEGER, user_id INTEGER,"
            "used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS market ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "key TEXT UNIQUE, name TEXT, active INTEGER DEFAULT 1)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS prices ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "app_key TEXT, country TEXT, price INTEGER,"
            "UNIQUE(app_key, country))"
        )
        for key, name, price in DEFAULT_MARKET:
            await db.execute(
                "INSERT OR IGNORE INTO market(key, name) VALUES(?, ?)",
                (key, name)
            )
            for cc, _ in COUNTRIES:
                await db.execute(
                    "INSERT OR IGNORE INTO prices(app_key, country, price) "
                    "VALUES(?, ?, ?)",
                    (key, cc, price)
                )
        await db.commit()


async def db_get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (uid,)
        ) as c:
            return await c.fetchone()


async def db_create_user(uid, username, first_name, ref=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users"
            "(user_id, username, first_name, referrer_id) "
            "VALUES(?, ?, ?, ?)",
            (uid, username, first_name, ref)
        )
        await db.commit()


async def db_set_member_flag(uid, flag):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET was_member = ? WHERE user_id = ?",
            (flag, uid)
        )
        await db.commit()


async def db_reset_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = 0, points = 0, "
            "referrer_id = NULL, reset_count = reset_count + 1 "
            "WHERE user_id = ?",
            (uid,)
        )
        await db.execute("DELETE FROM transactions WHERE user_id = ?", (uid,))
        await db.execute("DELETE FROM promo_uses WHERE user_id = ?", (uid,))
        await db.execute("DELETE FROM requests WHERE user_id = ?", (uid,))
        await db.commit()


async def db_update_balance(uid, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, uid)
        )
        await db.commit()


async def db_add_points(uid, p):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET points = points + ? WHERE user_id = ?",
            (p, uid)
        )
        await db.commit()


async def db_add_tx(uid, amount, ttype, desc):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions(user_id, amount, type, description) "
            "VALUES(?, ?, ?, ?)",
            (uid, amount, ttype, desc)
        )
        await db.commit()


async def db_total_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            return (await c.fetchone())[0]


async def db_user_txs(uid, limit=8):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT amount, type, description, created_at FROM transactions "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        ) as c:
            return await c.fetchall()


async def db_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            return [r[0] for r in await c.fetchall()]


async def db_top_referrers(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, first_name, username, points FROM users "
            "WHERE points > 0 ORDER BY points DESC LIMIT ?",
            (limit,)
        ) as c:
            return await c.fetchall()


async def db_get_market():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT key, name FROM market WHERE active = 1"
        ) as c:
            return await c.fetchall()


async def db_get_price(app_key, country):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT price FROM prices WHERE app_key = ? AND country = ?",
            (app_key, country)
        ) as c:
            r = await c.fetchone()
            return r[0] if r else 0


async def db_set_price(app_key, country, price):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO prices(app_key, country, price) VALUES(?, ?, ?) "
            "ON CONFLICT(app_key, country) DO UPDATE SET price = excluded.price",
            (app_key, country, price)
        )
        await db.commit()


async def db_create_request(uid, app_name, cc, price):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO requests(user_id, app_name, country_code, price) "
            "VALUES(?, ?, ?, ?)",
            (uid, app_name, cc, price)
        )
        await db.commit()
        return cur.lastrowid


async def db_get_request(rid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM requests WHERE id = ?", (rid,)
        ) as c:
            return await c.fetchone()


async def db_set_request_admin_msgs(rid, ids):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET admin_msg_ids = ? WHERE id = ?",
            (",".join(map(str, ids)), rid)
        )
        await db.commit()


async def db_complete_request(rid, phone, code):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET status = 'completed', phone = ?, code = ? "
            "WHERE id = ?",
            (phone, code, rid)
        )
        await db.commit()


async def db_set_request_status(rid, s):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET status = ? WHERE id = ?", (s, rid)
        )
        await db.commit()


async def db_pending_requests():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM requests WHERE status = 'pending'"
        ) as c:
            return await c.fetchall()


async def db_user_active_request(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM requests WHERE user_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (uid,)
        ) as c:
            return await c.fetchone()


async def db_create_promo(code, mx, hours, bonus, msg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promos(code, max_uses, remaining_hours, bonus, "
            "channel_message_id) VALUES(?, ?, ?, ?, ?)",
            (code, mx, hours, bonus, msg_id)
        )
        await db.commit()


async def db_get_promo(code):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM promos WHERE code = ? AND active = 1", (code,)
        ) as c:
            return await c.fetchone()


async def db_get_promo_by_id(pid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM promos WHERE id = ?", (pid,)
        ) as c:
            return await c.fetchone()


async def db_inc_promo_use(pid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE promos SET current_uses = current_uses + 1 WHERE id = ?",
            (pid,)
        )
        await db.commit()


async def db_update_promo_hours(pid, h):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE promos SET remaining_hours = ? WHERE id = ?", (h, pid)
        )
        await db.commit()


async def db_deactivate_promo(pid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE promos SET active = 0 WHERE id = ?", (pid,))
        await db.commit()


async def db_active_promos():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM promos WHERE active = 1"
        ) as c:
            return await c.fetchall()


async def db_user_used_promo(pid, uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM promo_uses WHERE promo_id = ? AND user_id = ?",
            (pid, uid)
        ) as c:
            return await c.fetchone() is not None


async def db_add_promo_use(pid, uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promo_uses(promo_id, user_id) VALUES(?, ?)",
            (pid, uid)
        )
        await db.commit()


def kb_main(is_admin=False):
    rows = [
        [InlineKeyboardButton(text="Market", callback_data="market")],
        [InlineKeyboardButton(text="Bakiye Yukle", callback_data="deposit")],
        [InlineKeyboardButton(text="Promo Kullan", callback_data="promo_use")],
        [
            InlineKeyboardButton(text="Profilim", callback_data="profile"),
            InlineKeyboardButton(text="Referans", callback_data="referral")
        ],
        [InlineKeyboardButton(text="Liderlik", callback_data="leaderboard")],
        [InlineKeyboardButton(text="Admin Iletisim", callback_data="contact_admin")],
        [InlineKeyboardButton(text="Yardim", callback_data="help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(
            text="Admin Paneli", callback_data="admin_panel"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_join():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Kanala Katil", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="Katildim Kontrol Et", callback_data="check_join")],
    ])


def kb_market(apps):
    rows = []
    row = []
    for key, name in apps:
        row.append(InlineKeyboardButton(text=name, callback_data="app:" + key))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Geri", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_country(app_key, prices):
    rows = []
    for cc, label in COUNTRIES:
        price = prices.get(cc, 0)
        rows.append([InlineKeyboardButton(
            text=label + " " + cc + " - " + str(price) + " TL",
            callback_data="buy:" + app_key + ":" + cc
        )])
    rows.append([InlineKeyboardButton(text="Geri", callback_data="market")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel(rid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Iptal Et Iade", callback_data="cancel_req:" + str(rid)
        )],
    ])


def kb_back(target="back_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Geri", callback_data=target)],
    ])


def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Bekleyenler", callback_data="adm_pendings")],
        [InlineKeyboardButton(text="Fiyat Duzenle", callback_data="adm_prices")],
        [InlineKeyboardButton(text="Istatistik", callback_data="adm_stats")],
        [InlineKeyboardButton(text="Promo Olustur", callback_data="adm_new_promo")],
        [InlineKeyboardButton(text="Aktif Promolar", callback_data="adm_list_promos")],
        [InlineKeyboardButton(text="Promo Sil", callback_data="adm_del_promo")],
        [InlineKeyboardButton(text="Duyuru", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="Ana Menu", callback_data="back_menu")],
    ])


def kb_request_action(rid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Numara ve Kod Gir",
            callback_data="fill:" + str(rid)
        )],
        [InlineKeyboardButton(
            text="Reddet Iade",
            callback_data="reject:" + str(rid)
        )],
    ])


def kb_admin_prices(apps):
    rows = []
    for key, name in apps:
        rows.append([InlineKeyboardButton(
            text=name, callback_data="editprices:" + key
        )])
    rows.append([InlineKeyboardButton(text="Geri", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_price_country(app_key, prices):
    rows = []
    for cc, label in COUNTRIES:
        rows.append([InlineKeyboardButton(
            text=label + " " + cc + " - " + str(prices.get(cc, 0)) + " TL",
            callback_data="setprice:" + app_key + ":" + cc
        )])
    rows.append([InlineKeyboardButton(text="Geri", callback_data="adm_prices")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class Flow(StatesGroup):
    promo_use = State()
    fill_phone_code = State()


class Adm(StatesGroup):
    newpromo_code = State()
    newpromo_uses = State()
    newpromo_days = State()
    newpromo_bonus = State()
    delpromo_code = State()
    broadcast_text = State()
    set_price_val = State()


router = Router()


def is_admin(uid):
    return uid in ADMIN_IDS


async def check_membership(bot, uid):
    u = await db_get_user(uid)
    if not u:
        return False
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        now = m.status in ("member", "administrator", "creator")
    except Exception:
        now = False
    was = u[6]
    if was == 1 and not now:
        await db_reset_user(uid)
        await db_set_member_flag(uid, 0)
        return False
    if now and was == 0:
        await db_set_member_flag(uid, 1)
    return now


@router.message(Command("start"))
async def cmd_start(m: Message, bot: Bot):
    uid = m.from_user.id
    ref = None
    parts = m.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1][4:])
        except Exception:
            pass
    before = await db_get_user(uid)
    is_new = before is None
    await db_create_user(uid, m.from_user.username, m.from_user.first_name, ref)
    if is_new and ref and ref != uid:
        r = await db_get_user(ref)
        if r:
            await db_add_points(ref, REFERRAL_POINT)
            await db_add_tx(ref, 0, "referral", "Davet " + str(uid))
            try:
                await bot.send_message(
                    ref,
                    "Yeni davet +" + str(REFERRAL_POINT) + " puan"
                )
            except Exception:
                pass
    if not await check_membership(bot, uid):
        await m.answer(
            "Hos geldin! Botu kullanmak icin " + CHANNEL +
            " kanalina katilman zorunlu. Kanaldan cikarsan tum verilerin "
            "sifirlanir ve geri gelmez.",
            reply_markup=kb_join()
        )
        return
    await m.answer(
        "Merhaba " + str(m.from_user.first_name) + "! VirtuNum Market botuna hos geldin.",
        reply_markup=kb_main(is_admin(uid))
    )


@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: Bot):
    if await check_membership(bot, cb.from_user.id):
        await cb.message.edit_text(
            "Katilim onaylandi!",
            reply_markup=kb_main(is_admin(cb.from_user.id))
        )
    else:
        await cb.answer("Henuz katilmadin!", show_alert=True)


@router.callback_query(F.data == "back_menu")
async def cb_back(cb: CallbackQuery):
    await cb.message.edit_text(
        "Ana menu:",
        reply_markup=kb_main(is_admin(cb.from_user.id))
    )


@router.callback_query(F.data == "market")
async def cb_market(cb: CallbackQuery):
    apps = await db_get_market()
    await cb.message.edit_text(
        "Market - bir uygulama sec:",
        reply_markup=kb_market(apps)
    )


@router.callback_query(F.data.startswith("app:"))
async def cb_app(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    apps = await db_get_market()
    name = None
    for k, n in apps:
        if k == key:
            name = n
            break
    if not name:
        await cb.answer("Bulunamadi", show_alert=True)
        return
    prices = {}
    for cc, _ in COUNTRIES:
        prices[cc] = await db_get_price(key, cc)
    await cb.message.edit_text(
        name + " - numara almak istedigin ulkeyi sec:",
        reply_markup=kb_country(key, prices)
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    app_key = parts[1]
    cc = parts[2]
    uid = cb.from_user.id
    if not await check_membership(bot, uid):
        await cb.message.edit_text(
            "Kanaldan cikmissin! Bakiyen geri gelmez.
