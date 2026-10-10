# -*- coding: utf-8 -*-
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as B

def main_menu(is_admin=False):
    rows = [
        [B(text="\U0001F6D2 Market", callback_data="market", style="success")],
        [B(text="\U0001F4B0 Bakiye Yukle", callback_data="deposit", style="primary")],
        [B(text="\U0001F381 Promo Kod Kullan", callback_data="promo_use", style="success")],
        [B(text="\U0001F464 Profilim", callback_data="profile", style="primary"),
         B(text="\U0001F3AF Referans", callback_data="referral", style="success")],
        [B(text="\U0001F3C6 Liderlik Tablosu", callback_data="leaderboard", style="primary")],
        [B(text="\U0001F4DE Admin Iletisim", callback_data="contact_admin", style="danger")],
        [B(text="\u2139\uFE0F Yardim", callback_data="help", style="primary")],
    ]
    if is_admin:
        rows.append([B(text="\U0001F451 Admin Paneli", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def join_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="\U0001F4E2 Kanala Katil", url="https://t.me/MythAX", style="primary")],
        [B(text="\u2705 Katildim, Kontrol Et", callback_data="check_join", style="success")],
    ])

def market_kb(apps):
    rows = []
    row = []
    for key, name, emoji in apps:
        row.append(B(text=f"{emoji} {name}", callback_data=f"app:{key}", style="primary"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([B(text="\U0001F519 Geri", callback_data="back_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def country_kb(app_key, prices):
    rows = []
    for cc, label in [("+90", "TURKIYE (+90)"), ("+1", "USA (+1)")]:
        price = prices.get(cc, 0)
        rows.append([B(text=f"{label} - {price} TL", callback_data=f"buy:{app_key}:{cc}", style="success")])
    rows.append([B(text="\U0001F519 Geri", callback_data="market", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb(rid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="\u274C Iptal Et (Iade)", callback_data=f"cancel_req:{rid}", style="danger")],
    ])

def back_kb(target="back_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="\U0001F519 Geri", callback_data=target, style="primary")],
    ])

def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="\U0001F4CB Bekleyen Talepler", callback_data="adm_pendings", style="primary")],
        [B(text="\U0001F4B5 Fiyat Duzenle", callback_data="adm_prices", style="success")],
        [B(text="\U0001F4CA Istatistikler", callback_data="adm_stats", style="primary")],
        [B(text="\U0001F381 Promo Olustur", callback_data="adm_new_promo", style="success")],
        [B(text="\U0001F4C3 Aktif Promolar", callback_data="adm_list_promos", style="primary")],
        [B(text="\U0001F5D1 Promo Sil", callback_data="adm_del_promo", style="danger")],
        [B(text="\U0001F4E2 Duyuru Gonder", callback_data="adm_broadcast", style="primary")],
        [B(text="\U0001F519 Ana Menu", callback_data="back_menu", style="primary")],
    ])

def request_action_kb(rid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="\u270F Numara & Kod Gir", callback_data=f"fill:{rid}", style="success")],
        [B(text="\u274C Reddet (Iade)", callback_data=f"reject:{rid}", style="danger")],
    ])

def admin_prices_kb(apps):
    rows = []
    for key, name, emoji in apps:
        rows.append([B(text=f"{emoji} {name}", callback_data=f"editprices:{key}", style="primary")])
    rows.append([B(text="\U0001F519 Geri", callback_data="admin_panel", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def price_country_kb(app_key, prices):
    rows = []
    for cc, label in [("+90", "TR +90"), ("+1", "USA +1")]:
        rows.append([B(text=f"{label} - {prices.get(cc,0)} TL", callback_data=f"setprice:{app_key}:{cc}", style="success")])
    rows.append([B(text="\U0001F519 Geri", callback_data="adm_prices", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
