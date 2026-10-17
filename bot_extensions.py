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
        await db.commit()ext or "").partition(" ")[2].strip()
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
    await message.answer(f"Destek talebiniz alindi. Talep numarasi: #{ticket_id}")
    for admin_id in admin_ids():
        try:
            await message.bot.send_message(
                admin_id,
                f"Yeni destek talebi #{ticket_id}\n"
                f"Kullanici: {message.from_user.id}\n{body[:1200]}",
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
            f"Talep #{row['ticket_id']} | Kullanici {row['user_id']}\n"
            f"{row['created_at']}\n{row['body'][:2500]}"
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
    ticket_id, body = int(parts[1]), " ".join(parts[2].split())[:3000]
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
            f"Destek ekibinden yanit (talep #{ticket_id}):\n{body}",
        )
    except TelegramAPIError:
        await message.answer("Kullaniciya mesaj gonderilemedi.")
        return
    await message.answer(f"Yanit talep #{ticket_id} icin gonderildi.")


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
        f"Kullanici {user_id} banlandi."
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
    await message.answer(
        "Ban kaldirildi." if cursor.rowcount else "Kullanici bulunamadi."
    )


@router.message(Command("usernote"))
async def user_note_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Kullanim: /usernote KULLANICI_ID NOT")
        return
    user_id, note = int(parts[1]), " ".join(parts[2].split())[:1000]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user_id,),
        )
        if await cursor.fetchone() is None:
            await message.answer("Kullanici bulunamadi.")
            return
        await db.execute(
            """
            INSERT INTO admin_user_notes (user_id, admin_id, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, message.from_user.id, note, timestamp()),
        )
        await db.commit()
    await message.answer(f"Kullanici {user_id} icin admin notu kaydedildi.")


class AdminBroadcastFlow(StatesGroup):
    body = State()
    confirmation = State()


@router.message(Command("broadcast"))
async def broadcast_start_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    body = (message.text or "").partition(" ")[2].strip()
    if body:
        await show_broadcast_confirmation(message, state, body)
        return
    await state.set_state(AdminBroadcastFlow.body)
    await message.answer(
        "📢 Duyuru metnini gönderin. Göndermeden önce önizleme ve onay "
        "ekranı açılacak. İptal için /cancel yazın."
    )


async def show_broadcast_confirmation(message, state, body):
    body = body.strip()
    if not body or len(body) > 3500:
        await message.answer("Mesaj 1-3500 karakter olmali.")
        return
    await state.update_data(broadcast_body=body)
    await state.set_state(AdminBroadcastFlow.confirmation)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Tüm kullanıcılara gönder",
                    callback_data="broadcast:send",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="↩️ İptal",
                    callback_data="broadcast:cancel",
                    style="primary",
                ),
            ]
        ]
    )
    await message.answer(
        "👀 Duyuru önizlemesi:\n\n" + body,
        reply_markup=keyboard,
    )


@router.message(AdminBroadcastFlow.body)
async def broadcast_body_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        return
    await show_broadcast_confirmation(message, state, message.text or "")


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_action_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Yetkiniz yok.", show_alert=True)
        return
    action = (callback.data or "").split(":", maxsplit=1)[-1]
    if action == "cancel":
        await state.clear()
        await callback.answer("Duyuru iptal edildi.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        return
    if action != "send" or await state.get_state() != AdminBroadcastFlow.confirmation.state:
        await callback.answer("Onay gecersiz.", show_alert=True)
        return
    body = str((await state.get_data()).get("broadcast_body", ""))
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE is_banned = 0 ORDER BY user_id"
        )
        rows = await cursor.fetchall()
    sent = failed = 0
    for row in rows:
        try:
            await cr "").partition(" ")[2].strip()
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
    await message.answer(f"Destek talebiniz alindi. Talep numarasi: #{ticket_id}")
    for admin_id in admin_ids():
        try:
            await message.bot.send_message(
                admin_id,
                f"Yeni destek talebi #{ticket_id}\n"
                f"Kullanici: {message.from_user.id}\n{body[:1200]}",
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
            f"Talep #{row['ticket_id']} | Kullanici {row['user_id']}\n"
            f"{row['created_at']}\n{row['body'][:2500]}"
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
    ticket_id, body = int(parts[1]), " ".join(parts[2].split())[:3000]
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
            f"Destek ekibinden yanit (talep #{ticket_id}):\n{body}",
        )
    except TelegramAPIError:
        await message.answer("Kullaniciya mesaj gonderilemedi.")
        return
    await message.answer(f"Yanit talep #{ticket_id} icin gonderildi.")


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
        f"Kullanici {user_id} banlandi."
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
    await message.answer(
        "Ban kaldirildi." if cursor.rowcount else "Kullanici bulunamadi."
    )


@router.message(Command("usernote"))
async def user_note_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Kullanim: /usernote KULLANICI_ID NOT")
        return
    user_id, note = int(parts[1]), " ".join(parts[2].split())[:1000]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user_id,),
        )
        if await cursor.fetchone() is None:
            await message.answer("Kullanici bulunamadi.")
            return
        await db.execute(
            """
            INSERT INTO admin_user_notes (user_id, admin_id, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, message.from_user.id, note, timestamp()),
        )
        await db.commit()
    await message.answer(f"Kullanici {user_id} icin admin notu kaydedildi.")


