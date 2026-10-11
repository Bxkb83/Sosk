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
    ReplyKeyboardMarkup,
    KeyboardButton,
    User,
)
from bot_extensions import (
    init_feature_schema,
    get_language,
    order_rating_keyboard,
    router as extensions_router,
    user_is_banned,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "").strip()
BANK_TRANSFER_ENABLED = os.getenv("BANK_TRANSFER_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
STAR_TOPUP_TIERS_RAW = os.getenv("STAR_TOPUP_TIERS", "").strip()
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
JOIN_CHANNEL_URL = os.getenv("JOIN_CHANNEL_URL", "").strip()
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()
PROMO_CHANNEL_ID = os.getenv("PROMO_CHANNEL_ID", "").strip()
CANCEL_SECONDS = 6 * 60
DAILY_REWARD_CENTS = 100
MAX_AMOUNT_CENTS = 100_000_000
router = Router()
logger = logging.getLogger("service-order-bot")


def channel_join_url():
    if JOIN_CHANNEL_URL:
        return JOIN_CHANNEL_URL
    if FORCE_JOIN_CHANNEL.startswith("@"):
        return "https://t.me/" + FORCE_JOIN_CHANNEL[1:]
    return ""


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


class AdminDeliveryFlow(StatesGroup):
    note = State()


def parse_star_topup_tiers(raw=STAR_TOPUP_TIERS_RAW):
    """Parse administrator-defined "stars:TL" pairs without inventing a rate."""
    tiers = []
    if not raw:
        return tiers
    seen = set()
    for item in raw.split(","):
        pair = item.strip().split(":", 1)
        if len(pair) != 2 or not pair[0].strip().isdigit():
            raise ValueError(
                "STAR_TOPUP_TIERS must use stars:TL pairs, e.g. 100:50,250:125."
            )
        stars = int(pair[0].strip())
        if stars < 1 or stars > 100_000:
            raise ValueError("Each Stars amount must be between 1 and 100000.")
        try:
            credit_cents = parse_money(pair[1].strip())
        except ValueError as exc:
            raise ValueError(
                "Each STAR_TOPUP_TIERS credit must be a positive TL amount."
            ) from exc
        if stars in seen:
            raise ValueError("STAR_TOPUP_TIERS contains a duplicate Stars amount.")
        seen.add(stars)
        tiers.append((stars, credit_cents))
    return sorted(tiers)


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


try:
    WELCOME_BONUS_CENTS = parse_money(
        os.getenv("WELCOME_BONUS_TL", "1").strip()
    )
except ValueError as exc:
    raise RuntimeError("WELCOME_BONUS_TL must be a positive TL amount.") from exc


try:
    STAR_TOPUP_TIERS = parse_star_topup_tiers()
except ValueError as exc:
    raise RuntimeError(str(exc)) from exc


try:
    REFERRAL_BONUS_CENTS = parse_money(
        os.getenv("REFERRAL_BONUS_TL", "5").strip()
    )
except ValueError as exc:
    raise RuntimeError("REFERRAL_BONUS_TL must be a positive TL amount.") from exc


def clean_text(value, max_length=180):
    text = " ".join(str(value).strip().split())
    return text[:max_length]


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
        member = await bot.get_chat_member(
            chat_id=FORCE_JOIN_CHANNEL,
            user_id=user_id,
        )
    except TelegramAPIError:
        logger.exception("Could not verify channel membership")
        return None
    if member.status in ("creator", "administrator", "member"):
        return True
    return member.status == "restricted" and bool(member.is_member)


def join_channel_keyboard():
    url = channel_join_url()
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Kanala Katil",
                    url=url,
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Katilinca Kontrol Et",
                    callback_data="check_join",
                    style="success",
                )
            ],
        ]
    )


class JoinChannelMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is not None and not is_admin(user.id):
            if await user_is_banned(user.id):
                if isinstance(event, Message):
                    await event.answer(
                        "Hesabiniz bot yoneticisi tarafindan kapatildi."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        "Hesabiniz bot yoneticisi tarafindan kapatildi.",
                        show_alert=True,
                    )
                return None
            async with db_session() as db:
                await db.execute(
                    "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
                    (timestamp(), user.id),
                )
                await db.commit()
        if user is None or is_admin(user.id):
            if not FORCE_JOIN_CHANNEL:
                return await handler(event, data)
        if not FORCE_JOIN_CHANNEL:
            return await handler(event, data)
        if user is None or is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
        if isinstance(event, Message):
            await ensure_user(
                user,
                referrer_id=referral_id_from_start(event.text),
            )
        if isinstance(event, CallbackQuery) and event.data == "check_join":
            return await handler(event, data)

        joined = await check_channel_membership(event.bot, user.id)
        if joined is True:
            return await handler(event, data)

        if joined is None:
            text = (
                "Kanal uyeligi su anda kontrol edilemiyor. "
                "Botun katilim kanalinda yonetici oldugunu kontrol edin."
            )
            keyboard = None
        else:
            text = "Botu kullanmak icin once duyuru kanalina katilin."
            keyboard = join_channel_keyboard()

        if isinstance(event, Message):
            await event.answer(text, reply_markup=keyboard)
        elif isinstance(event, CallbackQuery):
            if event.message:
                await event.message.answer(text, reply_markup=keyboard)
            await event.answer("Once kanala katilin.", show_alert=True)
        return None


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if (
            not isinstance(event, Message)
            or event.from_user is None
            or is_admin(event.from_user.id)
            or event.successful_payment
            or (event.text or "").strip() == "/cancel"
        ):
            return await handler(event, data)
        state = data.get("state")
        if state is not None:
            try:
                if await state.get_state():
                    return await handler(event, data)
            except Exception:
                pass
        user_id = event.from_user.id
        current = now_utc()
        current_text = current.isoformat(timespec="seconds")
        db = await open_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT last_message_at FROM message_rate_limits WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row:
                previous = datetime.fromisoformat(row["last_message_at"])
                remaining = 30 - int((current - previous).total_seconds())
                if remaining > 0:
                    await db.rollback()
                    await event.answer(
                        "Guvenlik siniri: yeni mesaj icin "
                        + str(remaining)
                        + " saniye bekleyin."
                    )
                    return None
            await db.execute(
                """
                INSERT INTO message_rate_limits (user_id, last_message_at)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_message_at = excluded.last_message_at
                """,
                (user_id, current_text),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        return await handler(event, data)


router.message.outer_middleware(JoinChannelMiddleware())
router.callback_query.outer_middleware(JoinChannelMiddleware())
router.message.outer_middleware(RateLimitMiddleware())
router.include_router(extensions_router)


async def log_event(bot, text):
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(LOG_CHANNEL_ID, "Bot kaydi:\n" + text)
    except TelegramAPIError:
        logger.exception("Could not write to the configured log channel")


async def validate_channels(bot):
    me = await bot.get_me()
    if FORCE_JOIN_CHANNEL:
        if not channel_join_url():
            raise RuntimeError(
                "A private FORCE_JOIN_CHANNEL needs JOIN_CHANNEL_URL."
            )
        try:
            membership = await bot.get_chat_member(
                chat_id=FORCE_JOIN_CHANNEL,
                user_id=me.id,
            )
        except TelegramAPIError as exc:
            raise RuntimeError(
                "Bot cannot access FORCE_JOIN_CHANNEL. Add it to the "
                "channel as an administrator and check the channel ID."
            ) from exc
        if membership.status not in ("administrator", "creator"):
            raise RuntimeError(
                "Bot must be an administrator in FORCE_JOIN_CHANNEL "
                "to verify member status."
            )
    if LOG_CHANNEL_ID:
        try:
            await bot.get_chat(LOG_CHANNEL_ID)
            membership = await bot.get_chat_member(
                chat_id=LOG_CHANNEL_ID,
                user_id=me.id,
            )
        except TelegramAPIError as exc:
            raise RuntimeError(
                "Bot cannot access LOG_CHANNEL_ID. Check the channel ID "
                "and add the bot as an administrator."
            ) from exc
        if membership.status not in ("administrator", "creator"):
            raise RuntimeError(
                "Bot must be an administrator in LOG_CHANNEL_ID."
            )
    if PROMO_CHANNEL_ID:
        try:
            await bot.get_chat(PROMO_CHANNEL_ID)
            membership = await bot.get_chat_member(
                chat_id=PROMO_CHANNEL_ID,
                user_id=me.id,
            )
        except TelegramAPIError as exc:
            raise RuntimeError(
                "Bot cannot access PROMO_CHANNEL_ID. Check the channel ID "
                "and add the bot as an administrator."
            ) from exc
        if membership.status not in ("administrator", "creator"):
            raise RuntimeError(
                "Bot must be an administrator in PROMO_CHANNEL_ID."
            )
    logger.info("Telegram bot connected as @%s", me.username or me.id)


def main_keyboard(user_id, language="tr"):
    english = language == "en"
    rows = [
        [
            KeyboardButton(text="Services" if english else "Hizmetler", style="primary"),
            KeyboardButton(text="Balance" if english else "Bakiye", style="success"),
        ],
        [
            KeyboardButton(text="Orders" if english else "Siparisler", style="primary"),
            KeyboardButton(text="Add balance" if english else "Bakiye Yukle", style="success"),
        ],
        [
            KeyboardButton(text="Promo" if english else "Promosyon", style="success"),
            KeyboardButton(text="Referrals" if english else "Referans", style="primary"),
        ],
        [
            KeyboardButton(
                text="Help" if english else "Yardim",
                style="primary",
            ),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="Admin Paneli", style="primary")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def service_keyboard(services):
    rows = []
    for service in services:
        label = clean_text(service["name"], 45)
        if service["vip_only"]:
            label = "💎 " + label
        if (
            service["sale_percent"]
            and service["sale_until"]
            and service["sale_until"] > timestamp()
        ):
            label = "🔥 " + label
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data="s:" + str(service["service_id"]),
                    style="primary",
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
                    style="success",
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
                    style="danger",
                )
            ]
        ]
    )


