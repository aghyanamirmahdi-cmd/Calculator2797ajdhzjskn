"""Medical GPA Bot for Telegram

Features:
- Dynamic university/course menu from inside the bot
- Admin panel with:
  - Add button (university/course/custom based on current menu)
  - Broadcast message
  - Forced join setting
  - Anonymous message links
  - Backup now + automatic backups every ~3.5 hours
  - Statistics
- User area:
  - GPA calculator
  - Calculation history
  - Edit previous calculations
  - Anonymous message flow
- Flask health server for keep-alive / deployment convenience

Environment variables:
- BOT_TOKEN               Telegram bot token (required)
- ADMIN_IDS               Comma-separated Telegram user IDs, e.g. 123,456
- BOT_USERNAME            @username without @ or with @ (optional but recommended for anon links)
- DATABASE_PATH           SQLite path (default: bot.db)
- BACKUP_DIR              Backup directory (default: backups)
- PORT                    Flask port (default: 8080)
- FLASK_HOST              Flask host (default: 0.0.0.0)
- AUTO_BACKUP_SECONDS     Backup interval in seconds (default: 12600 => 3.5h)
- FORCED_JOIN_CHANNEL     Default forced join channel (optional)

Notes:
- This file is intentionally self-contained.
- GPA formula used: sum(grade * credits) / sum(credits)
- Grades are expected on a 0..20 scale. If a user enters 0..100, it is accepted as-is; you may add conversion if needed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify
import telebot
from telebot import types

# ---------------------------
# Configuration
# ---------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "bot.db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups"))
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
AUTO_BACKUP_SECONDS = int(os.getenv("AUTO_BACKUP_SECONDS", str(3 * 60 * 60 + 30 * 60)))
DEFAULT_FORCED_JOIN = os.getenv("FORCED_JOIN_CHANNEL", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    # The bot can still start, but it will not be functional until a token is provided.
    pass

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

DB_LOCK = threading.RLock()
USER_STATE: Dict[int, Dict[str, Any]] = {}

# ---------------------------
# Database helpers
# ---------------------------

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(query: str, params: Tuple[Any, ...] = (), fetchone: bool = False, fetchall: bool = False):
    with DB_LOCK:
        conn = connect_db()
        try:
            cur = conn.execute(query, params)
            conn.commit()
            if fetchone:
                return cur.fetchone()
            if fetchall:
                return cur.fetchall()
            return cur.lastrowid
        finally:
            conn.close()


def init_db() -> None:
    with DB_LOCK:
        conn = connect_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    created_at TEXT,
                    last_seen TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS menu_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    credits REAL DEFAULT 0,
                    order_index INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS calculator_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    university_node_id INTEGER,
                    university_title TEXT,
                    total_credits REAL,
                    total_weighted REAL,
                    gpa REAL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS anonymous_links (
                    code TEXT PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    traceable INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    created_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS anonymous_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    sender_name TEXT,
                    username TEXT,
                    message_text TEXT NOT NULL,
                    traceable INTEGER DEFAULT 0,
                    created_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        if get_setting("forced_join_channel") is None and DEFAULT_FORCED_JOIN:
            set_setting("forced_join_channel", DEFAULT_FORCED_JOIN)


def get_setting(key: str) -> Optional[str]:
    row = db_execute("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    db_execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_user(user: types.User) -> None:
    db_execute(
        """
        INSERT INTO users(user_id, first_name, username, created_at, last_seen)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            username = excluded.username,
            last_seen = excluded.last_seen
        """,
        (
            user.id,
            user.first_name or "",
            user.username or "",
            now_iso(),
            now_iso(),
        ),
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def node_key(node_id: int) -> str:
    return f"node_{node_id}"


def parse_node_key(key: str) -> Optional[int]:
    m = re.fullmatch(r"node_(\d+)", key)
    return int(m.group(1)) if m else None


def get_node(node_id: int):
    return db_execute("SELECT * FROM menu_nodes WHERE id = ? AND active = 1", (node_id,), fetchone=True)


def get_children(parent_key: str):
    return db_execute(
        "SELECT * FROM menu_nodes WHERE parent_key = ? AND active = 1 ORDER BY order_index ASC, id ASC",
        (parent_key,),
        fetchall=True,
    )


def create_node(parent_key: str, title: str, kind: str, payload: str = "", credits: float = 0.0) -> int:
    return db_execute(
        """
        INSERT INTO menu_nodes(parent_key, title, kind, payload, credits, order_index, active, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (parent_key, title, kind, payload, credits, 0, now_iso(), now_iso()),
    )


