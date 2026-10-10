# -*- coding: utf-8 -*-
import aiosqlite
from config import DEFAULT_MARKET, COUNTRIES

DB = "bot.db"

async def init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT,
            balance INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            referrer_id INTEGER,
            was_member INTEGER DEFAULT 0,
            reset_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, amount INTEGER, type TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, app_name TEXT, country_code TEXT,
            price INTEGER, status TEXT DEFAULT 'pending',
            phone TEXT, code TEXT, admin_msg_ids TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS promos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE, max_uses INTEGER,
            current_uses INTEGER DEFAULT 0,
            remaining_hours INTEGER, bonus INTEGER,
            channel_message_id INTEGER, active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS promo_uses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER, user_id INTEGER,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS market(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE, name TEXT, emoji TEXT,
            active INTEGER DEFAULT 1)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS prices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_key TEXT, country TEXT, price INTEGER,
            UNIQUE(app_key, country))""")

        for key, name, emoji, price in DEFAULT_MARKET:
            await db.execute("INSERT OR IGNORE INTO market(key,name,emoji) VALUES(?,?,?)",
                             (key, name, emoji))
            for cc, _ in COUNTRIES:
                await db.execute("INSERT OR IGNORE INTO prices(app_key,country,price) VALUES(?,?,?)",
                                 (key, cc, price))
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            return await c.fetchone()

async def create_user(uid, username, first_name, ref=None):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id,username,first_name,referrer_id) VALUES(?,?,?,?)",
                         (uid, username, first_name, ref))
        await db.commit()

async def set_member_flag(uid, flag):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET was_member=? WHERE user_id=?", (flag, uid))
        await db.commit()

async def reset_user(uid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET balance=0, points=0, referrer_id=NULL, reset_count=reset_count+1 WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM transactions WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM promo_uses WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM requests WHERE user_id=?", (uid,))
        await db.commit()

async def update_balance(uid, amount):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
        await db.commit()

async def add_points(uid, p):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (p, uid))
        await db.commit()

async def add_tx(uid, amount, ttype, desc):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO transactions(user_id,amount,type,description) VALUES(?,?,?,?)",
                         (uid, amount, ttype, desc))
        await db.commit()

async def total_users():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            return (await c.fetchone())[0]

async def user_txs(uid, limit=8):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT amount,type,description,created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)) as c:
            return await c.fetchall()

async def all_user_ids():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            return [r[0] for r in await c.fetchall()]

async def top_referrers(limit=10):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT user_id, first_name, username, points FROM users WHERE points>0 ORDER BY points DESC LIMIT ?", (limit,)) as c:
            return await c.fetchall()

async def get_market():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT key,name,emoji FROM market WHERE active=1") as c:
            return await c.fetchall()

async def get_price(app_key, country):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT price FROM prices WHERE app_key=? AND country=?", (app_key, country)) as c:
            r = await c.fetchone()
            return r[0] if r else 0

async def set_price(app_key, country, price):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO prices(app_key,country,price) VALUES(?,?,?) ON CONFLICT(app_key,country) DO UPDATE SET price=excluded.price", (app_key, country, price))
        await db.commit()

async def create_request(uid, app_name, cc, price):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("INSERT INTO requests(user_id,app_name,country_code,price) VALUES(?,?,?,?)", (uid, app_name, cc, price))
        await db.commit()
        return cur.lastrowid

async def get_request(rid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM requests WHERE id=?", (rid,)) as c:
            return await c.fetchone()

async def set_request_admin_msgs(rid, ids):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE requests SET admin_msg_ids=? WHERE id=?", (",".join(map(str, ids)), rid))
        await db.commit()

async def complete_request(rid, phone, code):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE requests SET status='completed', phone=?, code=? WHERE id=?", (phone, code, rid))
        await db.commit()

async def set_request_status(rid, s):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE requests SET status=? WHERE id=?", (s, rid))
        await db.commit()

async def pending_requests():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM requests WHERE status='pending'") as c:
            return await c.fetchall()

async def user_active_request(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (uid,)) as c:
            return await c.fetchone()

async def create_promo(code, mx, hours, bonus, msg_id):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO promos(code,max_uses,remaining_hours,bonus,channel_message_id) VALUES(?,?,?,?,?)", (code, mx, hours, bonus, msg_id))
        await db.commit()

async def get_promo(code):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM promos WHERE code=? AND active=1", (code,)) as c:
            return await c.fetchone()

async def get_promo_by_id(pid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM promos WHERE id=?", (pid,)) as c:
            return await c.fetchone()

async def inc_promo_use(pid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE promos SET current_uses=current_uses+1 WHERE id=?", (pid,))
        await db.commit()

async def update_promo_hours(pid, h):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE promos SET remaining_hours=? WHERE id=?", (h, pid))
        await db.commit()

async def deactivate_promo(pid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE promos SET active=0 WHERE id=?", (pid,))
        await db.commit()

async def active_promos():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT * FROM promos WHERE active=1") as c:
            return await c.fetchall()

async def user_used_promo(pid, uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT 1 FROM promo_uses WHERE promo_id=? AND user_id=?", (pid, uid)) as c:
            return await c.fetchone() is not None

async def add_promo_use(pid, uid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO promo_uses(promo_id,user_id) VALUES(?,?)", (pid, uid))
        await db.commit()