def admin_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Teslimat notu gonder",
                    callback_data="o:deliver:" + str(order_id),
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Iptal Et ve Iade",
                    callback_data="o:refund:" + str(order_id),
                    style="danger",
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
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Reddet",
                    callback_data="d:reject:" + str(deposit_id),
                    style="danger",
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


async def ensure_db_column(db, table, column, declaration):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {row["name"] for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


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
                last_seen_at TEXT,
                is_banned INTEGER NOT NULL DEFAULT 0,
                vip_until TEXT,
                language TEXT NOT NULL DEFAULT 'tr',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS services (
                service_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                active INTEGER NOT NULL DEFAULT 1,
                vip_only INTEGER NOT NULL DEFAULT 0,
                sale_percent INTEGER NOT NULL DEFAULT 0,
                sale_until TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                service_id INTEGER NOT NULL REFERENCES services(service_id),
                service_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                base_price_cents INTEGER NOT NULL DEFAULT 0,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                discount_label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
           s"),
        ],
        [
            KeyboardButton(text="Promosyon", style="success"),
            KeyboardButton(text="Referans", style="primary"),
        ],
        [
            KeyboardButton(text="Yardim"),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="Admin Paneli", style="primary")])
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
                    style="success",
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
                    style="danger",
                )
            ]
        ]
    )


def admin_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Numara ve Kod Gir",
                    callback_data="n:" + str(order_id),
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Iptal Et ve Iade",
                    callback_data="o:refund:" + str(order_id),
                    style="danger",
                ),
            ],
        ]
    )


