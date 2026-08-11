# -*- coding: utf-8 -*-

import os
import sqlite3
import random
import asyncio
import logging
from datetime import datetime
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("8661672146:AAExFkeuEXxQhmLvmI5EsZtniKzDj-l6HhI")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set.")

DB_FILE = "roulette.db"

MAX_ROULETTE_LEVEL = 5
CHAMBER_SIZE = 6

DEFAULT_MIN_PLAYERS = 2
DEFAULT_MAX_PLAYERS = 20
DEFAULT_TURN_SECONDS = 30

XP_WIN = 100
XP_SURVIVE = 15
XP_ELIMINATED = 10

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False,
)

db.row_factory = sqlite3.Row

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    games INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_admin (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    user_id INTEGER UNIQUE,
    username TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS group_settings (
    chat_id INTEGER PRIMARY KEY,
    min_players INTEGER DEFAULT 2,
    max_players INTEGER DEFAULT 20,
    turn_seconds INTEGER DEFAULT 30,
    enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS games (
    chat_id INTEGER PRIMARY KEY,
    creator_id INTEGER,
    status TEXT DEFAULT 'lobby',
    level INTEGER DEFAULT 1,
    current_index INTEGER DEFAULT 0,
    message_id INTEGER,
    turn_token INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS game_players (
    chat_id INTEGER,
    user_id INTEGER,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    alive INTEGER DEFAULT 1,
    joined_at TEXT,
    PRIMARY KEY(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS game_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    winner_id INTEGER,
    winner_name TEXT,
    players_count INTEGER,
    created_at TEXT
);
""")

db.commit()

# ============================================================
# LOCK
# ============================================================

DB_LOCK = asyncio.Lock()

# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.utcnow().isoformat()


def execute(sql, params=()):
    with closing(db.cursor()) as cur:
        cur.execute(sql, params)
        result = cur.fetchall()
        db.commit()
        return result


def execute_one(sql, params=()):
    with closing(db.cursor()) as cur:
        cur.execute(sql, params)
        result = cur.fetchone()
        db.commit()
        return result


def ensure_user(user):
    row = execute_one(
        "SELECT * FROM users WHERE user_id=?",
        (user.id,),
    )

    if row is None:
        execute(
            """
            INSERT INTO users
            (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                utc_now(),
            ),
        )
    else:
        execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id,
            ),
        )

    return execute_one(
        "SELECT * FROM users WHERE user_id=?",
        (user.id,),
    )


def add_xp(user_id, amount):
    row = execute_one(
        "SELECT xp FROM users WHERE user_id=?",
        (user_id,),
    )

    if not row:
        return

    xp = row["xp"] + amount
    level = max(1, (xp // 100) + 1)

    execute(
        """
        UPDATE users
        SET xp=?, level=?
        WHERE user_id=?
        """,
        (xp, level, user_id),
    )


def get_admin():
    return execute_one(
        "SELECT * FROM bot_admin WHERE id=1"
    )


def is_admin(user_id):
    admin = get_admin()
    return bool(admin and admin["user_id"] == user_id)


def claim_admin(user):
    admin = get_admin()

    if admin:
        return False

    execute(
        """
        INSERT INTO bot_admin
        (id, user_id, username, created_at)
        VALUES (1, ?, ?, ?)
        """,
        (
            user.id,
            user.username or "",
            utc_now(),
        ),
    )

    return True


def get_settings(chat_id):
    row = execute_one(
        "SELECT * FROM group_settings WHERE chat_id=?",
        (chat_id,),
    )

    if row is None:
        execute(
            """
            INSERT INTO group_settings
            (chat_id, min_players, max_players, turn_seconds)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                DEFAULT_MIN_PLAYERS,
                DEFAULT_MAX_PLAYERS,
                DEFAULT_TURN_SECONDS,
            ),
        )

    return execute_one(
        "SELECT * FROM group_settings WHERE chat_id=?",
        (chat_id,),
    )


def get_game(chat_id):
    return execute_one(
        "SELECT * FROM games WHERE chat_id=?",
        (chat_id,),
    )


def get_players(chat_id, alive_only=False):
    if alive_only:
        return execute(
            """
            SELECT *
            FROM game_players
            WHERE chat_id=? AND alive=1
            ORDER BY joined_at ASC
            """,
            (chat_id,),
        )

    return execute(
        """
        SELECT *
        FROM game_players
        WHERE chat_id=?
        ORDER BY joined_at ASC
        """,
        (chat_id,),
    )


def get_player(chat_id, user_id):
    return execute_one(
        """
        SELECT *
        FROM game_players
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, user_id),
    )


def is_joined(chat_id, user_id):
    return get_player(chat_id, user_id) is not None


def is_alive(chat_id, user_id):
    player = get_player(chat_id, user_id)
    return bool(player and player["alive"] == 1)


def alive_count(chat_id):
    row = execute_one(
        """
        SELECT COUNT(*) AS c
        FROM game_players
        WHERE chat_id=? AND alive=1
        """,
        (chat_id,),
    )
    return row["c"]


def game_exists(chat_id):
    return get_game(chat_id) is not None


def game_status(chat_id):
    game = get_game(chat_id)
    return game["status"] if game else None


def safe_name(row):
    if not row:
        return "بازیکن"

    return (
        row["first_name"]
        or row["username"]
        or "بازیکن"
    )


# ============================================================
# POPUP
# ============================================================

async def popup(query, text):
    try:
        await query.answer(
            text=text,
            show_alert=True,
        )
    except Exception:
        pass


# ============================================================
# KEYBOARDS
# ============================================================

def lobby_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎮 جوین بازی",
                callback_data="join",
            ),
            InlineKeyboardButton(
                "👥 بازیکنان",
                callback_data="players",
            ),
        ],
        [
            InlineKeyboardButton(
                "▶️ شروع بازی",
                callback_data="start",
            ),
            InlineKeyboardButton(
                "🛑 لغو بازی",
                callback_data="cancel",
            ),
        ],
    ])