def update_node(node_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    keys = list(fields.keys())
    values = list(fields.values())
    set_clause = ", ".join([f"{k} = ?" for k in keys])
    db_execute(f"UPDATE menu_nodes SET {set_clause} WHERE id = ?", tuple(values + [node_id]))


def delete_node(node_id: int) -> None:
    db_execute("UPDATE menu_nodes SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), node_id))


def history_by_user(user_id: int):
    return db_execute(
        "SELECT * FROM calculator_history WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
        fetchall=True,
    )


def get_history(history_id: int):
    return db_execute("SELECT * FROM calculator_history WHERE id = ?", (history_id,), fetchone=True)

# ---------------------------
# Formatting helpers
# ---------------------------

def esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def main_menu_text() -> str:
    return (
        "<b>🎓 ماشین حساب معدل پزشکی</b>\n\n"
        "به ربات خوش اومدی! 👋\n"
        "از دکمه‌های زیر برای شروع استفاده کن:\n\n"
        "🏛 <b>دانشگاه‌ها</b> — دانشگاهت رو ببین و درساتو انتخاب کن\n"
        "🧮 <b>ماشین حساب معدل</b> — شروع یک محاسبه‌ی تازه\n"
        "📜 <b>تاریخچه</b> — محاسبه‌های قبلیت\n"
        "✉️ <b>پیام ناشناس</b> — پیام بدون نام برای ادمین"
    )


def join_required_text(channel_id: str) -> str:
    return (
        "<b>🔒 عضویت اجباری</b>\n\n"
        "برای استفاده از ربات باید ابتدا در کانال زیر عضو شوی 👇\n\n"
        f"<b>کانال:</b> <code>{esc(channel_id)}</code>\n\n"
        "بعد از عضویت، روی «✅ بررسی عضویت» بزن یا دوباره /start رو بفرست."
    )


def calc_status_text(session: Dict[str, Any]) -> str:
    uni = session.get("university_title", "—")
    items = session.get("items", [])
    lines = [f"<b>🧮 در حال محاسبه‌ی معدل</b>", "", f"<b>🏛 دانشگاه:</b> {esc(uni)}", "", "<b>📚 درس‌های انتخاب‌شده:</b>"]
    if not items:
        lines.append("هنوز درسی اضافه نشده است.")
    else:
        for i, item in enumerate(items, 1):
            grade = item.get("grade")
            credits = item.get("credits", 0)
            grade_text = "⏳ ثبت نشده" if grade is None else f"<code>{fmt_num(grade)}</code>"
            lines.append(f"{i}. {esc(item.get('title', ''))} | واحد: <code>{fmt_num(credits)}</code> | نمره: {grade_text}")
    lines.append("")
    lines.append("یک درس دیگر انتخاب کن یا روی «✅ نهایی کردن معدل» بزن.")
    return "\n".join(lines)


def gpa_from_items(items: List[Dict[str, Any]]) -> Tuple[float, float, float]:
    total_credits = 0.0
    total_weighted = 0.0
    for item in items:
        grade = item.get("grade")
        credits = float(item.get("credits", 0) or 0)
        if grade is None:
            continue
        total_credits += credits
        total_weighted += float(grade) * credits
    gpa = total_weighted / total_credits if total_credits else 0.0
    return total_credits, total_weighted, gpa

# ---------------------------
# State helpers
# ---------------------------

def set_state(user_id: int, **kwargs: Any) -> None:
    current = USER_STATE.get(user_id, {})
    current.update(kwargs)
    USER_STATE[user_id] = current


def clear_state(user_id: int) -> None:
    USER_STATE.pop(user_id, None)


def get_state(user_id: int) -> Dict[str, Any]:
    return USER_STATE.setdefault(user_id, {})

# ---------------------------
# Keyboard builders
# ---------------------------

def kb_main(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🏛 دانشگاه‌ها", callback_data="menu:universities"),
        types.InlineKeyboardButton("🧮 ماشین حساب معدل", callback_data="calc:start"),
        types.InlineKeyboardButton("📜 تاریخچه", callback_data="history:list"),
        types.InlineKeyboardButton("✉️ پیام ناشناس", callback_data="anon:menu"),
    )
    if is_admin(user_id):
        kb.add(types.InlineKeyboardButton("🛠 پنل ادمین", callback_data="admin:home"))
    return kb


def kb_children(parent_key: str, children: List[sqlite3.Row], admin_mode: bool = False, calc_mode: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in children:
        caption = f"🏛 {ch['title']}" if ch["kind"] == "university" else (f"📘 {ch['title']} | واحد: {fmt_num(ch['credits'])}" if ch["kind"] == "course" else f"📁 {ch['title']}")
        kb.add(types.InlineKeyboardButton(caption, callback_data=f"pick:{ch['id']}"))
    if calc_mode:
        kb.add(types.InlineKeyboardButton("✅ نهایی کردن معدل", callback_data="calc:finalize"))
    if admin_mode:
        kb.add(types.InlineKeyboardButton("➕ افزودن دکمه", callback_data=f"admin:addbtn:{parent_key}"))
    if parent_key != "root":
        kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data="menu:root"))
    else:
        kb.add(types.InlineKeyboardButton("↩️ بازگشت به منوی اصلی", callback_data="menu:root"))
    return kb


def kb_history(history_rows: List[sqlite3.Row]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in history_rows[:20]:
        kb.add(
            types.InlineKeyboardButton(
                f"🎓 معدل {fmt_num(row['gpa'])} | {row['created_at'][:19].replace('T', ' ')}",
                callback_data=f"history:view:{row['id']}",
            )
        )
    kb.add(types.InlineKeyboardButton("↩️ بازگشت به منوی اصلی", callback_data="menu:root"))
    return kb


def kb_history_detail(history_id: int, editable: bool = True) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if editable:
        kb.add(types.InlineKeyboardButton("✏️ ویرایش", callback_data=f"history:edit:{history_id}"))
    kb.add(types.InlineKeyboardButton("↩️ بازگشت به تاریخچه", callback_data="history:list"))
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:root"))
    return kb


def kb_admin_home() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ افزودن دکمه", callback_data="admin:addbtn:root"),
        types.InlineKeyboardButton("📣 پیام همگانی", callback_data="admin:broadcast"),
        types.InlineKeyboardButton("🔒 عضویت اجباری", callback_data="admin:forcedjoin"),
        types.InlineKeyboardButton("🔗 لینک پیام ناشناس", callback_data="admin:anonlinks"),
        types.InlineKeyboardButton("💾 بک‌آپ فوری", callback_data="admin:backup"),
        types.InlineKeyboardButton("📊 آمار", callback_data="admin:stats"),
    )
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:root"))
    return kb


def kb_anon_links(rows: List[sqlite3.Row]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for row in rows[:25]:
        trace = "🔍 ردیابی: روشن" if row["traceable"] else "🙈 ردیابی: خاموش"
        kb.add(
            types.InlineKeyboardButton(f"{row['title']}", callback_data=f"anon:pick:{row['code']}"),
            types.InlineKeyboardButton(trace, callback_data=f"anon:toggle:{row['code']}"),
        )
    kb.add(types.InlineKeyboardButton("➕ ساخت لینک جدید", callback_data="anon:newlink"))
    kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data="admin:home"))
    return kb

