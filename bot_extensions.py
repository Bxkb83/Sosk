# -*- coding: utf-8 -*-
import aiosqlite
import os
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
router = Router(name="store-extensions")

def now_utc():
    return datetime.now(timezone.utc)

def timestamp():
    return now_utc().isoformat(timespec="seconds")

def format_money(cents):
    whole, fraction = divmod(int(cents), 100)
    return str(whole) + "." + str(fraction).zfill(2) + " TL"

async def init_feature_schema():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0") # Hata verirse yoksay
        pass

async def get_language(user_id):
    return "tr"

async def user_is_banned(user_id):
    return False

def order_rating_keyboard(order_id):
    return None

class AdminBroadcastFlow:
    pass

class SupportFlow:
    pass

# ==========================================
# BUTON YAKALAYICILARI (Tüm menü butonları)
# ==========================================

@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(cb: CallbackQuery):
    await cb.message.answer("🛍️ Katalog yükleniyor... Lütfen bekleyin.\n\n(Hizmetleri yönetici ekleyecek)")
    await cb.answer()

@router.callback_query(F.data == "menu:balance")
async def menu_balance(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (cb.from_user.id,)) as c:
            row = await c.fetchone()
    balance = int(row["balance_cents"]) if row else 0
    await cb.message.answer("💰 Bakiyeniz: " + format_money(balance))
    await cb.answer()

@router.callback_query(F.data == "menu:orders")
async def menu_orders(cb: CallbackQuery):
    await cb.message.answer("📦 Siparişlerinizi görmek için /history yazabilirsiniz.")
    await cb.answer()

@router.callback_query(F.data == "menu:topup")
async def menu_topup(cb: CallbackQuery):
    await cb.message.answer("⭐ Bakiye yüklemek için /stars yazabilirsiniz.")
    await cb.answer()

@router.callback_query(F.data == "menu:vip")
async def menu_vip(cb: CallbackQuery):
    await cb.message.answer("💎 VIP üyelik sistemi yakında aktif olacak!")
    await cb.answer()

@router.callback_query(F.data == "menu:coupon")
async def menu_coupon(cb: CallbackQuery):
    await cb.message.answer("🎟️ İndirim kuponu sistemi yakında aktif olacak!")
    await cb.answer()

@router.callback_query(F.data == "menu:referral")
async def menu_referral(cb: CallbackQuery):
    me = await cb.bot.get_me()
    link = "https://t.me/" + str(me.username) + "?start=ref_" + str(cb.from_user.id)
    await cb.message.answer("👥 Referans linkiniz:\n" + link)
    await cb.answer()

@router.callback_query(F.data == "menu:leaderboard")
async def menu_leaderboard(cb: CallbackQuery):
    await cb.message.answer("🏆 Liderlik tablosu yükleniyor...")
    await cb.answer()

@router.callback_query(F.data == "menu:rewards")
async def menu_rewards(cb: CallbackQuery):
    await cb.message.answer("🎁 Ödüller ve görevler sistemi yakında aktif olacak!")
    await cb.answer()

@router.callback_query(F.data == "menu:support")
async def menu_support(cb: CallbackQuery):
    await cb.message.answer("💬 Destek için lütfen sorununuzu buraya tek mesaj olarak yazın.")
    await cb.answer()

@router.callback_query(F.data == "menu:language")
async def menu_language(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="language:tr"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="language:en"),
    ]])
    await cb.message.answer("🌐 Dil seçimi / Language selection:", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("language:"))
async def set_language(cb: CallbackQuery):
    await cb.message.answer("Dil ayarlandı.")
    await cb.answer()

@router.callback_query(F.data == "menu:help")
async def menu_help(cb: CallbackQuery):
    await cb.message.answer("ℹ️ Yardım menüsü.\nKomutlar için /help yazabilirsiniz.")
    await cb.answer()

@router.callback_query(F.data == "menu:broadcast")
async def menu_broadcast(cb: CallbackQuery):
    await cb.answer("Bu özellik sadece adminler içindir.", show_alert=True)

@router.callback_query(F.data == "menu:admin")
async def menu_admin(cb: CallbackQuery):
    await cb.message.answer("🛠️ Admin komutları için /admin yazın.")
    await cb.answer())
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
