import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import (JOIN_TRANSITION, ChatMemberUpdatedFilter, Command,
                             CommandObject, CommandStart)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated,
                           ChatPermissions, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ============================ НАСТРОЙКИ ============================
# Токен: вставьте его вместо текста в кавычках справа ИЛИ задайте переменную окружения BOT_TOKEN
TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬТЕ_ТОКЕН_СЮДА"
TZ = ZoneInfo("Europe/Moscow")           # часовой пояс для "день/неделя/месяц"
WARN_LIMIT = 3                           # сколько варнов до автобана
WARN_BAN = timedelta(days=7)             # срок автобана за лимит варнов
MAX_QUESTIONS = 10                       # максимум вопросов в анкете
MAX_ANSWER = 250                         # максимум символов в одном ответе
RANK_NAMES = {1: "участник", 2: "админ", 3: "старший админ", 4: "владелец"}
CLEANUP_EXEMPT_RANK = 2                  # ранг >= этого не проверяется при .очистке
# ===================================================================

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", "bot.db")   # на хостинге укажите путь на постоянном диске
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(chat_id INTEGER, user_id INTEGER, username TEXT, name TEXT, first_seen TEXT, first_seen_at TEXT, last_active TEXT, role TEXT, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS ranks(chat_id INTEGER, user_id INTEGER, rank INTEGER, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS warns(chat_id INTEGER, user_id INTEGER, cnt INTEGER, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS msgs(chat_id INTEGER, user_id INTEGER, day TEXT, cnt INTEGER, PRIMARY KEY(chat_id,user_id,day));
CREATE TABLE IF NOT EXISTS cleanup(chat_id INTEGER PRIMARY KEY, norm INTEGER, action TEXT, warns INTEGER);
CREATE TABLE IF NOT EXISTS forms(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, intro TEXT, questions TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS apps(id INTEGER PRIMARY KEY AUTOINCREMENT, form_id INTEGER, user_id INTEGER, username TEXT, name TEXT, answers TEXT, status TEXT, chat_id INTEGER);
CREATE TABLE IF NOT EXISTS chat_forms(chat_id INTEGER PRIMARY KEY, form_id INTEGER);
""")

# миграции для базы, созданной прошлой версией бота
for _tbl, _col, _typ in (("users", "first_seen_at", "TEXT"), ("users", "last_active", "TEXT"),
                         ("users", "role", "TEXT"), ("apps", "chat_id", "INTEGER")):
    try:
        db.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typ}")
    except sqlite3.OperationalError:
        pass
db.execute("UPDATE users SET first_seen_at=first_seen WHERE first_seen_at IS NULL")
db.commit()

STORAGE = MemoryStorage()   # общее хранилище состояний (нужно для автоприёма заявок)


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def run(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


def esc(s):
    return html.escape(str(s))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def now():
    return datetime.now(timezone.utc)


def today():
    return datetime.now(TZ).date()


# ============================ ВСПОМОГАТЕЛЬНОЕ ============================
UNITS = {"с": 1, "s": 1, "м": 60, "m": 60, "мин": 60, "min": 60, "ч": 3600, "h": 3600,
         "д": 86400, "d": 86400, "н": 604800, "w": 604800, "нед": 604800, "мес": 2592000}


def parse_dur(s):
    """-> (ok, timedelta | None). None = навсегда."""
    s = s.lower()
    if s in {"навсегда", "forever", "inf", "0", "-"}:
        return True, None
    mt = re.fullmatch(r"(\d+)(мес|мин|min|нед|с|s|м|m|ч|h|д|d|н|w)", s)
    if not mt:
        return False, None
    return True, max(timedelta(seconds=int(mt[1]) * UNITS[mt[2]]), timedelta(seconds=31))


def fmt_td(td):
    if td is None:
        return "навсегда"
    s = int(td.total_seconds())
    parts = []
    for n, u in ((86400, "д"), (3600, "ч"), (60, "м"), (1, "с")):
        if s >= n:
            parts.append(f"{s // n}{u}")
            s %= n
    return " ".join(parts)


def touch(chat_id, u, active=False, joined=False):
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    run("""INSERT INTO users(chat_id,user_id,username,name,first_seen,first_seen_at,last_active)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username, name=excluded.name,
             last_active=COALESCE(excluded.last_active, users.last_active)""",
        chat_id, u.id, u.username or "", u.full_name, today().isoformat(), ts, ts if active else None)
    if joined:
        run("UPDATE users SET first_seen=?, first_seen_at=? WHERE chat_id=? AND user_id=?",
            today().isoformat(), ts, chat_id, u.id)


def fmt_dt(s):
    if not s:
        return "—"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return s
    return d.strftime("%d.%m.%Y %H:%M") if "T" in s else d.strftime("%d.%m.%Y")


def msg_count(chat_id, uid, days):
    since = (today() - timedelta(days=days - 1)).isoformat()
    return q("SELECT COALESCE(SUM(cnt),0) c FROM msgs WHERE chat_id=? AND user_id=? AND day>=?",
             chat_id, uid, since)[0]["c"]


async def safe(coro):
    try:
        await coro
        return True
    except TelegramAPIError:
        return False


def add_msg(chat_id, uid):
    run("""INSERT INTO msgs(chat_id,user_id,day,cnt) VALUES(?,?,?,1)
           ON CONFLICT(chat_id,user_id,day) DO UPDATE SET cnt=cnt+1""",
        chat_id, uid, today().isoformat())


def db_rank(chat_id, uid):
    r = q("SELECT rank FROM ranks WHERE chat_id=? AND user_id=?", chat_id, uid)
    return r[0]["rank"] if r else 1


async def rank_of(bot, chat_id, uid):
    try:
        mem = await bot.get_chat_member(chat_id, uid)
        if mem.status == "creator":
            return 4
    except TelegramAPIError:
        pass
    return db_rank(chat_id, uid)


def resolve(m, args):
    """Ищем цель: @username / id / reply. -> ((id, name) | None, остаток аргументов)"""
    if args and args[0].startswith("@"):
        r = q("SELECT user_id,name FROM users WHERE chat_id=? AND lower(username)=?",
              m.chat.id, args[0][1:].lower())
        return ((r[0]["user_id"], r[0]["name"]) if r else None), args[1:]
    if args and args[0].isdigit() and len(args[0]) >= 6:
        r = q("SELECT user_id,name FROM users WHERE chat_id=? AND user_id=?", m.chat.id, int(args[0]))
        return ((r[0]["user_id"], r[0]["name"]) if r else (int(args[0]), args[0])), args[1:]
    rp = m.reply_to_message
    if rp and rp.from_user and not rp.from_user.is_bot:
        touch(m.chat.id, rp.from_user)
        return (rp.from_user.id, rp.from_user.full_name), args
    return None, args


async def prepare(m, bot, args, need, strict=True):
    my = await rank_of(bot, m.chat.id, m.from_user.id)
    if my < need:
        await m.reply(f"Нужен ранг {need}+ (у вас {my}).")
        return None
    t, rest = resolve(m, args)
    if not t:
        await m.reply("Не нашёл пользователя. Укажите @username (он должен был писать в чате) "
                      "или ответьте командой на его сообщение.")
        return None
    tid, tname = t
    if tid == bot.id or tid == m.from_user.id:
        await m.reply("Это действие к этому пользователю неприменимо.")
        return None
    if strict and await rank_of(bot, m.chat.id, tid) >= my:
        await m.reply("У цели ранг не ниже вашего.")
        return None
    return my, tid, tname, rest


async def default_perms(bot, chat_id):
    chat = await bot.get_chat(chat_id)
    if chat.permissions:
        return chat.permissions
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_invite_users=True)


def get_warns(chat_id, uid):
    r = q("SELECT cnt FROM warns WHERE chat_id=? AND user_id=?", chat_id, uid)
    return r[0]["cnt"] if r else 0


def set_warns(chat_id, uid, n):
    run("""INSERT INTO warns(chat_id,user_id,cnt) VALUES(?,?,?)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET cnt=excluded.cnt""", chat_id, uid, max(n, 0))


async def apply_warn(bot, chat_id, uid, n=1):
    cnt = get_warns(chat_id, uid) + n
    if cnt >= WARN_LIMIT:
        await bot.ban_chat_member(chat_id, uid, until_date=now() + WARN_BAN)
        set_warns(chat_id, uid, 0)
        return f"{WARN_LIMIT}/{WARN_LIMIT} варнов — бан на {fmt_td(WARN_BAN)}"
    set_warns(chat_id, uid, cnt)
    return f"варны: {cnt}/{WARN_LIMIT}"


# ============================ КОМАНДЫ МОДЕРАЦИИ ============================
async def cmd_ban(m, bot, args):
    c = await prepare(m, bot, args, 2)
    if not c:
        return
    _, tid, tname, rest = c
    if not rest:
        return await m.reply("Формат: .бан @username срок причина")
    ok, td = parse_dur(rest[0])
    if not ok:
        return await m.reply("Срок: 30м, 2ч, 7д, 2н, 1мес или «навсегда».")
    reason = " ".join(rest[1:]) or "не указана"
    try:
        await bot.ban_chat_member(m.chat.id, tid, until_date=(now() + td) if td else None)
    except TelegramAPIError as e:
        return await m.reply(f"Не вышло: {esc(e.message)}")
    await m.reply(f"🔨 {mention(tid, tname)} забанен ({fmt_td(td)}).\nПричина: {esc(reason)}")


async def cmd_unban(m, bot, args):
    c = await prepare(m, bot, args, 2, strict=False)
    if not c:
        return
    _, tid, tname, _ = c
    try:
        await bot.unban_chat_member(m.chat.id, tid, only_if_banned=True)
    except TelegramAPIError as e:
        return await m.reply(f"Не вышло: {esc(e.message)}")
    await m.reply(f"✅ {mention(tid, tname)} разбанен.")


async def cmd_mute(m, bot, args):
    c = await prepare(m, bot, args, 2)
    if not c:
        return
    _, tid, tname, rest = c
    if not rest:
        return await m.reply("Формат: .мут @username срок причина")
    ok, td = parse_dur(rest[0])
    if not ok:
        return await m.reply("Срок: 30м, 2ч, 7д, 2н, 1мес или «навсегда».")
    reason = " ".join(rest[1:]) or "не указана"
    try:
        await bot.restrict_chat_member(m.chat.id, tid, permissions=ChatPermissions(can_send_messages=False),
                                       until_date=(now() + td) if td else None)
    except TelegramAPIError as e:
        return await m.reply(f"Не вышло: {esc(e.message)}")
    await m.reply(f"🔇 {mention(tid, tname)} в муте ({fmt_td(td)}).\nПричина: {esc(reason)}")


async def cmd_unmute(m, bot, args):
    c = await prepare(m, bot, args, 2, strict=False)
    if not c:
        return
    _, tid, tname, _ = c
    try:
        await bot.restrict_chat_member(m.chat.id, tid, permissions=await default_perms(bot, m.chat.id))
    except TelegramAPIError as e:
        return await m.reply(f"Не вышло: {esc(e.message)}")
    await m.reply(f"🔊 {mention(tid, tname)} размучен.")


async def cmd_warn(m, bot, args):
    c = await prepare(m, bot, args, 2)
    if not c:
        return
    _, tid, tname, rest = c
    reason = " ".join(rest) or "не указана"
    try:
        info = await apply_warn(bot, m.chat.id, tid, 1)
    except TelegramAPIError as e:
        return await m.reply(f"Не вышло: {esc(e.message)}")
    await m.reply(f"⚠️ {mention(tid, tname)} получил варн ({info}).\nПричина: {esc(reason)}")


async def cmd_unwarn(m, bot, args):
    c = await prepare(m, bot, args, 2, strict=False)
    if not c:
        return
    _, tid, tname, _ = c
    set_warns(m.chat.id, tid, get_warns(m.chat.id, tid) - 1)
    await m.reply(f"✅ Варн снят с {mention(tid, tname)} ({get_warns(m.chat.id, tid)}/{WARN_LIMIT}).")


async def cmd_rank(m, bot, args):
    my = await rank_of(bot, m.chat.id, m.from_user.id)
    if not args and not m.reply_to_message:
        return await m.reply(f"Ваш ранг: {my} ({RANK_NAMES[my]}).")
    t, rest = resolve(m, args)
    if not t:
        return await m.reply("Не нашёл пользователя.")
    tid, tname = t
    cur = await rank_of(bot, m.chat.id, tid)
    if not rest:
        return await m.reply(f"Ранг {mention(tid, tname)}: {cur} ({RANK_NAMES[cur]}).")
    if not rest[0].isdigit() or not 1 <= int(rest[0]) <= 3:
        return await m.reply("Формат: .ранг @username 1-3")
    new = int(rest[0])
    if my < 3:
        return await m.reply("Менять ранги может ранг 3+.")
    if cur >= my or new >= my:
        return await m.reply("Можно менять и назначать только ранги ниже своего.")
    run("""INSERT INTO ranks(chat_id,user_id,rank) VALUES(?,?,?)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET rank=excluded.rank""", m.chat.id, tid, new)
    await m.reply(f"👑 {mention(tid, tname)} теперь: {new} ({RANK_NAMES[new]}).")


async def cmd_top(m, bot, args):
    args = [a.strip("(),") for a in args if a.strip("(),")]
    period = args[0].lower() if args else "день"
    days = {"день": 1, "неделя": 7, "месяц": 30}.get(period)
    if not days:
        return await m.reply("Формат: .топ день / неделя / месяц (число)")
    n = min(int(args[1]), 50) if len(args) > 1 and args[1].isdigit() else 10
    since = (today() - timedelta(days=days - 1)).isoformat()
    rows = q("""SELECT m.user_id, SUM(m.cnt) s, u.name FROM msgs m
                LEFT JOIN users u ON u.chat_id=m.chat_id AND u.user_id=m.user_id
                WHERE m.chat_id=? AND m.day>=? GROUP BY m.user_id ORDER BY s DESC LIMIT ?""",
             m.chat.id, since, n)
    if not rows:
        return await m.reply("Пока нет данных.")
    lines = [f"{i}. {mention(r['user_id'], r['name'] or r['user_id'])} — {r['s']}"
             for i, r in enumerate(rows, 1)]
    await m.reply(f"🏆 <b>Топ за {period}</b>\n" + "\n".join(lines))


async def cmd_setcleanup(m, bot, args):
    if await rank_of(bot, m.chat.id, m.from_user.id) < 4:
        return await m.reply("Только владелец (ранг 4).")
    tokens = [t for t in re.split(r"[,\s]+", " ".join(args).lower()) if t]
    if not tokens or not tokens[0].isdigit():
        return await m.reply("Формат: .настройкаочистки число, бан/варн, x2варн")
    norm, action, warns = int(tokens[0]), "warn", 1
    for t in tokens[1:]:
        if t == "бан":
            action = "ban"
        elif re.fullmatch(r"[xх]?2\s*варн\w*|[xх]2", t):   # х2варн -> 2 варна
            action, warns = "warn", 2
        elif t.startswith("варн"):                          # варн -> 1 варн
            action = "warn"
    run("""INSERT INTO cleanup(chat_id,norm,action,warns) VALUES(?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET norm=excluded.norm, action=excluded.action, warns=excluded.warns""",
        m.chat.id, norm, action, warns)
    what = "бан" if action == "ban" else f"{warns} варн(а)"
    await m.reply(f"⚙️ Норма: {norm} сообщ. за неделю. Наказание: {what}.")


async def cmd_cleanup(m, bot, args):
    if await rank_of(bot, m.chat.id, m.from_user.id) < 3:
        return await m.reply("Нужен ранг 3+.")
    s = q("SELECT * FROM cleanup WHERE chat_id=?", m.chat.id)
    if not s:
        return await m.reply("Сначала задайте норму: .настройкаочистки число, бан/варн, x2варн")
    s = s[0]
    since = (today() - timedelta(days=6)).isoformat()
    checked, skipped, punished = 0, 0, []
    await m.reply("Проверяю…")
    for u in q("SELECT user_id,name,first_seen FROM users WHERE chat_id=?", m.chat.id):
        uid = u["user_id"]
        if uid == bot.id:
            continue
        try:
            mem = await bot.get_chat_member(m.chat.id, uid)
        except TelegramAPIError:
            continue
        if mem.status in {"left", "kicked"} or (mem.status == "restricted" and not mem.is_member):
            continue
        rk = 4 if mem.status == "creator" else db_rank(m.chat.id, uid)
        if rk >= CLEANUP_EXEMPT_RANK:
            continue
        if u["first_seen"] > since:        # в базе меньше недели — данных недостаточно
            skipped += 1
            continue
        checked += 1
        cnt = q("SELECT COALESCE(SUM(cnt),0) c FROM msgs WHERE chat_id=? AND user_id=? AND day>=?",
                m.chat.id, uid, since)[0]["c"]
        if cnt < s["norm"]:
            try:
                if s["action"] == "ban":
                    await bot.ban_chat_member(m.chat.id, uid)
                    info = "бан"
                else:
                    info = await apply_warn(bot, m.chat.id, uid, s["warns"])
                punished.append(f"{mention(uid, u['name'])} — {cnt}/{s['norm']} ({info})")
            except TelegramAPIError:
                pass
        await asyncio.sleep(0.05)
    text = f"🧹 Проверено: {checked}, наказано: {len(punished)}."
    if skipped:
        text += f"\nПропущено (в базе меньше недели): {skipped}."
    if punished:
        text += "\n" + "\n".join(punished)
    await m.answer(text)


async def cmd_profile(m, bot, args):
    if not args and not m.reply_to_message:
        tid = m.from_user.id
    else:
        t, _ = resolve(m, args)
        if not t:
            return await m.reply("Не нашёл пользователя. Укажите @username или ответьте на его сообщение.")
        tid = t[0]
    r = q("SELECT * FROM users WHERE chat_id=? AND user_id=?", m.chat.id, tid)
    if not r:
        return await m.reply("Об этом пользователе пока нет данных.")
    r = r[0]
    nick = f"@{esc(r['username'])}" if r["username"] else "нет"
    role = esc(r["role"]) if r["role"] else "не назначена"
    last = fmt_dt(r["last_active"]) if r["last_active"] else "нет данных"
    await m.reply(
        f"👤 <b>Профиль</b> {mention(tid, r['name'] or tid)}\n"
        f"Ник: {nick}\n"
        f"Роль: {role}\n"
        f"Сообщений: за день — {msg_count(m.chat.id, tid, 1)}, за неделю — {msg_count(m.chat.id, tid, 7)}, "
        f"за месяц — {msg_count(m.chat.id, tid, 30)}\n"
        f"Последняя активность: {last}\n"
        f"В чате с: {fmt_dt(r['first_seen_at'])}")


async def cmd_role(m, bot, args):
    my = await rank_of(bot, m.chat.id, m.from_user.id)
    if my < 3:
        return await m.reply("Нужен ранг 3+.")
    t, rest = resolve(m, args)
    text = " ".join(rest).strip()
    if not t or not text:
        return await m.reply("Формат: .роль @username текст (или ответом на сообщение).\n"
                             "Снять роль: .роль @username -")
    tid, tname = t
    if tid != m.from_user.id and await rank_of(bot, m.chat.id, tid) >= my:
        return await m.reply("У цели ранг не ниже вашего.")
    if len(text) > 64:
        return await m.reply("Роль не длиннее 64 символов.")
    new = None if text in {"-", "снять"} else text
    cur = run("UPDATE users SET role=? WHERE chat_id=? AND user_id=?", new, m.chat.id, tid)
    if not cur.rowcount:
        return await m.reply("Об этом пользователе пока нет данных.")
    if new:
        await m.reply(f"🎭 {mention(tid, tname)} — роль: {esc(new)}")
    else:
        await m.reply(f"🎭 Роль {mention(tid, tname)} снята.")


async def cmd_requests(m, bot, args):
    """Автоприём заявок на вступление через анкету."""
    if await rank_of(bot, m.chat.id, m.from_user.id) < 4:
        return await m.reply("Только владелец (ранг 4).")
    cur = q("SELECT form_id FROM chat_forms WHERE chat_id=?", m.chat.id)
    if not args:
        return await m.reply(f"Автоприём включён: анкета #{cur[0]['form_id']}." if cur else
                             "Автоприём выключен. Включить: .заявки номер_анкеты")
    if args[0].lower() in {"выкл", "off"}:
        run("DELETE FROM chat_forms WHERE chat_id=?", m.chat.id)
        return await m.reply("Автоприём выключен.")
    if not args[0].isdigit() or not q("SELECT 1 FROM forms WHERE id=? AND owner_id=?",
                                      int(args[0]), m.from_user.id):
        return await m.reply("Анкета не найдена или она не ваша. Номера — командой /forms в личке с ботом.")
    run("""INSERT INTO chat_forms(chat_id,form_id) VALUES(?,?)
           ON CONFLICT(chat_id) DO UPDATE SET form_id=excluded.form_id""", m.chat.id, int(args[0]))
    warn = ""
    try:
        me = await bot.get_chat_member(m.chat.id, bot.id)
        if me.status != "administrator" or not getattr(me, "can_invite_users", False):
            warn = "\n⚠️ Дайте боту права админа «Добавление участников», иначе он не сможет принимать заявки."
    except TelegramAPIError:
        pass
    await m.reply(f"✅ Автоприём включён (анкета #{args[0]}). Заявки на вступление будут одобряться "
                  f"автоматически у тех, чью анкету принял её владелец.{warn}")


COMMANDS = {
    "профиль": cmd_profile, "роль": cmd_role, "заявки": cmd_requests,
    "бан": cmd_ban, "разбан": cmd_unban, "мут": cmd_mute, "размут": cmd_unmute,
    "варн": cmd_warn, "анварн": cmd_unwarn, "ранг": cmd_rank, "топ": cmd_top,
    "настройкаочистки": cmd_setcleanup, "очистка": cmd_cleanup,
}

# ============================ ГРУППОВОЙ РОУТЕР ============================
group = Router()
group.message.filter(F.chat.type.in_({"group", "supergroup"}))


@group.message(F.new_chat_members)
async def on_join(m: Message):
    for u in m.new_chat_members:
        if not u.is_bot:
            touch(m.chat.id, u, joined=True)


@group.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_member_join(ev: ChatMemberUpdated):
    u = ev.new_chat_member.user
    if not u.is_bot:
        touch(ev.chat.id, u, joined=True)


@group.chat_join_request()
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    """Заявка на вступление: одобряем автоматически, если анкета человека уже принята."""
    r = q("SELECT form_id FROM chat_forms WHERE chat_id=?", req.chat.id)
    if not r:
        return   # автоприём не включён — заявку решают админы вручную
    fid = r[0]["form_id"]
    statuses = {x["status"] for x in q("SELECT status FROM apps WHERE form_id=? AND user_id=?",
                                       fid, req.from_user.id)}
    if "accepted" in statuses:
        if await safe(bot.approve_chat_join_request(req.chat.id, req.from_user.id)):
            await safe(bot.send_message(req.user_chat_id,
                                        f"✅ Вы приняты в «{esc(req.chat.title)}». Добро пожаловать!"))
        return
    # анкета не принята: заявку не трогаем (её решают админы), человеку подсказываем что делать
    if "pending" in statuses:
        text = "Ваша анкета на рассмотрении. Заявка на вступление будет одобрена после её принятия."
    else:
        me = await bot.get_me()
        text = (f"Чтобы вступить в «{esc(req.chat.title)}», сначала заполните анкету:\n"
                f"https://t.me/{me.username}?start=apply_{fid}")
    await safe(bot.send_message(req.user_chat_id, text))


@group.message()
async def on_message(m: Message, bot: Bot):
    u = m.from_user
    if not u or u.is_bot:
        return
    touch(m.chat.id, u, active=True)
    text = m.text or ""
    if text.startswith("."):
        parts = text.split()
        handler = COMMANDS.get(parts[0][1:].lower())
        if handler:
            return await handler(m, bot, parts[1:])
    add_msg(m.chat.id, u.id)


# ============================ ПРИЁМ ЗАЯВОК (ЛС) ============================
priv = Router()
priv.message.filter(F.chat.type == "private")

HELP_PRIV = ("Я бот-модератор.\n\n"
             "<b>Приём заявок:</b>\n"
             "/newform — создать анкету\n"
             "/forms — мои анкеты и ссылки\n"
             "/delform номер — удалить анкету\n"
             "/cancel — отмена\n\n"
             "Модерация чата работает в группе: добавьте меня админом.")


class NewForm(StatesGroup):
    intro = State()
    questions = State()
    link = State()


class Apply(StatesGroup):
    answering = State()


@priv.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("apply_") and arg[6:].isdigit():
        f = q("SELECT * FROM forms WHERE id=?", int(arg[6:]))
        if not f:
            return await m.answer("Анкета не найдена.")
        f = f[0]
        qs = json.loads(f["questions"])
        await state.set_state(Apply.answering)
        await state.update_data(form_id=f["id"], answers=[])
        return await m.answer(f"{esc(f['intro'])}\n\nОтветьте на {len(qs)} вопр. Отмена — /cancel.\n\n"
                              f"<b>1/{len(qs)}.</b> {esc(qs[0])}")
    await m.answer(HELP_PRIV)


@priv.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")


@priv.message(Command("help"))
async def help_(m: Message):
    await m.answer(HELP_PRIV)


@priv.message(Command("newform"))
async def newform(m: Message, state: FSMContext):
    await state.set_state(NewForm.intro)
    await m.answer("Пришлите основную информацию: название, условия, что нужно знать. "
                   "Это увидит человек перед началом анкеты.")


@priv.message(NewForm.intro, F.text)
async def nf_intro(m: Message, state: FSMContext):
    await state.update_data(intro=m.text)
    await state.set_state(NewForm.questions)
    await m.answer(f"Теперь вопросы анкеты — каждый с новой строки (до {MAX_QUESTIONS}).")


@priv.message(NewForm.questions, F.text)
async def nf_questions(m: Message, state: FSMContext):
    qs = [x.strip() for x in m.text.splitlines() if x.strip()]
    if not qs or len(qs) > MAX_QUESTIONS:
        return await m.answer(f"Нужно от 1 до {MAX_QUESTIONS} вопросов, каждый с новой строки.")
    await state.update_data(questions=qs)
    await state.set_state(NewForm.link)
    await m.answer("Пришлите ссылку на чат (закрытую, с заявками на вступление) — бот отправит её "
                   "человеку после принятия анкеты, а его заявку на вход одобрит сам.\n"
                   "Если ссылка не нужна — отправьте «-».")


@priv.message(NewForm.link, F.text)
async def nf_link(m: Message, state: FSMContext, bot: Bot):
    link = m.text.strip()
    if link == "-":
        link = ""
    elif not link.startswith("http"):
        return await m.answer("Это не похоже на ссылку (должна начинаться с https://) или «-».")
    d = await state.get_data()
    await state.clear()
    cur = run("INSERT INTO forms(owner_id,intro,questions,link) VALUES(?,?,?,?)",
              m.from_user.id, d["intro"], json.dumps(d["questions"], ensure_ascii=False), link)
    me = await bot.get_me()
    await m.answer(f"✅ Анкета #{cur.lastrowid} создана.\nСсылка для кандидатов (разместите в чате/канале):\n"
                   f"https://t.me/{me.username}?start=apply_{cur.lastrowid}\n\n"
                   f"Автоприём заявок на вступление: в чате (владелец) напишите <code>.заявки {cur.lastrowid}</code>")


@priv.message(Command("forms"))
async def forms(m: Message, bot: Bot):
    rows = q("SELECT * FROM forms WHERE owner_id=?", m.from_user.id)
    if not rows:
        return await m.answer("Анкет нет. Создать: /newform")
    me = await bot.get_me()
    await m.answer("\n\n".join(
        f"#{r['id']} — {esc(r['intro'][:40])}\nhttps://t.me/{me.username}?start=apply_{r['id']}" for r in rows))


@priv.message(Command("delform"))
async def delform(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        return await m.answer("Формат: /delform номер")
    cur = run("DELETE FROM forms WHERE id=? AND owner_id=?", int(command.args), m.from_user.id)
    await m.answer("Удалено." if cur.rowcount else "Анкета не найдена.")


@priv.message(Apply.answering, F.text)
async def answer(m: Message, state: FSMContext, bot: Bot):
    if len(m.text) > MAX_ANSWER:
        return await m.answer(f"Слишком длинно — до {MAX_ANSWER} символов. Сократите.")
    d = await state.get_data()
    f = q("SELECT * FROM forms WHERE id=?", d["form_id"])
    if not f:
        await state.clear()
        return await m.answer("Анкета больше недоступна.")
    f = f[0]
    qs = json.loads(f["questions"])
    answers = d["answers"] + [m.text]
    if len(answers) < len(qs):
        await state.update_data(answers=answers)
        return await m.answer(f"<b>{len(answers) + 1}/{len(qs)}.</b> {esc(qs[len(answers)])}")
    await state.clear()
    u = m.from_user
    join_chat = d.get("chat_id")
    cur = run("INSERT INTO apps(form_id,user_id,username,name,answers,status,chat_id) VALUES(?,?,?,?,?,'pending',?)",
              f["id"], u.id, u.username or "", u.full_name, json.dumps(answers, ensure_ascii=False), join_chat)
    aid = cur.lastrowid
    src = "\nИсточник: заявка на вступление в чат" if join_chat else ""
    body = "\n\n".join(f"<b>{esc(a)}</b>\n{esc(b)}" for a, b in zip(qs, answers))
    uname = f" (@{esc(u.username)})" if u.username else ""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"app:ok:{aid}")
    kb.button(text="❌ Отклонить", callback_data=f"app:no:{aid}")
    kb.adjust(2)
    try:
        await bot.send_message(f["owner_id"], f"📩 <b>Новая анкета #{aid}</b>\n"
                                              f"От: {mention(u.id, u.full_name)}{uname}{src}\n\n{body}",
                               reply_markup=kb.as_markup())
    except TelegramAPIError:
        return await m.answer("Не удалось отправить анкету владельцу. Попробуйте позже.")
    await m.answer("Анкета отправлена. Ожидайте решения.")


@priv.message(Apply.answering)
async def answer_nontext(m: Message):
    await m.answer("Отправьте ответ текстом.")


@priv.callback_query(F.data.startswith("app:"))
async def decide(c: CallbackQuery, bot: Bot):
    _, act, aid = c.data.split(":")
    a = q("SELECT * FROM apps WHERE id=?", int(aid))
    if not a:
        return await c.answer("Заявка не найдена.", show_alert=True)
    a = a[0]
    f = q("SELECT * FROM forms WHERE id=?", a["form_id"])
    if not f or f[0]["owner_id"] != c.from_user.id:
        return await c.answer("Это не ваша анкета.", show_alert=True)
    if a["status"] != "pending":
        return await c.answer("Уже обработана.", show_alert=True)
    ok = act == "ok"
    run("UPDATE apps SET status=? WHERE id=?", "accepted" if ok else "rejected", a["id"])
    note = ""
    uid, link = a["user_id"], f[0]["link"]
    chats = [x["chat_id"] for x in q("SELECT chat_id FROM chat_forms WHERE form_id=?", a["form_id"])]
    if ok:
        added = False
        for cid in chats:   # если заявка на вход уже подана — одобряем сразу
            added = await safe(bot.approve_chat_join_request(cid, uid)) or added
        text = "🎉 Ваша анкета принята!"
        if added:
            text += " Заявка на вступление одобрена — вы добавлены в чат."
        elif chats:
            text += " Перейдите по ссылке и подайте заявку на вступление — бот одобрит её автоматически."
        if link:
            text += f"\nСсылка на чат:\n{link}"
    else:
        for cid in chats:
            await safe(bot.decline_chat_join_request(cid, uid))
        text = "К сожалению, ваша анкета отклонена."
    if not await safe(bot.send_message(uid, text)):
        note += "\n(не удалось написать кандидату)"
    await c.message.edit_text(c.message.html_text + ("\n\n✅ <b>Принято</b>" if ok else "\n\n❌ <b>Отклонено</b>") + note,
                              reply_markup=None)
    await c.answer("Готово")


# ============================ ЗАПУСК ============================
async def main():
    if not TOKEN or TOKEN.startswith("ВСТАВЬТЕ"):
        raise SystemExit("Вставьте токен в bot.py (строка TOKEN = ...) или задайте переменную BOT_TOKEN")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=STORAGE)
    dp.include_routers(priv, group)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