def deposit_keyboard(deposit_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Onayla",
                    callback_data="d:approve:" + str(deposit_id),
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Reddet",
                    callback_data="d:reject:" + str(deposit_id),
                    style="danger",
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
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL REFERENCES users(user_id),
                referred_user_id INTEGER NOT NULL UNIQUE REFERENCES users(user_id),
                reward_cents INTEGER NOT NULL DEFAULT 0,
                rewarded_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS star_payment_intents (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                stars_amount INTEGER NOT NULL CHECK(stars_amount > 0),
                credit_cents INTEGER NOT NULL CHECK(credit_cents > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_charge_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiry
                ON orders(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_orders_user
                ON orders(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(user_id, transaction_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_status
                ON deposit_requests(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                ON referrals(referrer_id, created_at);
            """
        )
        await db.commit()


async def ensure_user(user, referrer_id=None):
    async with db_session() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user.id,),
        )
        is_new = await cursor.fetchone() is None
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
        if (
            is_new
            and isinstance(referrer_id, int)
            and referrer_id > 0
            and referrer_id != user.id
        ):
            cursor = await db.execute(
                "SELECT 1 FROM users WHERE user_id = ?",
                (referrer_id,),
            )
            if await cursor.fetchone():
                await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (
                        referrer_id, referred_user_id, reward_cents,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        referrer_id,
                        user.id,
                        REFERRAL_BONUS_CENTS,
                        timestamp(),
                    ),
                )
        await db.commit()


async def resolve_user_id(identifier):
    value = str(identifier or "").strip()
    if value.isdigit() and int(value) > 0:
        user_id = int(value)
        async with db_session() as db:
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        return int(row["user_id"]) if row else None

    username = value[1:] if value.startswith("@") else value
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    async with db_session() as db:
async with db_session() as db:
    cursor = await db.execute(
        "SELECT * FROM orders WHERE order_id = ?",
        (order_id,),
    )
    order = await cursor.fetchone()
        ],
        [
            KeyboardButton(text="Promosyon", style="success"),
            KeyboardButton(text="Referans", style="primary"),
        ],
        [
            KeyboardButton(text="Yardim"),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="Admin Paneli", style="primary")])
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
                    style="success",
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
                    style="danger",
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
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Iptal Et ve Iade",
                    callback_data="o:refund:" + str(order_id),
                    style="danger",
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
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Reddet",
                    callback_data="d:reject:" + str(deposit_id),
                    style="danger",
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
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL REFERENCES users(user_id),
                referred_user_id INTEGER NOT NULL UNIQUE REFERENCES users(user_id),
                reward_cents INTEGER NOT NULL DEFAULT 0,
                rewarded_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS star_payment_intents (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                stars_amount INTEGER NOT NULL CHECK(stars_amount > 0),
                credit_cents INTEGER NOT NULL CHECK(credit_cents > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_charge_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiry
                ON orders(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_orders_user
                ON orders(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(user_id, transaction_id);
            CREATE INDEX IF NOT EXISTS idx_deposits_status
                ON deposit_requests(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                ON referrals(referrer_id, created_at);
            """
        )
        await db.commit()


async def ensure_user(user, referrer_id=None):
    async with db_session() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user.id,),
        )
        is_new = await cursor.fetchone() is None
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
        if (
            is_new
            and isinstance(referrer_id, int)
            and referrer_id > 0
            and referrer_id != user.id
        ):
            cursor = await db.execute(
                "SELECT 1 FROM users WHERE user_id = ?",
                (referrer_id,),
            )
            if await cursor.fetchone():
                await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (
                        referrer_id, referred_user_id, reward_cents,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        referrer_id,
                        user.id,
                        REFERRAL_BONUS_CENTS,
                        timestamp(),
                    ),
                )
        await db.commit()


async def resolve_user_id(identifier):
    value = str(identifier or "").strip()
    if value.isdigit() and int(value) > 0:
        user_id = int(value)
        async with db_session() as db:
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        return int(row["user_id"]) if row else None

    username = value[1:] if value.startswith("@") else value
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT user_id FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        )
        row = await cursor.fetchone()
    return int(row["user_id"]) if row else None


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
        result_order = dict(order)
        cursor = await db.execute(
            """
            SELECT referrer_id, reward_cents
            FROM referrals
            WHERE referred_user_id = ? AND rewarded_at IS NULL
            """,
            (order["user_id"],),
        )
        referral = await cursor.fetchone()
        if referral:
            cursor = await db.execute(
                """
                UPDATE referrals SET rewarded_at = ?
                WHERE referred_user_id = ? AND rewarded_at IS NULL
                """,
                (timestamp(), order["user_id"]),
            )
            if cursor.rowcount == 1:
                reward_cents = int(referral["reward_cents"])
                await db.execute(
                    """
                    UPDATE users SET balance_cents = balance_cents + ?
                    WHERE user_id = ?
                    """,
                    (reward_cents, referral["referrer_id"]),
                )
                await add_transaction(
                    db,
                    referral["referrer_id"],
                    reward_cents,
                    "referral_reward",
                    "Davet edilen ilk siparisini tamamladi",
                )
                result_order["referrer_id"] = int(referral["referrer_id"])
                result_order["referral_bonus_cents"] = reward_cents
        await db.commit()
        return "completed", result_order
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
            "/addbalance @kullanici_adi_veya_id tutar",
            "/removebalance @kullanici_adi_veya_id tutar",
            "/promoadd - adim adim promo olustur",
            "/promodel - kodu sorarak promo kapat",
            "/userinfo @kullanici_adi_veya_id",
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
    await ensure_user(
        message.from_user,
        referrer_id=referral_id_from_start(message.text),
    )
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


@router.callback_query(F.data == "check_join")
async def check_join_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    joined = await check_channel_membership(
        callback.bot,
        callback.from_user.id,
    )
    if joined is True:
        await ensure_user(callback.from_user)
        await callback.answer("Kanal uyeligi dogrulandi.")
        if callback.message:
            await callback.message.answer(
                "Tesekkurler. Botu kullanabilirsiniz.",
                reply_markup=main_keyboard(callback.from_user.id),
            )
    elif joined is False:
        await callback.answer(
            "Kanal uyeliginiz bulunamadi.",
            show_alert=True,
        )
    else:
        await callback.answer(
            "Kanal kontrol edilemiyor. Lutfen daha sonra tekrar deneyin.",
            show_alert=True,
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
        "/referral - Referans linki ve davet sayisi",
        "/stars - Stars ile bakiye yukle",
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


@router.message(Command("referral"))
@router.message(F.text == "Referans")
async def referral_handler(message: Message):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN rewarded_at IS NOT NULL
                                     THEN reward_cents ELSE 0 END), 0) AS earned
            FROM referrals
            WHERE referrer_id = ?
            """,
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
    me = await message.bot.get_me()
    if not me.username:
        await message.answer("Referans linki su anda olusturulamiyor.")
        return
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    await message.answer(
        "\n".join(
            [
                "Referans linkiniz:",
                link,
                "",
                "Davet edilen: " + str(row["total"]),
                "Kazanilan odul: " + format_money(row["earned"]),
                "Odul, davet ettiginiz kisi ilk siparisini tamamlayinca verilir.",
            ]
        )
    )


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
    await log_event(
        callback.bot,
        "Yeni siparis #"
        + str(order_id)
        + " | kullanici "
        + str(callback.from_user.id)
        + " | "
        + format_money(order["amount_cents"]),
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
        await log_event(
            callback.bot,
            "Kullanici siparisi iptal etti ve iade aldi: #"
            + str(order_id)
            + " | kullanici "
            + str(callback.from_user.id),
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
        await log_event(
            callback.bot,
            "Admin siparisi iade etti: #"
            + str(order_id)
            + " | admin "
            + str(callback.from_user.id),
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
        if order.get("referrer_id") and order.get("referral_bonus_cents"):
            await notify_user(
                callback.bot,
                order["referrer_id"],
                "Referans odulunuz eklendi: "
                + format_money(order["referral_bonus_cents"])
                + ". Davet ettiginiz kisi ilk siparisini tamamladi.",
            )
        await log_event(
            callback.bot,
            "Siparis tamamlandi: #"
            + str(order_id)
            + " | admin "
            + str(callback.from_user.id),
        )
        await callback.answer("Siparis tamamlandi.")
        return
    await callback.answer("Islem gecersiz.", show_alert=True)


def topup_keyboard():
    rows = [
        [
            InlineKeyboardButton(
                text=f"{stars} ⭐ → {format_money(credit_cents)}",
                callback_data="star:" + str(stars),
                style="success",
            )
        ]
        for stars, credit_cents in STAR_TOPUP_TIERS
    ]
    if BANK_TRANSFER_ENABLED and PAYMENT_INSTRUCTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Banka havalesi (fiziksel/offline)",
                    callback_data="bankdeposit",
                    style="primary",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_topup_methods(message):
    if message.from_user is None:
        return
    await ensure_user(message.from_user)
    keyboard = topup_keyboard()
    if keyboard is None:
        await message.answer(
            "Bakiye yukleme henuz ayarlanmamis. "
            "Yonetici Railway'de STAR_TOPUP_TIERS degerini tanimlamali."
        )
        return
    text = (
        "Bakiye yukleme yontemini secin.\n"
        "Dijital hizmet odemeleri Telegram Stars ile yapilir."
    )
    if BANK_TRANSFER_ENABLED and PAYMENT_INSTRUCTIONS:
        text += (
            "\nBanka havalesini yalnizca Telegram disinda teslim edilen "
            "fiziksel/offline urun ve hizmetler icin kullanin."
        )
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("stars"))
@router.message(F.text == "Bakiye Yukle")
async def deposit_start(message: Message):
    await show_topup_methods(message)


@router.callback_query(F.data == "bankdeposit")
async def bank_deposit_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user is None:
        await callback.answer()
        return
    if not BANK_TRANSFER_ENABLED or not PAYMENT_INSTRUCTIONS:
        await callback.answer("Banka havalesi etkin degil.", show_alert=True)
        return
    await ensure_user(callback.from_user)
    await state.set_state(DepositFlow.amount)
    if callback.message:
        await callback.message.answer(
            "Bu havale secenegini yalnizca Telegram disinda teslim edilen "
            "fiziksel/offline urun ve hizmetler icin kullanin.\n\n"
            "Yuklemek istediginiz tutari TL olarak yazin.\n\n"
            + PAYMENT_INSTRUCTIONS
        )
    await callback.answer()


@router.callback_query(F.data.startswith("star:"))
async def create_star_invoice_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    try:
        stars = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Odeme secenegi gecersiz.", show_alert=True)
        return
    tier = next(
        (
            (tier_stars, credit)
            for tier_stars, credit in STAR_TOPUP_TIERS
            if tier_stars == stars
        ),
        None,
    )
    if tier is None:
        await callback.answer("Bu Stars paketi aktif degil.", show_alert=True)
        return
    await ensure_user(callback.from_user)
    credit_cents = tier[1]
    payload = "topup:" + uuid4().hex
    async with db_session() as db:
        await db.execute(
            """
            INSERT INTO star_payment_intents (
                payload, user_id, stars_amount, credit_cents,
                status, created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                payload,
                callback.from_user.id,
                stars,
                credit_cents,
                timestamp(),
            ),
        )
        await db.commit()
    try:
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Telegram Stars bakiye",
            description=(
                f"{stars} Stars karsiliginda "
                f"{format_money(credit_cents)} bakiye"
            ),
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=format_money(credit_cents) + " bakiye",
                    amount=stars,
                )
            ],
        )
    except TelegramAPIError:
        async with db_session() as db:
            await db.execute(
                """
                UPDATE star_payment_intents SET status = 'failed'
                WHERE payload = ? AND status = 'pending'
                """,
                (payload,),
            )
            await db.commit()
        logger.exception("Could not create Telegram Stars invoice")
        await callback.answer(
            "Stars odemesi baslatilamadi. Daha sonra yeniden deneyin.",
            show_alert=True,
        )
        return
    await callback.answer("Telegram Stars odeme ekrani gonderildi.")


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT user_id, stars_amount, status
            FROM star_payment_intents
            WHERE payload = ?
            """,
            (query.invoice_payload,),
        )
        intent = await cursor.fetchone()
    valid = (
        intent is not None
        and intent["status"] == "pending"
        and int(intent["user_id"]) == query.from_user.id
        and query.currency == "XTR"
        and int(intent["stars_amount"]) == int(query.total_amount)
    )
    await query.answer(
        ok=valid,
        error_message=None
        if valid
        else "Bu odeme faturasi artik gecerli degil. Yeni bir fatura isteyin.",
    )


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    if message.from_user is None or message.successful_payment is None:
        return
    payment = message.successful_payment
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            """
            SELECT user_id, stars_amount, credit_cents, status
            FROM star_payment_intents
            WHERE payload = ?
            """,
            (payment.invoice_payload,),
        )
        intent = await cursor.fetchone()
        if (
            intent is not None
            and intent["status"] == "paid"
            and int(intent["user_id"]) == message.from_user.id
            and payment.currency == "XTR"
            and int(intent["stars_amount"]) == int(payment.total_amount)
        ):
            await db.rollback()
            return
        if (
            intent is None
            or intent["status"] != "pending"
            or int(intent["user_id"]) != message.from_user.id
            or payment.currency != "XTR"
            or int(intent["stars_amount"]) != int(payment.total_amount)
        ):
            await db.rollback()
            await notify_admins(
                message.bot,
                "Dogrulanamayan Telegram Stars odemesi alindi.\n"
                + "Kullanici: "
                + str(message.from_user.id)
                + "\nPayload: "
                + clean_text(payment.invoice_payload, 100),
            )
            await message.answer(
                "Odeme dogrulanamadi. Lutfen yoneticiyle iletisime gecin."
            )
            return
        credit_cents = int(intent["credit_cents"])
        cursor = await db.execute(
            """
            UPDATE star_payment_intents
            SET status = 'paid', telegram_charge_id = ?, paid_at = ?
            WHERE payload = ? AND status = 'pending'
            """,
            (
                payment.telegram_payment_charge_id,
                timestamp(),
                payment.invoice_payload,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            await message.answer(
                "Bu odeme daha once islenmis. Bakiye hareketlerinizi kontrol edin."
            )
            return
        await db.execute(
            """
            UPDATE users SET balance_cents = balance_cents + ?
            WHERE user_id = ?
            """,
            (credit_cents, message.from_user.id),
        )
        await add_transaction(
            db,
            message.from_user.id,
            credit_cents,
            "telegram_stars",
            f"Stars odemesi: {payment.total_amount} XTR",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    await message.answer(
        "Odeme alindi. "
        + format_money(credit_cents)
        + " bakiyenize eklendi."
    )
    await log_event(
        message.bot,
        "Stars odemesi alindi: kullanici "
        + str(message.from_user.id)
        + " | "
        + str(payment.total_amount)
        + " XTR | "
        + format_money(credit_cents),
    )


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
    await log_event(
        message.bot,
        "Yeni bakiye talebi #"
        + str(deposit_id)
        + " | kullanici "
        + str(message.from_user.id)
        + " | "
        + format_money(amount),
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
        await log_event(
            callback.bot,
            "Bakiye talebi onaylandi: #"
            + str(deposit_id)
            + " | admin "
            + str(callback.from_user.id)
            + " | "
            + format_money(deposit["amount_cents"]),
        )
        await callback.answer("Bakiye eklendi.")
    else:
        await notify_user(
            callback.bot,
            deposit["user_id"],
            "Bakiye talebiniz reddedildi.",
        )
        await log_event(
            callback.bot,
            "Bakiye talebi reddedildi: #"
            + str(deposit_id)
            + " | admin "
            + str(callback.from_user.id),
        )
        await callback.answer("Talep reddedildi.")


@router.message(Command("addbalance"))
async def add_balance_handler(message: Message, state: FSMContext):
    await start_admin_balance_flow(message, state, "add")


async def start_admin_balance_flow(message, state, operation):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await state.clear()
        await state.update_data(balance_operation=operation)
        await state.set_state(AdminBalanceFlow.target)
        await message.answer("Kullanici adini (@kullanici) veya Telegram ID'sini yazin.")
        return
    if len(parts) != 3:
        await message.answer(
            "Tam kullanim: /"
            + ("addbalance" if operation == "add" else "removebalance")
            + " @kullanici_adi_veya_id tutar\n"
            "Adim adim islem icin komutu tek basina gonderin."
        )
        return
    user_id = await resolve_user_id(parts[1])
    if user_id is None:
        await message.answer(
            "Kullanici bulunamadi. Kullanici botu en az bir kez baslatmis olmali."
        )
        return
    try:
        amount = parse_money(parts[2])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await apply_admin_balance_change(message, user_id, amount, operation)


@router.message(Command("removebalance"))
async def remove_balance_handler(message: Message, state: FSMContext):
    await start_admin_balance_flow(message, state, "remove")


@router.message(AdminBalanceFlow.target)
async def admin_balance_target_input(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    user_id = await resolve_user_id(message.text or "")
    if user_id is None:
        await message.answer(
            "Kullanici bulunamadi. @kullanici adi veya Telegram ID'si yazin."
        )
        return
    await state.update_data(balance_user_id=user_id)
    await state.set_state(AdminBalanceFlow.amount)
    await message.answer("Eklenecek/cikarilacak tutari TL olarak yazin.")


@router.message(AdminBalanceFlow.amount)
async def admin_balance_amount_input(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    try:
        amount = parse_money(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    await state.clear()
    await apply_admin_balance_change(
        message,
        int(data["balance_user_id"]),
        amount,
        data["balance_operation"],
    )


async def apply_admin_balance_change(message, user_id, amount, operation):
    is_add = operation == "add"
    db = await open_db()
    await db.execute("BEGIN IMMEDIATE")
    try:
        if is_add:
            cursor = await db.execute(
                """
                UPDATE users SET balance_cents = balance_cents + ?
                WHERE user_id = ?
                """,
                (amount, user_id),
            )
        else:
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
                "Kullanici bulunamadi."
                if is_add
                else "Kullanici yok veya bakiyesi yetersiz."
            )
            return
        await add_transaction(
            db,
            user_id,
            amount if is_add else -amount,
            "admin_credit" if is_add else "admin_debit",
            "Admin bakiye ekledi" if is_add else "Admin bakiye cikardi",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    await message.answer(
        format_money(amount)
        + (" kullaniciya eklendi." if is_add else " kullanicidan dusuldu.")
    )
    await notify_user(
        message.bot,
        user_id,
        (
            "Hesabiniza "
            + format_money(amount)
            + " eklendi. Yeni bakiyenizi /balance ile gorebilirsiniz."
            if is_add
            else "Bakiyenizden " + format_money(amount) + " dusuldu."
        ),
    )
    await log_event(
        message.bot,
        ("Admin bakiye ekledi: kullanici " if is_add else "Admin bakiye dusurdu: kullanici ")
        + str(user_id)
        + " | admin "
        + str(message.from_user.id)
        + " | "
        + format_money(amount),
    )


@router.message(Command("addservice"))
async def add_service_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    args = (message.text or "").partition(" ")[2]
    if not args.strip():
        await state.clear()
        await state.set_state(AdminServiceFlow.name)
        await message.answer("Yeni hizmetin adini yazin.")
        return
    parts = args.split("|", 2)
    if len(parts) < 2:
        await message.answer(
            "Tam kullanim: /addservice isim|fiyat|aciklama\n"
            "Sihirbaz icin yalnizca /addservice yazin."
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
    await save_service(message, name, price, description)


async def save_service(message, name, price, description):
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
    await log_event(
        message.bot,
        "Hizmet eklendi: #"
        + str(service_id)
        + " | admin "
        + str(message.from_user.id if message.from_user else 0)
        + " | "
        + clean_text(name, 60),
    )


@router.message(AdminServiceFlow.name)
async def service_wizard_name(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    name = clean_text(message.text or "", 60)
    if len(name) < 2:
        await message.answer("Hizmet adi en az iki karakter olmali.")
        return
    if contains_disallowed_service_text(name):
        await message.answer("Bu hizmet adi desteklenmiyor.")
        return
    await state.update_data(service_name=name)
    await state.set_state(AdminServiceFlow.price)
    await message.answer("Hizmet fiyatini TL olarak yazin.")


@router.message(AdminServiceFlow.price)
async def service_wizard_price(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    try:
        price = parse_money(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(service_price_cents=price)
    await state.set_state(AdminServiceFlow.description)
    await message.answer("Kisa aciklamayi yazin. Aciklama istemiyorsaniz - gonderin.")


@router.message(AdminServiceFlow.description)
async def service_wizard_description(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    description = "" if (message.text or "").strip() == "-" else clean_text(
        message.text or "",
        180,
    )
    data = await state.get_data()
    name = data["service_name"]
    if contains_disallowed_service_text(name + " " + description):
        await message.answer("Bu hizmet aciklamasi desteklenmiyor.")
        return
    await state.clear()
    await save_service(
        message,
        name,
        int(data["service_price_cents"]),
        description,
    )


@router.message(Command("delservice"))
async def delete_service_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await state.clear()
        await state.set_state(AdminDeleteServiceFlow.service_id)
        await message.answer("Kapatmak istediginiz hizmetin ID'sini yazin.")
        return
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /delservice hizmet_id")
        return
    await deactivate_service(message, int(parts[1]))


async def deactivate_service(message, service_id):
    async with db_session() as db:
        cursor = await db.execute(
            """
            UPDATE services SET active = 0
            WHERE service_id = ? AND active = 1
            """,
            (service_id,),
        )
        await db.commit()
    if cursor.rowcount == 1:
        await message.answer("Hizmet kapatildi.")
        await log_event(
            message.bot,
            "Hizmet kapatildi: #"
            + str(service_id)
            + " | admin "
            + str(message.from_user.id if message.from_user else 0),
        )
    else:
        await message.answer("Aktif hizmet bulunamadi.")


@router.message(AdminDeleteServiceFlow.service_id)
async def delete_service_id_input(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    value = (message.text or "").strip()
    if not value.isdigit() or int(value) < 1:
        await message.answer("Pozitif bir hizmet ID'si yazin.")
        return
    await state.clear()
    await deactivate_service(message, int(value))


async def create_promo_record(code, bonus_cents, max_uses, duration_minutes):
    expires_at = (
        now_utc() + timedelta(minutes=duration_minutes)
    ).isoformat(timespec="seconds")
    async with db_session() as db:
        await db.execute(
            """
            INSERT INTO promos (
                code, bonus_cents, max_uses, expires_at,
                active, created_at
            )
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (
                code,
                bonus_cents,
                max_uses,
                expires_at,
                timestamp(),
            ),
        )
        await db.commit()