def playing_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔫 شلیک",
                callback_data="shoot",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 وضعیت",
                callback_data="status",
            ),
        ],
    ])


def finished_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 بازی دوباره",
                callback_data="rematch",
            ),
            InlineKeyboardButton(
                "🏆 رتبه‌بندی",
                callback_data="leaderboard",
            ),
        ],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⚙️ تنظیمات",
                callback_data="admin_settings",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار بات",
                callback_data="admin_stats",
            ),
            InlineKeyboardButton(
                "🏆 رتبه‌بندی",
                callback_data="leaderboard",
            ),
        ],
        [
            InlineKeyboardButton(
                "🧹 پاکسازی",
                callback_data="admin_cleanup",
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ بستن",
                callback_data="admin_close",
            ),
        ],
    ])


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➖ حداقل",
                callback_data="min_down",
            ),
            InlineKeyboardButton(
                "➕ حداقل",
                callback_data="min_up",
            ),
        ],
        [
            InlineKeyboardButton(
                "➖ حداکثر",
                callback_data="max_down",
            ),
            InlineKeyboardButton(
                "➕ حداکثر",
                callback_data="max_up",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏱️ -5 ثانیه",
                callback_data="time_down",
            ),
            InlineKeyboardButton(
                "⏱️ +5 ثانیه",
                callback_data="time_up",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="admin_back",
            ),
        ],
    ])


# ============================================================
# TEXT
# ============================================================

def lobby_text(chat_id):
    settings = get_settings(chat_id)
    players = get_players(chat_id)

    lines = [
        "🎰 <b>رولت روسی — لابی بازی</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"👥 بازیکنان: <b>{len(players)}</b> / <b>{settings['max_players']}</b>",
        f"🎯 حداقل لازم: <b>{settings['min_players']}</b>",
        f"⏱️ زمان نوبت: <b>{settings['turn_seconds']} ثانیه</b>",
        "",
        "🔫 <b>سطح‌های رولت</b>",
        "▫️ Level 1 → 1 تیر مجازی",
        "▫️ Level 2 → 2 تیر مجازی",
        "▫️ Level 3 → 3 تیر مجازی",
        "▫️ Level 4 → 4 تیر مجازی",
        "▫️ Level 5 → 5 تیر مجازی",
        "",
        "🔁 بعد از Level 5 دوباره از Level 1 شروع می‌شود.",
        "",
        "🎮 برای ورود روی دکمه «جوین بازی» بزنید.",
    ]

    if players:
        lines.extend([
            "",
            "👥 <b>بازیکنان حاضر:</b>",
        ])

        for i, player in enumerate(players, 1):
            lines.append(
                f"{i}. 🟢 {safe_name(player)}"
            )

    return "\n".join(lines)


