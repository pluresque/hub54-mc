import json
import sqlite3
from pathlib import Path

import aiosqlite

from .config import (
    ADMIN_TELEGRAM_ID,
    DATABASE_PATH,
    INITIAL_WHITELIST_TELEGRAM_IDS,
)


async def initialize_database() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
                telegram_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS profiles (
                telegram_id INTEGER PRIMARY KEY,
                nickname TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                password_hash TEXT,
                last_login_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS profiles_nickname_unique
            ON profiles(nickname COLLATE NOCASE)
            WHERE nickname <> '';

            CREATE TABLE IF NOT EXISTS access_requests (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                handled_by INTEGER,
                handled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS notification_preferences (
                telegram_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('chat', 'join', 'leave', 'kill', 'death')),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, event_type)
            );

            CREATE TABLE IF NOT EXISTS minecraft_notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL CHECK(event_type IN ('chat', 'join', 'leave', 'kill', 'death')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS minecraft_notification_deliveries (
                notification_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                delivered_at TEXT,
                PRIMARY KEY (notification_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS minecraft_chat_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT
            );

            CREATE INDEX IF NOT EXISTS minecraft_chat_outbox_pending
            ON minecraft_chat_outbox(delivered_at, id);

            CREATE TABLE IF NOT EXISTS minecraft_private_message_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK(source IN ('minecraft', 'telegram')),
                sender_telegram_id INTEGER NOT NULL,
                sender_nickname TEXT NOT NULL,
                recipient_telegram_id INTEGER NOT NULL,
                recipient_nickname TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT
            );

            CREATE INDEX IF NOT EXISTS minecraft_private_message_pending
            ON minecraft_private_message_outbox(source, delivered_at, id);

            CREATE TABLE IF NOT EXISTS minecraft_private_message_bridge (
                bridge_name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS minecraft_private_message_context (
                telegram_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                minecraft_nickname TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, telegram_message_id)
            );

            CREATE TABLE IF NOT EXISTS minecraft_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'closed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS minecraft_poll_deliveries (
                poll_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (poll_id, telegram_id),
                FOREIGN KEY (poll_id) REFERENCES minecraft_polls(id)
            );

            CREATE TABLE IF NOT EXISTS minecraft_poll_votes (
                poll_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (poll_id, telegram_id),
                FOREIGN KEY (poll_id) REFERENCES minecraft_polls(id)
            );

            CREATE INDEX IF NOT EXISTS minecraft_poll_votes_poll
            ON minecraft_poll_votes(poll_id, option_index);
            """
        )
        cursor = await db.execute("PRAGMA table_info(profiles)")
        profile_columns = {row[1] for row in await cursor.fetchall()}
        if "password_hash" not in profile_columns:
            await db.execute("ALTER TABLE profiles ADD COLUMN password_hash TEXT")
        if "last_login_at" not in profile_columns:
            await db.execute("ALTER TABLE profiles ADD COLUMN last_login_at TEXT")
        await db.execute(
            """
            DELETE FROM minecraft_notification_deliveries
            WHERE notification_id IN (
                SELECT id FROM minecraft_notification_events
                WHERE created_at < datetime('now', '-7 days')
            )
            """
        )
        await db.execute(
            "DELETE FROM minecraft_notification_events "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        await db.execute(
            "DELETE FROM minecraft_chat_outbox "
            "WHERE created_at < datetime('now', '-1 day') "
            "OR (delivered_at IS NULL AND created_at < datetime('now', '-1 minute'))"
        )
        await db.execute(
            "DELETE FROM minecraft_private_message_context "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        await db.execute(
            "DELETE FROM minecraft_private_message_outbox "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA journal_mode = WAL")
        seeded_ids = INITIAL_WHITELIST_TELEGRAM_IDS | {ADMIN_TELEGRAM_ID}
        await db.executemany(
            "INSERT OR IGNORE INTO whitelist (telegram_id, added_by) VALUES (?, ?)",
            [(telegram_id, ADMIN_TELEGRAM_ID) for telegram_id in seeded_ids],
        )
        await db.commit()


async def create_poll(question: str, options: list[str], created_by: int) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO minecraft_polls (question, options_json, created_by)
            VALUES (?, ?, ?)
            """,
            (question, json.dumps(options, ensure_ascii=False), created_by),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def list_poll_recipient_ids() -> list[int]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT telegram_id FROM whitelist ORDER BY telegram_id"
        )
        return [int(row[0]) for row in await cursor.fetchall()]


