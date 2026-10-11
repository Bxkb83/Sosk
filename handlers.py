# -*- coding: utf-8 -*-
import asyncio
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice

from config import (ADMIN_IDS, CHANNEL, CHANNEL_LINK, CANCEL_MINUTES,
                    REFERRAL_POINT, IBAN, IBAN_NAME, COUNTRIES)
import database as db
from keyboards import (main_menu, join_kb, market_kb, country_kb,
                       cancel_kb, back_kb, request_action_kb)

router = Router()


class Flow(StatesGroup):
    promo_use       = State()
    fill_phone_code = State()


def is_admin(uid):
    return uid in ADMIN_IDS


async def check_membership(bot, uid) -> bool:
    u = await db.get_user(uid)
    if not u:
        return False
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        now = m.status in ("member", "administrator", "creator")
    except Exception:
        now = False
    was = u[6]
    if was == 1 and not now:
        await db.reset_user(uid)
        await db.set_member_flag(uid, 0)
        return False
    if now and was == 0:
        await db.set_member_flag(uid, 1)
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

    before = await db.get_user(uid)
    is_new = before is None
    await db.create_user(uid, m.from_user.username, m.from_user.first_name, ref)

    if is_new and ref and ref != uid:
        r = await db.get_user(ref)
        if r:
            await db.add_points(ref, REFERRAL_POINT)
            await db.add_tx(ref, 0, "referral", "Davet: " + str(uid))
            try:
                await bot.send_message(ref, "Yeni davet! +" + str(REFERRAL_POINT) + " referans puani kazandin.")
            except Exception:
                pass

    if not await check_membership(bot, uid):
        await m.answer(
            "Hos geldin!\n\nBotu kullanmak icin " + CHANNEL + " kanalina katilman zorunlu.\n\n"
            "Kanaldan cikarsan tum bakiyen, puanlarin ve gecmisin sifirlanir. "
            "Tekrar katilsan bile geri gelmez.",
            reply_markup=join_kb())
        return

    await m.answer("Merhaba " + str(m.from_user.first_name) + "!\n\nVirtuNum Market'e hos geldin.",
                   reply_markup=main_menu(is_admin(uid)))


@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: Bot):
    if await check_membership(bot, cb.from_user.id):
        await cb.message.edit_text("Katilim onaylandi!",
                                   reply_markup=main_menu(is_admin(cb.from_user.id)))
    else:
        await cb.answer("Henuz kanala katilmadin!", show_alert=True)


@router.callback_query(F.data == "back_menu")
async def cb_back(cb: CallbackQuery):
    await cb.message.edit_text("Ana menu:",
                               reply_markup=main_menu(is_admin(cb.from_user.id)))


@router.callback_query(F.data == "market")
async def cb_market(cb: CallbackQuery):
    apps = await db.get_market()
    await cb.message.edit_text("Market\n\nBir uygulama sec:",
                               reply_markup=market_kb(apps))


