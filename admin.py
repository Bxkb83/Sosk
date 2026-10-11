# -*- coding: utf-8 -*-
import asyncio
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import ADMIN_IDS, CHANNEL, COUNTRIES
import database as db
from keyboards import (admin_panel_kb, back_kb, admin_prices_kb,
                       price_country_kb)

router = Router()


class Adm(StatesGroup):
    newpromo_code  = State()
    newpromo_uses  = State()
    newpromo_days  = State()
    newpromo_bonus = State()
    delpromo_code  = State()
    broadcast_text = State()
    set_price_val  = State()


def is_admin(uid):
    return uid in ADMIN_IDS


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("Admin Paneli", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin_panel")
async def cb_panel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Admin Paneli", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "adm_pendings")
async def cb_pend(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    ps = await db.pending_requests()
    if not ps:
        await cb.answer("Bekleyen talep yok.", show_alert=True)
        return
    txt = "Bekleyen Talepler\n\n"
    for p in ps:
        txt += "#" + str(p[0]) + " - UID " + str(p[1]) + " - " + str(p[2]) + "\n"
    await cb.message.edit_text(txt, reply_markup=admin_panel_kb())


@router.callback_query(F.data == "adm_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    tot = await db.total_users()
    pend = len(await db.pending_requests())
    promos = len(await db.active_promos())
    await cb.answer("Kullanici: " + str(tot) + "\nBekleyen: " + str(pend) +
                    "\nPromo: " + str(promos), show_alert=True)


@router.callback_query(F.data == "adm_prices")
async def cb_prices(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    apps = await db.get_market()
    await cb.message.edit_text("Duzenlemek istedigin uygulamayi sec:",
                               reply_markup=admin_prices_kb(apps))


@router.callback_query(F.data.startswith("editprices:"))
async def cb_edit_prices(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    key = cb.data.split(":")[1]
    apps = await db.get_market()
    name = None
    emoji = None
    for k, n, e in apps:
        if k == key:
            name = n
            emoji = e
            break
    prices = {}
    for cc, _ in COUNTRIES:
        prices[cc] = await db.get_price(key, cc)
    await cb.message.edit_text(
        str(emoji) + " " + str(name) + " - Fiyatlar\n\nDuzenlemek icin ulke sec:",
        reply_markup=price_country_kb(key, prices))


@router.callback_query(F.data.startswith("setprice:"))
async def cb_set_price(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    parts = cb.data.split(":")
    key = parts[1]
    cc = parts[2]
    await state.set_state(Adm.set_price_val)
    await state.update_data(app_key=key, cc=cc)
    old = await db.get_price(key, cc)
    await cb.message.edit_text(
        str(key) + " -> " + str(cc) + "\nMevcut: " + str(old) + " TL\n\nYeni fiyati yaz (TL):",
        reply_markup=back_kb("adm_prices"))


@router.message(Adm.set_price_val)
async def recv_price(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        val = int(m.text.strip())
    except Exception:
        await m.answer("Sayi gir.")
        return
    d = await state.get_data()
    await db.set_price(d["app_key"], d["cc"], val)
    await m.answer(str(d["app_key"]) + " / " + str(d["cc"]) + " -> " + str(val) + " TL",
                   reply_markup=admin_panel_kb())
    await state.clear()


@router.callback_query(F.data == "adm_new_promo")
async def cb_np(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(Adm.newpromo_code)
    await cb.message.edit_text("1) Promo kodu yaz (orn: YENIYIL):")


@router.message(Adm.newpromo_code)
async def np_code(m: Message, state: FSMContext):
    await state.update_data(code=m.text.strip().upper())
    await state.set_state(Adm.newpromo_uses)
    await m.answer("2) Kac kisi kullanabilsin? (sayi)")


@router.message(Adm.newpromo_uses)
async def np_uses(m: Message, state: FSMContext):
    try:
        u = int(m.text)
    except Exception:
        await m.answer("Sayi gir.")
        return
    await state.update_data(max_uses=u)
    await state.set_state(Adm.newpromo_days)
    await m.answer("3) Kac gun gecerli? (orn: 2)")


@router.message(Adm.newpromo_days)
async def np_days(m: Message, state: FSMContext):
    try:
        d = int(m.text)
    except Exception:
        await m.answer("Sayi gir.")
        return
    await state.update_data(hours=d * 24)
    await state.set_state(Adm.newpromo_bonus)
    await m.answer("4) Kullaniciya kac TL odul?")


@router.message(Adm.newpromo_bonus)
async def np_bonus(m: Message, state: FSMContext, bot: Bot):
    try:
        bonus = int(m.text)
    except Exception:
        await m.answer("Sayi gir.")
        return
    d = await state.get_data()
    txt = ("PROMO KODU: " + str(d["code"]) + "\n\n"
           "Odul: " + str(bonus) + " TL\n"
           "Kullanim: 0/" + str(d["max_uses"]) + "\n"
           "Kalan Sure: " + str(d["hours"]) + " saat\n\n"
           "Botta Promo Kod Kullan butonundan kullan.")
    try:
        msg = await bot.send_message(CHANNEL, txt)
    except Exception as e:
        await m.answer("Kanala gonderilemedi: " + str(e) + "\nBot kanalda admin mi?")
        await state.clear()
        return
    await db.create_promo(d["code"], d["max_uses"], d["hours"], bonus, msg.message_id)
    await m.answer("Promo olusturuldu!\nKod: " + str(d["code"]),
                   reply_markup=admin_panel_kb())
    await state.clear()


@router.callback_query(F.data == "adm_list_promos")
async def cb_lp(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    ps = await db.active_promos()
    if not ps:
        await cb.answer("Aktif promo yok.", show_alert=True)
        return
    txt = "Aktif Promolar\n\n"
    for p in ps:
        txt += (str(p[1]) + " - " + str(p[3]) + "/" + str(p[2]) + " - " +
                str(p[4]) + "sa - " + str(p[5]) + " TL\n")
    await cb.message.edit_text(txt, reply_markup=admin_panel_kb())


@router.callback_query(F.data == "adm_del_promo")
async def cb_dp(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(Adm.delpromo_code)
    await cb.message.edit_text("Silmek istedigin promo kodunu yaz:")


@router.message(Adm.delpromo_code)
async def dp_recv(m: Message, state: FSMContext, bot: Bot):
    code = m.text.strip().upper()
    p = await db.get_promo(code)
    if not p:
        await m.answer("Bulunamadi.")
        await state.clear()
        return
    try:
        await bot.delete_message(CHANNEL, p[6])
    except Exception:
        pass
    await db.deactivate_promo(p[0])
    await m.answer(str(code) + " silindi.", reply_markup=admin_panel_kb())
    await state.clear()


@router.callback_query(F.data == "adm_broadcast")
async def cb_bc(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(Adm.broadcast_text)
    await cb.message.edit_text("Gondermek istedigin mesaji yaz:")


@router.message(Adm.broadcast_text)
async def bc_recv(m: Message, state: FSMContext, bot: Bot):
    ids = await db.all_user_ids()
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, m.text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(str(ok) + "/" + str(len(ids)) + " kullaniciya gonderildi.",
                   reply_markup=admin_panel_kb())
    await state.clear()


@router.message(Command("addbalance"))
async def cmd_addbal(m: Message):
    if not is_admin(m.from_user.id):
        return
    a = m.text.split()
    if len(a) != 3:
        await m.answer("Kullanim: /addbalance <uid> <tl>")
        return
    try:
        uid = int(a[1])
        amt = int(a[2])
    except Exception:
        await m.answer("Gecersiz.")
        return
    await db.update_balance(uid, amt)
    await db.add_tx(uid, amt, "admin_add", "Admin ekledi")
    await m.answer(str(uid) + " -> +" + str(amt) + " TL")
    try:
        await m.bot.send_message(uid, "Hesabina +" + str(amt) + " TL eklendi.")
    except Exception:
        pass


@router.message(Command("removebalance"))
async def cmd_rembal(m: Message):
    if not is_admin(m.from_user.id):
        return
    a = m.text.split()
    if len(a) != 3:
        await m.answer("Kullanim: /removebalance <uid> <tl>")
        return
    try:
        uid = int(a[1])
        amt = int(a[2])
    except Exception:
        await m.answer("Gecersiz.")
        return
    await db.update_balance(uid, -amt)
    await db.add_tx(uid, -amt, "admin_remove", "Admin sildi")
    await m.answer(str(uid) + " -> -" + str(amt) + " TL")


@router.message(Command("addpoints"))
async def cmd_addpts(m: Message):
    if not is_admin(m.from_user.id):
        return
    a = m.text.split()
    if len(a) != 3:
        await m.answer("Kullanim: /addpoints <uid> <puan>")
        return
    try:
        uid = int(a[1])
        p = int(a[2])
    except Exception:
        return
    await db.add_points(uid, p)
    await m.answer(str(uid) + " -> +" + str(p) + " puan")


@router.message(Command("userinfo"))
async def cmd_uinfo(m: Message):
    if not is_admin(m.from_user.id):
        return
    a = m.text.split()
    if len(a) != 2:
        await m.answer("Kullanim: /userinfo <uid>")
        return
    try:
        uid = int(a[1])
    except Exception:
        return
    u = await db.get_user(uid)
    if not u:
        await m.answer("Yok.")
        return
    await m.answer(
        "ID: " + str(u[0]) + "\nUsername: @" + str(u[1]) + "\nAd: " + str(u[2]) +
        "\nBakiye: " + str(u[3]) + " TL\nPuan: " + str(u[4]) +
        "\nwas_member: " + str(u[6]) + "\nreset: " + str(u[7]) +
        "\nKayit: " + str(u[8])[:16])


@router.message(Command("totalusers"))
async def cmd_total(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("Toplam kullanici: " + str(await db.total_users()))


@router.message(Command("pendings"))
async def cmd_pend(m: Message):
    if not is_admin(m.from_user.id):
        return
    ps = await db.pending_requests()
    if not ps:
        await m.answer("Bekleyen talep yok.")
        return
    txt = "Bekleyen\n\n"
    for p in ps:
        txt += "#" + str(p[0]) + " | UID " + str(p[1]) + " | " + str(p[2]) + "\n"
    await m.answer(txt)


@router.callback_query(F.data.startswith("fill:"))
async def cb_fill(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    from handlers import Flow
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        await cb.answer("Talep aktif degil.", show_alert=True)
        return
    await state.set_state(Flow.fill_phone_code)
    await state.update_data(rid=rid)
    await cb.message.answer("#" + str(rid) + " icin format: +905551234567 123456")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        return
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        await cb.answer("Talep aktif degil.", show_alert=True)
        return
    await db.set_request_status(rid, "rejected")
    await db.update_balance(r[1], r[4])
    await db.add_tx(r[1], r[4], "refund", "#" + str(rid) + " reddedildi")
    try:
        await bot.send_message(r[1], "#" + str(rid) + " reddedildi. +" + str(r[4]) + " TL iade.")
    except Exception:
        pass
    await cb.message.edit_text("#" + str(rid) + " reddedildi (iade yapildi).")!= "pending":
        await cb.answer("Talep aktif degil.", show_alert=True); return
    await state.set_state(Flow.fill_phone_code)
    await state.update_data(rid=rid)
    await cb.message.answer(f"#{rid} icin format: +905551234567 123456")

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(cb: CallbackQuery, bot: types.Bot):
    if not is_admin(cb.from_user.id): return
    rid = int(cb.data.split(":")[1])
    r = await db.get_request(rid)
    if not r or r[5] != "pending":
        await cb.answer("Talep aktif degil.", show_alert=True); return
    await db.set_request_status(rid, "rejected")
    await db.update_balance(r[1], r[4])
    await db.add_tx(r[1], r[4], "refund", f"#{rid} reddedildi")
    try: await bot.send_message(r[1], f"#{rid} reddedildi. +{r[4]} TL iade.")
    except: pass
    await cb.message.edit_text(f"#{rid} reddedildi (iade yapildi).")