# ---------------------------
# Access control
# ---------------------------

def forced_join_channel() -> str:
    return (get_setting("forced_join_channel") or "").strip()


def join_button_markup(channel_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    url = None
    raw = channel_id.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        url = raw
    elif raw.startswith("@"):
        url = f"https://t.me/{raw[1:]}"
    elif re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        url = f"https://t.me/{raw}"
    if url:
        kb.add(types.InlineKeyboardButton("📢 عضویت در کانال", url=url))
    kb.add(types.InlineKeyboardButton("✅ بررسی عضویت", callback_data="join:check"))
    return kb


def check_membership(user_id: int) -> bool:
    channel = forced_join_channel()
    if not channel:
        return True
    try:
        member = bot.get_chat_member(channel, user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False

# ---------------------------
# Menu rendering
# ---------------------------

def render_menu(chat_id: int, user_id: int, parent_key: str = "root", edit_message_id: Optional[int] = None) -> None:
    admin_mode = is_admin(user_id)
    if parent_key == "root":
        text = main_menu_text()
        kb = kb_main(user_id)
        send_or_edit(chat_id, text, kb, edit_message_id)
        return

    if parent_key == "universities":
        children = get_children("universities")
        text = "<b>🏛 دانشگاه‌ها</b>\n\nدانشگاهت رو انتخاب کن تا بریم سراغ درس‌ها."
        kb = kb_children(parent_key, children, admin_mode=admin_mode, calc_mode=False)
        send_or_edit(chat_id, text, kb, edit_message_id)
        return

    node_id = parse_node_key(parent_key)
    if node_id is not None:
        node = get_node(node_id)
        if not node:
            send_or_edit(chat_id, "این بخش پیدا نشد.", kb_main(user_id), edit_message_id)
            return
        if node["kind"] in ("university", "custom"):
            children = get_children(node_key(node_id))
            label = "🏛 دانشگاه" if node["kind"] == "university" else "📁 بخش"
            text = f"<b>{label}:</b> {esc(node['title'])}\n\n"
            if node["kind"] == "university":
                text += "درس‌های این دانشگاه رو انتخاب کن."
            else:
                text += "یکی از گزینه‌های زیر رو انتخاب کن."
            kb = kb_children(node_key(node_id), children, admin_mode=admin_mode, calc_mode=False)
            send_or_edit(chat_id, text, kb, edit_message_id)
            return

    send_or_edit(chat_id, "این منو وجود ندارد.", kb_main(user_id), edit_message_id)


def send_or_edit(chat_id: int, text: str, markup: Optional[types.InlineKeyboardMarkup] = None, message_id: Optional[int] = None) -> None:
    try:
        if message_id:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup, parse_mode="HTML")
        else:
            bot.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=markup)

# ---------------------------
# Calculator flow
# ---------------------------

def start_calc(user_id: int, chat_id: int, university_node_id: Optional[int] = None, edit_message_id: Optional[int] = None) -> None:
    if university_node_id is None:
        universities = get_children("universities")
        if not universities:
            send_or_edit(
                chat_id,
                "هنوز هیچ دانشگاهی ثبت نشده است. لطفاً بعداً دوباره امتحان کن.",
                kb_main(user_id),
                edit_message_id,
            )
            return
        text = "<b>🧮 ماشین حساب معدل</b>\n\nابتدا دانشگاهت رو انتخاب کن 👇"
        kb = kb_children("universities", universities, admin_mode=is_admin(user_id), calc_mode=False)
        send_or_edit(chat_id, text, kb, edit_message_id)
        set_state(user_id, mode="calc_choose_university")
        return

    node = get_node(university_node_id)
    if not node:
        send_or_edit(chat_id, "دانشگاه انتخاب‌شده پیدا نشد.", kb_main(user_id), edit_message_id)
        return

    session = {
        "mode": "calc_running",
        "university_node_id": university_node_id,
        "university_title": node["title"],
        "items": [],
        "awaiting": None,
        "history_id": None,
        "editing": False,
    }
    set_state(user_id, **session)
    show_calc_menu(chat_id, user_id, edit_message_id)