async def finish_promo_creation(
    message,
    code,
    bonus_cents,
    max_uses,
    duration_minutes,
):
    try:
        await create_promo_record(
            code,
            bonus_cents,
            max_uses,
            duration_minutes,
        )
    except aiosqlite.IntegrityError:
        await message.answer("Bu promo kodu zaten var.")
        return
    await message.answer(
        "Promo olusturuldu:\n"
        + "Kod: "
        + code
        + "\nBonus: "
        + format_money(bonus_cents)
        + "\nKullanim: "
        + str(max_uses)
        + "\nSure: "
        + str(duration_minutes)
        + " dakika"
    )
    await log_event(
        message.bot,
        "Promo olusturuldu: "
        + code
        + " | bonus "
        + format_money(bonus_cents)
        + " | kullanim "
        + str(max_uses)
        + " | admin "
        + str(message.from_user.id if message.from_user else 0),
    )


@router.message(Command("promoadd"))
async def add_promo_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await state.clear()
        await state.set_state(AdminPromoFlow.code)
        await message.answer("Promo kodunu yazin (3-32 harf, sayi, _ veya -).")
        return
    if len(parts) != 5:
        await message.answer(
            "Tam komut: /promoadd KOD bonus kullanim dakika\n"
            "Sihirbaz icin yalnizca /promoadd yazin."
        )
        return
    code = parts[1].upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer(
            "Kod 3-32 karakter olmali; harf, sayi, _ veya - kullanin."
        )
        return
    if not parts[3].isdigit() or not parts[4].isdigit():
        await message.answer("Kullanim ve sure tam sayi olmali.")
        return
    try:
        bonus = parse_money(parts[2])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    max_uses = int(parts[3])
    duration_minutes = int(parts[4])
    if max_uses < 1 or max_uses > 100_000:
        await message.answer("Kullanim siniri 1-100000 araliginda olmali.")
        return
    if duration_minutes < 1 or duration_minutes > 525_600:
        await message.answer("Sure 1-525600 dakika araliginda olmali.")
        return
    await finish_promo_creation(
        message,
        code,
        bonus,
        max_uses,
        duration_minutes,
    )


