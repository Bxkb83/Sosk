"""Secondary Telegram bot features kept separate from the core order flows."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import aiosqlite
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
router = Router(name="store-extensions")
logger = logging.getLogger("bot_extensions")


def admin_ids():
    result = set()
    for item in os.getenv("ADMIN_IDS", "").split(","):
        item = item.strip()
        if item.isdigit() and int(item) > 0:
            result.add(int(item))
    return result


def is_admin(user_id):
    return user_id in admin_ids()


def now_utc():
    return datetime.now(timezone.utc)


def timestamp():
    return now_utc().isoformat(timespec="seconds")


def format_money(cents):
    whole, fraction = divmod(int(cents), 100)
    return str(whole) + "." + str(fraction).zfill(2) + " TL"


async def init_feature_schema():
    """Ek tablolar ve kolonlar oluşturur."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = {row["name"] for row in await cursor.fetchall()}
        except Exception:
            columns = set()
        if "is_banned" not in columns:
            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                logger.exception("Could not add is_banned column")
        if "language" not in columns:
            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'tr'"
                )
            except Exception:
                pass
        if "vip_until" not in columns:
            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN vip_until TEXT"
                )
            except Exception:
                pass
        if "last_seen_at" not in columns:
            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN last_seen_at TEXT"
                )
            except Exception:
                pass

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_user_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fraud_alerts (
                alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                kind TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_ratings (
                rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await db.commit()


async def get_language(user_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT language FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
    except Exception:
        return "tr"
    if row and row["language"]:
        return row["language"]
    return "tr"


async def user_is_banned(user_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT is_banned FROM users WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
    except Exception:
        return False
    return bool(row and row["is_banned"])


def order_rating_keyboard(order_id):
    buttons = []
    for i in range(1, 6):
        buttons.append(
            InlineKeyboardButton(
                text=str(i) + " \u2b50",
                callback_data="rate:" + str(order_id) + ":" + str(i),
                style="success",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


class TransferFlow(StatesGroup):
    confirmation = State()


class SupportFlow(StatesGroup):
    body = State()


class AdminBroadcastFlow(StatesGroup):
    body = State()
    confirmation = State()


def transfer_confirmation_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Transferi onayla",
                    callback_data="transfer:confirm",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="Vazgec",
                    callback_data="transfer:cancel",
                    style="danger",
                ),
            ]
        ]
    )


@router.message(Command("leaderboard"))
async def leaderboard_handler(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT u.user_id, u.username, u.first_name,
                   COUNT(r.referral_id) AS invite_count
            FROM users AS u
            JOIN referrals AS r ON r.referrer_id = u.user_id
            GROUP BY u.user_id
            ORDER BY invite_count DESC, u.user_id ASC
            LIMIT 10
            """
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Liderlik tablosu henuz bos.")
        return
    lines = ["\U0001F3C6 Davet liderleri - ilk 10"]
    for rank, row in enumerate(rows, start=1):
        name = row["username"] or row["first_name"] or str(row["user_id"])
        if row["username"]:
            name = "@" + name
        lines.append(str(rank) + ". " + name + " - " + str(row["invite_count"]) + " davet")
    await message.answer("\n".join(lines))


@router.message(Command("transfer"))
async def transfer_handler(message: Message, state: FSMContext):
    if message.from_user is None:
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer(
            "Kullanim: /transfer KULLANICI_ID TUTAR\n"
            "Ornek: /transfer 123456789 25"
        )
        return
    target = parts[1].lstrip("@")
    if not target.isdigit() or int(target) <= 0:
        await message.answer("Guvenlik icin alici sayisal Telegram ID olmali.")
        return
    try:
        parsed_amount = Decimal(parts[2].replace(",", ".")) * 100
        if parsed_amount != parsed_amount.to_integral_value():
            raise InvalidOperation
        amount = int(parsed_amount)
    except (InvalidOperation, ValueError, OverflowError):
        await message.answer("Gecerli bir TL tutari yazin.")
        return
    if amount < 1 or amount > 100_000_000:
        await message.answer("Tutar izin verilen aralikta degil.")
        return
    recipient_id = int(target)
    if recipient_id == message.from_user.id:
        await message.answer("Kendinize bakiye gonderemezsiniz.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, is_banned FROM users WHERE user_id = ?",
            (recipient_id,),
        )
        recipient = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT balance_cents, is_banned FROM users WHERE user_id = ?",
            (message.from_user.id,),
        )
        sender = await cursor.fetchone()
    if recipient is None or recipient["is_banned"]:
        await message.answer("Alici bulunamadi veya transfer alamiyor.")
        return
    if sender is None or int(sender["balance_cents"]) < amount:
        await message.answer("Bakiyeniz bu transfer icin yeterli degil.")
        return
    await state.clear()
    await state.update_data(
        transfer_target=recipient_id,
        transfer_amount=amount,
    )
    await state.set_state(TransferFlow.confirmation)
    await message.answer(
        format_money(amount) + " tutarini " + str(recipient_id) +
        " kullanicisina gondermek istiyor musunuz?",
        reply_markup=transfer_confirmation_keyboard(),
    )


@router.callback_query(F.data.startswith("transfer:"))
async def transfer_confirmation_handler(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user is None:
        await callback.answer()
        return
    action = (callback.data or "").split(":", maxsplit=1)[-1]
    if action == "cancel":
        await state.clear()
        await callback.answer("Transfer iptal edildi.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        return
    if action != "confirm" or await state.get_state() != TransferFlow.confirmation.state:
        await callback.answer("Transfer istegi gecersiz.", show_alert=True)
        return
    data = await state.get_data()
    target_id = int(data.get("transfer_target", 0))
    amount = int(data.get("transfer_amount", 0))
    sender_id = callback.from_user.id
    await state.clear()
    if target_id <= 0 or amount <= 0:
        await callback.answer("Transfer bilgisi gecersiz.", show_alert=True)
        return

    alert_reason = None
    db = await aiosqlite.connect(DB_PATH, timeout=10)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 10000")
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT balance_cents, is_banned FROM users WHERE user_id = ?",
            (sender_id,),
        )
        sender = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT is_banned FROM users WHERE user_id = ?",
            (target_id,),
        )
        recipient = await cursor.fetchone()
        if sender is None or sender["is_banned"] or recipient is None or recipient["is_banned"]:
            await db.rollback()
            await callback.answer("Transfer hesaplarindan biri uygun degil.", show_alert=True)
            return
        if int(sender["balance_cents"]) < amount:
            await db.rollback()
            await callback.answer("Bakiye transfer icin yetersiz.", show_alert=True)
            return
        await db.execute(
            "UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?",
            (amount, sender_id),
        )
        await db.execute(
            "UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?",
            (amount, target_id),
        )
        created_at = timestamp()
        await db.executemany(
            """
            INSERT INTO transactions (user_id, amount_cents, kind, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (sender_id, -amount, "transfer_out", "Transfer to " + str(target_id), created_at),
                (target_id, amount, "transfer_in", "Transfer from " + str(sender_id), created_at),
            ],
        )
        if amount >= 100_000:
            alert_reason = "Yuksek tutarli transfer: " + format_money(amount)
        else:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM transactions
                WHERE user_id = ? AND kind = 'transfer_out'
                  AND created_at >= ?
                """,
                (sender_id, (now_utc() - timedelta(hours=1)).isoformat(timespec="seconds")),
            )
            cnt = (await cursor.fetchone())[0]
            if int(cnt) >= 5:
                alert_reason = "Bir saat icinde bes veya daha fazla transfer"
        if alert_reason:
            await db.execute(
                """
                INSERT INTO fraud_alerts (user_id, amount_cents, kind, details, created_at)
                VALUES (?, ?, 'transfer', ?, ?)
                """,
                (sender_id, amount, alert_reason, created_at),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()

    try:
        await callback.bot.send_message(
            target_id,
            format_money(amount) + " bakiye aldiniz. Gonderen: " + str(sender_id),
        )
    except TelegramAPIError:
        pass
    await callback.answer("Transfer tamamlandi.")
    if callback.message:
        try:
            await callback.message.edit_text(
                format_money(amount) + " transfer edildi."
            )
        except TelegramAPIError:
            pass
    if alert_reason:
        for admin_id in admin_ids():
            try:
                await callback.bot.send_message(
                    admin_id,
                    "Supheli islem uyarisi\nKullanici: " + str(sender_id) +
                    "\nTutar: " + format_money(amount) + "\n" + alert_reason,
                )
            except TelegramAPIError:
                continue


@router.message(Command("ticket"))
async def ticket_handler(message: Message, state: FSMContext):
    if message.from_user is None:
        return
    body = (message.text or "").partition(" ")[2].strip()
    if not body:
        await state.set_state(SupportFlow.body)
        await message.answer("Destek talebinizi yazin. Iptal icin /cancel.")
        return
    await create_ticket(message, body)


async def create_ticket(message, body):
    if message.from_user is None:
        return
    body = " ".join(body.split())[:3000]
    if len(body) < 4:
        await message.answer("Destek talebiniz en az 4 karakter olmali.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO support_tickets (user_id, body, status, created_at)
            VALUES (?, ?, 'open', ?)
            """,
            (message.from_user.id, body, timestamp()),
        )
        ticket_id = cursor.lastrowid
        await db.commit()
    await message.answer("Destek talebiniz alindi. Talep numarasi: #" + str(ticket_id))
    for admin_id in admin_ids():
        try:
            await message.bot.send_message(
                admin_id,
                "Yeni destek talebi #" + str(ticket_id) +
                "\nKullanici: " + str(message.from_user.id) +
                "\n" + body[:1200],
            )
        except TelegramAPIError:
            continue


@router.message(SupportFlow.body)
async def ticket_body_handler(message: Message, state: FSMContext):
    await state.clear()
    await create_ticket(message, message.text or "")


@router.message(Command("tickets"))
async def tickets_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ticket_id, user_id, body, created_at
            FROM support_tickets WHERE status = 'open'
            ORDER BY ticket_id LIMIT 20
            """
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("Acik destek talebi yok.")
        return
    for row in rows:
        await message.answer(
            "Talep #" + str(row["ticket_id"]) +
            " | Kullanici " + str(row["user_id"]) +
            "\n" + row["created_at"] + "\n" + row["body"][:2500]
        )


@router.message(Command("ticketreply"))
async def ticket_reply_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Kullanim: /ticketreply TALEP_ID YANIT")
        return
    ticket_id = int(parts[1])
    body = " ".join(parts[2].split())[:3000]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id FROM support_tickets WHERE ticket_id = ? AND status = 'open'",
            (ticket_id,),
        )
        ticket = await cursor.fetchone()
    if ticket is None:
        await message.answer("Acik destek talebi bulunamadi.")
        return
    try:
        await message.bot.send_message(
            int(ticket["user_id"]),
            "Destek ekibinden yanit (talep #" + str(ticket_id) + "):\n" + body,
        )
    except TelegramAPIError:
        await message.answer("Kullaniciya mesaj gonderilemedi.")
        return
    await message.answer("Yanit talep #" + str(ticket_id) + " icin gonderildi.")


@router.message(Command("ticketclose"))
async def ticket_close_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /ticketclose TALEP_ID")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE support_tickets
            SET status = 'closed', closed_at = ?
            WHERE ticket_id = ? AND status = 'open'
            """,
            (timestamp(), int(parts[1])),
        )
        await db.commit()
    await message.answer(
        "Destek talebi kapatildi." if cursor.rowcount else "Acik talep bulunamadi."
    )


@router.message(Command("ban"))
async def ban_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /ban KULLANICI_ID")
        return
    user_id = int(parts[1])
    if is_admin(user_id):
        await message.answer("Admin hesabi banlanamaz.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET is_banned = 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
    await message.answer(
        "Kullanici " + str(user_id) + " banlandi."
        if cursor.rowcount
        else "Kullanici bulunamadi."
    )


@router.message(Command("unban"))
async def unban_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Kullanim: /unban KULLANICI_ID")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET is_banned = 0 WHERE user_id = ?",
            (int(parts[1]),),
        )
        await db.commit()