def playing_text(chat_id, prefix=None):
    game = get_game(chat_id)

    if not game:
        return "❌ بازی وجود ندارد."

    players = get_players(chat_id, alive_only=True)

    if not players:
        return "❌ بازیکنی باقی نمانده."

    index = game["current_index"]

    if index >= len(players):
        index = 0

    current = players[index]

    level = game["level"]

    lines = []

    if prefix:
        lines.extend([
            prefix,
            "",
        ])

    lines.extend([
        "🎰 <b>رولت روسی</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"🔥 مرحله رولت: <b>Level {level}</b>",
        f"🔫 تیرهای مجازی: <b>{level}</b>",
        "",
        f"🎯 نوبت: <b>{safe_name(current)}</b>",
        "",
        f"🟢 بازیکنان باقی‌مانده: <b>{len(players)}</b>",
        "",
        "⚠️ فقط بازیکن صاحب نوبت می‌تواند دکمه شلیک را استفاده کند.",
    ])

    return "\n".join(lines)


def settings_text(chat_id):
    settings = get_settings(chat_id)

    return (
        "⚙️ <b>تنظیمات رولت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 حداقل بازیکن: <b>{settings['min_players']}</b>\n"
        f"👥 حداکثر بازیکن: <b>{settings['max_players']}</b>\n"
        f"⏱️ زمان هر نوبت: <b>{settings['turn_seconds']} ثانیه</b>\n\n"
        "تنظیم موردنظر را انتخاب کنید."
    )


# ============================================================
# MESSAGE MANAGEMENT
# ============================================================

async def update_game_message(
    context,
    chat_id,
    text,
    keyboard,
):
    game = get_game(chat_id)

    if not game or not game["message_id"]:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game["message_id"],
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.warning(
            "Could not edit game message: %s",
            exc,
        )


# ============================================================
# /ROULETTE
# ============================================================

async def roulette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "❌ این بازی فقط داخل گروه قابل اجراست."
        )
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    ensure_user(user)
    get_settings(chat_id)

    existing = get_game(chat_id)

    if existing and existing["status"] in (
        "lobby",
        "playing",
    ):
        await update.message.reply_text(
            "⚠️ یک بازی در این گروه در حال اجراست."
        )
        return

    if existing:
        execute(
            "DELETE FROM games WHERE chat_id=?",
            (chat_id,),
        )

    execute(
        "DELETE FROM game_players WHERE chat_id=?",
        (chat_id,),
    )

    execute(
        """
        INSERT INTO games
        (
            chat_id,
            creator_id,
            status,
            level,
            current_index,
            message_id,
            turn_token,
            created_at
        )
        VALUES (?, ?, 'lobby', 1, 0, NULL, 0, ?)
        """,
        (
            chat_id,
            user.id,
            utc_now(),
        ),
    )

    message = await update.message.reply_text(
        lobby_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=lobby_keyboard(),
    )

    execute(
        """
        UPDATE games
        SET message_id=?
        WHERE chat_id=?
        """,
        (
            message.message_id,
            chat_id,
        ),
    )


# ============================================================
# JOIN
# ============================================================