class AdminBroadcastFlow(StatesGroup):
    body = State()
    confirmation = State()


@router.message(Command("broadcast"))
async def broadcast_start_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    body = (message.text or "").partition(" ")[2].strip()
    if body:
        await show_broadcast_confirmation(message, state, body)
        return
    await state.set_state(AdminBroadcastFlow.body)
    await message.answer(
        "Toplu mesaj metnini gonderin. Gondermeden once onay ekrani acilacak. "
        "Iptal icin /cancel."
    )


async def show_broadcast_confirmation(message, state, body):
    body = body.strip()
    if not body or len(body) > 3500:
        await message.answer("Mesaj 1-3500 karakter olmali.")
        return
    await state.update_data(broadcast_body=body)
    await state.set_state(AdminBroadcastFlow.confirmation)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Tum kullanicilara gonder",
                    callback_data="broadcast:send",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="Iptal",
                    callback_data="broadcast:cancel",
                    style="primary",
                ),
            ]
        ]
    )
    await message.answer(
        "Mesaj onizlemesi:\n\n" + body,
        reply_markup=keyboard,
    )


@router.message(AdminBroadcastFlow.body)
async def broadcast_body_handler(message: Message, state: FSMContext):
    if message.from_user is None or not is_admin(message.from_user.id):
        await state.clear()
        return
    await show_broadcast_confirmation(message, state, message.text or "")


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_action_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user is None or not is_admin(callback.from_user.id):
        await callback.answer("Yetkiniz yok.", show_alert=True)
        return
    action = (callback.data or "").split(":", maxsplit=1)[-1]
    if action == "cancel":
        await state.clear()
        await callback.answer("Toplu mesaj iptal edildi.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        return
    if action != "send" or await state.get_state() != AdminBroadcastFlow.confirmation.state:
        await callback.answer("Onay gecersiz.", show_alert=True)
        return
    body = str((await state.get_data()).get("broadcast_body", ""))
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE is_banned = 0 ORDER BY user_id"
        )
        rows = await cursor.fetchall()
    sent = failed = 0
    for row in rows:
        try:
            await callback.bot.send_message(int(row[0]), body)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await callback.answer("Toplu mesaj gonderimi tamamlandi.")
    if callback.message:
        await callback.message.edit_text(
            f"Broadcast tamamlandi. Gonderildi: {sent}, gonderilemedi: {failed}."
        )


