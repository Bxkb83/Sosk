# -*- coding: utf-8 -*-
import asyncio
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import (ADMIN_IDS, CHANNEL, CHANNEL_LINK, CANCEL_MINUTES,
                    REFERRAL_POINT, IBAN, IBAN_NAME, COUNTRIES)
import database as db
from keyboards import (main_menu, join_kb, market_kb, country_kb,
                       cancel_kb, back_kb, request_action_kb)

router = Router()

class Flow(StatesGroup):
    promo_use       = State()
    fill_phone_code = State()

def is_admin(uid): return uid in ADMIN_IDS

async def check_membership(bot, uid) -> bool:
    u = await db.get_user(uid)
    if not u: return False
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
async def cmd_start(m: Message, bot: types.Bot):
    uid = m.from_user.id
    ref = None
    parts = m.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try: ref = int(parts[1][4:])
        except: pass

    before = await db.get_user(uid)
    is_new = before is None
    await db.create_user(uid, m.from_user.username, m.from_user.first_name, ref)

    if is_new and ref and ref != uid:
        r = await db.get_user(ref)
        if r:
            await db.add_points(ref, REFERRAL_POINT)
            await db.add_tx(ref, 0, "referral", f"Davet: {uid} (+{REFERRAL_POINT} puan)")
            try:
                await bot.send_message(ref, f"Yeni davet! +{REFERRAL_POINT} referans puani kazandin.")
            except: pass

    if not await check_membership(bot, uid):
        await m.answer(
            f"Hos geldin!\n\nBotu kullanmak icin {CHANNEL} kanalina katilman zorunlu.\n\n"
            "Kanaldan cikarsan tum bakiyen, puanlarin ve gecmisin sifirlanir. "
            "Tekrar katilsan bile geri gelmez.",
            reply_markup=join_kb())
        return

    await m.answer(f"Merhaba {m.from_user.first_name}!\n\nVirtuNum Market'e hos geldin.",
                   reply_markup=main_menu(is_admin(uid)))

@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: types.Bot):
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
    name = emoji = None
    for k, n, e in apps:
        if k == key: name, emoji = n, e; break
    if not name:
        await cb.answer("Uygulama bulunamadi.", show_alert=True); return
    prices = {}
    for cc, _ in COUNTRIES:
        prices[cc] = await db.get_price(key, cc)
    await cb.message.edit_text(
        f"{emoji} {name}\n\nNumara almak istedigin ulkeyi sec:",
        reply_markup=country_kb(key, prices))

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: types.Bot):
    _, app_key, cc = cb.data.split(":")
    uid = cb.from_user.id

    if not await check_membership(bot, uid):
        await cb.message.edit_text("Kanaldan cikmissin! Bakiyen geri gelmez.",
                                   reply_markup=join_kb()); return

    if await db.user_active_request(uid):
        await cb.answer("Zaten bekleyen numara talebin var!", show_alert=True); return

    apps = await db.get_market()
    name = next((n for k, n, _ in apps if k == app_key), app_key)
    price = await db.get_price(app_key, cc)

    u = await db.get_user(uid)
    if u[3] < price:
        await cb.message.edit_text(
            f"Yetersiz bakiye!\nGerekli: {price} TL | Mevcut: {u[3]} TL",
            reply_markup=main_menu(is_admin(uid)))
        return

    await db.update_balance(uid, -price)
    await db.add_tx(uid, -price, "hold", f"Rezerve: {name} {cc}")
    rid = await db.create_request(uid, f"{name} ({cc})", cc, price)

    await cb.message.edit_text(
        f"Siparis Alindi!\n\nUygulama: {name}\nUlke: {cc}\nUcret: {price} TL (rezerve)\n"
        f"Talep: #{rid}\n\nAdmin numarayi manuel iletecek.\n"
        f"{CANCEL_MINUTES} dakika icinde iptal edip iade alabilirsin.",
        reply_markup=cancel_kb(rid))

    admin_msg_ids = []
    for adm in ADMIN_IDS:
        try:
            msg = await bot.send_message(adm,
                f"YENI SIPARIS\n\n#{rid}\nUygulama: {name}\nUlke: {cc}\nUID: {uid}\nUcret: {price} TL",
                reply_markup=request_action_kb(rid))
            admin_msg_ids.append(msg.message_id)
        except: pass
    await db.set_request_admin_msgs(rid, admin_msg_ids)

    asyncio.create_task(auto_expire(bot, rid, CANCEL_MINUTES * 60))