async def join_game(query):
    chat_id = query.message.chat.id
    user = query.from_user

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    if game["status"] != "lobby":
        await popup(
            query,
            "❌ زمان جوین کردن تمام شده.",
        )
        return

    settings = get_settings(chat_id)
    players = get_players(chat_id)

    if is_joined(chat_id, user.id):
        await popup(
            query,
            "ℹ️ شما قبلاً جوین شده‌اید.",
        )
        return

    if len(players) >= settings["max_players"]:
        await popup(
            query,
            "❌ ظرفیت بازی تکمیل شده.",
        )
        return

    ensure_user(user)

    execute(
        """
        INSERT INTO game_players
        (
            chat_id,
            user_id,
            username,
            first_name,
            alive,
            joined_at
        )
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        (
            chat_id,
            user.id,
            user.username or "",
            user.first_name or "",
            utc_now(),
        ),
    )

    await popup(
        query,
        "✅ با موفقیت وارد بازی شدید!",
    )

    await query.message.edit_text(
        lobby_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=lobby_keyboard(),
    )


# ============================================================
# PLAYERS
# ============================================================

async def show_players(query):
    chat_id = query.message.chat.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    players = get_players(chat_id)

    if not players:
        await popup(
            query,
            "👥 هنوز کسی جوین نشده.",
        )
        return

    lines = [
        "👥 <b>بازیکنان</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for i, player in enumerate(players, 1):
        if player["alive"]:
            icon = "🟢"
            state = "زنده"
        else:
            icon = "☠️"
            state = "حذف‌شده"

        lines.append(
            f"{i}. {icon} <b>{safe_name(player)}</b> — {state}"
        )

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

    await query.answer()


# ============================================================
# START
# ============================================================

async def start_game(query, context):
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    if game["status"] != "lobby":
        await popup(
            query,
            "❌ بازی قبلاً شروع شده.",
        )
        return

    if (
        game["creator_id"] != user_id
        and not is_admin(user_id)
    ):
        await popup(
            query,
            "❌ فقط سازنده بازی یا ادمین می‌تواند شروع کند.",
        )
        return

    settings = get_settings(chat_id)
    players = get_players(chat_id)

    if len(players) < settings["min_players"]:
        await popup(
            query,
            f"❌ حداقل {settings['min_players']} بازیکن لازم است.",
        )
        return

    execute(
        """
        UPDATE games
        SET status='playing',
            level=1,
            current_index=0,
            turn_token=turn_token+1
        WHERE chat_id=?
        """,
        (chat_id,),
    )

    await query.message.edit_text(
        playing_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=playing_keyboard(),
    )

    await query.answer("🔥 بازی شروع شد!")

    await start_timer(chat_id, context)


# ============================================================
# TIMER
# ============================================================

async def start_timer(chat_id, context):
    game = get_game(chat_id)

    if not game or game["status"] != "playing":
        return

    token = game["turn_token"]
    settings = get_settings(chat_id)

    await asyncio.sleep(
        settings["turn_seconds"]
    )

    current = get_game(chat_id)

    if not current:
        return

    if current["status"] != "playing":
        return

    if current["turn_token"] != token:
        return

    players = get_players(
        chat_id,
        alive_only=True,
    )

    if len(players) <= 1:
        return

    index = current["current_index"]

    if index >= len(players):
        index = 0

    player = players[index]

    execute(
        """
        UPDATE game_players
        SET alive=0
        WHERE chat_id=? AND user_id=?
        """,
        (
            chat_id,
            player["user_id"],
        ),
    )

    execute(
        """
        UPDATE users
        SET losses=losses+1,
            games=games+1
        WHERE user_id=?
        """,
        (player["user_id"],),
    )

    add_xp(
        player["user_id"],
        XP_ELIMINATED,
    )

    await advance_after_elimination(
        chat_id,
        context,
        (
            f"⏱️ <b>{safe_name(player)}</b>\n"
            "زمان نوبتش تمام شد و از بازی حذف شد. ☠️"
        ),
    )


# ============================================================
# SHOOT
# ============================================================

async def shoot(query, context):
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    if game["status"] != "playing":
        await popup(
            query,
            "❌ بازی در حال اجرا نیست.",
        )
        return

    player = get_player(
        chat_id,
        user_id,
    )

    if not player:
        await popup(
            query,
            "❌ شما در این بازی جوین نشده‌اید.",
        )
        return

    if not player["alive"]:
        await popup(
            query,
            "☠️ شما حذف شده‌اید و دیگر دسترسی ندارید.",
        )
        return

    players = get_players(
        chat_id,
        alive_only=True,
    )

    if not players:
        await popup(
            query,
            "❌ بازیکنی باقی نمانده.",
        )
        return

    index = game["current_index"]

    if index >= len(players):
        index = 0

    current = players[index]

    if current["user_id"] != user_id:
        await popup(
            query,
            "⏳ الان نوبت شما نیست.",
        )
        return

    level = max(
        1,
        min(
            game["level"],
            MAX_ROULETTE_LEVEL,
        ),
    )

    chamber = [False] * CHAMBER_SIZE

    for i in range(level):
        chamber[i] = True

    random.shuffle(chamber)

    fired = random.choice(chamber)

    if fired:
        execute(
            """
            UPDATE game_players
            SET alive=0
            WHERE chat_id=? AND user_id=?
            """,
            (
                chat_id,
                user_id,
            ),
        )

        execute(
            """
            UPDATE users
            SET losses=losses+1,
                games=games+1
            WHERE user_id=?
            """,
            (user_id,),
        )

        add_xp(
            user_id,
            XP_ELIMINATED,
        )

        await popup(
            query,
            "💥 حذف شدید!",
        )

        await advance_after_elimination(
            chat_id,
            context,
            (
                f"💥 <b>{safe_name(current)}</b>\n"
                "از بازی حذف شد! ☠️"
            ),
        )

    else:
        add_xp(
            user_id,
            XP_SURVIVE,
        )

        await popup(
            query,
            "😮 زنده ماندید!",
        )

        await advance_turn(
            chat_id,
            context,
            (
                f"😮 <b>{safe_name(current)}</b>\n"
                "زنده ماند!"
            ),
        )


# ============================================================
# ADVANCE TURN
# ============================================================

async def advance_turn(chat_id, context, prefix=None):
    game = get_game(chat_id)

    if not game:
        return

    players = get_players(
        chat_id,
        alive_only=True,
    )

    if len(players) <= 1:
        await finish_game(
            chat_id,
            context,
            prefix,
        )
        return

    index = game["current_index"] + 1

    new_level = game["level"]

    if index >= len(players):
        index = 0
        new_level += 1

        if new_level > MAX_ROULETTE_LEVEL:
            new_level = 1

    execute(
        """
        UPDATE games
        SET level=?,
            current_index=?,
            turn_token=turn_token+1
        WHERE chat_id=?
        """,
        (
            new_level,
            index,
            chat_id,
        ),
    )

    text = playing_text(
        chat_id,
        prefix,
    )

    await update_game_message(
        context,
        chat_id,
        text,
        playing_keyboard(),
    )

    asyncio.create_task(
        start_timer(
            chat_id,
            context,
        )
    )


# ============================================================
# ADVANCE AFTER ELIMINATION
# ============================================================

async def advance_after_elimination(
    chat_id,
    context,
    prefix=None,
):
    game = get_game(chat_id)

    if not game:
        return

    players = get_players(
        chat_id,
        alive_only=True,
    )

    if len(players) <= 1:
        await finish_game(
            chat_id,
            context,
            prefix,
        )
        return

    index = game["current_index"]

    if index >= len(players):
        index = 0

    new_level = game["level"]

    execute(
        """
        UPDATE games
        SET current_index=?,
            level=?,
            turn_token=turn_token+1
        WHERE chat_id=?
        """,
        (
            index,
            new_level,
            chat_id,
        ),
    )

    text = playing_text(
        chat_id,
        prefix,
    )

    await update_game_message(
        context,
        chat_id,
        text,
        playing_keyboard(),
    )

    asyncio.create_task(
        start_timer(
            chat_id,
            context,
        )
    )


# ============================================================
# FINISH
# ============================================================

async def finish_game(
    chat_id,
    context,
    prefix=None,
):
    players = get_players(
        chat_id,
        alive_only=True,
    )

    all_players = get_players(
        chat_id,
        alive_only=False,
    )

    winner = players[0] if players else None

    if winner:
        execute(
            """
            UPDATE users
            SET wins=wins+1,
                games=games+1
            WHERE user_id=?
            """,
            (winner["user_id"],),
        )

        add_xp(
            winner["user_id"],
            XP_WIN,
        )

        execute(
            """
            INSERT INTO game_history
            (
                chat_id,
                winner_id,
                winner_name,
                players_count,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                winner["user_id"],
                safe_name(winner),
                len(all_players),
                utc_now(),
            ),
        )

    execute(
        """
        UPDATE games
        SET status='finished',
            turn_token=turn_token+1
        WHERE chat_id=?
        """,
        (chat_id,),
    )

    if winner:
        text = (
            "🏆 <b>بازی تمام شد!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👑 برنده:\n"
            f"<b>{safe_name(winner)}</b>\n\n"
            f"⭐ +{XP_WIN} XP\n"
            "🎉 تبریک!"
        )
    else:
        text = (
            "🏆 <b>بازی تمام شد!</b>\n\n"
            "❌ هیچ بازیکنی باقی نماند."
        )

    if prefix:
        text = prefix + "\n\n" + text

    await update_game_message(
        context,
        chat_id,
        text,
        finished_keyboard(),
    )