@router.message(Command("export"))
async def export_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    header = workbook.add_format({"bold": True, "bg_color": "#DCE8F2"})
    with_user_fields = [
        ("Kullanicilar", "SELECT user_id, username, first_name, balance_cents, created_at FROM users ORDER BY user_id"),
        ("Siparisler", "SELECT order_id, user_id, service_name, amount_cents, status, created_at FROM orders ORDER BY order_id"),
        ("Islemler", "SELECT transaction_id, user_id, amount_cents, kind, note, created_at FROM transactions ORDER BY transaction_id"),
    ]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for sheet_name, query in with_user_fields:
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
            sheet = workbook.add_worksheet(sheet_name)
            columns = list(rows[0].keys()) if rows else []
            if not columns:
                if sheet_name == "Kullanicilar":
                    columns = ["user_id", "username", "first_name", "balance_cents", "created_at"]
                elif sheet_name == "Siparisler":
                    columns = ["order_id", "user_id", "service_name", "amount_cents", "status", "created_at"]
                else:
                    columns = ["transaction_id", "user_id", "amount_cents", "kind", "note", "created_at"]
            for col, value in enumerate(columns):
                sheet.write_string(0, col, value, header)
            for row_index, row in enumerate(rows, start=1):
                for col, value in enumerate(row):
                    if value is None:
                        continue
                    if isinstance(value, (int, float)):
                        sheet.write_number(row_index, col, value)
                    else:
                        sheet.write_string(row_index, col, str(value))
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    workbook.close()
    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename="magaza-raporu.xlsx"),
        caption="Kullanici, siparis ve bakiye hareketleri raporu.",
    )


def order_rating_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{score} yildiz",
                    callback_data=f"rating:{order_id}:{score}",
                    style="primary",
                )
                for score in range(1, 6)
            ]
        ]
    )


@router.callback_query(F.data.startswith("rating:"))
async def order_rating_handler(callback: CallbackQuery):
    if callback.from_user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Puanlama gecersiz.", show_alert=True)
        return
    order_id, score = int(parts[1]), int(parts[2])
    if score < 1 or score > 5:
        await callback.answer("Puan 1-5 arasinda olmali.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO order_ratings
                (order_id, user_id, score, created_at)
            SELECT order_id, user_id, ?, ?
            FROM orders
            WHERE order_id = ? AND user_id = ? AND status = 'completed'
            """,
            (score, timestamp(), order_id, callback.from_user.id),
        )
        await db.commit()
    if cursor.rowcount != 1:
        await callback.answer(
            "Siparis bulunamadi, tamamlanmadi veya daha once puanlandi.",
            show_alert=True,
        )
        return
    await callback.answer("Puaniniz kaydedildi.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


async def init_feature_schema():
    if DB_PATH != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        user_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()
        }
        if "is_banned" not in user_columns:
            await db.execute(
                "ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0"
            )
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_rate_limits (
                user_id INTEGER PRIMARY KEY,
                last_message_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_user_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                admin_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS order_ratings (
                order_id INTEGER PRIMARY KEY REFERENCES orders(order_id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fraud_alerts (
                alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                amount_cents INTEGER NOT NULL,
                kind TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discount_coupons (
                coupon_id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 90),
                max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                current_uses INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coupon_uses (
                coupon_id INTEGER NOT NULL REFERENCES discount_coupons(coupon_id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                used_at TEXT NOT NULL,
                PRIMARY KEY(coupon_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS user_coupon_credits (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                coupon_id INTEGER NOT NULL REFERENCES discount_coupons(coupon_id),
                percent INTEGER NOT NULL,
                activated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_points (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                points INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS mystery_claims (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                claim_day TEXT NOT NULL,
                points_awarded INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, claim_day)
            );
            CREATE TABLE IF NOT EXISTS weekly_mission_claims (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                week_start TEXT NOT NULL,
                mission_key TEXT NOT NULL,
                points_awarded INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, week_start, mission_key)
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status
                ON support_tickets(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_admin_notes_user
                ON admin_user_notes(user_id, created_at);
            """
        )
        await db.commit()


