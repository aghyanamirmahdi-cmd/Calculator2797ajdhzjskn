"""Medical GPA Bot for Telegram

Features:
- Dynamic university/course menu from inside the bot
- Admin panel with:
  - Add button (university/course/custom based on current menu)
  - Delete button (browse the menu tree and remove any button)
  - Broadcast message (to everyone, or only to users who registered a GPA
    for a specific university - the list of universities updates
    automatically as you add new ones)
  - Forced join setting
  - Anonymous message links management (trace toggle - admin only)
  - Backup now + automatic backups every ~3.5 hours
  - Restore data from a backup file
  - Statistics
- User area:
  - GPA calculator
  - Calculation history
  - Edit previous calculations
  - Personal anonymous-message link: every user can get their own link,
    share it (e.g. in their channel), and anyone who opens it can send
    them an anonymous message straight into this bot. Regular users never
    see the "traceability" controls - only admins can see/change that.
- Flask health server for keep-alive / deployment convenience

Environment variables:
  BOT_TOKEN               Telegram bot token (required)
  ADMIN_IDS               Comma-separated Telegram user IDs, e.g. 123,456
  BOT_USERNAME            @username without @ or with @ (optional but recommended for anon links)
  DATABASE_PATH            SQLite path (default: bot.db)
  BACKUP_DIR               Backup directory (default: backups)
  PORT                     Flask port (default: 8080)
  FLASK_HOST               Flask host (default: 0.0.0.0)
  AUTO_BACKUP_SECONDS      Backup interval in seconds (default: 12600 => 3.5h)
  FORCED_JOIN_CHANNEL      Default forced join channel (optional)

Notes:
- This file is intentionally self-contained.
- GPA formula used: sum(grade * credits) / sum(credits)
- Grades are expected on a 0..20 scale. If a user enters 0..100, it is
  accepted as-is; you may add conversion if needed.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify
import telebot
from telebot import types

# ---------------------------
# Configuration
# ---------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", )
BOT_USERNAME = os.getenv("BOT_USERNAME", "SWMC_CalculatorBot").lstrip("@")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "bot.db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups"))
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
AUTO_BACKUP_SECONDS = int(os.getenv("AUTO_BACKUP_SECONDS", str(3 * 60 * 60 + 30 * 60)))
DEFAULT_FORCED_JOIN = os.getenv("FORCED_JOIN_CHANNEL", "")
PAGE_SIZE = 10  # number of buttons shown per page in paginated menus
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "7726600296").split(",")
    if x.strip().isdigit()
}

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
                    notif_chat_id INTEGER,
                    notif_message_id INTEGER,
                    created_at TEXT
                )
                """
            )
            # Best-effort migration for databases created before the
            # notif_chat_id / notif_message_id columns existed.
            try:
                cur.execute("ALTER TABLE anonymous_messages ADD COLUMN notif_chat_id INTEGER")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE anonymous_messages ADD COLUMN notif_message_id INTEGER")
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()


def apply_default_forced_join() -> None:
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


def get_node_any(node_id: int):
    """Fetch a node regardless of active state (used for delete confirmations)."""
    return db_execute("SELECT * FROM menu_nodes WHERE id = ?", (node_id,), fetchone=True)


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


def duplicate_node(node_id: int, new_parent_key: str) -> int:
    """Deep-copy a node (and, recursively, all of its descendants) under a
    new parent. The original node and its subtree are left untouched."""
    node = get_node_any(node_id)
    if not node:
        return -1
    new_id = create_node(
        new_parent_key,
        node["title"],
        node["kind"],
        payload=node["payload"] or "",
        credits=node["credits"] or 0,
    )
    for child in get_children(node_key(node_id)):
        duplicate_node(int(child["id"]), node_key(new_id))
    return new_id