# ============================================================
# STATUS
# ============================================================

async def show_status(query):
    chat_id = query.message.chat.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    all_players = get_players(
        chat_id,
        alive_only=False,
    )

    alive = get_players(
        chat_id,
        alive_only=True,
    )

    eliminated = len(all_players) - len(alive)

    text = (
        "📊 <b>وضعیت بازی</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Level: <b>{game['level']}</b>\n"
        f"👥 کل بازیکنان: <b>{len(all_players)}</b>\n"
        f"🟢 زنده: <b>{len(alive)}</b>\n"
        f"☠️ حذف‌شده: <b>{eliminated}</b>\n"
        f"🎮 وضعیت: <b>{game['status']}</b>"
    )

    await query.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )

    await query.answer()


# ============================================================
# REMATCH
# ============================================================

async def rematch(query):
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    if game["status"] != "finished":
        await popup(
            query,
            "❌ بازی هنوز تمام نشده.",
        )
        return

    if (
        not is_joined(chat_id, user_id)
        and not is_admin(user_id)
    ):
        await popup(
            query,
            "❌ شما در بازی قبلی حضور نداشتید.",
        )
        return

    execute(
        """
        UPDATE games
        SET creator_id=?,
            status='lobby',
            level=1,
            current_index=0,
            turn_token=turn_token+1
        WHERE chat_id=?
        """,
        (
            user_id,
            chat_id,
        ),
    )

    execute(
        """
        DELETE FROM game_players
        WHERE chat_id=?
        """,
        (chat_id,),
    )

    await query.message.edit_text(
        lobby_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=lobby_keyboard(),
    )

    await query.answer(
        "🔄 بازی جدید ساخته شد."
    )