def show_calc_menu(chat_id: int, user_id: int, edit_message_id: Optional[int] = None) -> None:
    state = get_state(user_id)
    uni_id = state.get("university_node_id")
    if not uni_id:
        start_calc(user_id, chat_id, None, edit_message_id)
        return
    courses = get_children(node_key(int(uni_id)))
    text = calc_status_text(state)
    kb = types.InlineKeyboardMarkup(row_width=1)
    picked_ids = {it["course_id"] for it in state.get("items", [])}
    for course in courses:
        mark = "✅ " if course["id"] in picked_ids else "📘 "
        kb.add(
            types.InlineKeyboardButton(
                f"{mark}{course['title']} | واحد: {fmt_num(course['credits'])}",
                callback_data=f"pick:{course['id']}",
            )
        )
    if not courses:
        text += "\n\n⚠️ هنوز درسی برای این دانشگاه ثبت نشده است."
    kb.add(types.InlineKeyboardButton("✅ نهایی کردن معدل", callback_data="calc:finalize"))
    kb.add(types.InlineKeyboardButton("↩️ بازگشت به منوی اصلی", callback_data="menu:root"))
    send_or_edit(chat_id, text, kb, edit_message_id)


def load_history_into_state(user_id: int, history_row: sqlite3.Row) -> None:
    detail = json.loads(history_row["detail_json"])
    set_state(
        user_id,
        mode="calc_running",
        university_node_id=detail.get("university_node_id"),
        university_title=detail.get("university_title"),
        items=detail.get("items", []),
        awaiting=None,
        history_id=history_row["id"],
        editing=True,
    )


def finalize_calc(chat_id: int, user_id: int) -> None:
    state = get_state(user_id)
    items = state.get("items", [])
    total_credits, total_weighted, gpa = gpa_from_items(items)
    if not items or total_credits <= 0:
        bot.send_message(
            chat_id,
            "⚠️ هنوز هیچ نمره‌ی معتبری ثبت نکرده‌ای.\nحداقل یک درس رو انتخاب کن و نمره‌اش رو وارد کن.",
            reply_markup=kb_main(user_id),
        )
        return

    detail = {
        "university_node_id": state.get("university_node_id"),
        "university_title": state.get("university_title"),
        "items": items,
        "total_credits": total_credits,
        "total_weighted": total_weighted,
        "gpa": gpa,
    }

    if state.get("editing") and state.get("history_id"):
        db_execute(
            """
            UPDATE calculator_history
            SET university_node_id = ?, university_title = ?, total_credits = ?, total_weighted = ?, gpa = ?, detail_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                state.get("university_node_id"),
                state.get("university_title"),
                total_credits,
                total_weighted,
                gpa,
                json.dumps(detail, ensure_ascii=False),
                now_iso(),
                state.get("history_id"),
                user_id,
            ),
        )
        title_line = "<b>✅ محاسبه ویرایش و به‌روزرسانی شد</b>"
    else:
        db_execute(
            """
            INSERT INTO calculator_history(user_id, university_node_id, university_title, total_credits, total_weighted, gpa, detail_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                state.get("university_node_id"),
                state.get("university_title"),
                total_credits,
                total_weighted,
                gpa,
                json.dumps(detail, ensure_ascii=False),
                now_iso(),
                now_iso(),
            ),
        )
        title_line = "<b>✅ معدل محاسبه شد</b>"

    clear_state(user_id)
    text = (
        f"{title_line}\n\n"
        f"<b>🏛 دانشگاه:</b> {esc(detail['university_title'])}\n"
        f"<b>📚 تعداد دروس:</b> {len(items)}\n"
        f"<b>🧮 جمع واحد:</b> <code>{fmt_num(total_credits)}</code>\n"
        f"<b>🎓 معدل:</b> <code>{gpa:.2f}</code>\n\n"
        "این محاسبه در تاریخچه‌ات ذخیره شد و هر وقت خواستی می‌تونی ویرایشش کنی."
    )
    bot.send_message(chat_id, text, reply_markup=kb_main(user_id))

# ---------------------------
# Anonymous messaging
# ---------------------------

def list_active_anon_links_for_user(user_id: int) -> List[sqlite3.Row]:
    # Any active link is visible; admin chooses the target when creating it.
    return db_execute(
        "SELECT * FROM anonymous_links WHERE active = 1 ORDER BY created_at DESC",
        fetchall=True,
    )


def make_anon_link(code: str) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=anon_{code}"
    return f"/start anon_{code}"


def ensure_anon_context(user_id: int, code: str) -> Optional[sqlite3.Row]:
    row = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
    if row:
        set_state(user_id, mode="anon_write", anon_code=code)
    return row


