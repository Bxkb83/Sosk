import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from aiogram.types import User

import main


class BotLogicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = main.DB_PATH
        main.DB_PATH = self.temp_dir.name + "/test.db"
        await main.init_db()
        await main.ensure_user(
            User(id=101, is_bot=False, first_name="Test", username="tester")
        )

    async def asyncTearDown(self):
        main.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def add_balance(self, amount_cents):
        async with main.db_session() as db:
            await db.execute(
                "UPDATE users SET balance_cents = ? WHERE user_id = 101",
                (amount_cents,),
            )
            await db.commit()

    async def add_service(self, price_cents=1250):
        async with main.db_session() as db:
            cursor = await db.execute(
                """
                INSERT INTO services (name, description, price_cents, created_at)
                VALUES ('Test service', '', ?, ?)
                """,
                (price_cents, main.timestamp()),
            )
            await db.commit()
            return cursor.lastrowid

    async def test_money_parsing_and_formatting(self):
        self.assertEqual(main.parse_money("12,50"), 1250)
        self.assertEqual(main.format_money(1250), "12.50 TL")
        with self.assertRaises(ValueError):
            main.parse_money("-1")
        with self.assertRaises(ValueError):
            main.parse_money("1.001")

    async def test_forced_join_checks_member_status(self):
        old_channel = main.FORCE_JOIN_CHANNEL
        main.FORCE_JOIN_CHANNEL = "@public_test_channel"
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member", is_member=True)
            )
        )
        try:
            self.assertTrue(await main.check_channel_membership(bot, 101))
            bot.get_chat_member.return_value = SimpleNamespace(
                status="left",
                is_member=False,
            )
            self.assertFalse(await main.check_channel_membership(bot, 101))
        finally:
            main.FORCE_JOIN_CHANNEL = old_channel

    async def test_order_reserves_and_refunds_balance_once(self):
        service_id = await self.add_service()
        await self.add_balance(2000)

        status, order = await main.create_order(101, service_id)
        self.assertEqual(status, "created")
        self.assertEqual(order["amount_cents"], 1250)

        result, _ = await main.refund_order(order["order_id"], owner_id=101)
        self.assertEqual(result, "refunded")
        result, _ = await main.refund_order(order["order_id"], owner_id=101)
        self.assertEqual(result, "closed")

        async with main.db_session() as db:
            cursor = await db.execute(
                "SELECT balance_cents FROM users WHERE user_id = 101"
            )
            user = await cursor.fetchone()
        self.assertEqual(user["balance_cents"], 2000)

    async def test_order_rejects_insufficient_balance(self):
        service_id = await self.add_service()
        status, order = await main.create_order(101, service_id)
        self.assertEqual(status, "balance")
        self.assertIsNone(order)

    async def test_deposit_approval_is_applied_once(self):
        async with main.db_session() as db:
            cursor = await db.execute(
                """
                INSERT INTO deposit_requests (
                    user_id, amount_cents, reference, status, created_at
                ) VALUES (101, 5000, 'test-reference', 'pending', ?)
                """,
                (main.timestamp(),),
            )
            deposit_id = cursor.lastrowid
            await db.commit()

        result, _ = await main.review_deposit(deposit_id, approve=True)
        self.assertEqual(result, "approved")
        result, _ = await main.review_deposit(deposit_id, approve=True)
        self.assertEqual(result, "closed")

        async with main.db_session() as db:
            cursor = await db.execute(
                "SELECT balance_cents FROM users WHERE user_id = 101"
            )
            user = await cursor.fetchone()
        self.assertEqual(user["balance_cents"], 5000)

    async def test_promo_can_only_be_claimed_once_per_user(self):
        expires_at = (
            main.now_utc() + timedelta(hours=1)
        ).isoformat(timespec="seconds")
        async with main.db_session() as db:
            await db.execute(
                """
                INSERT INTO promos (
                    code, bonus_cents, max_uses, expires_at, created_at
                ) VALUES ('TEST10', 1000, 10, ?, ?)
                """,
                (expires_at, main.timestamp()),
            )
            await db.commit()

        result, amount = await main.claim_promo(101, "TEST10")
        self.assertEqual((result, amount), ("claimed", 1000))
        result, amount = await main.claim_promo(101, "TEST10")
        self.assertEqual((result, amount), ("already_used", 0))


if __name__ == "__main__":
    unittest.main()