async def user_is_banned(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT is_banned FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
    return bool(row and row[0])


def parse_price_cents(raw):
    try:
        value = Decimal(raw.replace(",", ".")) * 100
        if value != value.to_integral_value() or value <= 0:
            return None
        return int(value)
    except (InvalidOperation, ValueError):
        return None


@router.message(Command("coupon"))
async def coupon_redeem_handler(message: Message):
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Kullanim: /coupon KOD")
        return
    code = parts[1].strip().upper()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT coupon_id, percent, max_uses, current_uses, expires_at, active "
                "FROM discount_coupons WHERE code = ?",
                (code,),
            )
            coupon = await cursor.fetchone()
            if coupon is None or not coupon["active"]:
                await db.rollback()
                await message.answer("Kupon bulunamadi veya aktif degil.")
                return
            if coupon["expires_at"] <= timestamp() or coupon["current_uses"] >= coupon["max_uses"]:
                await db.rollback()
                await message.answer("Kuponun suresi dolmus veya kullanim hakki bitmis.")
                return
            cursor = await db.execute(
                "SELECT 1 FROM user_coupon_credits WHERE user_id = ?",
                (message.from_user.id,),
            )
            if await cursor.fetchone():
                await db.rollback()
                await message.answer(
                    "Once aktif kuponunuzu bir sipariste kullanin."
                )
                return
            try:
                await db.execute(
                    "INSERT INTO coupon_uses (coupon_id, user_id, used_at) VALUES (?, ?, ?)",
                    (coupon["coupon_id"], message.from_user.id, timestamp()),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                await message.answer("Bu kuponu daha once kullandiniz.")
                return
            await db.execute(
                """
                UPDATE discount_coupons
                SET current_uses = current_uses + 1,
                    active = CASE WHEN current_uses + 1 >= max_uses THEN 0 ELSE active END
                WHERE coupon_id = ?
                """,
                (coupon["coupon_id"],),
            )
            await db.execute(
                """
                INSERT INTO user_coupon_credits
                    (user_id, coupon_id, percent, activated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    message.from_user.id,
                    coupon["coupon_id"],
                    coupon["percent"],
                    timestamp(),
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    await message.answer(
        f"Kupon aktif: sonraki siparisinizde %{coupon['percent']} indirim uygulanir."
    )


@router.message(Command("couponadd"))
async def coupon_add_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 5:
        await message.answer(
            "Kullanim: /couponadd KOD YUZDE KULLANIM DAKIKA"
        )
        return
    code = parts[1].upper()
    if not code.isalnum() or not 3 <= len(code) <= 32:
        await message.answer("Kupon kodu 3-32 harf veya rakamdan olusmali.")
        return
    if not parts[2].isdigit() or not 1 <= int(parts[2]) <= 90:
        await message.answer("Indirim yuzdesi 1-90 araliginda olmali.")
        return
    if not parts[3].isdigit() or not 1 <= int(parts[3]) <= 100_000:
        await message.answer("Kullanim sayisi 1-100000 araliginda olmali.")
        return
    if not parts[4].isdigit() or not 1 <= int(parts[4]) <= 525_600:
        await message.answer("Sure 1-525600 dakika araliginda olmali.")
        return
    expires = (now_utc() + timedelta(minutes=int(parts[4]))).isoformat(timespec="seconds")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO discount_coupons
                    (code, percent, max_uses, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (code, int(parts[2]), int(parts[3]), expires, timestamp()),
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        await message.answer("Bu kupon kodu zaten var.")
        return
    await message.answer(f"Kupon {code} olusturuldu: %{parts[2]} indirim.")


@router.message(Command("vip"))
async def vip_status_handler(message: Message):
    if message.from_user is None:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT vip_until FROM users WHERE user_id = ?",
            (message.from_user.id,),
        )
        row = await cursor.fetchone()
    until = row[0] if row else None
    active = bool(until and datetime.fromisoformat(until) > now_utc())
    raw_price = os.getenv("VIP_PRICE_TL", "").strip()
    if active:
        text = "VIP uyeliginiz aktif. Siparislerde %20 indirim alirsiniz.\nBitis: " + str(until)
    else:
        text = "VIP uyeliginiz aktif degil."
    if raw_price:
        text += "\nVIP satin almak icin /vipbuy yazin."
    else:
        text += "\nVIP satin alma fiyati yonetici tarafindan ayarlanmamis."
    await message.answer(text)


@router.message(Command("vipbuy"))
async def vip_buy_handler(message: Message):
    if message.from_user is None:
        return
    price = parse_price_cents(os.getenv("VIP_PRICE_TL", "").strip())
    if price is None:
        await message.answer("VIP satin alma henuz yapilandirilmamis.")
        return
    try:
        days = int(os.getenv("VIP_DURATION_DAYS", "30"))
    except ValueError:
        await message.answer("VIP_DURATION_DAYS ayari gecersiz.")
        return
    if not 1 <= days <= 365:
        await message.answer("VIP uyelik suresi 1-365 gun araliginda olmali.")
        return
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT balance_cents, vip_until FROM users WHERE user_id = ?",
            (message.from_user.id,),
        )
        user = await cursor.fetchone()
        if user is None:
            await db.rollback()
            await message.answer("Once /start komutuyla kayit olun.")
            return
        if int(user["balance_cents"]) < price:
            await db.rollback()
            await message.answer(
                "Bakiye yetersiz. VIP fiyati: " + format_money(price)
            )
            return
        now = now_utc()
        old_until = datetime.fromisoformat(user["vip_until"]) if user["vip_until"] else now
        starts_at = old_until if old_until > now else now
        vip_until = (starts_at + timedelta(days=days)).isoformat(timespec="seconds")
        await db.execute(
            "UPDATE users SET balance_cents = balance_cents - ?, vip_until = ? WHERE user_id = ?",
            (price, vip_until, message.from_user.id),
        )
        await db.execute(
            """
            INSERT INTO transactions (user_id, amount_cents, kind, note, created_at)
            VALUES (?, ?, 'vip_membership', ?, ?)
            """,
            (
                message.from_user.id,
                -price,
                f"VIP {days} gun",
                timestamp(),
            ),
        )
        await db.commit()
    await message.answer(
        f"VIP aktif edildi. %20 indirim {vip_until} tarihine kadar gecerli."
    )


@router.message(Command("vipgrant"))
async def vip_grant_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Kullanim: /vipgrant KULLANICI_ID GUN")
        return
    user_id, days = int(parts[1]), int(parts[2])
    if not 1 <= days <= 365:
        await message.answer("Sure 1-365 gun araliginda olmali.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT vip_until FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await message.answer("Kullanici bulunamadi.")
            return
        now = now_utc()
        old_until = datetime.fromisoformat(row[0]) if row[0] else now
        vip_until = (
            (old_until if old_until > now else now) + timedelta(days=days)
        ).isoformat(timespec="seconds")
        await db.execute(
            "UPDATE users SET vip_until = ? WHERE user_id = ?",
            (vip_until, user_id),
        )
        await db.commit()
    await message.answer(f"Kullanici {user_id} icin VIP {days} gun uzatildi.")


@router.message(Command("vipservice"))
async def vip_service_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("on", "off"):
        await message.answer("Kullanim: /vipservice HIZMET_ID on|off")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE services SET vip_only = ? WHERE service_id = ?",
            (1 if parts[2] == "on" else 0, int(parts[1])),
        )
        await db.commit()
    await message.answer(
        "VIP market ayari guncellendi."
        if cursor.rowcount
        else "Hizmet bulunamadi."
    )


@router.message(Command("flashsale"))
async def flash_sale_handler(message: Message):
    if message.from_user is None or not is_admin(message.from_user.id):
        await message.answer("Yetkiniz yok.")
        return
    parts = (message.text or "").split()
    if (
        len(parts) != 4
        or not parts[1].isdigit()
        or not parts[2].isdigit()
        or not parts[3].isdigit()
        or not 1 <= int(parts[2]) <= 90
        or not 1 <= int(parts[3]) <= 10_080
    ):
        await message.answer("Kullanim: /flashsale HIZMET_ID YUZDE DAKIKA")
        return
    expires = (now_utc() + timedelta(minutes=int(parts[3]))).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE services
            SET sale_percent = ?, sale_until = ?
            WHERE service_id = ? AND active = 1
            """,
            (int(parts[2]), expires, int(parts[1])),
        )
        await db.commit()
    await message.answer(
        f"Flash indirim ayarlandi: %{parts[2]}."
        if cursor.rowcount
        else "Aktif hizmet bulunamadi."
    )


@router.message(Command("language"))
async def language_handler(message: Message):
    if message.from_user is None:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("tr", "en"):
        await message.answer("Dil secimi: /language tr veya /language en")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET language = ? WHERE user_id = ?",
            (parts[1].lower(), message.from_user.id),
        )
        await db.commit()
    if not cursor.rowcount:
        await message.answer("Once /start komutuyla kayit olun.")
    elif parts[1].lower() == "en":
        await message.answer("Language set to English. Use /help for commands.")
    else:
        await message.answer("Dil Turkce olarak ayarlandi. Komutlar icin /help.")