async def auto_expire(bot, rid, seconds):
    await asyncio.sleep(seconds)
    r = await db.get_request(rid)
    if not r or r[5] != "pending": return
    uid, cost = r[1], r[4]
    await db.set_request_status(rid, "expired")
    await db.update_balance(uid, cost)
    await db.add_tx(uid, cost, "refund", f"#{rid} sure doldu")
    try:
        await bot.send_message(uid, f"#{rid} suresi doldu. +{cost} TL iade.")
    except: pass

@router.callback_query(F.data.startswith("cancel_req:"))
async def cb_cancel_req(cb: CallbackQuery, bot: types.Bot):
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r[1] != cb.from_user.id:
        await cb.answer("Talep bulunamadi.", show_alert=True); return
    if r[5] != "pending":
        await cb.answer("Iptal edilemez.", show_alert=True); return
    await db.set_request_status(rid, "cancelled")
    await db.update_balance(cb.from_user.id, r[4])
    await db.add_tx(cb.from_user.id, r[4], "refund", f"#{rid} iptal")
    await cb.message.edit_text(f"#{rid} iptal. +{r[4]} TL iade.",
                               reply_markup=main_menu(is_admin(cb.from_user.id)))
    if r[8]:
        for adm in ADMIN_IDS:
            for mid in str(r[8]).split(","):
                try:
                    await bot.edit_message_text(chat_id=adm, message_id=int(mid),
                        text=f"#{rid} kullanici iptal etti (iade yapildi).")
                except: pass

@router.message(Flow.fill_phone_code)
async def recv_phone_code(m: Message, state: FSMContext, bot: types.Bot):
    if not is_admin(m.from_user.id): return
    data = await state.get_data(); rid = data.get("rid"); text = m.text.strip()
    if " " not in text:
        await m.answer("Format: +905551234567 123456"); return
    phone, code = text.rsplit(" ", 1)
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        await m.answer("Talep artik aktif degil."); await state.clear(); return
    uid, cost = r[1], r[4]
    await db.complete_request(rid, phone, code)
    try:
        await bot.send_message(uid,
            f"Numaran Hazir!\n\nNumara: {phone}\nKod: {code}\n\nUcret: {cost} TL (kesin cekildi, iade yok)")
    except Exception as e:
        await m.answer(f"Kullaniciya gonderilemedi: {e}")
    if r[8]:
        for adm in ADMIN_IDS:
            for mid in str(r[8]).split(","):
                try:
                    await bot.edit_message_text(chat_id=adm, message_id=int(mid),
                        text=f"#{rid} tamamlandi. Numara: {phone} | Kod: {code}")
                except: pass
    await m.answer(f"Talep #{rid} tamamlandi.")
    await state.clear()

@router.callback_query(F.data == "deposit")
async def cb_deposit(cb: CallbackQuery):
    txt = (
        "Bakiye Yukleme\n\n"
        f"1) IBAN: {IBAN}\nAlici: {IBAN_NAME}\nAciklama: {cb.from_user.id}\n\n"
        "2) Telegram Yildiz: Asagidaki butondan ode.\n\n"
        f"3) Referans Sistemi: Her davet = +{REFERRAL_POINT} puan.\n\n"
        "Tum odemeler manuel onaylanir."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Yildiz ile Ode", callback_data="pay_stars", style="success")],
        [InlineKeyboardButton(text="Geri", callback_data="back_menu", style="primary")],
    ])
    await cb.message.edit_text(txt, reply_markup=kb)

@router.callback_query(F.data == "pay_stars")
async def cb_pay_stars(cb: CallbackQuery, bot: types.Bot):
    try:
        await bot.send_invoice(
            chat_id=cb.from_user.id, title="Bakiye Yukleme",
            description="10 TL = 10 Yildiz", payload="topup_10",
            provider_token="", currency="XTR",
            prices=[types.LabeledPrice(label="Bakiye", amount=10)])
    except Exception:
        await cb.answer("Yildiz odeme su an kapali.", show_alert=True)