def update_node(node_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    keys = list(fields.keys())
    values = list(fields.values())
    set_clause = ", ".join([f"{k} = ?" for k in keys])
    db_execute(f"UPDATE menu_nodes SET {set_clause} WHERE id = ?", tuple(values + [node_id]))


def delete_node(node_id: int) -> None:
    # Soft delete this node and (recursively) any descendants so they stop
    # showing up anywhere in the menus.
    db_execute("UPDATE menu_nodes SET active = 0, updated_at = ? WHERE id = ?", (now_iso(), node_id))
    children = db_execute(
        "SELECT id FROM menu_nodes WHERE parent_key = ? AND active = 1",
        (node_key(node_id),),
        fetchall=True,
    )
    for child in children:
        delete_node(int(child["id"]))


def history_by_user(user_id: int):
    return db_execute(
        "SELECT * FROM calculator_history WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
        fetchall=True,
    )


def get_history(history_id: int):
    return db_execute("SELECT * FROM calculator_history WHERE id = ?", (history_id,), fetchone=True)


def get_anon_message(msg_id: int):
    return db_execute("SELECT * FROM anonymous_messages WHERE id = ?", (msg_id,), fetchone=True)


# ---------------------------
# Bulk-add parsing helper
# ---------------------------

def parse_bulk_lines(text: str) -> List[Tuple[str, Optional[float]]]:
    """Parses lines like 'عنوان - واحد' (title - credits) for bulk button
    creation. Credits are optional; separators '-', ',' and '|' are all
    accepted."""
    results: List[Tuple[str, Optional[float]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"[-,|]", line, maxsplit=1)
        title = parts[0].strip()
        credits: Optional[float] = None
        if len(parts) > 1:
            num_match = re.search(r"[\d.]+", parts[1])
            if num_match:
                try:
                    credits = float(num_match.group(0))
                except ValueError:
                    credits = None
        if title:
            results.append((title, credits))
    return results


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
        "🧮 <b>ماشین حساب معدل</b> — شروع یک محاسبه‌ی تازه\n"
        "📜 <b>تاریخچه</b> — محاسبه‌های قبلیت\n"
        "✉️ <b>پیام ناشناس</b> — پیام بدون نام برای ادمین\n"
        "🔗 <b>لینک ناشناس من</b> — لینک شخصی خودت برای دریافت پیام ناشناس از بقیه"
    )


def join_required_text(channel_id: str) -> str:
    return (
        "<b>🔒 عضویت اجباری</b>\n\n"
        "برای استفاده از ربات باید ابتدا در کانال زیر عضو شوی 👇\n\n"
        f"<b>کانال:</b> <code>{esc(channel_id)}</code>\n\n"
        "بعد از عضویت، روی «✅ بررسی عضویت» بزن یا دوباره /start رو بفرست."
    )


def calc_status_text(title: str, items: List[Dict[str, Any]]) -> str:
    lines = ["<b>🧮 در حال محاسبه‌ی معدل</b>", "", f"<b>📍 مسیر:</b> {esc(title)}", "", "<b>📚 درس‌های انتخاب‌شده:</b>"]
    if not items:
        lines.append("هنوز درسی اضافه نشده است.")
    else:
        for i, item in enumerate(items, 1):
            grade = item.get("grade")
            credits = item.get("credits", 0)
            grade_text = "⏳ ثبت نشده" if grade is None else f"<code>{fmt_num(grade)}</code>"
            lines.append(f"{i}. {esc(item.get('title', ''))} | واحد: <code>{fmt_num(credits)}</code> | نمره: {grade_text}")
    lines.append("")
    lines.append("یک درس دیگه انتخاب کن، وارد یه زیرشاخه بشو، یا روی «✅ نهایی کردن معدل» بزن.")
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

def container_items(state: Dict[str, Any], container_id: int) -> List[Dict[str, Any]]:
    by_container = state.get("items_by_container", {})
    return list(by_container.get(str(container_id), []))


def set_container_items(user_id: int, container_id: int, items: List[Dict[str, Any]]) -> None:
    state = get_state(user_id)
    by_container = dict(state.get("items_by_container", {}))
    by_container[str(container_id)] = items
    set_state(user_id, items_by_container=by_container)


def container_breadcrumb(container_id: int) -> str:
    chain = []
    node_id = container_id
    guard = 0
    while node_id is not None and guard < 12:
        guard += 1
        node = get_node_any(node_id)
        if not node:
            break
        chain.append(node["title"])
        node_id = parse_node_key(node["parent_key"])
    return " › ".join(reversed(chain))
# ---------------------------
# Pagination helpers
# ---------------------------

def paginate_rows(rows: List[sqlite3.Row], page: int, page_size: int = PAGE_SIZE) -> Tuple[List[sqlite3.Row], int, int]:
    """Slice rows for the given page. Returns (page_items, clamped_page, total_pages)."""
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = start + page_size
    return rows[start:end], page, total_pages


def add_pagination_row(kb: types.InlineKeyboardMarkup, page: int, total_pages: int, callback_prefix: str) -> None:
    """Adds a Prev / page-indicator / Next row to an existing keyboard, if needed."""
    if total_pages <= 1:
        return
    buttons = []
    if page > 0:
        buttons.append(types.InlineKeyboardButton("◀️ قبلی", callback_data=f"{callback_prefix}:{page - 1}"))
    buttons.append(types.InlineKeyboardButton(f"صفحه {page + 1} از {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        buttons.append(types.InlineKeyboardButton("بعدی ▶️", callback_data=f"{callback_prefix}:{page + 1}"))
    kb.row(*buttons)


# ---------------------------
# Keyboard builders
# ---------------------------

def kb_main(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🧮 ماشین حساب معدل", callback_data="calc:start"),
        types.InlineKeyboardButton("📜 تاریخچه", callback_data="history:list"),
    )
    kb.add(
        types.InlineKeyboardButton("✉️ پیام ناشناس", callback_data="anon:menu"),
        types.InlineKeyboardButton("🔗 لینک ناشناس من", callback_data="anon:mylink"),
    )
    if is_admin(user_id):
        kb.add(types.InlineKeyboardButton("🛠 پنل ادمین", callback_data="admin:home"))
    return kb


def kb_children(
    parent_key: str,
    children: List[sqlite3.Row],
    admin_mode: bool = False,
    calc_mode: bool = False,
    page: int = 0,
    total_pages: int = 1,
    page_prefix: Optional[str] = None,
) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in children:
        if ch["kind"] == "finalize":
            continue  # finalize buttons only ever render inside the calculator flow
        caption = f"🏛 {ch['title']}" if ch["kind"] == "university" else (f"📘 {ch['title']} | واحد: {fmt_num(ch['credits'])}" if ch["kind"] == "course" else f"📁 {ch['title']}")
        kb.add(types.InlineKeyboardButton(caption, callback_data=f"pick:{ch['id']}"))
    if page_prefix:
        add_pagination_row(kb, page, total_pages, page_prefix)
    if calc_mode:
        kb.add(types.InlineKeyboardButton("✅ نهایی کردن معدل", callback_data="calc:finalize"))
    if admin_mode:
        kb.add(types.InlineKeyboardButton("🛠 مدیریت این بخش", callback_data=f"admin:manage:{parent_key}:0"))
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
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🗂 مدیریت دانشگاه‌ها و دروس", callback_data="admin:manage:universities:0"))
    kb.add(
        types.InlineKeyboardButton("📣 پیام همگانی", callback_data="admin:broadcast"),
        types.InlineKeyboardButton("🔒 عضویت اجباری", callback_data="admin:forcedjoin"),
    )
    kb.add(
        types.InlineKeyboardButton("🔗 لینک‌های پیام ناشناس", callback_data="admin:anonlinks"),
        types.InlineKeyboardButton("📊 آمار", callback_data="admin:stats"),
    )
    kb.add(
        types.InlineKeyboardButton("💾 بک‌آپ فوری", callback_data="admin:backup"),
        types.InlineKeyboardButton("📥 بازیابی اطلاعات", callback_data="admin:restore"),
    )
    kb.add(types.InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu:root"))
    return kb


def kb_admin_node(
    parent_key: str,
    node: Optional[sqlite3.Row],
    children: List[sqlite3.Row],
    page: int,
    total_pages: int,
) -> types.InlineKeyboardMarkup:
    """Inline admin management view for one menu node (or the root 'universities' list).

    Every row lets the admin navigate into containers (university/custom) and
    always exposes ✏️ (edit) / 📦 (move) / 🗑 (delete) for that specific item,
    all without ever leaving this same message.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in children:
        icon = {"university": "🏛", "course": "📘", "finalize": "✅"}.get(ch["kind"], "📁")
        suffix = f" | واحد: {fmt_num(ch['credits'])}" if ch["kind"] == "course" else ""
        label = f"{icon} {ch['title']}{suffix}"
        if ch["kind"] in ("university", "custom"):
            main_cb = f"admin:manage:{node_key(ch['id'])}:0"
        else:
            main_cb = "noop"
        kb.row(types.InlineKeyboardButton(label, callback_data=main_cb))
        kb.row(
            types.InlineKeyboardButton("✏️ ویرایش", callback_data=f"admin:editnode:{ch['id']}"),
            types.InlineKeyboardButton("📦 انتقال", callback_data=f"admin:movestart:{ch['id']}"),
            types.InlineKeyboardButton("🗑 حذف", callback_data=f"admin:delconfirm:{ch['id']}"),
        )
    add_pagination_row(kb, page, total_pages, f"admin:managepage:{parent_key}")
    kb.add(types.InlineKeyboardButton("➕ افزودن دکمه", callback_data=f"admin:addstart:{parent_key}"))
    kb.add(types.InlineKeyboardButton("➕ افزودن گروهی", callback_data=f"admin:bulkaddstart:{parent_key}"))
    kb.add(types.InlineKeyboardButton("🔀 تغییر ترتیب دکمه‌ها", callback_data=f"admin:reorderstart:{parent_key}"))
    kb.add(types.InlineKeyboardButton("📋 کپی گروهی", callback_data=f"admin:bulkcopystart:{parent_key}"))
    if parent_key == "universities":
        kb.add(types.InlineKeyboardButton("↩️ بازگشت به پنل ادمین", callback_data="admin:home"))
    else:
        back_target = node["parent_key"] if node else "universities"
        kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data=f"admin:manage:{back_target}:0"))
    return kb


def render_admin_node(chat_id: int, user_id: int, parent_key: str, edit_message_id: Optional[int] = None, page: int = 0) -> None:
    if not is_admin(user_id):
        return
    node = None
    if parent_key == "universities":
        title_line = "<b>🗂 مدیریت دانشگاه‌ها</b>\n\nاز اینجا می‌تونی دانشگاه اضافه/ویرایش/حذف کنی و وارد هر دانشگاه بشی تا درس‌هاشو مدیریت کنی."
    else:
        node_id = parse_node_key(parent_key)
        node = get_node_any(node_id) if node_id is not None else None
        if not node or not node["active"]:
            send_or_edit(chat_id, "این بخش پیدا نشد یا قبلاً حذف شده است.", kb_admin_home(), edit_message_id)
            return
        icon = "🏛" if node["kind"] == "university" else "📁"
        label = "دانشگاه" if node["kind"] == "university" else "بخش سفارشی"
        title_line = f"<b>{icon} مدیریت {label}:</b> {esc(node['title'])}\n\nدرس‌ها یا زیربخش‌های این مورد رو اینجا مدیریت کن."

    all_children = get_children(parent_key)
    children, page, total_pages = paginate_rows(all_children, page)
    text = title_line
    if not all_children:
        text += "\n\n<i>هنوز چیزی اضافه نشده. با «➕ افزودن دکمه» شروع کن.</i>"
    kb = kb_admin_node(parent_key, node, children, page, total_pages)
    send_or_edit(chat_id, text, kb, edit_message_id)


def render_reorder(chat_id: int, user_id: int, edit_message_id: Optional[int] = None) -> None:
    state = get_state(user_id)
    ids: List[int] = state.get("reorder_ids", [])
    titles: Dict[int, str] = state.get("reorder_titles", {})
    picked: List[int] = state.get("reorder_picked", [])

    lines = [
        "<b>🔀 تغییر ترتیب دکمه‌ها</b>",
        "",
        "یکی از دو راه:",
        "۱. روی دکمه‌ها به همون ترتیبی که می‌خوای بزن.",
        "۲. یا همینجا شماره‌ها رو به ترتیب دلخواه بفرست (مثلاً: 3 1 2)",
        "",
        "<b>لیست فعلی:</b>",
    ]
    for idx, nid in enumerate(ids, 1):
        title = esc(titles.get(nid, "—"))
        if nid in picked:
            rank = picked.index(nid) + 1
            lines.append(f"{idx}. {title}  ✅ (ترتیب جدید: {rank})")
        else:
            lines.append(f"{idx}. {title}")

    kb = types.InlineKeyboardMarkup(row_width=1)
    for idx, nid in enumerate(ids, 1):
        if nid in picked:
            continue
        kb.add(types.InlineKeyboardButton(f"{idx}) {titles.get(nid, '—')}", callback_data=f"admin:reorderpick:{nid}"))
    if picked:
        kb.add(types.InlineKeyboardButton("✅ اعمال همین ترتیب (بقیه به ترتیب قبلی)", callback_data="admin:reorderapply"))
        kb.add(types.InlineKeyboardButton("🔄 شروع دوباره", callback_data="admin:reorderreset"))
    kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="admin:reordercancel"))
    send_or_edit(chat_id, "\n".join(lines), kb, edit_message_id)


def finalize_reorder(user_id: int, chat_id: int, message_id: int, final_order: List[int]) -> None:
    state = get_state(user_id)
    ids: List[int] = state.get("reorder_ids", [])
    parent_key = state.get("reorder_parent", "universities")
    return_msg = state.get("reorder_return_msg", message_id)
    # Anything not explicitly placed keeps its old relative order, appended at the end.
    seen = set(final_order)
    complete_order = final_order + [nid for nid in ids if nid not in seen]
    for pos, nid in enumerate(complete_order):
        update_node(nid, order_index=pos)
    clear_state(user_id)
    render_admin_node(chat_id, user_id, parent_key, edit_message_id=return_msg)


def kb_cancel_prompt(cancel_cb: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ لغو", callback_data=cancel_cb))
    return kb


def kb_anon_links(rows: List[sqlite3.Row]) -> types.InlineKeyboardMarkup:
    """Admin-only management screen. Trace toggle and reveal-sender are only ever shown here."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    for row in rows[:25]:
        trace = "🔍 ردیابی: روشن" if row["traceable"] else "🙈 ردیابی: خاموش"
        kb.add(
            types.InlineKeyboardButton(f"{row['title']}", callback_data=f"anon:messages:{row['code']}:0"),
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

def render_menu(chat_id: int, user_id: int, parent_key: str = "root", edit_message_id: Optional[int] = None, page: int = 0) -> None:
    admin_mode = is_admin(user_id)
    if parent_key == "root":
        text = main_menu_text()
        kb = kb_main(user_id)
        send_or_edit(chat_id, text, kb, edit_message_id)
        return

    if parent_key == "universities":
        all_children = get_children("universities")
        children, page, total_pages = paginate_rows(all_children, page)
        text = "<b>🏛 دانشگاه‌ها</b>\n\nدانشگاهت رو انتخاب کن تا بریم سراغ درس‌ها."
        kb = kb_children(parent_key, children, admin_mode=admin_mode, calc_mode=False, page=page, total_pages=total_pages, page_prefix="menu:universities")
        send_or_edit(chat_id, text, kb, edit_message_id)
        return

    node_id = parse_node_key(parent_key)
    if node_id is not None:
        node = get_node(node_id)
        if not node:
            send_or_edit(chat_id, "این بخش پیدا نشد.", kb_main(user_id), edit_message_id)
            return
        if node["kind"] in ("university", "custom"):
            all_children = get_children(node_key(node_id))
            children, page, total_pages = paginate_rows(all_children, page)
            label = "🏛 دانشگاه" if node["kind"] == "university" else "📁 بخش"
            text = f"<b>{label}:</b> {esc(node['title'])}\n\n"
            if node["kind"] == "university":
                text += "درس‌های این دانشگاه رو انتخاب کن."
            else:
                text += "یکی از گزینه‌های زیر رو انتخاب کن."
            kb = kb_children(node_key(node_id), children, admin_mode=admin_mode, calc_mode=False, page=page, total_pages=total_pages, page_prefix=f"menu:{node_key(node_id)}")
            send_or_edit(chat_id, text, kb, edit_message_id)
            return

    send_or_edit(chat_id, "این منو وجود ندارد.", kb_main(user_id), edit_message_id)


def send_or_edit(chat_id: int, text: str, markup: Optional[types.InlineKeyboardMarkup] = None, message_id: Optional[int] = None) -> Optional[int]:
    try:
        if message_id:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup, parse_mode="HTML")
            return message_id
        msg = bot.send_message(chat_id, text, reply_markup=markup)
        return msg.message_id
    except Exception:
        msg = bot.send_message(chat_id, text, reply_markup=markup)
        return msg.message_id

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
        "container_id": university_node_id,
        "items_by_container": {},
        "awaiting": None,
        "history_id": None,
        "editing": False,
        "course_page": 0,
        "list_msg_id": None,
    }
    set_state(user_id, **session)
    show_calc_menu(chat_id, user_id, edit_message_id)

def show_calc_menu(chat_id: int, user_id: int, edit_message_id: Optional[int] = None, page: Optional[int] = None) -> None:
    state = get_state(user_id)
    uni_id = state.get("university_node_id")
    if not uni_id:
        start_calc(user_id, chat_id, None, edit_message_id)
        return

    container_id = int(state.get("container_id") or uni_id)
    if page is None:
        page = int(state.get("course_page", 0) or 0)

    all_children = get_children(node_key(container_id))
    folders = [c for c in all_children if c["kind"] == "custom"]
    courses = [c for c in all_children if c["kind"] == "course"]
    finalize_buttons = [c for c in all_children if c["kind"] == "finalize"]

    courses_page, page, total_pages = paginate_rows(courses, page)
    set_state(user_id, course_page=page, container_id=container_id)

    items = container_items(state, container_id)
    title = container_breadcrumb(container_id)
    text = calc_status_text(title, items)
    if not folders and not courses:
        text += "\n\n⚠️ هنوز چیزی برای این بخش ثبت نشده است."

    kb = types.InlineKeyboardMarkup(row_width=1)
    for folder in folders:
        kb.add(types.InlineKeyboardButton(f"📁 {folder['title']}", callback_data=f"calcfolder:{folder['id']}"))

    items_by_course_id = {it["course_id"]: it for it in items}
    for course in courses_page:
        picked_item = items_by_course_id.get(course["id"])
        if picked_item and picked_item.get("grade") is not None:
            mark = "✅ "
            grade_suffix = f" | نمره: {fmt_num(picked_item['grade'])}"
        else:
            mark = "📘 "
            grade_suffix = ""
        kb.add(
            types.InlineKeyboardButton(
                f"{mark}{course['title']} | واحد: {fmt_num(course['credits'])}{grade_suffix}",
                callback_data=f"pick:{course['id']}",
            )
        )
    add_pagination_row(kb, page, total_pages, "calc:coursepage")

    # Admin-placed "finalize" buttons: can live anywhere in the tree, any
    # number of times, and are added/removed just like any other button.
    for fb in finalize_buttons:
        kb.add(types.InlineKeyboardButton(f"✅ {fb['title']}", callback_data="calc:finalize"))

    if items and not finalize_buttons:
        # Fallback so the flow still works in sections where the admin
        # hasn't placed a dedicated finalize button yet.
        kb.add(types.InlineKeyboardButton("✅ نهایی کردن معدل این بخش", callback_data="calc:finalize"))
    if container_id != int(uni_id):
        kb.add(types.InlineKeyboardButton("↩️ بازگشت به بخش قبلی", callback_data="calc:backfolder"))
    kb.add(types.InlineKeyboardButton("🚪 خروج به منوی اصلی", callback_data="menu:root"))

    msg_id = send_or_edit(chat_id, text, kb, edit_message_id)
    set_state(user_id, list_msg_id=msg_id)

def load_history_into_state(user_id: int, history_row: sqlite3.Row) -> None:
    detail = json.loads(history_row["detail_json"])
    uni_id = detail.get("university_node_id")
    container_id = detail.get("container_id", uni_id)
    items = detail.get("items", [])
    set_state(
        user_id,
        mode="calc_running",
        university_node_id=uni_id,
        university_title=detail.get("university_title"),
        container_id=container_id,
        items_by_container={str(container_id): items},
        awaiting=None,
        history_id=history_row["id"],
        editing=True,
        course_page=0,
        list_msg_id=None,
    )

def finalize_calc(chat_id: int, user_id: int) -> None:
    state = get_state(user_id)
    uni_id = state.get("university_node_id")
    container_id = int(state.get("container_id") or uni_id)
    items = container_items(state, container_id)
    total_credits, total_weighted, gpa = gpa_from_items(items)
    if not items or total_credits <= 0:
        bot.send_message(
            chat_id,
            "⚠️ هنوز هیچ نمره‌ی معتبری ثبت نکرده‌ای.\nحداقل یک درس رو انتخاب کن و نمره‌اش رو وارد کن.",
            reply_markup=kb_main(user_id),
        )
        return

    title = container_breadcrumb(container_id)
    detail = {
        "university_node_id": uni_id,
        "university_title": state.get("university_title"),
        "container_id": container_id,
        "container_title": title,
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
            (uni_id, title, total_credits, total_weighted, gpa, json.dumps(detail, ensure_ascii=False), now_iso(), state.get("history_id"), user_id),
        )
        title_line = "<b>✅ محاسبه ویرایش و به‌روزرسانی شد</b>"
    else:
        db_execute(
            """
            INSERT INTO calculator_history(user_id, university_node_id, university_title, total_credits, total_weighted, gpa, detail_json, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, uni_id, title, total_credits, total_weighted, gpa, json.dumps(detail, ensure_ascii=False), now_iso(), now_iso()),
        )
        title_line = "<b>✅ معدل محاسبه شد</b>"

    clear_state(user_id)
    text = (
        f"{title_line}\n\n"
        f"<b>📍 مسیر:</b> {esc(title)}\n"
        f"<b>📚 تعداد دروس:</b> {len(items)}\n"
        f"<b>🧮 جمع واحد:</b> <code>{fmt_num(total_credits)}</code>\n"
        f"<b>🎓 معدل:</b> <code>{gpa:.2f}</code>\n\n"
        "این محاسبه در تاریخچه‌ات ذخیره شد و هر وقت خواستی می‌تونی ویرایشش کنی."
    )
    bot.send_message(chat_id, text, reply_markup=kb_main(user_id))

# ---------------------------
# Anonymous messaging
# ---------------------------

def make_anon_link(code: str) -> str:
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=anon_{code}"
    return f"/start anon_{code}"


def ensure_anon_context(user_id: int, code: str) -> Optional[sqlite3.Row]:
    row = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
    if row:
        set_state(user_id, mode="anon_write", anon_code=code)
    return row


def get_or_create_personal_anon_link(user_id: int, display_name: str) -> sqlite3.Row:
    """Every user (admin or not) can own exactly one personal anonymous link."""
    row = db_execute(
        "SELECT * FROM anonymous_links WHERE admin_id = ? AND active = 1 ORDER BY created_at ASC LIMIT 1",
        (user_id,),
        fetchone=True,
    )
    if row:
        return row
    code = uuid.uuid4().hex[:10]
    title = f"لینک {display_name or user_id}".strip()
    db_execute(
        "INSERT INTO anonymous_links(code, admin_id, title, traceable, active, created_at) VALUES(?, ?, ?, 0, 1, ?)",
        (code, user_id, title, now_iso()),
    )
    return db_execute("SELECT * FROM anonymous_links WHERE code = ?", (code,), fetchone=True)


def get_or_create_default_admin_link() -> sqlite3.Row:
    """A single, private, non-browsable link used for the generic '✉️ پیام
    ناشناس' button so normal users never see a directory of everyone's links.
    """
    code = get_setting("default_anon_code")
    if code:
        row = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
        if row:
            return row
    owner = min(ADMIN_IDS) if ADMIN_IDS else 0
    new_code = uuid.uuid4().hex[:10]
    db_execute(
        "INSERT INTO anonymous_links(code, admin_id, title, traceable, active, created_at) VALUES(?, ?, ?, 0, 1, ?)",
        (new_code, owner, "پیام به ادمین", now_iso()),
    )
    set_setting("default_anon_code", new_code)
    return db_execute("SELECT * FROM anonymous_links WHERE code = ?", (new_code,), fetchone=True)


def send_anonymous_message_to_admin(code: str, user: types.User, text: str) -> Optional[int]:
    """Deliver an anonymous message to the link owner and return the new
    anonymous_messages row id (or None if the link doesn't exist).

    IMPORTANT: the notification the owner receives never contains the
    sender's name, username, user id, or even whether the link is
    traceable. That information is only ever retrievable by a real admin,
    on demand, from the dedicated "🔗 لینک‌های پیام ناشناس" settings screen -
    never inline in the message itself.

    The owner also gets a "↩️ پاسخ" button so a real back-and-forth
    conversation can happen while staying fully anonymous on the sender's
    side, and the sender gets back a message id they can later use to
    delete their own message if they change their mind.
    """
    link = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
    if not link:
        return None
    msg_id = db_execute(
        """
        INSERT INTO anonymous_messages(code, admin_id, user_id, sender_name, username, message_text, traceable, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            link["admin_id"],
            user.id,
            user.first_name or "",
            user.username or "",
            text,
            int(link["traceable"]),
            now_iso(),
        ),
    )
    text_owner = (
        f"<b>✉️ پیام ناشناس جدید</b>\n\n"
        f"<b>عنوان لینک:</b> {esc(link['title'])}\n"
        f"<b>شماره پیام:</b> <code>#{msg_id}</code>\n\n"
        f"<b>متن پیام:</b>\n{esc(text)}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("↩️ پاسخ", callback_data=f"anon:reply:{msg_id}"),
        types.InlineKeyboardButton("🗑 حذف", callback_data=f"anon:deletenotif:{msg_id}"),
    )
    try:
        sent = bot.send_message(int(link["admin_id"]), text_owner, reply_markup=kb)
        db_execute(
            "UPDATE anonymous_messages SET notif_chat_id = ?, notif_message_id = ? WHERE id = ?",
            (int(link["admin_id"]), sent.message_id, msg_id),
        )
    except Exception:
        pass
    return msg_id


def kb_anon_messages(code: str, rows: List[sqlite3.Row], page: int, total_pages: int, traceable: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        preview = row["message_text"].replace("\n", " ")
        if len(preview) > 30:
            preview = preview[:30] + "…"
        buttons = [types.InlineKeyboardButton(f"#{row['id']} | {preview}", callback_data="noop")]
        kb.row(*buttons)
        action_row = [types.InlineKeyboardButton("↩️ پاسخ", callback_data=f"anon:reply:{row['id']}")]
        if traceable:
            action_row.append(types.InlineKeyboardButton("🔍 نمایش فرستنده", callback_data=f"anon:reveal:{row['id']}"))
        kb.row(*action_row)
    add_pagination_row(kb, page, total_pages, f"anon:messages:{code}")
    kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data="admin:anonlinks"))
    return kb


# ---------------------------
# Backup and restore
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


def restore_from_backup(zip_path: Path) -> None:
    """Restore the SQLite database from a previously generated backup zip."""
    with DB_LOCK:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            db_name = next((n for n in names if n == DATABASE_PATH.name or n.endswith(".db")), None)
            if not db_name:
                raise ValueError("فایل دیتابیس (.db) در داخل بک‌آپ پیدا نشد.")
            extracted = zf.read(db_name)
        if DATABASE_PATH.exists():
            safety_copy = DATABASE_PATH.with_name(DATABASE_PATH.stem + ".before_restore.db")
            try:
                shutil.copy(DATABASE_PATH, safety_copy)
            except Exception:
                pass
        DATABASE_PATH.write_bytes(extracted)
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass


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
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("❌ لغو ارسال", callback_data="anon:cancelwrite"))
            bot.send_message(
                chat_id,
                f"<b>✉️ ارسال پیام ناشناس</b>\n\nلینک انتخاب شد: <b>{esc(link['title'])}</b>\nحالا متن پیامت رو بفرست.",
                reply_markup=kb,
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


@bot.message_handler(content_types=["document"])
def on_document(message: types.Message):
    save_user(message.from_user)
    user_id = message.from_user.id
    chat_id = message.chat.id
    state = get_state(user_id)

    if state.get("mode") == "admin_wait_restore_file":
        if not is_admin(user_id):
            bot.send_message(chat_id, "دسترسی ندارید.")
            clear_state(user_id)
            return
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = BACKUP_DIR / f"restore_{uuid.uuid4().hex[:8]}.zip"
            tmp_path.write_bytes(downloaded)
            restore_from_backup(tmp_path)
            clear_state(user_id)
            bot.send_message(
                chat_id,
                "<b>✅ بازیابی اطلاعات با موفقیت انجام شد.</b>\nیک نسخه از دیتابیس قبلی هم به‌عنوان پشتیبان نگه داشته شد.",
            )
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
        except Exception as e:
            bot.send_message(chat_id, f"❌ خطا در بازیابی اطلاعات: {esc(str(e))}")
        return
    # Documents outside of the restore flow are ignored.


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
            new_msg_id = send_anonymous_message_to_admin(code, message.from_user, text)
            clear_state(user_id)
            if new_msg_id:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🗑 حذف پیام", callback_data=f"anon:senderdelete:{new_msg_id}"))
                bot.send_message(
                    chat_id,
                    "<b>✅ پیامت ارسال شد.</b>\nاگه پشیمون شدی، تا وقتی که ادمین حذفش نکرده می‌تونی خودت پاکش کنی.",
                    reply_markup=kb,
                )
            else:
                bot.send_message(chat_id, "❌ این لینک دیگه معتبر نیست.")
            bot.send_message(chat_id, main_menu_text(), reply_markup=kb_main(user_id))
            return

    if state.get("mode") == "admin_reply_wait_text":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        target_user = state.get("reply_target_user")
        prompt_msg = state.get("reply_prompt_msg")
        reply_msg_id = state.get("reply_msg_id")
        source_row = get_anon_message(reply_msg_id) if reply_msg_id else None
        code = source_row["code"] if source_row else None
        sent_ok = True
        try:
            kb_user = None
            if code:
                kb_user = types.InlineKeyboardMarkup()
                kb_user.add(types.InlineKeyboardButton("↩️ پاسخ به ادمین", callback_data=f"anon:continue:{code}"))
            bot.send_message(
                int(target_user),
                f"<b>↩️ پاسخ ادمین به پیام ناشناس شما</b>\n\n{esc(text)}",
                reply_markup=kb_user,
            )
        except Exception:
            sent_ok = False
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        if prompt_msg:
            try:
                bot.delete_message(chat_id, prompt_msg)
            except Exception:
                pass
        if sent_ok:
            bot.send_message(chat_id, "<b>✅ پاسخ ارسال شد.</b>")
        else:
            bot.send_message(chat_id, "❌ ارسال پاسخ ناموفق بود (شاید کاربر ربات رو مسدود کرده باشد).")
        return

    if state.get("mode") == "admin_reorder":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        ids = state.get("reorder_ids", [])
        numbers = [int(n) for n in re.findall(r"\d+", text)]
        sequence: List[int] = []
        for n in numbers:
            if 1 <= n <= len(ids):
                nid = ids[n - 1]
                if nid not in sequence:
                    sequence.append(nid)
        if not sequence:
            bot.send_message(chat_id, "شماره‌های معتبر رو با فاصله یا کاما بفرست (مثلاً: 3 1 2) یا از دکمه‌ها استفاده کن.")
            return
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        return_msg = state.get("reorder_return_msg")
        bot.send_message(chat_id, "<b>✅ ترتیب جدید ذخیره شد.</b>")
        finalize_reorder(user_id, chat_id, return_msg, sequence)
        return

    if state.get("mode") == "admin_bulk_add_wait":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        parent_key = state.get("add_parent", "universities")
        return_msg = state.get("add_return_msg")
        prompt_msg = state.get("add_prompt_msg")
        items = parse_bulk_lines(text)
        if not items:
            bot.send_message(chat_id, "هیچ خط معتبری پیدا نشد. هر خط رو به شکل «عنوان - واحد» بفرست.")
            return
        created = 0
        for title, credits in items:
            if parent_key == "universities":
                create_node(parent_key, title, "university")
            elif credits is not None:
                create_node(parent_key, title, "course", credits=credits)
            else:
                create_node(parent_key, title, "custom")
            created += 1
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        if prompt_msg:
            try:
                bot.delete_message(chat_id, prompt_msg)
            except Exception:
                pass
        bot.send_message(chat_id, f"<b>✅ {created} دکمه اضافه شد.</b>")
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=return_msg)
        return

    if state.get("mode") == "admin_add_wait_title":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        prompt_msg = state.get("add_prompt_msg")
        add_parent = state.get("add_parent", "universities")
        set_state(user_id, mode="admin_add_wait_kind", add_title=text)
        kb = types.InlineKeyboardMarkup(row_width=1)
        if add_parent == "universities":
            kb.add(types.InlineKeyboardButton("🏛 دانشگاه", callback_data="admin:kindpick:university"))
            kb.add(types.InlineKeyboardButton("📁 سفارشی (پوشه)", callback_data="admin:kindpick:custom"))
        else:
            kb.add(types.InlineKeyboardButton("📘 درس", callback_data="admin:kindpick:course"))
            kb.add(types.InlineKeyboardButton("📁 زیرپوشه", callback_data="admin:kindpick:custom"))
            kb.add(types.InlineKeyboardButton("✅ دکمه‌ی ثبت نهایی معدل", callback_data="admin:kindpick:finalize"))
        kb.add(types.InlineKeyboardButton("❌ لغو", callback_data="admin:addcancel"))
        try:
            bot.edit_message_text(
                f"<b>➕ افزودن دکمه</b>\n\n<b>عنوان:</b> {esc(text)}\n\nنوع دکمه رو انتخاب کن:",
                chat_id,
                prompt_msg,
                reply_markup=kb,
            )
        except Exception:
            bot.send_message(chat_id, "نوع دکمه رو انتخاب کن:", reply_markup=kb)
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        return

    if state.get("mode") == "admin_add_wait_kind":
        # Kind must be picked via the inline buttons above, not free text.
        bot.send_message(chat_id, "لطفاً یکی از دکمه‌های بالا رو برای انتخاب نوع بزن. 🙂")
        return

    if state.get("mode") == "admin_add_wait_credits":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        prompt_msg = state.get("add_prompt_msg")
        try:
            credits = float(text)
            if credits <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, "عدد معتبر و بزرگ‌تر از صفر بفرست (مثلاً 2 یا 1.5).")
            return
        parent_key = state.get("add_parent", "universities")
        title = state.get("add_title", "")
        return_msg = state.get("add_return_msg")
        new_id = create_node(parent_key, title, "course", credits=credits)
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, prompt_msg)
        except Exception:
            pass
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=return_msg)
        return

    if state.get("mode") == "admin_edit_wait_title":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        node_id = state.get("edit_node_id")
        node = get_node_any(node_id) if node_id else None
        if not node:
            clear_state(user_id)
            bot.send_message(chat_id, "این دکمه دیگر پیدا نشد.")
            return
        update_node(node_id, title=text)
        prompt_msg = state.get("edit_prompt_msg")
        return_msg = state.get("edit_return_msg")
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        if node["kind"] == "course":
            set_state(user_id, mode="admin_edit_wait_credits")
            try:
                bot.edit_message_text(
                    f"<b>✏️ ویرایش</b>\n\n<b>عنوان جدید:</b> {esc(text)}\n\nواحد جدید این درس رو بفرست (یا همون عدد قبلی رو دوباره بفرست):",
                    chat_id,
                    prompt_msg,
                    reply_markup=kb_cancel_prompt("admin:editcancel"),
                )
            except Exception:
                pass
            return
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, prompt_msg)
        except Exception:
            pass
        render_admin_node(chat_id, user_id, node["parent_key"], edit_message_id=return_msg)
        return

    if state.get("mode") == "admin_edit_wait_credits":
        if not is_admin(user_id):
            clear_state(user_id)
            return
        try:
            credits = float(text)
            if credits <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, "عدد معتبر و بزرگ‌تر از صفر بفرست.")
            return
        node_id = state.get("edit_node_id")
        node = get_node_any(node_id) if node_id else None
        if not node:
            clear_state(user_id)
            bot.send_message(chat_id, "این درس دیگر پیدا نشد.")
            return
        update_node(node_id, credits=credits)
        prompt_msg = state.get("edit_prompt_msg")
        return_msg = state.get("edit_return_msg")
        parent_key = node["parent_key"]
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        try:
            bot.delete_message(chat_id, prompt_msg)
        except Exception:
            pass
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=return_msg)
        return

    if state.get("mode") == "admin_wait_broadcast":
        if text == "بازگشت":
            clear_state(user_id)
            bot.send_message(chat_id, "لغو شد.", reply_markup=types.ReplyKeyboardRemove())
            bot.send_message(chat_id, "<b>🛠 پنل ادمین</b>", reply_markup=kb_admin_home())
            return

        target = state.get("broadcast_target", "all")
        if target == "university":
            uni_id = state.get("broadcast_university_id")
            rows = db_execute(
                "SELECT DISTINCT user_id FROM calculator_history WHERE university_node_id = ?",
                (uni_id,),
                fetchall=True,
            )
            label = f"کاربران «{state.get('broadcast_university_title', '')}»"
        else:
            rows = db_execute("SELECT user_id FROM users", fetchall=True)
            label = "همه کاربران"

        sent = 0
        failed = 0
        for row in rows:
            try:
                bot.send_message(int(row["user_id"]), text)
                sent += 1
            except Exception:
                failed += 1
        clear_state(user_id)
        bot.send_message(
            chat_id,
            f"<b>📣 پیام ارسال شد ({esc(label)})</b>\n\n✅ موفق: <code>{sent}</code>\n❌ ناموفق: <code>{failed}</code>",
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

        container_id = int(state.get("container_id") or state.get("university_node_id") or 0)
        items = container_items(state, container_id)
        replaced = False
        for item in items:
            if item["course_id"] == course_id:
                item["grade"] = grade
                replaced = True
                break
        if not replaced:
            items.append({
                "course_id": course_id,
                "title": course["title"],
                "credits": float(course["credits"] or 0),
                "grade": grade,
            })
        set_container_items(user_id, container_id, items)

        list_msg_id = state.get("list_msg_id")
        page = state.get("course_page", 0)
        grade_prompt_msg = state.get("grade_prompt_msg")

        set_state(user_id, mode="calc_running", awaiting=None, course_id=None, grade_prompt_msg=None)

        # Delete the user's own grade message and the bot's grade-request
        # prompt so the chat doesn't get cluttered after a grade is saved.
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        if grade_prompt_msg:
            try:
                bot.delete_message(chat_id, grade_prompt_msg)
            except Exception:
                pass

        show_calc_menu(chat_id, user_id, edit_message_id=list_msg_id, page=page)
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

    if data == "noop":
        bot.answer_callback_query(call.id)
        return

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
        parts = data.split(":")
        parent_key = parts[1]
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        if parent_key == "root":
            clear_state(user_id)
        render_menu(chat_id, user_id, parent_key, call.message.message_id, page=page)
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
            container_id = int(state.get("container_id") or state.get("university_node_id") or 0)
            items = container_items(state, container_id)
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
            prompt = bot.send_message(
                chat_id,
                f"<b>📘 {esc(node['title'])}</b>\n\n<b>واحد:</b> <code>{fmt_num(node['credits'])}</code>{extra}\n\nنمره‌ی این درس رو بفرست (مثلاً <code>18.25</code>)",
                reply_markup=kb,
            )
            set_state(user_id, grade_prompt_msg=prompt.message_id)
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
            set_state(user_id, mode="calc_running", course_id=None, awaiting=None, grade_prompt_msg=None)
        list_msg_id = state.get("list_msg_id")
        page = state.get("course_page", 0)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "لغو شد.")
        show_calc_menu(chat_id, user_id, edit_message_id=list_msg_id, page=page)
        return

    if data.startswith("calc:coursepage:"):
        page = int(data.split(":")[-1])
        show_calc_menu(chat_id, user_id, call.message.message_id, page=page)
        bot.answer_callback_query(call.id)
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
    if data.startswith("calcfolder:"):
        folder_id = int(data.split(":", 1)[1])
        node = get_node(folder_id)
        if not node or node["kind"] != "custom":
            bot.answer_callback_query(call.id, "این بخش پیدا نشد.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") not in ("calc_running", "calc_choose_grade"):
            bot.answer_callback_query(call.id, "ابتدا دانشگاه رو انتخاب کن.", show_alert=True)
            return
        set_state(user_id, container_id=folder_id, course_page=0)
        show_calc_menu(chat_id, user_id, call.message.message_id, page=0)
        bot.answer_callback_query(call.id)
        return

    if data == "calc:backfolder":
        state = get_state(user_id)
        uni_id = state.get("university_node_id")
        container_id = state.get("container_id")
        if not uni_id or not container_id or int(container_id) == int(uni_id):
            bot.answer_callback_query(call.id)
            return
        node = get_node_any(int(container_id))
        parent_id = parse_node_key(node["parent_key"]) if node else None
        new_container = parent_id if parent_id is not None else int(uni_id)
        set_state(user_id, container_id=new_container, course_page=0)
        show_calc_menu(chat_id, user_id, call.message.message_id, page=0)
        bot.answer_callback_query(call.id)
        return
        
    if data == "admin:home":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        clear_state(user_id)
        send_or_edit(chat_id, "<b>🛠 پنل ادمین</b>\n\nاز بخش موردنظر استفاده کن.", kb_admin_home(), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:manage:") or data.startswith("admin:managepage:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        # data shapes: admin:manage:<parent_key>:<page>  /  admin:managepage:<parent_key>:<page>
        parts = data.split(":")
        parent_key = parts[2]
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        render_admin_node(chat_id, user_id, parent_key, call.message.message_id, page=page)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:addstart:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parent_key = data.split(":", 2)[2]
        set_state(
            user_id,
            mode="admin_add_wait_title",
            add_parent=parent_key,
            add_return_msg=call.message.message_id,
        )
        prompt = bot.send_message(
            chat_id,
            "<b>➕ افزودن دکمه</b>\n\nعنوان دکمه جدید رو بفرست:",
            reply_markup=kb_cancel_prompt("admin:addcancel"),
        )
        set_state(user_id, add_prompt_msg=prompt.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:bulkaddstart:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parent_key = data.split(":", 2)[2]
        set_state(
            user_id,
            mode="admin_bulk_add_wait",
            add_parent=parent_key,
            add_return_msg=call.message.message_id,
        )
        if parent_key == "universities":
            example = "دانشگاه علوم پزشکی شیراز\nدانشگاه علوم پزشکی اصفهان"
            hint = "چون توی لیست دانشگاه‌هایی، فقط اسم هر خط به‌عنوان یک دانشگاه اضافه میشه (واحد لازم نیست)."
        else:
            example = "آناتومی - 3\nفیزیولوژی - 4\nبیوشیمی - 2"
            hint = "هر خط رو به شکل «عنوان - واحد» بفرست تا همه یهو به‌عنوان درس اضافه بشن."
        prompt = bot.send_message(
            chat_id,
            f"<b>➕ افزودن گروهی دکمه</b>\n\n{hint}\n\nمثال:\n<code>{esc(example)}</code>",
            reply_markup=kb_cancel_prompt("admin:addcancel"),
        )
        set_state(user_id, add_prompt_msg=prompt.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:addcancel":
        clear_state(user_id)
        try:
            bot.edit_message_text("لغو شد.", chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "لغو شد.")
        return

    if data.startswith("admin:kindpick:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_add_wait_kind":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        kind = data.split(":", 2)[2]
        parent_key = state.get("add_parent", "universities")
        title = state.get("add_title", "")
        prompt_msg = state.get("add_prompt_msg")
        return_msg = state.get("add_return_msg")
        if kind == "course":
            set_state(user_id, mode="admin_add_wait_credits", add_kind="course")
            try:
                bot.edit_message_text(
                    f"<b>➕ افزودن درس</b>\n\n<b>عنوان:</b> {esc(title)}\n\nتعداد واحد این درس رو بفرست (مثلاً 2 یا 1.5):",
                    chat_id,
                    prompt_msg,
                    reply_markup=kb_cancel_prompt("admin:addcancel"),
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id)
            return
        new_id = create_node(parent_key, title, kind)
        clear_state(user_id)
        try:
            bot.delete_message(chat_id, prompt_msg)
        except Exception:
            pass
        kind_label = {"university": "دانشگاه", "custom": "بخش سفارشی", "finalize": "دکمه‌ی ثبت نهایی"}.get(kind, kind)
        bot.answer_callback_query(call.id, f"✅ {kind_label} اضافه شد.")
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=return_msg)
        return

    if data.startswith("admin:editnode:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        node_id = int(data.split(":")[-1])
        node = get_node_any(node_id)
        if not node or not node["active"]:
            bot.answer_callback_query(call.id, "پیدا نشد یا قبلاً حذف شده.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_edit_wait_title",
            edit_node_id=node_id,
            edit_return_msg=call.message.message_id,
        )
        prompt = bot.send_message(
            chat_id,
            f"<b>✏️ ویرایش</b>\n\n<b>عنوان فعلی:</b> {esc(node['title'])}\n\nعنوان جدید رو بفرست:",
            reply_markup=kb_cancel_prompt("admin:editcancel"),
        )
        set_state(user_id, edit_prompt_msg=prompt.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:editcancel":
        clear_state(user_id)
        try:
            bot.edit_message_text("لغو شد.", chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "لغو شد.")
        return

    if data.startswith("admin:delconfirm:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        node_id = int(data.split(":")[-1])
        node = get_node(node_id)
        if not node:
            bot.answer_callback_query(call.id, "پیدا نشد یا قبلاً حذف شده.", show_alert=True)
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"admin:delyes:{node_id}"),
            types.InlineKeyboardButton("❌ انصراف", callback_data=f"admin:manage:{node['parent_key']}:0"),
        )
        send_or_edit(
            chat_id,
            f"آیا از حذف «{esc(node['title'])}» مطمئنی؟\n⚠️ اگر این دکمه زیرمجموعه (مثل درس‌ها) داشته باشه، اونا هم حذف میشن.",
            kb,
            call.message.message_id,
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:delyes:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        node_id = int(data.split(":")[-1])
        node = get_node_any(node_id)
        parent_key = node["parent_key"] if node else "universities"
        delete_node(node_id)
        bot.answer_callback_query(call.id, "حذف شد.")
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=call.message.message_id)
        return

    if data.startswith("admin:movestart:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        node_id = int(data.split(":")[-1])
        node = get_node(node_id)
        if not node:
            bot.answer_callback_query(call.id, "پیدا نشد یا قبلاً حذف شده.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_moving",
            move_node_id=node_id,
            move_browse_key="universities",
            move_return_msg=call.message.message_id,
            move_return_parent=node["parent_key"],
        )
        render_move_browser(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:movebrowse:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_moving":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        target_key = data.split(":", 2)[2]
        set_state(user_id, move_browse_key=target_key)
        render_move_browser(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:movehere":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_moving":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        node_id = state.get("move_node_id")
        dest_key = state.get("move_browse_key", "universities")
        node = get_node(node_id) if node_id else None
        if not node:
            bot.answer_callback_query(call.id, "این دکمه دیگر پیدا نشد.", show_alert=True)
            clear_state(user_id)
            return
        if dest_key == node_key(node_id) or is_descendant_key(dest_key, node_id):
            bot.answer_callback_query(call.id, "نمی‌تونی چیزی رو داخل خودش یا زیرمجموعه‌ی خودش منتقل کنی.", show_alert=True)
            return
        if dest_key == node["parent_key"]:
            bot.answer_callback_query(call.id, "همین الان همین‌جاست.", show_alert=True)
            return
        # courses/finalize buttons can only live inside a university/custom
        # container; university/custom items can only live inside another
        # container or at the root "universities" list.
        update_node(node_id, parent_key=dest_key)
        return_parent = state.get("move_return_parent", "universities")
        return_msg = state.get("move_return_msg")
        clear_state(user_id)
        bot.answer_callback_query(call.id, "✅ منتقل شد.")
        render_admin_node(chat_id, user_id, return_parent, edit_message_id=return_msg)
        return

    if data == "admin:movecancel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        return_parent = state.get("move_return_parent", "universities")
        return_msg = state.get("move_return_msg")
        clear_state(user_id)
        bot.answer_callback_query(call.id, "لغو شد.")
        render_admin_node(chat_id, user_id, return_parent, edit_message_id=return_msg)
        return

    if data.startswith("admin:bulkcopystart:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parent_key = data.split(":", 2)[2]
        all_children = get_children(parent_key)
        if not all_children:
            bot.answer_callback_query(call.id, "این بخش هیچ موردی نداره که کپی بشه.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_bulk_copy_select",
            bulkcopy_parent=parent_key,
            bulkcopy_selected=[],
            bulkcopy_return_msg=call.message.message_id,
        )
        render_bulk_copy_select(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:bulkcopytoggle:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_bulk_copy_select":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        nid = int(data.split(":")[-1])
        selected = list(state.get("bulkcopy_selected", []))
        if nid in selected:
            selected.remove(nid)
        else:
            selected.append(nid)
        set_state(user_id, bulkcopy_selected=selected)
        render_bulk_copy_select(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:bulkcopynext":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_bulk_copy_select" or not state.get("bulkcopy_selected"):
            bot.answer_callback_query(call.id, "حداقل یک مورد رو انتخاب کن.", show_alert=True)
            return
        set_state(user_id, mode="admin_bulk_copy_browse", bulkcopy_browse_key="universities")
        render_bulk_copy_browser(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:bulkcopybrowse:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_bulk_copy_browse":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        target_key = data.split(":", 2)[2]
        set_state(user_id, bulkcopy_browse_key=target_key)
        render_bulk_copy_browser(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:bulkcopyhere":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_bulk_copy_browse":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        selected = state.get("bulkcopy_selected", [])
        dest_key = state.get("bulkcopy_browse_key", "universities")
        for nid in selected:
            duplicate_node(nid, dest_key)
        return_parent = state.get("bulkcopy_parent", "universities")
        return_msg = state.get("bulkcopy_return_msg")
        clear_state(user_id)
        bot.answer_callback_query(call.id, f"✅ {len(selected)} مورد کپی شد.")
        render_admin_node(chat_id, user_id, return_parent, edit_message_id=return_msg)
        return

    if data == "admin:bulkcopycancel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        return_parent = state.get("bulkcopy_parent", "universities")
        return_msg = state.get("bulkcopy_return_msg")
        clear_state(user_id)
        bot.answer_callback_query(call.id, "لغو شد.")
        render_admin_node(chat_id, user_id, return_parent, edit_message_id=return_msg)
        return

    if data.startswith("admin:reorderstart:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parent_key = data.split(":", 2)[2]
        all_children = get_children(parent_key)
        if len(all_children) < 2:
            bot.answer_callback_query(call.id, "برای ترتیب‌بندی حداقل ۲ مورد لازمه.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_reorder",
            reorder_parent=parent_key,
            reorder_ids=[c["id"] for c in all_children],
            reorder_titles={c["id"]: c["title"] for c in all_children},
            reorder_picked=[],
            reorder_return_msg=call.message.message_id,
        )
        render_reorder(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:reorderpick:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_reorder":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        nid = int(data.split(":")[-1])
        ids = state.get("reorder_ids", [])
        picked = state.get("reorder_picked", [])
        if nid in ids and nid not in picked:
            picked = picked + [nid]
            set_state(user_id, reorder_picked=picked)
        if len(picked) >= len(ids):
            bot.answer_callback_query(call.id, "ترتیب ذخیره شد.")
            finalize_reorder(user_id, chat_id, call.message.message_id, picked)
            return
        render_reorder(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:reorderapply":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        if state.get("mode") != "admin_reorder":
            bot.answer_callback_query(call.id, "این درخواست منقضی شده.", show_alert=True)
            return
        picked = state.get("reorder_picked", [])
        bot.answer_callback_query(call.id, "ترتیب ذخیره شد.")
        finalize_reorder(user_id, chat_id, call.message.message_id, picked)
        return

    if data == "admin:reorderreset":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        set_state(user_id, reorder_picked=[])
        render_reorder(chat_id, user_id, call.message.message_id)
        bot.answer_callback_query(call.id, "از اول شروع شد.")
        return

    if data == "admin:reordercancel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        state = get_state(user_id)
        parent_key = state.get("reorder_parent", "universities")
        clear_state(user_id)
        bot.answer_callback_query(call.id, "لغو شد.")
        render_admin_node(chat_id, user_id, parent_key, edit_message_id=call.message.message_id)
        return

    if data == "admin:broadcast":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🌐 همه کاربران", callback_data="admin:broadcast:all"))
        kb.add(types.InlineKeyboardButton("🏛 فقط کاربران یک دانشگاه خاص", callback_data="admin:broadcast:byuni"))
        kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data="admin:home"))
        send_or_edit(chat_id, "<b>📣 پیام همگانی</b>\n\nمخاطبین پیام رو انتخاب کن:", kb, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:broadcast:all":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        set_state(user_id, mode="admin_wait_broadcast", broadcast_target="all")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("بازگشت")
        bot.send_message(chat_id, "متن پیام همگانی رو بفرست (برای همه‌ی کاربران ارسال میشه).", reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:broadcast:byuni"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parts = data.split(":")
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        all_universities = get_children("universities")
        if not all_universities:
            bot.answer_callback_query(call.id, "هنوز دانشگاهی ثبت نشده.", show_alert=True)
            return
        universities, page, total_pages = paginate_rows(all_universities, page)
        kb = types.InlineKeyboardMarkup(row_width=1)
        for uni in universities:
            kb.add(types.InlineKeyboardButton(f"🏛 {uni['title']}", callback_data=f"admin:broadcast:uni:{uni['id']}"))
        add_pagination_row(kb, page, total_pages, "admin:broadcast:byuni")
        kb.add(types.InlineKeyboardButton("↩️ بازگشت", callback_data="admin:broadcast"))
        send_or_edit(
            chat_id,
            "<b>🏛 انتخاب دانشگاه</b>\n\nپیام فقط برای کاربرانی ارسال میشه که معدل این دانشگاه رو ثبت کرده باشن.\n(هر دانشگاه جدیدی که اضافه کنی، خودکار همینجا اضافه میشه.)",
            kb,
            call.message.message_id,
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin:broadcast:uni:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        uni_id = int(data.split(":")[-1])
        uni = get_node(uni_id)
        if not uni:
            bot.answer_callback_query(call.id, "دانشگاه پیدا نشد.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_wait_broadcast",
            broadcast_target="university",
            broadcast_university_id=uni_id,
            broadcast_university_title=uni["title"],
        )
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add("بازگشت")
        bot.send_message(
            chat_id,
            f"متن پیام رو بفرست. فقط به کاربرانی که معدل «{esc(uni['title'])}» رو ثبت کرده باشن ارسال میشه.",
            reply_markup=kb,
        )
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

    if data == "admin:restore":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        set_state(user_id, mode="admin_wait_restore_file")
        bot.send_message(
            chat_id,
            "<b>📥 بازیابی اطلاعات</b>\n\n"
            "فایل بک‌آپ (zip ساخته‌شده توسط دکمه‌ی «💾 بک‌آپ فوری») رو به‌صورت داکیومنت بفرست.\n"
            "⚠️ توجه: با این کار اطلاعات فعلی جایگزین میشه (یک نسخه‌ی احتیاطی از وضعیت فعلی هم نگه داشته میشه).",
        )
        bot.answer_callback_query(call.id)
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
            "هر لینک می‌تونه برای دریافت پیام از کاربران استفاده بشه (شامل لینک‌های شخصی کاربرای عادی).\n"
            "پیام‌هایی که برای صاحب هر لینک ارسال میشن هیچ نام یا هویتی رو نشون نمیدن؛ "
            "فقط از همینجا و فقط ادمین می‌تونه (در صورت روشن بودن ردیابی) فرستنده‌ی یک پیام مشخص رو ببینه.\n"
            "<i>این بخش فقط اینجا و فقط برای ادمین قابل مشاهده و تغییره.</i>"
        )
        send_or_edit(chat_id, text, kb_anon_links(rows), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("anon:messages:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        parts = data.split(":")
        code = parts[2]
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        link = db_execute("SELECT * FROM anonymous_links WHERE code = ?", (code,), fetchone=True)
        if not link:
            bot.answer_callback_query(call.id, "لینک پیدا نشد.", show_alert=True)
            return
        all_rows = db_execute(
            "SELECT * FROM anonymous_messages WHERE code = ? ORDER BY id DESC",
            (code,),
            fetchall=True,
        )
        rows, page, total_pages = paginate_rows(all_rows, page)
        text = (
            f"<b>✉️ پیام‌های لینک «{esc(link['title'])}»</b>\n\n"
            + ("هنوز پیامی دریافت نشده." if not all_rows else "برای پاسخ یا دیدن فرستنده‌ی هر پیام (در صورت روشن بودن ردیابی) روی دکمه‌ی مربوطه بزن.")
        )
        send_or_edit(chat_id, text, kb_anon_messages(code, rows, page, total_pages, bool(link["traceable"])), call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("anon:reveal:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        msg_id = int(data.split(":")[-1])
        row = db_execute("SELECT * FROM anonymous_messages WHERE id = ?", (msg_id,), fetchone=True)
        if not row:
            bot.answer_callback_query(call.id, "پیدا نشد.", show_alert=True)
            return
        if not row["traceable"]:
            bot.answer_callback_query(call.id, "این لینک بدون ردیابیه؛ فرستنده قابل نمایش نیست.", show_alert=True)
            return
        username = f"@{row['username']}" if row["username"] else "ندارد"
        alert = f"فرستنده: {row['sender_name'] or 'کاربر'}\nیوزرنیم: {username}\nآیدی: {row['user_id']}"
        bot.answer_callback_query(call.id, alert, show_alert=True)
        return

    if data.startswith("anon:reply:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        msg_id = int(data.split(":")[-1])
        row = db_execute("SELECT * FROM anonymous_messages WHERE id = ?", (msg_id,), fetchone=True)
        if not row:
            bot.answer_callback_query(call.id, "این پیام دیگر پیدا نشد.", show_alert=True)
            return
        set_state(
            user_id,
            mode="admin_reply_wait_text",
            reply_target_user=row["user_id"],
            reply_msg_id=msg_id,
        )
        prompt = bot.send_message(
            chat_id,
            f"<b>↩️ پاسخ به پیام ناشناس #{msg_id}</b>\n\nمتن پاسخ رو بفرست؛ مستقیم برای فرستنده ارسال میشه (فرستنده هم می‌تونه دوباره جواب بده تا گفتگو ادامه پیدا کنه).",
            reply_markup=kb_cancel_prompt("admin:replycancel"),
        )
        set_state(user_id, reply_prompt_msg=prompt.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin:replycancel":
        clear_state(user_id)
        try:
            bot.edit_message_text("لغو شد.", chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "لغو شد.")
        return

    if data.startswith("anon:deletenotif:"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "دسترسی ندارید.", show_alert=True)
            return
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "حذف شد.")
        return

    if data.startswith("anon:senderdelete:"):
        # The original sender can pull back their own anonymous message
        # (deleting the admin's notification copy too) if they regret it.
        msg_id = int(data.split(":")[-1])
        row = get_anon_message(msg_id)
        if not row or int(row["user_id"]) != user_id:
            bot.answer_callback_query(call.id, "این پیام دیگر پیدا نشد یا مال شما نیست.", show_alert=True)
            return
        if row["notif_chat_id"] and row["notif_message_id"]:
            try:
                bot.delete_message(int(row["notif_chat_id"]), int(row["notif_message_id"]))
            except Exception:
                pass
        db_execute("DELETE FROM anonymous_messages WHERE id = ?", (msg_id,))
        try:
            bot.edit_message_text("🗑 پیامت حذف شد.", chat_id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "پیام حذف شد.")
        return

    if data.startswith("anon:continue:"):
        # Lets the original sender reply again to the same anonymous
        # thread after the admin has responded, keeping the chat two-way
        # while staying fully anonymous.
        code = data.split(":", 2)[2]
        link = db_execute("SELECT * FROM anonymous_links WHERE code = ? AND active = 1", (code,), fetchone=True)
        if not link:
            bot.answer_callback_query(call.id, "این گفتگو دیگر فعال نیست.", show_alert=True)
            return
        set_state(user_id, mode="anon_write", anon_code=code)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ لغو ارسال", callback_data="anon:cancelwrite"))
        bot.send_message(
            chat_id,
            "متن پیامت رو بفرست؛ باز هم کاملاً ناشناس ارسال میشه.",
            reply_markup=kb,
        )
        bot.answer_callback_query(call.id)
        return

    if data == "anon:menu":
        # No directory of links is ever shown here — this is a private bot
        # used by many people, so browsing everyone else's anonymous link
        # would defeat the point. This button goes straight to a single
        # private "message the admin" channel; personal links are only
        # reachable through the link the owner explicitly shares.
        link = get_or_create_default_admin_link()
        set_state(user_id, mode="anon_write", anon_code=link["code"])
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ لغو ارسال", callback_data="anon:cancelwrite"))
        send_or_edit(
            chat_id,
            "<b>✉️ پیام ناشناس به ادمین</b>\n\nمتن پیامت رو بفرست؛ هویتت برای گیرنده نمایش داده نمیشه.",
            kb,
            call.message.message_id,
        )
        bot.answer_callback_query(call.id)
        return

    if data == "anon:cancelwrite":
        if get_state(user_id).get("mode") == "anon_write":
            clear_state(user_id)
        send_or_edit(chat_id, main_menu_text(), kb_main(user_id), call.message.message_id)
        bot.answer_callback_query(call.id, "لغو شد.")
        return

    if data == "anon:mylink":
        # Personal link for ANY user - no trace controls are ever shown here.
        row = get_or_create_personal_anon_link(user_id, call.from_user.first_name or "")
        url = make_anon_link(row["code"])
        text = (
            "<b>🔗 لینک پیام ناشناس شما</b>\n\n"
            f"<code>{esc(url)}</code>\n\n"
            "این لینک رو توی کانال، بیو، یا هرجایی که دوست داری بذار. هرکسی روش بزنه می‌تونه "
            "بدون اینکه هویتش برات مشخص بشه، مستقیم توی همین ربات برات پیام بفرسته.\n\n"
            "<b>✨ پیشنهاد:</b> این متن آماده رو کپی کن و زیر هر پست کانالت بذار (لینک داخلش حفظ میشه):\n"
            f"<blockquote>💬 <a href=\"{esc(url)}\">چت ناشناس</a></blockquote>"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ بازگشت به منوی اصلی", callback_data="menu:root"))
        send_or_edit(chat_id, text, kb, call.message.message_id)
        bot.answer_callback_query(call.id)
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
            "این لینک رو برای دریافت پیام در اختیار کاربرا بذار. با دکمه‌ی «تغییر ردیابی» می‌تونی مشخص کنی هویت فرستنده برات نمایش داده بشه یا نه (فقط از طریق تنظیمات، هیچ‌وقت داخل خود پیام).\n\n"
            "<b>متن آماده برای زیر پست کانال:</b>\n"
            f"<blockquote>💬 <a href=\"{esc(url)}\">چت ناشناس</a></blockquote>",
            reply_markup=kb_admin_home(),
        )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


# ---------------------------
# Move-node browser (admin)
# ---------------------------

def is_descendant_key(candidate_key: str, ancestor_node_id: int) -> bool:
    """True if candidate_key refers to ancestor_node_id itself or lives
    somewhere underneath it in the tree (used to block moving a node into
    its own subtree, which would orphan it)."""
    node_id = parse_node_key(candidate_key)
    guard = 0
    while node_id is not None and guard < 50:
        guard += 1
        if node_id == ancestor_node_id:
            return True
        node = get_node_any(node_id)
        if not node:
            return False
        node_id = parse_node_key(node["parent_key"])
    return False


def render_move_browser(chat_id: int, user_id: int, edit_message_id: Optional[int] = None) -> None:
    state = get_state(user_id)
    move_node_id = state.get("move_node_id")
    node = get_node(move_node_id) if move_node_id else None
    if not node:
        clear_state(user_id)
        send_or_edit(chat_id, "این دکمه دیگر پیدا نشد.", kb_admin_home(), edit_message_id)
        return

    browse_key = state.get("move_browse_key", "universities")
    if browse_key == "universities":
        location_label = "🗂 لیست دانشگاه‌ها (ریشه)"
    else:
        bnode_id = parse_node_key(browse_key)
        bnode = get_node_any(bnode_id) if bnode_id is not None else None
        location_label = esc(container_breadcrumb(bnode_id)) if bnode else "؟"

    # Only containers (university/custom) can be browsed into or be a
    # destination; skip the node being moved and anything under it.
    all_children = get_children(browse_key)
    containers = [
        c for c in all_children
        if c["kind"] in ("university", "custom") and c["id"] != move_node_id and not is_descendant_key(node_key(c["id"]), move_node_id)
    ]

    text = (
        f"<b>📦 انتقال «{esc(node['title'])}»</b>\n\n"
        f"<b>موقعیت فعلی مرور:</b> {location_label}\n\n"
        "وارد پوشه/دانشگاه موردنظر شو، یا همینجا «✅ انتقال به اینجا» رو بزن."
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in containers:
        icon = "🏛" if c["kind"] == "university" else "📁"
        kb.add(types.InlineKeyboardButton(f"{icon} {c['title']}", callback_data=f"admin:movebrowse:{node_key(c['id'])}"))
    if browse_key != node["parent_key"]:
        kb.add(types.InlineKeyboardButton("✅ انتقال به اینجا", callback_data="admin:movehere"))
    if browse_key != "universities":
        bnode_id = parse_node_key(browse_key)
        bnode = get_node_any(bnode_id) if bnode_id is not None else None
        parent_of_browse = bnode["parent_key"] if bnode else "universities"
        kb.add(types.InlineKeyboardButton("↩️ بازگشت یک سطح", callback_data=f"admin:movebrowse:{parent_of_browse}"))
    kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="admin:movecancel"))
    send_or_edit(chat_id, text, kb, edit_message_id)


# ---------------------------
# Bulk-copy selection & destination browser (admin)
# ---------------------------

def render_bulk_copy_select(chat_id: int, user_id: int, edit_message_id: Optional[int] = None) -> None:
    state = get_state(user_id)
    parent_key = state.get("bulkcopy_parent", "universities")
    all_children = get_children(parent_key)
    selected: List[int] = state.get("bulkcopy_selected", [])

    kb = types.InlineKeyboardMarkup(row_width=1)
    for ch in all_children:
        mark = "✅ " if ch["id"] in selected else "▫️ "
        icon = {"university": "🏛", "course": "📘", "finalize": "✅"}.get(ch["kind"], "📁")
        suffix = f" | واحد: {fmt_num(ch['credits'])}" if ch["kind"] == "course" else ""
        kb.add(types.InlineKeyboardButton(f"{mark}{icon} {ch['title']}{suffix}", callback_data=f"admin:bulkcopytoggle:{ch['id']}"))
    if selected:
        kb.add(types.InlineKeyboardButton(f"➡️ ادامه ({len(selected)} مورد انتخاب شد)", callback_data="admin:bulkcopynext"))
    kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="admin:bulkcopycancel"))

    text = (
        "<b>📋 کپی گروهی دکمه</b>\n\n"
        "مواردی که می‌خوای کپی بشن رو انتخاب کن (میشه چندتا رو باهم انتخاب کرد)، بعد روی «ادامه» بزن تا مقصد رو انتخاب کنی.\n"
        "⚠️ این کار اصل دکمه‌ها رو جابه‌جا نمی‌کنه؛ فقط یک نسخه‌ی جدید (به همراه زیرمجموعه‌هاش) توی مقصد ساخته میشه."
    )
    send_or_edit(chat_id, text, kb, edit_message_id)


def render_bulk_copy_browser(chat_id: int, user_id: int, edit_message_id: Optional[int] = None) -> None:
    state = get_state(user_id)
    selected: List[int] = state.get("bulkcopy_selected", [])
    if not selected:
        clear_state(user_id)
        send_or_edit(chat_id, "چیزی برای کپی انتخاب نشده بود.", kb_admin_home(), edit_message_id)
        return

    browse_key = state.get("bulkcopy_browse_key", "universities")
    if browse_key == "universities":
        location_label = "🗂 لیست دانشگاه‌ها (ریشه)"
    else:
        bnode_id = parse_node_key(browse_key)
        bnode = get_node_any(bnode_id) if bnode_id is not None else None
        location_label = esc(container_breadcrumb(bnode_id)) if bnode else "؟"

    all_children = get_children(browse_key)
    containers = [c for c in all_children if c["kind"] in ("university", "custom")]

    text = (
        f"<b>📋 انتخاب مقصد کپی</b>\n\n"
        f"<b>تعداد موارد انتخاب‌شده:</b> {len(selected)}\n"
        f"<b>موقعیت فعلی مرور:</b> {location_label}\n\n"
        "وارد پوشه/دانشگاه موردنظر شو، یا همینجا «✅ کپی به اینجا» رو بزن."
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in containers:
        icon = "🏛" if c["kind"] == "university" else "📁"
        kb.add(types.InlineKeyboardButton(f"{icon} {c['title']}", callback_data=f"admin:bulkcopybrowse:{node_key(c['id'])}"))
    kb.add(types.InlineKeyboardButton("✅ کپی به اینجا", callback_data="admin:bulkcopyhere"))
    if browse_key != "universities":
        bnode_id = parse_node_key(browse_key)
        bnode = get_node_any(bnode_id) if bnode_id is not None else None
        parent_of_browse = bnode["parent_key"] if bnode else "universities"
        kb.add(types.InlineKeyboardButton("↩️ بازگشت یک سطح", callback_data=f"admin:bulkcopybrowse:{parent_of_browse}"))
    kb.add(types.InlineKeyboardButton("❌ انصراف", callback_data="admin:bulkcopycancel"))
    send_or_edit(chat_id, text, kb, edit_message_id)


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
# Bot command menu (persistent "/start" access)
# ---------------------------

def setup_bot_commands() -> None:
    """Registers the bot's command list so Telegram shows a permanent ☰
    menu button next to the message box.

    Regular users only ever see "/start" here — the "/admin" command is
    registered with a per-chat scope so it only shows up in the menu for
    actual admins. Admins already get a "🛠 پنل ادمین" inline button from
    /start anyway, so there is no need to expose "/admin" to everyone.
    """
    try:
        bot.set_my_commands(
            [types.BotCommand("start", "🏠 شروع / منوی اصلی")]
        )
    except Exception:
        pass

    for admin_id in ADMIN_IDS:
        try:
            bot.set_my_commands(
                [
                    types.BotCommand("start", "🏠 شروع / منوی اصلی"),
                    types.BotCommand("admin", "🛠 پنل ادمین (فقط ادمین)"),
                ],
                scope=types.BotCommandScopeChat(admin_id),
            )
        except Exception:
            pass


# ---------------------------
# App startup
# ---------------------------

def startup() -> None:
    init_db()
    apply_default_forced_join()
    bootstrap_default_data()
    setup_bot_commands()

    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=flask_worker, daemon=True).start()

    # Polling keeps the bot alive without requiring webhook setup.
    bot.infinity_polling(timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    startup()