# ============================================================
# PROFILE
# ============================================================

async def profile(update, context):
    if not update.effective_user:
        return

    user = update.effective_user
    row = ensure_user(user)

    text = (
        "👤 <b>پروفایل</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 نام: <b>{user.first_name}</b>\n"
        f"⭐ Level: <b>{row['level']}</b>\n"
        f"✨ XP: <b>{row['xp']}</b>\n\n"
        f"🏆 برد: <b>{row['wins']}</b>\n"
        f"☠️ باخت: <b>{row['losses']}</b>\n"
        f"🎮 بازی: <b>{row['games']}</b>"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# LEADERBOARD
# ============================================================

async def leaderboard(query=None, update=None):
    rows = execute(
        """
        SELECT *
        FROM users
        ORDER BY wins DESC, xp DESC, level DESC
        LIMIT 10
        """
    )

    lines = [
        "🏆 <b>TOP 10</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    medals = [
        "🥇",
        "🥈",
        "🥉",
    ]

    if not rows:
        lines.append(
            "هنوز آماری ثبت نشده."
        )
    else:
        for i, row in enumerate(rows):
            prefix = (
                medals[i]
                if i < 3
                else f"{i + 1}."
            )

            name = (
                row["first_name"]
                or row["username"]
                or "Unknown"
            )

            lines.extend([
                f"{prefix} <b>{name}</b>",
                (
                    f"   ⭐ Level {row['level']} | "
                    f"🏆 {row['wins']} برد | "
                    f"✨ {row['xp']} XP"
                ),
                "",
            ])

    text = "\n".join(lines)

    if query:
        await query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
        )
        await query.answer()

    elif update:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# PANEL
# ============================================================

