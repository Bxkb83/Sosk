# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    User,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "").strip()
CANCEL_SECONDS = 6 * 60
DAILY_REWARD_CENTS = 100
MAX_AMOUNT_CENTS = 100_000_000
router = Router()
logger = logging.getLogger("service-order-bot")


def parse_admin_ids():
    raw_ids = os.getenv("ADMIN_IDS", "")
    result = set()
    for item in raw_ids.split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            result.add(int(item))
    return result


ADMIN_IDS = parse_admin_ids()


class DepositFlow(StatesGroup):
    amount = State()
    reference = State()


class PromoFlow(StatesGroup):
    code = State()


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


def clean_text(value, max_length=180):
    text = " ".join(str(value).strip().split())
    return text[:max_length]


def is_admin(user_id):
    return user_id in ADMIN_IDS


def main_keyboard(user_id):
    rows = [
        [
            KeyboardButton(text="Hizmetler"),
            KeyboardButton(text="Bakiye"),
        ],
        [
            KeyboardButton(text="Siparisler"),
            KeyboardButton(text="Bakiye Yukle"),
        ],
        [
            KeyboardButton(text="Promosyon"),
            KeyboardButton(text="Yardim"),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="Admin Paneli")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def service_keyboard(services):
    rows = []
    for service in services:
        label = clean_text(service["name"], 45)
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data="s:" + str(service["service_id"]),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_detail_keyboard(service_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Siparis Olustur",
                    callback_data="b:" + str(service_id),
                )
            ]
        ]
    )


def order_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Iptal Et ve Iade Al",
                    callback_data="o:cancel:" + str(order_id),
                )
            ]
        ]
    )


def admin_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Tamamlandi",
                    callback_data="o:done:" + str(order_id),
                ),
                InlineKeyboardButton(
                    text="Iptal Et ve Iade",
                    callback_data="o:refund:" + str(order_id),
                ),
            ]
        ]
    )


def deposit_keyboard(deposit_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Onayla",
                    callback_data="d:approve:" + str(deposit_id),
                ),
                InlineKeyboardButton(
                    text="Reddet",
                    callback_data="d:reject:" + str(deposit_id),
                ),
            ]
        ]
    )


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


