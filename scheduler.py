# -*- coding: utf-8 -*-
import asyncio
from aiogram import Bot
from config import CHANNEL
import database as db

def promo_text(p):
    _, code, mx, cur, hours, bonus, _, _, _ = p
    return (f"PROMO KODU: {code}\n\n"
            f"Odul: {bonus} TL\nKullanim: {cur}/{mx}\n"
            f"Kalan Sure: {hours} saat\n\n"
            "Botta Promo Kod Kullan butonundan kullan.")

async def hourly_tick(bot: Bot):
    while True:
        try:
            await asyncio.sleep(3600)
            for p in await db.active_promos():
                pid = p[0]
                new_h = p[4] - 1
                if new_h <= 0:
                    try: await bot.delete_message(CHANNEL, p[6])
                    except: pass
                    await db.deactivate_promo(pid); continue
                await db.update_promo_hours(pid, new_h)
                p2 = await db.get_promo_by_id(pid)
                try:
                    await bot.edit_message_text(chat_id=CHANNEL, message_id=p2[6], text=promo_text(p2))
                except: pass
        except Exception as e:
            print("[scheduler]", e)

async def start_scheduler(bot: Bot):
    asyncio.create_task(hourly_tick(bot))