@router.message(AdminPromoFlow.code)
async def promo_wizard_code(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    code = clean_text(message.text or "", 32).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer(
            "Kod 3-32 karakter olmali; harf, sayi, _ veya - kullanin."
        )
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminPromoFlow.bonus)
    await message.answer("Kac TL bonus verecek? Ornek: 30 veya 30,50")


@router.message(AdminPromoFlow.bonus)
async def promo_wizard_bonus(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    try:
        bonus = parse_money(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(promo_bonus_cents=bonus)
    await state.set_state(AdminPromoFlow.uses)
    await message.answer("Bu promo kodu toplam kac kez kullanilabilsin?")


@router.message(AdminPromoFlow.uses)
async def promo_wizard_uses(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 100_000:
        await message.answer("Kullanim siniri 1-100000 araliginda tam sayi olmali.")
        return
    await state.update_data(promo_max_uses=int(value))
    await state.set_state(AdminPromoFlow.duration_minutes)
    await message.answer("Promo kac dakika aktif kalsin?")


@router.message(AdminPromoFlow.duration_minutes)
async def promo_wizard_duration(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    value = (message.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 525_600:
        await message.answer("Sure 1-525600 dakika araliginda tam sayi olmali.")
        return
    data = await state.get_data()
    await state.clear()
    await finish_promo_creation(
        message,
        data["promo_code"],
        int(data["promo_bonus_cents"]),
        int(data["promo_max_uses"]),
        int(value),
    )


async def deactivate_promo(message, code):
    async with db_session() as db:
        cursor = await db.execute(
            "UPDATE promos SET active = 0 WHERE code = ? AND active = 1",
            (code.upper(),),
        )
        await db.commit()
    if cursor.rowcount == 1:
        await message.answer("Promo kapatildi.")
        await log_event(
            message.bot,
            "Promo kapatildi: "
            + code.upper()
            + " | admin "
            + str(message.from_user.id if message.from_user else 0),
        )
    else:
        await message.answer("Aktif promo bulunamadi.")


@router.message(Command("promodel"))
async def delete_promo_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await state.clear()
        await state.set_state(AdminPromoDeleteFlow.code)
        await message.answer("Kapatmak istediginiz promo kodunu yazin.")
        return
    if len(parts) != 2:
        await message.answer(
            "Kod sorulsun istiyorsaniz /promodel yazin; "
            "dogrudan kapatmak icin /promodel KOD kullanin."
        )
        return
    await deactivate_promo(message, parts[1])


@router.message(AdminPromoDeleteFlow.code)
async def promo_delete_code(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Yetkiniz yok.")
        return
    code = clean_text(message.text or "", 32).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Gecerli bir promo kodu yazin.")
        return
    await state.clear()
    await deactivate_promo(message, code)


@router.message(Command("userinfo"))
async def user_info_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Kullanim: /userinfo @kullanici_adi_veya_id")
        return
    user_id = await resolve_user_id(parts[1])
    if user_id is None:
        await message.answer("Kullanici bulunamadi.")
        return
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, first_name, balance_cents, created_at
            FROM users WHERE user_id = ?
            """,
            (user_id,),
        )
        user = await cursor.fetchone()
        if user:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS total FROM orders WHERE user_id = ?
                """,
                (user_id,),
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
        raise RuntimeError(
            "BOT_TOKEN is missing. Set the token as a Railway Variable; "
            "do not put it in the source code."
        )
    if not ADMIN_IDS:
        raise RuntimeError(
            "Set ADMIN_IDS in Railway Variables to one or more Telegram user IDs."
        )
    log_level = getattr(
        logging,
        os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        logging.INFO,
    )
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    expiry_task = None
    try:
        await validate_channels(bot)
        await init_db()
        expiry_task = asyncio.create_task(expire_orders(bot))
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        if expiry_task is not None:
            expiry_task.cancel()
            try:
                await expiry_task
            except asyncio.CancelledError:
                pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