async def panel(update, context):
    if not update.effective_user:
        return

    user = update.effective_user
    ensure_user(user)

    admin = get_admin()

    if admin is None:
        claim_admin(user)

        await update.message.reply_text(
            "👑 <b>پنل فعال شد</b>\n\n"
            "شما اولین نفری بودید که پنل را فعال کردید.\n"
            "از این لحظه ادمین دائمی بات هستید.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )
        return

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ شما ادمین نیستید."
        )
        return

    await update.message.reply_text(
        "👑 <b>پنل مدیریت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN SETTINGS
# ============================================================

async def admin_settings(query):
    if not is_admin(query.from_user.id):
        await popup(
            query,
            "❌ شما ادمین نیستید.",
        )
        return

    chat_id = query.message.chat.id

    await query.message.edit_text(
        settings_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )

    await query.answer()


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(query):
    if not is_admin(query.from_user.id):
        await popup(
            query,
            "❌ شما ادمین نیستید.",
        )
        return

    users = execute_one(
        "SELECT COUNT(*) AS c FROM users"
    )["c"]

    games = execute_one(
        "SELECT COUNT(*) AS c FROM game_history"
    )["c"]

    active = execute_one(
        """
        SELECT COUNT(*) AS c
        FROM games
        WHERE status='playing'
        """
    )["c"]

    groups = execute_one(
        """
        SELECT COUNT(*) AS c
        FROM group_settings
        """
    )["c"]

    text = (
        "📊 <b>آمار بات</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 کاربران: <b>{users}</b>\n"
        f"🎮 بازی‌های تمام‌شده: <b>{games}</b>\n"
        f"🔥 بازی‌های فعال: <b>{active}</b>\n"
        f"👥 گروه‌های ثبت‌شده: <b>{groups}</b>"
    )

    await query.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )

    await query.answer()


# ============================================================
# ADMIN CLEANUP
# ============================================================

async def admin_cleanup(query):
    if not is_admin(query.from_user.id):
        await popup(
            query,
            "❌ شما ادمین نیستید.",
        )
        return

    execute(
        """
        DELETE FROM games
        WHERE status='finished'
        """
    )

    execute(
        """
        DELETE FROM game_players
        WHERE chat_id NOT IN (
            SELECT chat_id FROM games
        )
        """
    )

    await popup(
        query,
        "✅ اطلاعات بازی‌های تمام‌شده پاک شد.",
    )


# ============================================================
# SETTINGS
# ============================================================