async def get_language(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT language FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
    return row[0] if row and row[0] in ("tr", "en") else "tr"


@router.message(Command("mystery"))
async def mystery_box_handler(message: Message):
    if message.from_user is None:
        return
    rewards = (5, 10, 20, 50)
    reward = secrets.choice(rewards)
    today = now_utc().date().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO mystery_claims
                (user_id, claim_day, points_awarded, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (message.from_user.id, today, reward, timestamp()),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            await message.answer("Gizemli kutu gunluk olarak bir kez acilabilir.")
            return
        await db.execute(
            """
            INSERT INTO user_points (user_id, points)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET points = points + excluded.points
            """,
            (message.from_user.id, reward),
        )
        await db.commit()
    await message.answer(
        f"Gizemli kutudan {reward} puan kazandiniz. "
        "Puanlar nakde cevrilemez ve devredilemez."
    )


@router.message(Command("badges"))
async def badges_handler(message: Message):
    if message.from_user is None:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'completed'",
            (message.from_user.id,),
        )
        orders = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (message.from_user.id,),
        )
        referrals = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            "SELECT points FROM user_points WHERE user_id = ?",
            (message.from_user.id,),
        )
        point_row = await cursor.fetchone()
    badges = []
    if orders >= 1:
        badges.append("Ilk siparis")
    if orders >= 10:
        badges.append("10 siparis")
    if referrals >= 5:
        badges.append("5 davet")
    if referrals >= 25:
        badges.append("25 davet")
    await message.answer(
        "Rozetleriniz: " + (", ".join(badges) if badges else "Henuz rozet yok.")
        + "\nPuan: " + str(point_row[0] if point_row else 0)
    )