@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    u = await db.get_user(cb.from_user.id)
    txs = await db.user_txs(cb.from_user.id, 8)
    txt = f"Profil\n\nID: {u[0]}\nBakiye: {u[3]} TL\nReferans Puani: {u[4]}\nKayit: {str(u[8])[:16]}\n\nSon Islemler:\n"
    for t in txs:
        amt, _, desc, _ = t
        sign = "+" if amt > 0 else ""
        txt += f"{sign}{amt} TL - {desc}\n"
    if not txs: txt += "Henuz islem yok"
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))

@router.callback_query(F.data == "referral")
async def cb_referral(cb: CallbackQuery, bot: types.Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    u = await db.get_user(cb.from_user.id)
    await cb.message.edit_text(
        f"Referans Sistemin\n\n{link}\n\nHer davet = +{REFERRAL_POINT} puan\nMevcut Puanin: {u[4]}",
        reply_markup=main_menu(is_admin(cb.from_user.id)))

@router.callback_query(F.data == "leaderboard")
async def cb_lb(cb: CallbackQuery):
    top = await db.top_referrers(10)
    txt = "Referans Liderlik Tablosu\n\n"
    if not top:
        txt += "Henuz kimse referans toplamamis"
    else:
        for i, (uid, name, uname, pts) in enumerate(top, 1):
            medal = ["1.","2.","3."][i-1] if i <= 3 else f"{i}."
            txt += f"{medal} {name or uname or uid} - {pts} puan\n"
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))

@router.callback_query(F.data == "contact_admin")
async def cb_contact(cb: CallbackQuery, bot: types.Bot):
    admin_id = ADMIN_IDS[0]
    try:
        chat = await bot.get_chat(admin_id)
        if chat.username:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Admin ile Konus", url=f"https://t.me/{chat.username}", style="success")],
                [InlineKeyboardButton(text="Geri", callback_data="back_menu", style="primary")],
            ])
            await cb.message.edit_text(f"Admin Iletisim\n\n@{chat.username}", reply_markup=kb)
        else:
            await cb.answer(f"Admin: {chat.first_name}\nID: {admin_id}", show_alert=True)
    except Exception:
        await cb.answer(f"Admin ID: {admin_id}", show_alert=True)

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    txt = (
        "Yardim\n\n"
        "Marketten uygulama sec, ulke sec, satin al.\n"
        "Admin manuel olarak numara + kodu iletir.\n"
        "Kod gelirse bakiye kesin cekilir.\n"
        f"{CANCEL_MINUTES} dk icinde iptal = tam iade.\n"
        f"Her referans = +{REFERRAL_POINT} puan.\n\n"
        f"Kanal: {CHANNEL_LINK}"
    )
    await cb.message.edit_text(txt, reply_markup=main_menu(is_admin(cb.from_user.id)))

@router.callback_query(F.data == "promo_use")
async def cb_promo_use(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.promo_use)
    await cb.message.edit_text("Promo kodunu yaz:", reply_markup=back_kb())

@router.message(Flow.promo_use)
async def recv_promo(m: Message, state: FSMContext, bot: types.Bot):
    code = m.text.strip().upper()
    p = await db.get_promo(code)
    if not p:
        await m.answer("Gecersiz kod.", reply_markup=main_menu(is_admin(m.from_user.id)))
        await state.clear(); return
    pid = p[0]
    if p[3] >= p[2] or await db.user_used_promo(pid, m.from_user.id):
        await m.answer("Kod kullanilamaz.", reply_markup=main_menu(is_admin(m.from_user.id)))
        await state.clear(); return
    await db.inc_promo_use(pid)
    await db.add_promo_use(pid, m.from_user.id)
    await db.update_balance(m.from_user.id, p[5])
    await db.add_tx(m.from_user.id, p[5], "promo", f"Promo: {code}")
    await m.answer(f"Promo kullanildi! +{p[5]} TL",
                   reply_markup=main_menu(is_admin(m.from_user.id)))
    await state.clear()
    from scheduler import promo_text
    p2 = await db.get_promo_by_id(pid)
    if p2[3] >= p2[2]:
        try: await bot.delete_message(CHANNEL, p2[6])
        except: pass
        await db.deactivate_promo(pid)
    else:
        try:
            await bot.edit_message_text(chat_id=CHANNEL, message_id=p2[6], text=promo_text(p2))
        except: pass