@router.callback_query(F.data.startswith("app:"))
async def cb_app(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    apps = await db.get_market()
    name = None
    emoji = None
    for k, n, e in apps:
        if k == key:
            name = n
            emoji = e
            break
    if not name:
        await cb.answer("Uygulama bulunamadi.", show_alert=True)
        return
    prices = {}
    for cc, _ in COUNTRIES:
        prices[cc] = await db.get_price(key, cc)
    await cb.message.edit_text(
        str(emoji) + " " + str(name) + "\n\nNumara almak istedigin ulkeyi sec:",
        reply_markup=country_kb(key, prices))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    app_key = parts[1]
    cc = parts[2]
    uid = cb.from_user.id

    if not await check_membership(bot, uid):
        await cb.message.edit_text("Kanaldan cikmissin! Bakiyen geri gelmez.",
                                   reply_markup=join_kb())
        return

    if await db.user_active_request(uid):
        await cb.answer("Zaten bekleyen numara talebin var!", show_alert=True)
        return

    apps = await db.get_market()
    name = app_key
    for k, n, e in apps:
        if k == app_key:
            name = n
            break
    price = await db.get_price(app_key, cc)

    u = await db.get_user(uid)
    if u[3] < price:
        await cb.message.edit_text(
            "Yetersiz bakiye!\nGerekli: " + str(price) + " TL | Mevcut: " + str(u[3]) + " TL",
            reply_markup=main_menu(is_admin(uid)))
        return

    await db.update_balance(uid, -price)
    await db.add_tx(uid, -price, "hold", "Rezerve: " + str(name) + " " + str(cc))
    rid = await db.create_request(uid, str(name) + " (" + str(cc) + ")", cc, price)

    await cb.message.edit_text(
        "Siparis Alindi!\n\nUygulama: " + str(name) + "\nUlke: " + str(cc) +
        "\nUcret: " + str(price) + " TL (rezerve)\n"
        "Talep: #" + str(rid) + "\n\nAdmin numarayi manuel iletecek.\n" +
        str(CANCEL_MINUTES) + " dakika icinde iptal edip iade alabilirsin.",
        reply_markup=cancel_kb(rid))

    admin_msg_ids = []
    for adm in ADMIN_IDS:
        try:
            msg = await bot.send_message(adm,
                "YENI SIPARIS\n\n#" + str(rid) + "\nUygulama: " + str(name) +
                "\nUlke: " + str(cc) + "\nUID: " + str(uid) + "\nUcret: " + str(price) + " TL",
                reply_markup=request_action_kb(rid))
            admin_msg_ids.append(msg.message_id)
        except Exception:
            pass
    await db.set_request_admin_msgs(rid, admin_msg_ids)

    asyncio.create_task(auto_expire(bot, rid, CANCEL_MINUTES * 60))


async def auto_expire(bot, rid, seconds):
    await asyncio.sleep(seconds)
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        return
    uid = r[1]
    cost = r[4]
    await db.set_request_status(rid, "expired")
    await db.update_balance(uid, cost)
    await db.add_tx(uid, cost, "refund", "#" + str(rid) + " sure doldu")
    try:
        await bot.send_message(uid, "#" + str(rid) + " suresi doldu. +" + str(cost) + " TL iade.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("cancel_req:"))
async def cb_cancel_req(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r[1] != cb.from_user.id:
        await cb.answer("Talep bulunamadi.", show_alert=True)
        return
    if r[5] != "pending":
        await cb.answer("Iptal edilemez.", show_alert=True)
        return
    await db.set_request_status(rid, "cancelled")
    await db.update_balance(cb.from_user.id, r[4])
    await db.add_tx(cb.from_user.id, r[4], "refund", "#" + str(rid) + " iptal")
    await cb.message.edit_text("#" + str(rid) + " iptal. +" + str(r[4]) + " TL iade.",
                               reply_markup=main_menu(is_admin(cb.from_user.id)))
    if r[8]:
        for adm in ADMIN_IDS:
            for mid in str(r[8]).split(","):
                try:
                    await bot.edit_message_text(chat_id=adm, message_id=int(mid),
                        text="#" + str(rid) + " kullanici iptal etti (iade yapildi).")
                except Exception:
                    pass


@router.message(Flow.fill_phone_code)
async def recv_phone_code(m: Message, state: FSMContext, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    rid = data.get("rid")
    text = m.text.strip()
    if " " not in text:
        await m.answer("Format: +905551234567 123456")
        return
    phone, code = text.rsplit(" ", 1)
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        await m.answer("Talep artik aktif degil.")
        await state.clear()
        return
    uid = r[1]
    cost = r[4]
    await db.complete_request(rid, phone, code)
    try:
        await bot.send_message(uid,
            "Numaran Hazir!\n\nNumara: " + str(phone) + "\nKod: " + str(code) +
            "\n\nUcret: " + str(cost) + " TL (kesin cekildi, iade yok)")
    except Exception as e:
        await m.answer("Kullaniciya gonderilemedi: " + str(e))
    if r[8]:
        for adm in ADMIN_IDS:
            for mid in str(r[8]).split(","):
                try:
                    await bot.edit_message_text(chat_id=adm, message_id=int(mid),
                        text="#" + str(rid) + " tamamlandi. Numara: " + str(phone) + " | Kod: " + str(code))
                except Exception:
                    pass
    await m.answer("Talep #" + str(rid) + " tamamlandi.")
    await state.clear()


@router.callback_query(F.data == "deposit")
async def cb_deposit(cb: CallbackQuery):
    txt = (
        "Bakiye Yukleme\n\n"
        "1) IBAN: " + str(IBAN) + "\nAlici: " + str(IBAN_NAME) +
        "\nAciklama: " + str(cb.from_user.id) + "\n\n"
        "2) Telegram Yildiz: Asagidaki butondan ode.\n\n"
        "3) Referans Sistemi: Her davet = +" + str(REFERRAL_POINT) + " puan.\n\n"
        "Tum odemeler manuel onaylanir."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Yildiz ile Ode", callback_data="pay_stars")],
        [InlineKeyboardButton(text="Geri", callback_data="back_menu")],
    ])
    await cb.message.edit_text(txt, reply_markup=kb)


@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(cb: CallbackQuery, bot: Bot):
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id,
            title="Bakiye Yukleme",
            description="10 TL = 10 Yildiz",
            payload="topup_10",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Bakiye", amount=10)])
    except Exception:
        await cb.answer("Yildiz odeme su an kapali.", show_alert=True)


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    txs = await db.user_txs(cb.from_user.id, 8)
    txt = ("Profil\n\nID: " + str(u[0]) + "\nBakiye: " + str(u[3]) + " TL\n"
           "Referans Puani: " + str(u[4]) + "\nKayit: " + str(u[8])[:16] + "\n\nSon Islemler:\n")
    for t in txs:
        amt = t[0]
        desc = t[2]
        sign = "+" if amt > 0 else ""
        txt += sign + str(amt) + " TL - " + str(desc) + "\n"
    if not txs:
        txt += "Henuz islem yok"
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))


@router.callback_query(F.data == "referral")
async def cb_referral(cb: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    link = "https://t.me/" + str(me.username) + "?start=ref_" + str(cb.from_user.id)
    u = await db.get_user(cb.from_user.id)
    await cb.message.edit_text(
        "Referans Sistemin\n\n" + link + "\n\nHer davet = +" + str(REFERRAL_POINT) +
        " puan\nMevcut Puanin: " + str(u[4]),
        reply_markup=main_menu(is_admin(cb.from_user.id)))


@router.callback_query(F.data == "leaderboard")
async def cb_lb(cb: CallbackQuery):
    top = await db.top_referrers(10)
    txt = "Referans Liderlik Tablosu\n\n"
    if not top:
        txt += "Henuz kimse referans toplamamis"
    else:
        i = 1
        for uid, name, uname, pts in top:
            medal = str(i) + "."
            if i == 1:
                medal = "1."
            elif i == 2:
                medal = "2."
            elif i == 3:
                medal = "3."
            txt += medal + " " + str(name or uname or uid) + " - " + str(pts) + " puan\n"
            i += 1
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))