@router.message(Command("missions"))
async def weekly_missions_handler(message: Message):
    if message.from_user is None:
        return
    now = now_utc()
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    week_timestamp = week_start + "T00:00:00+00:00"
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM orders
            WHERE user_id = ? AND status = 'completed' AND completed_at >= ?
            """,
            (message.from_user.id, week_timestamp),
        )
        completed_orders = int((await cursor.fetchone())[0])
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM referrals
            WHERE referrer_id = ? AND created_at >= ?
            """,
            (message.from_user.id, week_timestamp),
        )
        referrals = int((await cursor.fetchone())[0])
        missions = (
            ("orders_2", completed_orders >= 2, 20, "Bu hafta 2 siparisi tamamla"),
            ("referrals_3", referrals >= 3, 30, "Bu hafta 3 kisi davet et"),
        )
        newly_claimed = []
        for key, complete, points, label in missions:
            if not complete:
                continue
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO weekly_mission_claims
                    (user_id, week_start, mission_key, points_awarded, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.from_user.id,
                    week_start,
                    key,
                    points,
                    timestamp(),
                ),
            )
            if cursor.rowcount == 1:
                newly_claimed.append(points)
                await db.execute(
                    """
                    INSERT INTO user_points (user_id, points)
                    VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET points = points + excluded.points
                    """,
                    (message.from_user.id, points),
                )
        cursor = await db.execute(
            "SELECT mission_key FROM weekly_mission_claims WHERE user_id = ? AND week_start = ?",
            (message.from_user.id, week_start),
        )
        claimed = {row[0] for row in await cursor.fetchall()}
        cursor = await db.execute(
            "SELECT points FROM user_points WHERE user_id = ?",
            (message.from_user.id,),
        )
        points_row = await cursor.fetchone()
        await db.commit()
    lines = [
        f"Haftalik gorevler ({week_start}):",
        f"{'Tamamlandi' if completed_orders >= 2 else 'Ilerleme'}: 2 siparis ({completed_orders}/2) — 20 puan",
        f"{'Tamamlandi' if referrals >= 3 else 'Ilerleme'}: 3 davet ({referrals}/3) — 30 puan",
    ]
    if newly_claimed:
        lines.append("Yeni gorev puani: " + str(sum(newly_claimed)))
    lines.append("Toplam puan: " + str(points_row[0] if points_row else 0))
    lines.append("Puanlar nakde cevrilemez.")
    await message.answer("\n".join(lines))