async def init_db():
    if DB_PATH != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    async with db_session() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                last_daily_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS services (
                service_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                service_id INTEGER NOT NULL REFERENCES services(service_id),
                service_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                amount_cents INTEGER NOT NULL,
                kind TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deposit_requests (
                deposit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                reference TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promos (
                promo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                bonus_cents INTEGER NOT NULL CHECK(bonus_cents > 0),
                max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                current_uses INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS promo_uses (
                promo_id INTEGER NOT NULL REFERENCES promos(promo_id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                used_at TEXT NOT NULL,
                PRIMARY KEY(promo_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiry
                ON orders(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_orders_user
                ON orders(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(user_id, transaction_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_status
                ON deposit_requests(status, created_at);
            """
        )
        await db.commit()


async def ensure_user(user):
    async with db_session() as db:
        await db.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (
                user.id,
                clean_text(user.username or "", 64),
                clean_text(user.first_name or "User", 100),
                timestamp(),
            ),
        )
        await db.commit()


async def add_transaction(db, user_id, amount_cents, kind, note):
    await db.execute(
        """
        INSERT INTO transactions (
            user_id, amount_cents, kind, note, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            user_id,
            amount_cents,
            kind,
            clean_text(note, 180),
            timestamp(),
        ),
    )


async def notify_admins(bot, text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                text,
                reply_markup=reply_markup,
            )
        except TelegramAPIError:
            logger.warning("Could not notify admin %s", admin_id)


async def notify_user(bot, user_id, text):
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        logger.warning("Could not notify user %s", user_id)


async def show_services(message):
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT service_id, name, description, price_cents
            FROM services
            WHERE active = 1
            ORDER BY service_id
            """
        )
        services = await cursor.fetchall()
    if not services:
        await message.answer(
            "Su an aktif hizmet yok. Yonetici daha sonra hizmet ekleyebilir."
        )
        return
    await message.answer(
        "Bir hizmet secin:",
        reply_markup=service_keyboard(services),
    )


def contains_disallowed_service_text(value):
    normalized = str(value).lower()
    replacements = {
        "ı": "i",
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ö": "o",
        "ç": "c",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    blocked_phrases = (
        "otp",
        "sms",
        "verification",
        "verify code",
        "verification code",
        "dogrulama",
        "activation code",
        "activate account",
        "sanal numara",
        "virtual number",
        "phone number",
        "telefon numara",
        "numara kirala",
        "numara satin",
        "sms code",
        "sms kod",
        "kod al",
    )
    return any(phrase in normalized for phrase in blocked_phrases)


async def create_order(user_id, service_id):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            SELECT service_id, name, price_cents
            FROM services
            WHERE service_id = ? AND active = 1
            """,
            (service_id,),
        )
        service = await cursor.fetchone()
        if service is None:
            await db.rollback()
            return "missing", None
        amount = int(service["price_cents"])
        cursor = await db.execute(
            """
            UPDATE users
            SET balance_cents = balance_cents - ?
            WHERE user_id = ? AND balance_cents >= ?
            """,
            (amount, user_id, amount),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return "balance", None
        created = now_utc()
        expires = created + timedelta(seconds=CANCEL_SECONDS)
        cursor = await db.execute(
            """
            INSERT INTO orders (
                user_id, service_id, service_name, amount_cents,
                status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                user_id,
                service["service_id"],
                clean_text(service["name"], 80),
                amount,
                created.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
        order_id = cursor.lastrowid
        await add_transaction(
            db,
            user_id,
            -amount,
            "order_reserve",
            "Order #" + str(order_id),
        )
        await db.commit()
        return "created", {
            "order_id": order_id,
            "service_name": service["name"],
            "amount_cents": amount,
            "expires_at": expires.isoformat(timespec="seconds"),
        }
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def refund_order(order_id, owner_id=None, enforce_expiry=False):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,),
        )
        order = await cursor.fetchone()
        if order is None or order["status"] != "pending":
            await db.rollback()
            return "closed", None
        if owner_id is not None and order["user_id"] != owner_id:
            await db.rollback()
            return "denied", None
        if enforce_expiry and order["expires_at"] <= timestamp():
            await db.rollback()
            return "expired", dict(order)
        cursor = await db.execute(
            """
            UPDATE orders
            SET status = 'refunded'
            WHERE order_id = ? AND status = 'pending'
            """,
            (order_id,),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return "closed", None
        await db.execute(
            """
            UPDATE users
            SET balance_cents = balance_cents + ?
            WHERE user_id = ?
            """,
            (order["amount_cents"], order["user_id"]),
        )
        await add_transaction(
            db,
            order["user_id"],
            order["amount_cents"],
            "order_refund",
            "Order #" + str(order_id) + " refund",
        )
        await db.commit()
        return "refunded", dict(order)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def complete_order(order_id):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,),
        )
        order = await cursor.fetchone()
        if order is None or order["status"] != "pending":
            await db.rollback()
            return "closed", None
        if order["expires_at"] <= timestamp():
            await db.rollback()
            result, row = await refund_order(order_id)
            if result == "refunded":
                return "expired", row
            return "closed", None
        cursor = await db.execute(
            """
            UPDATE orders
            SET status = 'completed', completed_at = ?
            WHERE order_id = ? AND status = 'pending'
            """,
            (timestamp(), order_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return "closed", None
        await db.commit()
        return "completed", dict(order)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def expire_orders(bot):
    while True:
        try:
            async with db_session() as db:
                cursor = await db.execute(
                    """
                    SELECT order_id, user_id
                    FROM orders
                    WHERE status = 'pending' AND expires_at <= ?
                    ORDER BY order_id
                    LIMIT 100
                    """,
                    (timestamp(),),
                )
                rows = await cursor.fetchall()
            for row in rows:
                status, order = await refund_order(row["order_id"])
                if status == "refunded":
                    await notify_user(
                        bot,
                        order["user_id"],
                        "Siparis suresi doldu. Tutar bakiyenize iade edildi.",
                    )
        except Exception:
            logger.exception("Order expiry task failed")
        await asyncio.sleep(15)


async def review_deposit(deposit_id, approve):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            SELECT * FROM deposit_requests
            WHERE deposit_id = ? AND status = 'pending'
            """,
            (deposit_id,),
        )
        deposit = await cursor.fetchone()
        if deposit is None:
            await db.rollback()
            return "closed", None
        next_status = "approved" if approve else "rejected"
        cursor = await db.execute(
            """
            UPDATE deposit_requests
            SET status = ?
            WHERE deposit_id = ? AND status = 'pending'
            """,
            (next_status, deposit_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return "closed", None
        if approve:
            await db.execute(
                """
                UPDATE users
                SET balance_cents = balance_cents + ?
                WHERE user_id = ?
                """,
                (deposit["amount_cents"], deposit["user_id"]),
            )
            await add_transaction(
                db,
                deposit["user_id"],
                deposit["amount_cents"],
                "deposit",
                "Deposit #" + str(deposit_id),
            )
        await db.commit()
        return next_status, dict(deposit)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def claim_promo(user_id, code):
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            SELECT * FROM promos
            WHERE code = ? AND active = 1
            """,
            (code.upper(),),
        )
        promo = await cursor.fetchone()
        if promo is None:
            await db.rollback()
            return "missing", 0
        if promo["expires_at"] <= timestamp():
            await db.rollback()
            return "expired", 0
        if promo["current_uses"] >= promo["max_uses"]:
            await db.rollback()
            return "used_up", 0
        cursor = await db.execute(
            """
            SELECT 1 FROM promo_uses
            WHERE promo_id = ? AND user_id = ?
            """,
            (promo["promo_id"], user_id),
        )
        if await cursor.fetchone():
            await db.rollback()
            return "already_used", 0
        await db.execute(
            """
            INSERT INTO promo_uses (promo_id, user_id, used_at)
            VALUES (?, ?, ?)
            """,
            (promo["promo_id"], user_id, timestamp()),
        )
        await db.execute(
            """
            UPDATE promos
            SET current_uses = current_uses + 1
            WHERE promo_id = ?
            """,
            (promo["promo_id"],),
        )
        await db.execute(
            """
            UPDATE users
            SET balance_cents = balance_cents + ?
            WHERE user_id = ?
            """,
            (promo["bonus_cents"], user_id),
        )
        await add_transaction(
            db,
            user_id,
            promo["bonus_cents"],
            "promo",
            "Promo " + code.upper(),
        )
        await db.commit()
        return "claimed", int(promo["bonus_cents"])
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def process_promo(message, code):
    if message.from_user is not None:
        await ensure_user(message.from_user)
    result, bonus = await claim_promo(
        message.from_user.id,
        clean_text(code, 32).upper(),
    )
    replies = {
        "missing": "Promo bulunamadi veya artik aktif degil.",
        "expired": "Promonun suresi dolmus.",
        "used_up": "Promo kullanim sinirina ulasmis.",
        "already_used": "Bu promoyu daha once kullandiniz.",
    }
    if result == "claimed":
        await message.answer(
            "Promo uygulandi. Bakiyenize "
            + format_money(bonus)
            + " eklendi."
        )
        return
    await message.answer(replies.get(result, "Promo kullanilamadi."))


def admin_help_text():
    return "\n".join(
        [
            "Admin komutlari:",
            "/addservice isim|fiyat|aciklama",
            "/delservice hizmet_id",
            "/addbalance kullanici_id tutar",
            "/removebalance kullanici_id tutar",
            "/promoadd KOD bonus kullanim saat",
            "/promodel KOD",
            "/userinfo kullanici_id",
            "/totalusers",
            "/pendings",
            "/deposits",
            "/stats",
        ]
    )


async def send_pending_orders(message):
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT order_id, user_id, service_name, amount_cents,
                   expires_at
            FROM orders
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT 30
            """
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Bekleyen siparis yok.")
        return
    for row in rows:
        text = "\n".join(
            [
                "Siparis #" + str(row["order_id"]),
                "Kullanici: " + str(row["user_id"]),
                "Hizmet: " + clean_text(row["service_name"], 80),
                "Tutar: " + format_money(row["amount_cents"]),
                "Son tarih: " + row["expires_at"],
            ]
        )
        await message.answer(
            text,
            reply_markup=admin_order_keyboard(row["order_id"]),
        )


async def send_pending_deposits(message):
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT deposit_id, user_id, amount_cents, reference
            FROM deposit_requests
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT 30
            """
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Bekleyen bakiye talepleri yok.")
        return
    for row in rows:
        text = "\n".join(
            [
                "Bakiye talebi #" + str(row["deposit_id"]),
                "Kullanici: " + str(row["user_id"]),
                "Tutar: " + format_money(row["amount_cents"]),
                "Referans: " + clean_text(row["reference"], 180),
            ]
        )
        await message.answer(
            text,
            reply_markup=deposit_keyboard(row["deposit_id"]),
        )


@router.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    text = "\n".join(
        [
            "Merhaba. Bu bot genel hizmet siparislerini yonetir.",
            "Telefon numarasi ve SMS dogrulama kodu islenmez.",
            "Asagidaki menuden devam edin.",
        ]
    )
    await message.answer(
        text,
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(Command("help"))
@router.message(F.text == "Yardim")
async def help_handler(message: Message):
    lines = [
        "Kullanici komutlari:",
        "/start - Ana menu",
        "/services - Hizmetleri gor",
        "/balance - Bakiyeyi gor",
        "/history - Siparis gecmisi",
        "/daily - Gunluk odul",
        "/promo KOD - Promo kullan",
        "",
        "Siparisler yalnizca genel hizmetler icindir.",
        "Telefon numarasi veya SMS kodu girilmez ya da iletilmez.",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cancel_flow_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Islem iptal edildi.")


@router.message(Command("services"))
@router.message(F.text == "Hizmetler")
async def services_handler(message: Message):
    await show_services(message)


@router.message(Command("balance"))
@router.message(F.text == "Bakiye")
async def balance_handler(message: Message):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    async with db_session() as db:
        cursor = await db.execute(
            "SELECT balance_cents FROM users WHERE user_id = ?",
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
    balance = int(row["balance_cents"]) if row else 0
    await message.answer("Bakiyeniz: " + format_money(balance))


@router.message(Command("history"))
@router.message(F.text == "Siparisler")
async def history_handler(message: Message):
    if message.from_user is None:
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT order_id, service_name, amount_cents, status, created_at
            FROM orders
            WHERE user_id = ?
            ORDER BY order_id DESC
            LIMIT 10
            """,
            (message.from_user.id,),
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Henuz siparisiniz yok.")
        return
    statuses = {
        "pending": "Bekliyor",
        "completed": "Tamamlandi",
        "refunded": "Iade edildi",
    }
    lines = ["Son siparisleriniz:"]
    for row in rows:
        line = (
            "#"
            + str(row["order_id"])
            + " "
            + clean_text(row["service_name"], 50)
            + " - "
            + format_money(row["amount_cents"])
            + " - "
            + statuses.get(row["status"], row["status"])
        )
        lines.append(line)
    await message.answer("\n".join(lines))


@router.message(Command("daily"))
async def daily_handler(message: Message):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT last_daily_at FROM users WHERE user_id = ?",
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
        current = now_utc()
        previous = row["last_daily_at"] if row else None
        if previous:
            last_claim = datetime.fromisoformat(previous)
            wait_until = last_claim + timedelta(hours=24)
            if current < wait_until:
                minutes = int((wait_until - current).total_seconds() / 60)
                await db.rollback()
                await message.answer(
                    "Gunluk odul icin yaklasik "
                    + str(max(minutes, 1))
                    + " dakika bekleyin."
                )
                return
        await db.execute(
            """
            UPDATE users
            SET balance_cents = balance_cents + ?, last_daily_at = ?
            WHERE user_id = ?
            """,
            (
                DAILY_REWARD_CENTS,
                current.isoformat(timespec="seconds"),
                message.from_user.id,
            ),
        )
        await add_transaction(
            db,
            message.from_user.id,
            DAILY_REWARD_CENTS,
            "daily",
            "Gunluk odul",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    await message.answer(
        "Gunluk odul eklendi: "
        + format_money(DAILY_REWARD_CENTS)
    )


@router.message(Command("promo"))
async def promo_command(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        await process_promo(message, parts[1])
        return
    await state.set_state(PromoFlow.code)
    await message.answer("Promo kodunu gonderin.")


@router.message(F.text == "Promosyon")
async def promo_button(message: Message, state: FSMContext):
    await state.set_state(PromoFlow.code)
    await message.answer("Promo kodunu gonderin.")


@router.message(PromoFlow.code)
async def promo_code_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Gecerli bir promo kodu yazin.")
        return
    await state.clear()
    await process_promo(message, message.text)


@router.callback_query(F.data.startswith("s:"))
async def service_detail_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        service_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Hizmet bulunamadi.", show_alert=True)
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT service_id, name, description, price_cents
            FROM services
            WHERE service_id = ? AND active = 1
            """,
            (service_id,),
        )
        service = await cursor.fetchone()
    if service is None:
        await callback.answer("Hizmet artik aktif degil.", show_alert=True)
        return
    description = clean_text(service["description"], 180)
    text = (
        clean_text(service["name"], 80)
        + "\n"
        + description
        + "\nFiyat: "
        + format_money(service["price_cents"])
    )
    if callback.message:
        await callback.message.answer(
            text,
            reply_markup=service_detail_keyboard(service_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("b:"))
async def buy_service_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        service_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Hizmet bulunamadi.", show_alert=True)
        return
    await ensure_user(callback.from_user)
    result, order = await create_order(
        callback.from_user.id,
        service_id,
    )
    if result == "missing":
        await callback.answer("Hizmet artik aktif degil.", show_alert=True)
        return
    if result == "balance":
        await callback.answer(
            "Yetersiz bakiye. Yoneticiyle gorusun.",
            show_alert=True,
        )
        return
    order_id = order["order_id"]
    body = "\n".join(
        [
            "Siparis #" + str(order_id) + " olusturuldu.",
            "Hizmet: " + clean_text(order["service_name"], 80),
            "Rezerve edilen tutar: "
            + format_money(order["amount_cents"]),
            "Iptal suresi: 6 dakika.",
            "Admin siparisi tamamlayabilir veya iade edebilir.",
        ]
    )
    if callback.message:
        await callback.message.answer(
            body,
            reply_markup=order_keyboard(order_id),
        )
    admin_body = "\n".join(
        [
            "Yeni siparis #" + str(order_id),
            "Kullanici: " + str(callback.from_user.id),
            "Hizmet: " + clean_text(order["service_name"], 80),
            "Tutar: " + format_money(order["amount_cents"]),
            "Bu bot numara veya SMS kodu gondermez.",
        ]
    )
    await notify_admins(
        callback.bot,
        admin_body,
        reply_markup=admin_order_keyboard(order_id),
    )
    await callback.answer("Siparis olusturuldu.")


@router.callback_query(F.data.startswith("o:"))
async def order_action_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Islem gecersiz.", show_alert=True)
        return
    action = parts[1]
    try:
        order_id = int(parts[2])
    except ValueError:
        await callback.answer("Siparis bulunamadi.", show_alert=True)
        return
    if action == "cancel":
        result, order = await refund_order(
            order_id,
            owner_id=callback.from_user.id,
            enforce_expiry=True,
        )
        if result == "expired":
            refund_result, order = await refund_order(order_id)
            if refund_result == "refunded" and order:
                await notify_user(
                    callback.bot,
                    order["user_id"],
                    "Siparis suresi doldu. Tutar bakiyenize iade edildi.",
                )
            await callback.answer(
                "Iptal suresi doldu.",
                show_alert=True,
            )
            return
        if result == "denied":
            await callback.answer("Bu siparis size ait degil.", True)
            return
        if result != "refunded" or order is None:
            await callback.answer("Siparis artik beklemiyor.", True)
            return
        await callback.answer("Tutar iade edildi.")
        if callback.message:
            await callback.message.answer(
                "Siparis iptal edildi. "
                + format_money(order["amount_cents"])
                + " bakiyenize iade edildi."
            )
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Yetkiniz yok.", show_alert=True)
        return
    if action == "refund":
        result, order = await refund_order(order_id)
        if result != "refunded" or order is None:
            await callback.answer("Siparis artik beklemiyor.", True)
            return
        await notify_user(
            callback.bot,
            order["user_id"],
            "Siparis iptal edildi. Tutar bakiyenize iade edildi.",
        )
        await callback.answer("Iade tamamlandi.")
        return
    if action == "done":
        result, order = await complete_order(order_id)
        if order is None:
            await callback.answer("Siparis artik beklemiyor.", True)
            return
        if result == "expired":
            await notify_user(
                callback.bot,
                order["user_id"],
                "Siparis suresi doldu. Tutar bakiyenize iade edildi.",
            )
            await callback.answer("Sure dolmustu; tutar iade edildi.")
            return
        if result != "completed":
            await callback.answer("Siparis tamamlanamadi.", True)
            return
        await notify_user(
            callback.bot,
            order["user_id"],
            "Siparis tamamlandi.",
        )
        await callback.answer("Siparis tamamlandi.")
        return
    await callback.answer("Islem gecersiz.", show_alert=True)


@router.message(F.text == "Bakiye Yukle")
async def deposit_start(message: Message, state: FSMContext):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    lines = ["Yuklemek istediginiz tutari TL olarak yazin."]
    if PAYMENT_INSTRUCTIONS:
        lines.extend(["", PAYMENT_INSTRUCTIONS])
    else:
        lines.extend(
            [
                "",
                "Odeme bilgileri yonetici tarafindan iletilir.",
                "Talep, yonetici odemeyi kontrol ettikten sonra onaylanir.",
            ]
        )
    await state.set_state(DepositFlow.amount)
    await message.answer("\n".join(lines))


@router.message(DepositFlow.amount)
async def deposit_amount_input(message: Message, state: FSMContext):
    try:
        amount = parse_money(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(amount_cents=amount)
    await state.set_state(DepositFlow.reference)
    await message.answer(
        "Transfer aciklamasini veya odeme referansini gonderin."
    )


@router.message(DepositFlow.reference)
async def deposit_reference_input(message: Message, state: FSMContext):
    if message.from_user is None:
        return
    reference = clean_text(message.text or "", 180)
    if len(reference) < 2:
        await message.answer("Gecerli bir referans yazin.")
        return
    data = await state.get_data()
    amount = int(data.get("amount_cents", 0))
    if amount <= 0:
        await state.clear()
        await message.answer("Talep olusturulamadi. Yeniden deneyin.")
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            INSERT INTO deposit_requests (
                user_id, amount_cents, reference, status, created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (
                message.from_user.id,
                amount,
                reference,
                timestamp(),
            ),
        )
        deposit_id = cursor.lastrowid
        await db.commit()
    await state.clear()
    await message.answer(
        "Bakiye talebiniz #" + str(deposit_id) + " yoneticiye iletildi."
    )
    admin_text = "\n".join(
        [
            "Yeni bakiye talebi #" + str(deposit_id),
            "Kullanici: " + str(message.from_user.id),
            "Tutar: " + format_money(amount),
            "Referans: " + reference,
        ]
    )
    await notify_admins(
        message.bot,
        admin_text,
        reply_markup=deposit_keyboard(deposit_id),
    )


@router.callback_query(F.data.startswith("d:"))
async def deposit_action_handler(callback: CallbackQuery):
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Yetkiniz yok.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("approve", "reject"):
        await callback.answer("Islem gecersiz.", show_alert=True)
        return
    try:
        deposit_id = int(parts[2])
    except ValueError:
        await callback.answer("Talep bulunamadi.", show_alert=True)
        return
    approve = parts[1] == "approve"
    result, deposit = await review_deposit(deposit_id, approve)
    if result == "closed" or deposit is None:
        await callback.answer("Talep artik beklemiyor.", show_alert=True)
        return
    if approve:
        await notify_user(
            callback.bot,
            deposit["user_id"],
            "Bakiye talebiniz onaylandi. "
            + format_money(deposit["amount_cents"])
            + " hesabiniza eklendi.",
        )
        await callback.answer("Bakiye eklendi.")
    else:
        await notify_user(
            callback.bot,
            deposit["user_id"],
            "Bakiye talebiniz reddedildi.",
        )
        await callback.answer("Talep reddedildi.")


@router.message(Command("addbalance"))
async def add_balance_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Kullanim: /addbalance kullanici_id tutar")
        return
    try:
        amount = parse_money(parts[2])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    user_id = int(parts[1])
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            UPDATE users SET balance_cents = balance_cents + ?
            WHERE user_id = ?
            """,
            (amount, user_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            await message.answer("Kullanici bulunamadi.")
            return
        await add_transaction(
            db,
            user_id,
            amount,
            "admin_credit",
            "Admin bakiye ekledi",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    await message.answer(
        format_money(amount) + " kullaniciya eklendi."
    )
    await notify_user(
        message.bot,
        user_id,
        "Hesabiniza "
        + format_money(amount)
        + " eklendi. Yeni bakiyenizi /balance ile gorebilirsiniz.",
    )


@router.message(Command("removebalance"))
async def remove_balance_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Kullanim: /removebalance kullanici_id tutar"
        )
        return
    try:
        amount = parse_money(parts[2])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    user_id = int(parts[1])
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            UPDATE users
            SET balance_cents = balance_cents - ?
            WHERE user_id = ? AND balance_cents >= ?
            """,
            (amount, user_id, amount),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            await message.answer(
                "Kullanici yok veya bakiyesi yetersiz."
            )
            return
        await add_transaction(
            db,
            user_id,
            -amount,
            "admin_debit",
            "Admin bakiye cikardi",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    await message.answer(
        format_money(amount) + " kullanicidan dusuldu."
    )
    await notify_user(
        message.bot,
        user_id,
        "Bakiyenizden " + format_money(amount) + " dusuldu.",
    )


@router.message(Command("addservice"))
async def add_service_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    args = (message.text or "").partition(" ")[2]
    parts = args.split("|", 2)
    if len(parts) < 2:
        await message.answer(
            "Kullanim: /addservice isim|fiyat|aciklama"
        )
        return
    name = clean_text(parts[0], 60)
    description = clean_text(parts[2], 180) if len(parts) > 2 else ""
    if len(name) < 2:
        await message.answer("Hizmet adi en az iki karakter olmali.")
        return
    if contains_disallowed_service_text(name + " " + description):
        await message.answer(
            "Bu hizmet adi/aciklamasi desteklenmiyor. "
            "Genel hizmetleri kullanin."
        )
        return
    try:
        price = parse_money(parts[1])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            INSERT INTO services (
                name, description, price_cents, active, created_at
            )
            VALUES (?, ?, ?, 1, ?)
            """,
            (name, description, price, timestamp()),
        )
        service_id = cursor.lastrowid
        await db.commit()
    await message.answer(
        "Hizmet eklendi. Hizmet ID: "
        + str(service_id)
        + ", fiyat: "
        + format_money(price)
    )


@router.message(Command("delservice"))
async def delete_service_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /delservice hizmet_id")
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            UPDATE services SET active = 0
            WHERE service_id = ? AND active = 1
            """,
            (int(parts[1]),),
        )
        await db.commit()
    if cursor.rowcount == 1:
        await message.answer("Hizmet kapatildi.")
    else:
        await message.answer("Aktif hizmet bulunamadi.")


@router.message(Command("promoadd"))
async def add_promo_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 5:
        await message.answer(
            "Kullanim: /promoadd KOD bonus kullanim saat"
        )
        return
    code = parts[1].upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer(
            "Kod 3-32 karakter olmali; harf, sayi, _ veya - kullanin."
        )
        return
    if not parts[3].isdigit() or not parts[4].isdigit():
        await message.answer("Kullanim ve saat tam sayi olmali.")
        return
    try:
        bonus = parse_money(parts[2])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    max_uses = int(parts[3])
    hours = int(parts[4])
    if max_uses < 1 or max_uses > 100_000:
        await message.answer("Kullanim siniri 1-100000 araliginda olmali.")
        return
    if hours < 1 or hours > 8760:
        await message.answer("Sure 1-8760 saat araliginda olmali.")
        return
    expires_at = (
        now_utc() + timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    try:
        async with db_session() as db:
            await db.execute(
                """
                INSERT INTO promos (
                    code, bonus_cents, max_uses, expires_at,
                    active, created_at
                )
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (code, bonus, max_uses, expires_at, timestamp()),
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        await message.answer("Bu promo kodu zaten var.")
        return
    await message.answer(
        "Promo olusturuldu: "
        + code
        + " - "
        + format_money(bonus)
        + " - "
        + str(max_uses)
        + " kullanim"
    )


@router.message(Command("promodel"))
async def delete_promo_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Kullanim: /promodel KOD")
        return
    async with db_session() as db:
        cursor = await db.execute(
            "UPDATE promos SET active = 0 WHERE code = ? AND active = 1",
            (parts[1].upper(),),
        )
        await db.commit()
    if cursor.rowcount == 1:
        await message.answer("Promo kapatildi.")
    else:
        await message.answer("Aktif promo bulunamadi.")


@router.message(Command("userinfo"))
async def user_info_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /userinfo kullanici_id")
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, first_name, balance_cents, created_at
            FROM users WHERE user_id = ?
            """,
            (int(parts[1]),),
        )
        user = await cursor.fetchone()
        if user:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS total FROM orders WHERE user_id = ?
                """,
                (int(parts[1]),),
            )
            orders = await cursor.fetchone()
    if user is None:
        await message.answer("Kullanici bulunamadi.")
        return
    await message.answer(
        "\n".join(
            [
                "Kullanici: " + str(user["user_id"]),
                "Ad: " + clean_text(user["first_name"], 100),
                "Kullanici adi: "
                + clean_text(user["username"] or "-", 64),
                "Bakiye: " + format_money(user["balance_cents"]),
                "Siparis: " + str(orders["total"]),
                "Kayit: " + user["created_at"],
            ]
        )
    )


@router.message(Command("totalusers"))
async def total_users_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    async with db_session() as db:
        cursor = await db.execute("SELECT COUNT(*) AS total FROM users")
        row = await cursor.fetchone()
    await message.answer("Toplam kullanici: " + str(row["total"]))


@router.message(Command("pendings"))
async def pending_orders_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    await send_pending_orders(message)


@router.message(Command("deposits"))
async def pending_deposits_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    await send_pending_deposits(message)


@router.message(Command("stats"))
async def stats_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    async with db_session() as db:
        cursor = await db.execute("SELECT COUNT(*) AS total FROM users")
        users = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*) AS total FROM orders WHERE status = 'pending'"
        )
        pending = await cursor.fetchone()
        cursor = await db.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM orders WHERE status = 'completed'
            """
        )
        sales = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT COUNT(*) AS total FROM services WHERE active = 1"
        )
        services = await cursor.fetchone()
    await message.answer(
        "\n".join(
            [
                "Kullanici: " + str(users["total"]),
                "Bekleyen siparis: " + str(pending["total"]),
                "Aktif hizmet: " + str(services["total"]),
                "Tamamlanan siparis tutari: "
                + format_money(sales["total"]),
            ]
        )
    )


@router.message(Command("admin"))
@router.message(F.text == "Admin Paneli")
async def admin_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    await message.answer(admin_help_text())
    await send_pending_orders(message)


@router.message()
async def fallback_handler(message: Message):
    await message.answer(
        "Komut taninmadi. /help yazarak yardim alabilirsiniz.",
        reply_markup=main_keyboard(
            message.from_user.id if message.from_user else 0
        ),
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    if not ADMIN_IDS:
        raise RuntimeError("Set ADMIN_IDS to one or more Telegram user IDs.")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    expiry_task = asyncio.create_task(expire_orders(bot))
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