def send_anonymous_message_to_admin(code: str, user: types.User, text: str) -> None:
    link = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
    if not link:
        return
    sender_name = user.first_name or "کاربر"
    username = f"@{user.username}" if user.username else "ندارد"
    db_execute(
        """
        INSERT INTO anonymous_messages(code, admin_id, user_id, sender_name, username, message_text, traceable, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            link["admin_id"],
            user.id,
            sender_name,
            user.username or "",
            text,
            int(link["traceable"]),
            now_iso(),
        ),
    )
    visible_sender = f"\n<b>👤 فرستنده:</b> {esc(sender_name)}"
    if link["traceable"]:
        visible_sender += f"\n<b>یوزرنیم:</b> {esc(username)}\n<b>آیدی:</b> <code>{user.id}</code>"
    else:
        visible_sender += "\n<i>(این لینک بدون ردیابی است)</i>"
    text_admin = (
        f"<b>✉️ پیام ناشناس جدید</b>\n\n"
        f"<b>عنوان لینک:</b> {esc(link['title'])}\n"
        f"<b>کد:</b> <code>{esc(code)}</code>"
        f"{visible_sender}\n\n"
        f"<b>متن پیام:</b>\n{esc(text)}"
    )
    try:
        bot.send_message(int(link["admin_id"]), text_admin)
    except Exception:
        pass

# ---------------------------
# Backup and stats
# ---------------------------

def backup_now() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_path = BACKUP_DIR / f"backup_{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if DATABASE_PATH.exists():
            zf.write(DATABASE_PATH, arcname=DATABASE_PATH.name)
        # Include a JSON export of settings and menu data for convenience.
        export_path = BACKUP_DIR / f"export_{stamp}.json"
        data = export_snapshot()
        export_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        zf.write(export_path, arcname=export_path.name)
        export_path.unlink(missing_ok=True)
    return zip_path


def export_snapshot() -> Dict[str, Any]:
    users = db_execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    universities = db_execute("SELECT COUNT(*) AS c FROM menu_nodes WHERE parent_key = 'universities' AND active = 1", fetchone=True)["c"]
    courses = db_execute("SELECT COUNT(*) AS c FROM menu_nodes WHERE kind = 'course' AND active = 1", fetchone=True)["c"]
    histories = db_execute("SELECT COUNT(*) AS c FROM calculator_history", fetchone=True)["c"]
    rows = db_execute("SELECT * FROM settings", fetchall=True)
    settings = {r["key"]: r["value"] for r in rows}
    return {
        "generated_at": now_iso(),
        "users": users,
        "universities": universities,
        "courses": courses,
        "histories": histories,
        "settings": settings,
    }


def send_backup_to_admins(path: Path, caption: str = "بک‌آپ دیتابیس") -> None:
    for admin_id in ADMIN_IDS:
        try:
            with path.open("rb") as f:
                bot.send_document(admin_id, f, caption=caption)
        except Exception:
            continue


def stats_text() -> str:
    users = db_execute("SELECT COUNT(*) AS c FROM users", fetchone=True)["c"]
    histories = db_execute("SELECT COUNT(*) AS c FROM calculator_history", fetchone=True)["c"]
    universities = db_execute("SELECT COUNT(*) AS c FROM menu_nodes WHERE parent_key = 'universities' AND active = 1", fetchone=True)["c"]
    courses = db_execute("SELECT COUNT(*) AS c FROM menu_nodes WHERE kind = 'course' AND active = 1", fetchone=True)["c"]
    links = db_execute("SELECT COUNT(*) AS c FROM anonymous_links WHERE active = 1", fetchone=True)["c"]
    avg = db_execute("SELECT AVG(gpa) AS a FROM calculator_history WHERE gpa IS NOT NULL", fetchone=True)["a"]
    top = db_execute("SELECT user_id, gpa, university_title, created_at FROM calculator_history ORDER BY gpa DESC, id DESC LIMIT 5", fetchall=True)

    lines = [
        "<b>📊 آمار ربات</b>",
        "",
        f"<b>👤 کاربران:</b> <code>{users}</code>",
        f"<b>🧮 تعداد محاسبه‌ها:</b> <code>{histories}</code>",
        f"<b>🏛 دانشگاه‌ها:</b> <code>{universities}</code>",
        f"<b>📚 دروس:</b> <code>{courses}</code>",
        f"<b>✉️ لینک‌های ناشناس فعال:</b> <code>{links}</code>",
        f"<b>📈 میانگین کل معدل‌ها:</b> <code>{fmt_num(float(avg or 0))}</code>",
        "",
        "<b>🏆 بالاترین معدل‌ها:</b>",
    ]
    if top:
        for i, row in enumerate(top, 1):
            lines.append(f"{i}. کاربر <code>{row['user_id']}</code> | معدل <code>{float(row['gpa']):.2f}</code> | {esc(row['university_title'] or '—')}")
    else:
        lines.append("داده‌ای ثبت نشده است.")
    return "\n".join(lines)

# ---------------------------
# Message / callback handlers
# ---------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    save_user(message.from_user)
    user_id = message.from_user.id
    chat_id = message.chat.id

    # /start anon_CODE support
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("anon_"):
        code = args[1][5:].strip()
        link = ensure_anon_context(user_id, code)
        if link:
            bot.send_message(
                chat_id,
                f"<b>✉️ ارسال پیام ناشناس</b>\n\nلینک انتخاب شد: <b>{esc(link['title'])}</b>\nحالا متن پیامت رو بفرست.",
            )
            return
        bot.send_message(chat_id, "این لینک ناشناس معتبر نیست یا غیرفعال شده است.")
        return

    channel = forced_join_channel()
    if channel and not check_membership(user_id):
        bot.send_message(chat_id, join_required_text(channel), reply_markup=join_button_markup(channel))
        return

    clear_state(user_id)
    bot.send_message(chat_id, main_menu_text(), reply_markup=kb_main(user_id))


@bot.message_handler(commands=["admin"])
def cmd_admin(message: types.Message):
    save_user(message.from_user)
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "دسترسی ندارید.")
        return
    bot.send_message(message.chat.id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())


@bot.message_handler(content_types=["text"])
def on_text(message: types.Message):
    save_user(message.from_user)
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    state = get_state(user_id)

    # Forced join check for all non-admin users
    channel = forced_join_channel()
    if channel and not check_membership(user_id) and not is_admin(user_id):
        bot.send_message(chat_id, join_required_text(channel), reply_markup=join_button_markup(channel))
        return

    # Anonymous flow
    if state.get("mode") == "anon_write":
        code = state.get("anon_code")
        if code:
            send_anonymous_message_to_admin(code, message.from_user, text)
            clear_state(user_id)
            bot.send_message(chat_id, "<b>✅ پیامت ارسال شد.</b>", reply_markup=kb_main(user_id))
            return

    # Admin text states
    if state.get("mode") == "admin_wait_title":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return
        set_state(user_id, mode="admin_wait_kind", new_title=text)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("دانشگاه", "درس", "سفارشی")
        kb.add("بازگشت")
        bot.send_message(chat_id, "نوع دکمه رو انتخاب کن:", reply_markup=kb)
        return

    if state.get("mode") == "admin_wait_kind":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return
        kind_map = {"دانشگاه": "university", "درس": "course", "سفارشی": "custom"}
        kind = kind_map.get(text)
        if not kind:
            bot.send_message(chat_id, "یکی از گزینه‌ها رو انتخاب کن.")
            return
        set_state(user_id, mode="admin_wait_credits", new_kind=kind)
        if kind == "course":
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("بازگشت")
            bot.send_message(chat_id, "تعداد واحد این درس رو بفرست. مثال: 2 یا 1.5", reply_markup=kb)
        else:
            # For university/custom we can create immediately, no extra info needed
            parent_key = state.get("add_parent", "root")
            if parent_key == "root":
                parent_key = "universities"
            new_id = create_node(parent_key, state.get("new_title", ""), kind, payload="")
            title_snapshot = state.get("new_title", "")
            clear_state(user_id)
            kind_label = "دانشگاه" if kind == "university" else "بخش سفارشی"
            bot.send_message(
                chat_id,
                f"<b>✅ {kind_label} جدید ساخته شد</b>\n\n<b>عنوان:</b> {esc(title_snapshot)}\n<b>شناسه:</b> <code>{new_id}</code>",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
        return

    if state.get("mode") == "admin_wait_credits":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return
        try:
            credits = float(text)
        except ValueError:
            bot.send_message(chat_id, "عدد معتبر بفرست.")
            return
        if credits <= 0:
            bot.send_message(chat_id, "واحد باید بیشتر از صفر باشد.")
            return
        parent_key = state.get("add_parent", "root")
        if parent_key == "root":
            parent_key = "universities"
        new_id = create_node(parent_key, state.get("new_title", ""), "course", payload="", credits=credits)
        title_snapshot = state.get("new_title", "")
        clear_state(user_id)
        bot.send_message(
            chat_id,
            f"<b>✅ درس جدید اضافه شد</b>\n\n<b>عنوان:</b> {esc(title_snapshot)}\n<b>واحد:</b> <code>{fmt_num(credits)}</code>\n<b>شناسه:</b> <code>{new_id}</code>",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
        return

    if state.get("mode") == "admin_wait_broadcast":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return
        users = db_execute("SELECT user_id FROM users", fetchall=True)
        sent = 0
        failed = 0
        for row in users:
            try:
                bot.send_message(int(row["user_id"]), text)
                sent += 1
            except Exception:
                failed += 1
        clear_state(user_id)
        bot.send_message(
            chat_id,
            f"<b>📣 پیام همگانی ارسال شد</b>\n\n✅ موفق: <code>{sent}</code>\n❌ ناموفق: <code>{failed}</code>",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
        return

    if state.get("mode") == "admin_wait_forced_join":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return
        set_setting("forced_join_channel", text)
        clear_state(user_id)
        bot.send_message(
            chat_id,
            f"<b>✅ عضویت اجباری تنظیم شد</b>\n\n<b>کانال:</b> <code>{esc(text)}</code>",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
        return

    if state.get("mode") == "calc_choose_grade" and state.get("course_id"):
        try:
            grade = float(text)
        except ValueError:
            bot.send_message(chat_id, "لطفاً یک عدد معتبر بفرست (مثلاً 17.5).")
            return
        course_id = int(state["course_id"])
        course = get_node(course_id)
        if not course:
            bot.send_message(chat_id, "درس پیدا نشد.")
            clear_state(user_id)
            return

        items = state.get("items", [])
        replaced = False
        for item in items:
            if item["course_id"] == course_id:
                item["grade"] = grade
                replaced = True
                break
        if not replaced:
            items.append(
                {
                    "course_id": course_id,
                    "title": course["title"],
                    "credits": float(course["credits"] or 0),
                    "grade": grade,
                }
            )
        set_state(user_id, mode="calc_running", items=items, awaiting=None, course_id=None)
        bot.send_message(chat_id, f"<b>✅ ثبت شد:</b> {esc(course['title'])} | نمره: <code>{fmt_num(grade)}</code>")
        show_calc_menu(chat_id, user_id)
        return

    # Generic text fallback
    if text == "/start":
        cmd_start(message)
        return
    bot.send_message(chat_id, "از دکمه‌ها استفاده کن. 🙂", reply_markup=kb_main(user_id))


@bot.callback_query_handler(func=lambda call: True)
def on_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data or ""
    save_user(call.from_user)

    if data == "join:check":
        channel = forced_join_channel()
        if channel and not check_membership(user_id):
            bot.answer_callback_query(call.id, "هنوز عضو نیستی.", show_alert=True)
            try:
                bot.edit_message_text(join_required_text(channel), chat_id, call.message.message_id, reply_markup=join_button_markup(channel))
            except Exception:
                pass
            return
        bot.answer_callback_query(call.id, "عضویت تایید شد ✅")
        clear_state(user_id)
        send_or_edit(chat_id, main_menu_text(), kb_main(user_id), call.message.message_id)
        return

    if data.startswith("menu:"):
        parent_key = data.split(":", 1)[1]
        if parent_key == "root":
            clear_state(user_id)
        render_menu(chat_id, user_id, parent_key, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("pick:"):
        node_id = int(data.split(":", 1)[1])
        node = get_node(node_id)
        if not node:
            bot.answer_callback_query(call.id, "مورد پیدا نشد.", show_alert=True)
            return

        # University -> start calculator session on that university
        if node["kind"] == "university":
            start_calc(user_id, chat_id, node_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        # Course -> ask for grade in calculator session
        if node["kind"] == "course":
            state = get_state(user_id)
            items = state.get("items", [])
            if state.get("mode") not in ("calc_running", "calc_choose_grade"):
                bot.answer_callback_query(call.id, "ابتدا دانشگاه رو انتخاب کن.", show_alert=True)
                return
            set_state(user_id, mode="calc_choose_grade", course_id=node_id, awaiting="grade_input")
            existing = next((it for it in items if it["course_id"] == node_id), None)
            extra = ""
            if existing and existing.get("grade") is not None:
                extra = f"\n<b>نمره فعلی:</b> <code>{fmt_num(existing['grade'])}</code> (با ارسال عدد جدید عوض میشه)"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="calc:cancelgrade"))
            bot.send_message(
                chat_id,
                f"<b>📘 {esc(node['title'])}</b>\n\n<b>واحد:</b> <code>{fmt_num(node['credits'])}</code>{extra}\n\nنمره‌ی این درس رو بفرست (مثلاً <code>18.25</code>)",
                reply_markup=kb,
            )
            bot.answer_callback_query(call.id)
            return

        # custom branch
        if node["kind"] == "custom":
            render_menu(chat_id, user_id, node_key(node_id), call.message.message_id)
            bot.answer_callback_query(call.id)
            return

    if data == "calc:start":
        clear_state(user_id)
        start_calc(user_id, chat_id, None, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "calc:cancelgrade":
        state = get_state(user_id)
        if state.get("mode") == "calc_choose_grade":
            set_state(user_id, mode="calc_running", course_id=None, awaiting=None)
        bot.answer_callback_query(call.id, "لغو شد.")
        show_calc_menu(chat_id, user_id)
        return

    if data == "calc:finalize":
        finalize_calc(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "history:list":
        rows = history_by_user(user_id)
        if not rows:
            send_or_edit(chat_id, "هنوز هیچ تاریخی ثبت نشده است.", kb_main(user_id), call.message.message_id)
        else:
            send_or_edit(chat_id, "<b>📜 تاریخچه محاسبه‌ها</b>", kb_history(rows), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("history:view:"):
        hid = int(data.split(":")[-1])
        row = get_history(hid)
        if not row or row["user_id"] != user_id:
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        detail = json.loads(row["detail_json"])
        lines = [
            "<b>📄 جزئیات محاسبه</b>",
            "",
            f"<b>🏛 دانشگاه:</b> {esc(detail.get('university_title', '—'))}",
            f"<b>🎓 معدل:</b> <code>{float(row['gpa']):.2f}</code>",
            f"<b>🧮 جمع واحد:</b> <code>{fmt_num(float(row['total_credits'] or 0))}</code>",
            "",
            "<b>📚 دروس:</b>",
        ]
        for i, item in enumerate(detail.get("items", []), 1):
            lines.append(f"{i}. {esc(item['title'])} | واحد: <code>{fmt_num(float(item['credits']))}</code> | نمره: <code>{fmt_num(float(item['grade']))}</code>")
        send_or_edit(chat_id, "\n".join(lines), kb_history_detail(hid), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("history:edit:"):
        hid = int(data.split(":")[-1])
        row = get_history(hid)
        if not row or row["user_id"] != user_id:
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        load_history_into_state(user_id, row)
        bot.answer_callback_query(call.id, "حالت ویرایش فعال شد.")
        show_calc_menu(chat_id, user_id, call.message.message_id)
        return

    if data == "admin:home":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        send_or_edit(chat_id, "<b>🛠 پنل ادمین</b>\n\nاز بخش موردنظر استفاده کن.", kb_admin_home(), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:addbtn:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parent_key = data.split(":", 2)[2]
        set_state(user_id, mode="admin_wait_title", add_parent=parent_key)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("بازگشت")
        bot.send_message(chat_id, f"برای منوی <code>{esc(parent_key)}</code> عنوان دکمه جدید رو بفرست.", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:broadcast":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        set_state(user_id, mode="admin_wait_broadcast")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("بازگشت")
        bot.send_message(chat_id, "متن پیام همگانی رو بفرست.", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:forcedjoin":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        set_state(user_id, mode="admin_wait_forced_join")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("بازگشت")
        bot.send_message(chat_id, "آیدی کانال یا @username یا لینک کانال رو بفرست.", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:backup":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        path = backup_now()
        send_backup_to_admins(path, "بک‌آپ فوری ربات")
        bot.answer_callback_query(call.id, "بک‌آپ ساخته شد.")
        bot.send_message(chat_id, f"<b>✅ بک‌آپ ساخته شد:</b> <code>{esc(path.name)}</code>")
        return

    if data == "admin:stats":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        send_or_edit(chat_id, stats_text(), kb_admin_home(), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:anonlinks":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        rows = db_execute("SELECT * FROM anonymous_links ORDER BY created_at DESC", fetchall=True)
        text = (
            "<b>🔗 لینک‌های پیام ناشناس</b>\n\n"
            "هر لینک می‌تونه برای دریافت پیام از کاربران استفاده بشه.\n"
            "توی لینک‌های ردیابی‌دار، فرستنده برای ادمین قابل مشاهده‌ست."
        )
        send_or_edit(chat_id, text, kb_anon_links(rows), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "anon:menu":
        rows = list_active_anon_links_for_user(user_id)
        if not rows:
            send_or_edit(chat_id, "فعلاً لینکی برای پیام ناشناس وجود نداره.", kb_main(user_id), call.message.message_id)
        else:
            text = "<b>✉️ پیام ناشناس</b>\n\nیک لینک رو انتخاب کن و بعد پیامت رو بفرست."
            send_or_edit(chat_id, text, kb_anon_links(rows), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("anon:pick:"):
        code = data.split(":")[-1]
        row = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
        if not row:
            bot.answer_callback_query(call.id, "لینک پیدا نشد.", show_alert=True)
            return
        set_state(user_id, mode="anon_write", anon_code=code)
        bot.send_message(chat_id, f"<b>✉️ {esc(row['title'])}</b>\n\nحالا متن پیامت رو بفرست.")
        bot.answer_callback_query(call.id)
        return

    if data.startswith("anon:toggle:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        code = data.split(":")[-1]
        row = db_execute("SELECT * FROM anonymous_links WHERE code = ?", (code,), fetchone=True)
        if not row:
            bot.answer_callback_query(call.id, "لینک پیدا نشد.", show_alert=True)
            return
        new_value = 0 if row["traceable"] else 1
        db_execute("UPDATE anonymous_links SET traceable = ? WHERE code = ?", (new_value, code))
        bot.answer_callback_query(call.id, "ردیابی تغییر کرد.")
        rows = db_execute("SELECT * FROM anonymous_links ORDER BY created_at DESC", fetchall=True)
        send_or_edit(
            chat_id,
            "<b>🔗 لینک‌های پیام ناشناس</b>\n\nهر لینک می‌تونه برای دریافت پیام از کاربران استفاده بشه.\nتوی لینک‌های ردیابی‌دار، فرستنده برای ادمین قابل مشاهده‌ست.",
            kb_anon_links(rows),
            call.message.message_id,
        )
        return

    if data == "anon:newlink":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        code = uuid.uuid4().hex[:10]
        title = f"لینک {code[:4]}"
        db_execute(
            "INSERT INTO anonymous_links(code, admin_id, title, traceable, active, created_at) VALUES(?, ?, ?, 0, 1, ?)",
            (code, user_id, title, now_iso()),
        )
        url = make_anon_link(code)
        bot.send_message(
            chat_id,
            "<b>✅ لینک ناشناس ساخته شد</b>\n\n"
            f"<b>عنوان:</b> {esc(title)}\n"
            f"<b>کد:</b> <code>{esc(code)}</code>\n"
            f"<b>لینک:</b> <code>{esc(url)}</code>\n\n"
            "این لینک رو برای دریافت پیام در اختیار کاربرا بذار. با دکمه‌ی «تغییر ردیابی» می‌تونی مشخص کنی هویت فرستنده برات نمایش داده بشه یا نه.",
            reply_markup=kb_admin_home(),
        )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)

# ---------------------------
# Default data bootstrap
# ---------------------------

def bootstrap_default_data() -> None:
    # Only add defaults if the universities menu is empty.
    if db_execute("SELECT COUNT(*) AS c FROM menu_nodes WHERE parent_key = 'universities' AND active = 1", fetchone=True)["c"] == 0:
        # A few starter entries help the bot feel alive out of the box.
        u1 = create_node("universities", "دانشگاه علوم پزشکی ایران", "university")
        u2 = create_node("universities", "دانشگاه علوم پزشکی تهران", "university")
        create_node(node_key(u1), "آناتومی", "course", credits=3)
        create_node(node_key(u1), "فیزیولوژی", "course", credits=4)
        create_node(node_key(u1), "باکتری‌شناسی", "course", credits=2)
        create_node(node_key(u2), "آناتومی", "course", credits=3)
        create_node(node_key(u2), "ایمونولوژی", "course", credits=2)
        create_node(node_key(u2), "بیوشیمی", "course", credits=4)

# ---------------------------
# Background jobs
# ---------------------------

def backup_worker() -> None:
    while True:
        try:
            path = backup_now()
            if ADMIN_IDS:
                send_backup_to_admins(path, "بک‌آپ خودکار ربات")
        except Exception:
            pass
        time.sleep(AUTO_BACKUP_SECONDS)


def flask_worker() -> None:
    app.run(host=FLASK_HOST, port=PORT, debug=False, use_reloader=False)

# ---------------------------
# Flask routes
# ---------------------------

@app.route("/")
def index():
    return jsonify(
        ok=True,
        bot_username=BOT_USERNAME or None,
        database=str(DATABASE_PATH),
        admins=len(ADMIN_IDS),
        forced_join=forced_join_channel() or None,
    )

# ---------------------------
# App startup
# ---------------------------

def startup() -> None:
    init_db()
    bootstrap_default_data()

    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=flask_worker, daemon=True).start()

    # Polling keeps the bot alive without requiring webhook setup.
    bot.infinity_polling(timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    startup()