@router.callback_query(F.data == "contact_admin")
async def cb_contact(cb: CallbackQuery, bot: Bot):
    admin_id = ADMIN_IDS[0]
    try:
        chat = await bot.get_chat(admin_id)
        if chat.username:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Admin ile Konus", url="https://t.me/" + str(chat.username))],
                [InlineKeyboardButton(text="Geri", callback_data="back_menu")],
            ])
            await cb.message.edit_text("Admin Iletisim\n\n@" + str(chat.username), reply_markup=kb)
        else:
            await cb.answer("Admin: " + str(chat.first_name) + "\nID: " + str(admin_id), show_alert=True)
    except Exception:
        await cb.answer("Admin ID: " + str(admin_id), show_alert=True)


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    txt = (
        "Yardim\n\n"
        "Marketten uygulama sec, ulke sec, satin al.\n"
        "Admin manuel olarak numara + kodu iletir.\n"
        "Kod gelirse bakiye kesin cekilir.\n" +
        str(CANCEL_MINUTES) + " dk icinde iptal = tam iade.\n"
        "Her referans = +" + str(REFERRAL_POINT) + " puan.\n\n"
        "Kanal: " + str(CHANNEL_LINK)
    )
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))


@router.callback_query(F.data == "promo_use")
async def cb_promo_use(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.promo_use)
    await cb.message.edit_text("Promo kodunu yaz:", reply_markup=back_kb())


@router.message(Flow.promo_use)
async def recv_promo(m: Message, state: FSMContext, bot: Bot):
    code = m.text.strip().upper()
    p = await db.get_promo(code)
    if not p:
        await m.answer("Gecersiz kod.", reply_markup=main_menu(is_admin(m.from_user.id)))
        await state.clear()
        return
    pid = p[0]
    if p[3] >= p[2] or await db.user_used_promo(pid, m.from_user.id):
        await m.answer("Kod kullanilamaz.", reply_markup=main_menu(is_admin(m.from_user.id)))
        await state.clear()
        return
    await db.inc_promo_use(pid)
    await db.add_promo_use(pid, m.from_user.id)
    await db.update_balance(m.from_user.id, p[5])
    await db.add_tx(m.from_user.id, p[5], "promo", "Promo: " + str(code))
    await m.answer("Promo kullanildi! +" + str(p[5]) + " TL",
                   reply_markup=main_menu(is_admin(m.from_user.id)))
    await state.clear()
    from scheduler import promo_text
    p2 = await db.get_promo_by_id(pid)
    if p2[3] >= p2[2]:
        try:
            await bot.delete_message(CHANNEL, p2[6])
        except Exception:
            pass
        await db.deactivate_promo(pid)
    else:
        try:
            await bot.edit_message_text(chat_id=CHANNEL, message_id=p2[6], text=promo_text(p2))
        except Exception:
            pass