async def save_poll_delivery(
    poll_id: int, telegram_id: int, message_id: int
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO minecraft_poll_deliveries
                (poll_id, telegram_id, message_id)
            VALUES (?, ?, ?)
            """,
            (poll_id, telegram_id, message_id),
        )
        await db.commit()


async def get_poll_results(poll_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT question, options_json, status
            FROM minecraft_polls WHERE id = ?
            """,
            (poll_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        options = json.loads(row[1])
        counts = [0] * len(options)
        cursor = await db.execute(
            """
            SELECT option_index, COUNT(*)
            FROM minecraft_poll_votes
            WHERE poll_id = ?
            GROUP BY option_index
            """,
            (poll_id,),
        )
        for option_index, count in await cursor.fetchall():
            if 0 <= option_index < len(counts):
                counts[option_index] = count

        return {
            "id": poll_id,
            "question": row[0],
            "options": options,
            "status": row[2],
            "counts": counts,
            "total_votes": sum(counts),
        }


async def cast_poll_vote(
    poll_id: int, telegram_id: int, option_index: int
) -> tuple[dict | None, bool]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT question, options_json, status
            FROM minecraft_polls WHERE id = ?
            """,
            (poll_id,),
        )
        poll = await cursor.fetchone()
        if poll is None or poll[2] != "active":
            await db.rollback()
            return None, False

        options = json.loads(poll[1])
        if option_index < 0 or option_index >= len(options):
            await db.rollback()
            return None, False

        cursor = await db.execute(
            """
            SELECT 1 FROM minecraft_poll_deliveries d
            INNER JOIN whitelist w ON w.telegram_id = d.telegram_id
            WHERE d.poll_id = ? AND d.telegram_id = ?
            """,
            (poll_id, telegram_id),
        )
        if await cursor.fetchone() is None:
            await db.rollback()
            return None, False

        cursor = await db.execute(
            """
            SELECT option_index FROM minecraft_poll_votes
            WHERE poll_id = ? AND telegram_id = ?
            """,
            (poll_id, telegram_id),
        )
        previous_vote = await cursor.fetchone()
        changed = previous_vote is None or previous_vote[0] != option_index
        if changed:
            await db.execute(
                """
                INSERT INTO minecraft_poll_votes
                    (poll_id, telegram_id, option_index)
                VALUES (?, ?, ?)
                ON CONFLICT(poll_id, telegram_id) DO UPDATE SET
                    option_index = excluded.option_index,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (poll_id, telegram_id, option_index),
            )

        cursor = await db.execute(
            """
            SELECT option_index, COUNT(*)
            FROM minecraft_poll_votes
            WHERE poll_id = ?
            GROUP BY option_index
            """,
            (poll_id,),
        )
        counts = [0] * len(options)
        for index, count in await cursor.fetchall():
            if 0 <= index < len(counts):
                counts[index] = count
        await db.commit()

        return {
            "id": poll_id,
            "question": poll[0],
            "options": options,
            "status": "active",
            "counts": counts,
            "total_votes": sum(counts),
        }, changed


async def close_poll(poll_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE minecraft_polls
            SET status = 'closed', closed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (poll_id,),
        )
        await db.commit()
    return await get_poll_results(poll_id)


async def list_poll_deliveries(poll_id: int) -> list[tuple[int, int]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT telegram_id, message_id
            FROM minecraft_poll_deliveries
            WHERE poll_id = ?
            ORDER BY telegram_id
            """,
            (poll_id,),
        )
        return [(int(row[0]), int(row[1])) for row in await cursor.fetchall()]


async def is_whitelisted(telegram_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM whitelist WHERE telegram_id = ?", (telegram_id,)
        )
        return await cursor.fetchone() is not None


async def get_nickname(telegram_id: int) -> str | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT nickname FROM profiles WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_account_profile(
    telegram_id: int,
) -> tuple[str, str | None, str | None] | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT nickname, password_hash, last_login_at
            FROM profiles WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return await cursor.fetchone()


async def nickname_is_taken(nickname: str, telegram_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1 FROM profiles
            WHERE nickname = ? COLLATE NOCASE
                AND telegram_id <> ?
                AND nickname <> ''
            LIMIT 1
            """,
            (nickname, telegram_id),
        )
        return await cursor.fetchone() is not None


async def save_nickname(telegram_id: int, nickname: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO profiles (telegram_id, nickname) VALUES (?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_id, nickname),
            )
        except sqlite3.IntegrityError:
            await db.rollback()
            return False
        await db.commit()
        return True


async def save_password_hash(telegram_id: int, password_hash: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE profiles
            SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
            """,
            (password_hash, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def enqueue_minecraft_chat_message(
    telegram_id: int, message: str
) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            INSERT INTO minecraft_chat_outbox (telegram_id, nickname, message)
            SELECT p.telegram_id, p.nickname, ?
            FROM profiles p
            INNER JOIN whitelist w ON w.telegram_id = p.telegram_id
            WHERE p.telegram_id = ? AND p.nickname <> ''
            """,
            (message, telegram_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return False

        cursor = await db.execute(
            "SELECT nickname FROM profiles WHERE telegram_id = ? AND nickname <> ''",
            (telegram_id,),
        )
        profile = await cursor.fetchone()
        if not profile:
            await db.rollback()
            return False

        cursor = await db.execute(
            "INSERT INTO minecraft_notification_events (event_type, message) "
            "VALUES ('chat', ?)",
            (f"{profile[0]}: {message}",),
        )
        notification_id = cursor.lastrowid
        await db.execute(
            """
            INSERT INTO minecraft_notification_deliveries
                (notification_id, telegram_id)
            SELECT ?, telegram_id FROM notification_preferences
            WHERE event_type = 'chat' AND enabled = 1 AND telegram_id <> ?
            """,
            (notification_id, telegram_id),
        )
        await db.execute(
            """
            DELETE FROM minecraft_notification_deliveries
            WHERE notification_id IN (
                SELECT id FROM minecraft_notification_events
                WHERE created_at < datetime('now', '-7 days')
            )
            """
        )
        await db.execute(
            "DELETE FROM minecraft_notification_events "
            "WHERE created_at < datetime('now', '-7 days')"
        )
        await db.commit()
        return True


async def enqueue_telegram_private_message(
    telegram_id: int, recipient_nickname: str, message: str
) -> str | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        sender_cursor = await db.execute(
            """
            SELECT p.nickname FROM profiles p
            INNER JOIN whitelist w ON w.telegram_id = p.telegram_id
            WHERE p.telegram_id = ? AND p.nickname <> ''
            """,
            (telegram_id,),
        )
        sender = await sender_cursor.fetchone()
        if not sender:
            await db.rollback()
            return None

        recipient_cursor = await db.execute(
            """
            SELECT p.telegram_id, p.nickname FROM profiles p
            INNER JOIN whitelist w ON w.telegram_id = p.telegram_id
            WHERE p.nickname = ? COLLATE NOCASE
              AND p.nickname <> ''
              AND p.password_hash IS NOT NULL
              AND p.password_hash <> ''
            LIMIT 1
            """,
            (recipient_nickname,),
        )
        recipient = await recipient_cursor.fetchone()
        if not recipient:
            await db.rollback()
            return None

        await db.execute(
            """
            INSERT INTO minecraft_private_message_outbox (
                source, sender_telegram_id, sender_nickname,
                recipient_telegram_id, recipient_nickname, message
            ) VALUES ('telegram', ?, ?, ?, ?, ?)
            """,
            (telegram_id, sender[0], recipient[0], recipient[1], message),
        )
        await db.commit()
        return recipient[1]


async def private_message_bridge_enabled() -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT enabled FROM minecraft_private_message_bridge
            WHERE bridge_name = 'private_messages'
            """
        )
        row = await cursor.fetchone()
        return bool(row and row[0])


async def get_private_message_reply_target(
    telegram_id: int, telegram_message_id: int
) -> str | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT minecraft_nickname FROM minecraft_private_message_context
            WHERE telegram_id = ? AND telegram_message_id = ?
              AND created_at >= datetime('now', '-7 days')
            """,
            (telegram_id, telegram_message_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_pending_minecraft_private_messages(
    limit: int = 100,
) -> list[tuple[int, int, str, str]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE minecraft_private_message_outbox
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE source = 'minecraft' AND delivered_at IS NULL AND (
                created_at < datetime('now', '-7 days')
                OR NOT EXISTS (
                    SELECT 1 FROM whitelist w
                    INNER JOIN profiles p ON p.telegram_id = w.telegram_id
                    WHERE w.telegram_id = recipient_telegram_id
                      AND p.nickname = recipient_nickname COLLATE NOCASE
                      AND p.nickname <> ''
                )
                OR NOT EXISTS (
                    SELECT 1 FROM whitelist w
                    INNER JOIN profiles p ON p.telegram_id = w.telegram_id
                    WHERE w.telegram_id = sender_telegram_id
                      AND p.nickname = sender_nickname COLLATE NOCASE
                      AND p.nickname <> ''
                )
            )
            """
        )
        cursor = await db.execute(
            """
            SELECT id, recipient_telegram_id, sender_nickname, message
            FROM minecraft_private_message_outbox
            WHERE source = 'minecraft' AND delivered_at IS NULL
            ORDER BY id LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await db.commit()
        return rows


async def mark_minecraft_private_message_delivered(
    message_id: int,
    recipient_telegram_id: int,
    telegram_message_id: int | None = None,
    reply_nickname: str | None = None,
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE minecraft_private_message_outbox
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE id = ? AND source = 'minecraft' AND delivered_at IS NULL
            """,
            (message_id,),
        )
        if telegram_message_id is not None and reply_nickname:
            await db.execute(
                """
                INSERT OR REPLACE INTO minecraft_private_message_context (
                    telegram_id, telegram_message_id, minecraft_nickname
                ) VALUES (?, ?, ?)
                """,
                (recipient_telegram_id, telegram_message_id, reply_nickname),
            )
        await db.commit()


NOTIFICATION_TYPES = ("chat", "join", "leave", "kill", "death")


async def get_notification_preferences(telegram_id: int) -> dict[str, bool]:
    preferences = {event_type: False for event_type in NOTIFICATION_TYPES}
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT event_type, enabled FROM notification_preferences "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        for event_type, enabled in await cursor.fetchall():
            if event_type in preferences:
                preferences[event_type] = bool(enabled)
    return preferences


async def set_notification_preference(
    telegram_id: int, event_type: str, enabled: bool
) -> None:
    if event_type not in NOTIFICATION_TYPES:
        raise ValueError("Неизвестный тип уведомления")

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO notification_preferences (telegram_id, event_type, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, event_type) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, event_type, int(enabled)),
        )
        if not enabled:
            await db.execute(
                """
                UPDATE minecraft_notification_deliveries
                SET delivered_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ? AND delivered_at IS NULL
                  AND notification_id IN (
                    SELECT id FROM minecraft_notification_events WHERE event_type = ?
                  )
                """,
                (telegram_id, event_type),
            )
        await db.commit()


async def get_pending_notification_deliveries(
    limit: int = 30,
) -> list[tuple[int, str, str, int]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT e.id, e.event_type, e.message, d.telegram_id
            FROM minecraft_notification_deliveries d
            INNER JOIN minecraft_notification_events e ON e.id = d.notification_id
            WHERE d.delivered_at IS NULL
            ORDER BY e.id, d.telegram_id
            LIMIT ?
            """,
            (limit,),
        )
        return await cursor.fetchall()


async def mark_notification_delivered(
    notification_id: int, telegram_id: int
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE minecraft_notification_deliveries
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE notification_id = ? AND telegram_id = ? AND delivered_at IS NULL
            """,
            (notification_id, telegram_id),
        )
        await db.commit()


async def create_access_request(
    telegram_id: int, username: str | None, full_name: str
) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT status FROM access_requests WHERE telegram_id = ?",
            (telegram_id,),
        )
        existing = await cursor.fetchone()
        if existing and existing[0] == "pending":
            await db.rollback()
            return False

        await db.execute(
            """
            INSERT INTO access_requests
                (telegram_id, username, full_name, status, requested_at, handled_by, handled_at)
            VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP, NULL, NULL)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                status = 'pending',
                requested_at = CURRENT_TIMESTAMP,
                handled_by = NULL,
                handled_at = NULL
            """,
            (telegram_id, username, full_name),
        )
        await db.commit()
        return True


async def list_pending_requests() -> list[tuple[int, str | None, str]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT telegram_id, username, full_name
            FROM access_requests
            WHERE status = 'pending'
            ORDER BY requested_at
            """
        )
        return await cursor.fetchall()


async def add_to_whitelist(telegram_id: int, added_by: int) -> tuple[bool, bool]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "INSERT OR IGNORE INTO whitelist (telegram_id, added_by) VALUES (?, ?)",
            (telegram_id, added_by),
        )
        added = cursor.rowcount == 1
        request_cursor = await db.execute(
            """
            UPDATE access_requests
            SET status = 'approved', handled_by = ?, handled_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ? AND status = 'pending'
            """,
            (added_by, telegram_id),
        )
        await db.commit()
        return added, request_cursor.rowcount == 1


async def remove_from_whitelist(telegram_id: int, removed_by: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM whitelist WHERE telegram_id = ?", (telegram_id,)
        )
        await db.execute("DELETE FROM profiles WHERE telegram_id = ?", (telegram_id,))
        await db.execute(
            "DELETE FROM notification_preferences WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.execute(
            "DELETE FROM minecraft_notification_deliveries WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.execute(
            """
            UPDATE access_requests
            SET status = 'denied', handled_by = ?, handled_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ? AND status = 'pending'
            """,
            (removed_by, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def resolve_access_request(
    telegram_id: int, approved: bool, handled_by: int
) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT status FROM access_requests WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] != "pending":
            await db.rollback()
            return False

        if approved:
            await db.execute(
                "INSERT OR IGNORE INTO whitelist (telegram_id, added_by) VALUES (?, ?)",
                (telegram_id, handled_by),
            )
        status = "approved" if approved else "denied"
        cursor = await db.execute(
            """
            UPDATE access_requests
            SET status = ?, handled_by = ?, handled_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ? AND status = 'pending'
            """,
            (status, handled_by, telegram_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return False
        await db.commit()
        return True


async def list_whitelisted_users() -> list[tuple[int, str | None]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT w.telegram_id, NULLIF(p.nickname, '')
            FROM whitelist w
            LEFT JOIN profiles p ON p.telegram_id = w.telegram_id
            ORDER BY w.added_at, w.telegram_id
            """
        )
        return await cursor.fetchall()