async def change_setting(query, action):
    if not is_admin(query.from_user.id):
        await popup(
            query,
            "❌ شما ادمین نیستید.",
        )
        return

    chat_id = query.message.chat.id
    settings = get_settings(chat_id)

    minimum = settings["min_players"]
    maximum = settings["max_players"]
    seconds = settings["turn_seconds"]

    if action == "min_up":
        minimum = min(
            minimum + 1,
            20,
        )

    elif action == "min_down":
        minimum = max(
            minimum - 1,
            2,
        )

    elif action == "max_up":
        maximum = min(
            maximum + 1,
            50,
        )

    elif action == "max_down":
        maximum = max(
            maximum - 1,
            2,
        )

    elif action == "time_up":
        seconds = min(
            seconds + 5,
            300,
        )

    elif action == "time_down":
        seconds = max(
            seconds - 5,
            5,
        )

    if maximum < minimum:
        maximum = minimum

    execute(
        """
        UPDATE group_settings
        SET min_players=?,
            max_players=?,
            turn_seconds=?
        WHERE chat_id=?
        """,
        (
            minimum,
            maximum,
            seconds,
            chat_id,
        ),
    )

    await query.message.edit_text(
        settings_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(),
    )

    await query.answer(
        "✅ تنظیمات تغییر کرد."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(query):
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    game = get_game(chat_id)

    if not game:
        await popup(
            query,
            "❌ بازی وجود ندارد.",
        )
        return

    if game["status"] == "playing":
        if (
            game["creator_id"] != user_id
            and not is_admin(user_id)
        ):
            await popup(
                query,
                "❌ بازی شروع شده و شما اجازه لغو ندارید.",
            )
            return

    elif game["status"] == "lobby":
        if (
            game["creator_id"] != user_id
            and not is_admin(user_id)
        ):
            await popup(
                query,
                "❌ فقط سازنده بازی یا ادمین می‌تواند لغو کند.",
            )
            return

    else:
        await popup(
            query,
            "❌ این بازی قابل لغو نیست.",
        )
        return

    execute(
        "DELETE FROM game_players WHERE chat_id=?",
        (chat_id,),
    )

    execute(
        "DELETE FROM games WHERE chat_id=?",
        (chat_id,),
    )

    await query.message.edit_text(
        "🛑 <b>بازی لغو شد.</b>",
        parse_mode=ParseMode.HTML,
    )

    await query.answer(
        "بازی لغو شد."
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):
    query = update.callback_query

    if not query:
        return

    data = query.data

    try:
        if data == "join":
            await join_game(query)

        elif data == "players":
            await show_players(query)

        elif data == "start":
            await start_game(
                query,
                context,
            )

        elif data == "cancel":
            await cancel_game(query)

        elif data == "shoot":
            await shoot(
                query,
                context,
            )

        elif data == "status":
            await show_status(query)

        elif data == "rematch":
            await rematch(query)

        elif data == "leaderboard":
            await leaderboard(query=query)

        elif data == "admin_settings":
            await admin_settings(query)

        elif data == "admin_stats":
            await admin_stats(query)

        elif data == "admin_cleanup":
            await admin_cleanup(query)

        elif data == "admin_close":
            if not is_admin(query.from_user.id):
                await popup(
                    query,
                    "❌ شما ادمین نیستید.",
                )
                return

            try:
                await query.message.delete()
            except Exception:
                pass

            await query.answer()

        elif data == "admin_back":
            if not is_admin(query.from_user.id):
                await popup(
                    query,
                    "❌ شما ادمین نیستید.",
                )
                return

            await query.message.edit_text(
                "👑 <b>پنل مدیریت</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "یکی از گزینه‌ها را انتخاب کنید.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(),
            )

            await query.answer()

        elif data in (
            "min_up",
            "min_down",
            "max_up",
            "max_down",
            "time_up",
            "time_down",
        ):
            await change_setting(
                query,
                data,
            )

        else:
            await popup(
                query,
                "❌ این دکمه دیگر معتبر نیست.",
            )

    except Exception as exc:
        logger.exception(
            "Callback error: %s",
            exc,
        )

        try:
            await query.answer(
                "❌ یک خطای داخلی رخ داد.",
                show_alert=True,
            )
        except Exception:
            pass


# ============================================================
# /TOP
# ============================================================

async def top_command(update, context):
    await leaderboard(update=update)


# ============================================================
# /HELP
# ============================================================

async def help_command(update, context):
    text = (
        "🎰 <b>راهنمای رولت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "/roulette — ساخت بازی\n"
        "/r — ساخت بازی\n"
        "/profile — پروفایل\n"
        "/me — پروفایل\n"
        "/top — رتبه‌بندی\n"
        "/panel — پنل ادمین\n\n"
        "🎮 بعد از ساخت بازی:\n"
        "1️⃣ جوین کنید\n"
        "2️⃣ حداقل بازیکن را کامل کنید\n"
        "3️⃣ سازنده بازی آن را شروع کند\n"
        "4️⃣ هر بازیکن در نوبت خودش شلیک کند\n\n"
        "🔫 Level 1 → 1 تیر مجازی\n"
        "🔫 Level 2 → 2 تیر مجازی\n"
        "🔫 Level 3 → 3 تیر مجازی\n"
        "🔫 Level 4 → 4 تیر مجازی\n"
        "🔫 Level 5 → 5 تیر مجازی\n\n"
        "🏆 بازی تا باقی‌ماندن یک نفر ادامه دارد."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):
    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            ["roulette", "r"],
            roulette,
        )
    )

    app.add_handler(
        CommandHandler(
            ["profile", "me"],
            profile,
        )
    )

    app.add_handler(
        CommandHandler(
            "panel",
            panel,
        )
    )

    app.add_handler(
        CommandHandler(
            "top",
            top_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_router,
        )
    )

    app.add_error_handler(
        error_handler,
    )

    logger.info(
        "Roulette bot started."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
```0
