"""The Teachers: окна преподавателей и запись на уроки (FastAPI + aiogram + Telegram Mini App)."""
import asyncio, hashlib, hmac, json, os, sqlite3, time
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, timedelta, time as dtime
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo(os.environ.get("BUSINESS_TZ", "Europe/Moscow"))  # все часы хранятся в этом поясе
DB = os.environ.get("DB_PATH") or ("/data/data.db" if os.path.isdir("/data") else "data.db")  # /data: постоянное хранилище Amvera
REMIND_AFTER_MIN = 30  # через сколько минут напомнить менеджерам внести урок в langlisto
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

bot, dp = Bot(TOKEN), Dispatcher()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(tg_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('teacher','manager','admin')), timezone TEXT NOT NULL DEFAULT 'Europe/Moscow');
CREATE TABLE IF NOT EXISTS availability(teacher_id INTEGER, weekday INTEGER, hour INTEGER,
  PRIMARY KEY(teacher_id, weekday, hour));
CREATE TABLE IF NOT EXISTS days_off(teacher_id INTEGER, day TEXT, PRIMARY KEY(teacher_id, day));
CREATE TABLE IF NOT EXISTS blocks(teacher_id INTEGER, day TEXT, hour INTEGER, PRIMARY KEY(teacher_id, day, hour));
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER NOT NULL,
  start TEXT NOT NULL, student TEXT NOT NULL, note TEXT DEFAULT '', created_by INTEGER,
  status TEXT DEFAULT 'active', confirmed INTEGER DEFAULT 0, synced INTEGER DEFAULT 0,
  reminded INTEGER DEFAULT 0, changed_at TEXT NOT NULL, recurrence_id TEXT DEFAULT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_lesson_per_slot ON bookings(teacher_id, start) WHERE status='active';
CREATE TABLE IF NOT EXISTS recurring_series(id TEXT PRIMARY KEY, teacher_id INTEGER NOT NULL,
  student TEXT NOT NULL, note TEXT DEFAULT '', weekdays TEXT NOT NULL, hour INTEGER NOT NULL,
  start_day TEXT NOT NULL, weeks INTEGER NOT NULL, created_by INTEGER NOT NULL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sent_weekly_reminders(day TEXT NOT NULL, teacher_id INTEGER NOT NULL,
  PRIMARY KEY(day, teacher_id));
"""


# ---------- база ----------
def init_db():
    with closing(sqlite3.connect(DB)) as c:
        c.executescript(SCHEMA)
        # Безопасные миграции существующей базы: старые записи и пользователи сохраняются.
        user_cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "timezone" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Moscow'")
        booking_cols = {r[1] for r in c.execute("PRAGMA table_info(bookings)")}
        if "recurrence_id" not in booking_cols:
            c.execute("ALTER TABLE bookings ADD COLUMN recurrence_id TEXT DEFAULT NULL")


def q(sql, args=()):
    with closing(sqlite3.connect(DB)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def tx(stmts):
    with closing(sqlite3.connect(DB)) as c, c:
        last = None
        for sql, args in stmts:
            last = c.execute(sql, args).lastrowid
        return last


def run(sql, args=()):
    return tx([(sql, args)])


def now_utc():
    return datetime.now(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()


def local_start(start):
    """Перевод времени расписания (Europe/Moscow) в UTC для точного отображения у преподавателя."""
    d = datetime.fromisoformat(start)
    return d.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC")).isoformat()


def teacher_local(start, timezone):
    try:
        d = datetime.fromisoformat(local_start(start)).replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(timezone))
        return f"{WD[d.weekday()]} {d:%d.%m %H:%M}"
    except Exception:
        return fmt(start)


def today():
    return datetime.now(TZ).date().isoformat()


def norm(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:00").strftime("%Y-%m-%dT%H:00")
    except ValueError:
        raise HTTPException(422, "Неверный формат времени")


def fmt(start):
    d = datetime.fromisoformat(start)
    return f"{WD[d.weekday()]} {d:%d.%m %H:%M}"


def free_hours(tid, day: date):
    """Окна по шаблону минус выходные даты минус уже занятые уроки минус прошедшее время."""
    if q("SELECT 1 FROM days_off WHERE teacher_id=? AND day=?", (tid, day.isoformat())):
        return []
    hrs = {r["hour"] for r in q("SELECT hour FROM availability WHERE teacher_id=? AND weekday=?", (tid, day.weekday()))}
    hrs -= {r["hour"] for r in q("SELECT hour FROM blocks WHERE teacher_id=? AND day=?", (tid, day.isoformat()))}
    taken = {int(r["start"][11:13]) for r in q(
        "SELECT start FROM bookings WHERE teacher_id=? AND status='active' AND start LIKE ?",
        (tid, day.isoformat() + "%"))}
    now = datetime.now(TZ)
    return sorted(h for h in hrs - taken if datetime.combine(day, dtime(h), TZ) > now)


# ---------- авторизация через Telegram initData ----------
def tg_id(init_data: str) -> int:
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got)
        if ok and time.time() - int(data["auth_date"]) < 3 * 86400:
            return json.loads(data["user"])["id"]
    except Exception:
        pass
    raise HTTPException(401, "Откройте приложение из Telegram")


def user(x_init_data: str = Header("")):
    uid = tg_id(x_init_data)
    rows = q("SELECT * FROM users WHERE tg_id=?", (uid,))
    if not rows and uid in ADMINS:
        run("INSERT INTO users(tg_id,name,role) VALUES(?,?,?)", (uid, "Админ", "admin"))
        rows = q("SELECT * FROM users WHERE tg_id=?", (uid,))
    if not rows:
        raise HTTPException(403, f"Нет доступа. Передайте администратору ваш Telegram ID: {uid}")
    return rows[0]


def manager(u=Depends(user)):
    if u["role"] not in ("manager", "admin"):
        raise HTTPException(403, "Только для менеджеров")
    return u


def teacher(u=Depends(user)):
    if u["role"] != "teacher":
        raise HTTPException(403, "Только для преподавателей")
    return u


# ---------- бот ----------
async def notify(uid, text, kb=None):
    try:
        await bot.send_message(uid, text, reply_markup=kb)
    except Exception:
        pass  # пользователь мог не запускать бота


def confirm_kb(bid):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ок, вижу", callback_data=f"ok:{bid}")]])


async def notify_managers(text):
    for m in q("SELECT tg_id FROM users WHERE role IN ('manager','admin')"):
        await notify(m["tg_id"], text)


@dp.message(Command("start"))
async def cmd_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Открыть расписание", web_app=WebAppInfo(url=BASE_URL))]])
    await m.answer("Расписание The Teachers. Если нет доступа, отправьте администратору команду /id.", reply_markup=kb)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


@dp.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    if m.from_user.id not in ADMINS:
        return
    try:
        uid, role, name = command.args.split(maxsplit=2)
        assert role in ("teacher", "manager")
        run("INSERT OR REPLACE INTO users(tg_id,name,role) VALUES(?,?,?)", (int(uid), name, role))
    except Exception:
        return await m.answer("Формат: /add <telegram_id> <teacher|manager> <имя>")
    await m.answer(f"Добавлено: {name} ({role})")


@dp.callback_query(F.data.startswith("ok:"))
async def on_ok(c: CallbackQuery):
    rows = q("SELECT b.start, b.student, u.name FROM bookings b JOIN users u ON u.tg_id=b.teacher_id "
             "WHERE b.id=? AND b.status='active'", (int(c.data[3:]),))
    if rows:
        run("UPDATE bookings SET confirmed=1 WHERE id=?", (int(c.data[3:]),))
        await notify_managers(f"{rows[0]['name']} подтвердил(а) урок {fmt(rows[0]['start'])}, ученик {rows[0]['student']}")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Принято")


async def reminders():
    """Напоминания менеджерам о langlisto и преподавателям по пятницам в 12:00 МСК."""
    while True:
        try:
            cutoff = (datetime.now(ZoneInfo("UTC")).replace(tzinfo=None) - timedelta(minutes=REMIND_AFTER_MIN)).isoformat()
            for b in q("SELECT b.*, u.name tname FROM bookings b JOIN users u ON u.tg_id=b.teacher_id "
                       "WHERE status='active' AND synced=0 AND reminded=0 AND changed_at<?", (cutoff,)):
                await notify_managers(f"Внесите в langlisto: {fmt(b['start'])}, {b['tname']}, ученик {b['student']}")
                run("UPDATE bookings SET reminded=1 WHERE id=?", (b["id"],))

            now = datetime.now(TZ)
            if now.weekday() == 4 and now.hour >= 12:
                day_key = now.date().isoformat()
                for t in q("SELECT tg_id, name FROM users WHERE role='teacher'"):
                    if not q("SELECT 1 FROM sent_weekly_reminders WHERE day=? AND teacher_id=?", (day_key, t["tg_id"])):
                        await notify(t["tg_id"], "Добрый день! Пожалуйста, актуализируйте расписание на следующую неделю в The Teachers. Если ваши окна не изменились, откройте приложение и проверьте, что расписание актуально.",
                                     InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть расписание", web_app=WebAppInfo(url=BASE_URL))]]))
                        run("INSERT OR IGNORE INTO sent_weekly_reminders(day, teacher_id) VALUES(?,?)", (day_key, t["tg_id"]))
        except Exception:
            pass
        await asyncio.sleep(30)


# ---------- API ----------
class Slots(BaseModel):
    slots: list[tuple[int, int]]


class DayOff(BaseModel):
    day: str


class BookIn(BaseModel):
    teacher_id: int
    start: str
    student: str
    note: str = ""


class MoveIn(BaseModel):
    start: str


class TimezoneIn(BaseModel):
    timezone: str


class RecurringBookingIn(BaseModel):
    teacher_id: int
    student: str
    note: str = ""
    weekdays: list[int]
    hour: int
    start_day: str
    weeks: int = 8


@asynccontextmanager
async def lifespan(app):
    init_db()
    tasks = [asyncio.create_task(dp.start_polling(bot, handle_signals=False)), asyncio.create_task(reminders())]
    yield
    for t in tasks:
        t.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    # В загруженном архиве index.html лежит в корне репозитория.
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(path)


@app.get("/api/me")
def me(u=Depends(user)):
    return {**u, "moscow_now": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
            "teacher_local_now": datetime.now(ZoneInfo(u.get("timezone") or "Europe/Moscow")).strftime("%Y-%m-%dT%H:%M")}


@app.put("/api/timezone")
def put_timezone(b: TimezoneIn, u=Depends(teacher)):
    try:
        ZoneInfo(b.timezone)
    except Exception:
        raise HTTPException(422, "Выберите корректный часовой пояс")
    run("UPDATE users SET timezone=? WHERE tg_id=?", (b.timezone, u["tg_id"]))
    return {"ok": True, "timezone": b.timezone}


@app.get("/api/availability")
def get_availability(u=Depends(teacher)):
    t = u["tg_id"]
    return {"slots": [[r["weekday"], r["hour"]] for r in q("SELECT weekday,hour FROM availability WHERE teacher_id=?", (t,))],
            "days_off": [r["day"] for r in q("SELECT day FROM days_off WHERE teacher_id=? AND day>=? ORDER BY day", (t, today()))],
            "blocks": q("SELECT day, hour FROM blocks WHERE teacher_id=? AND day>=? ORDER BY day, hour", (t, today())),
            "timezone": u.get("timezone") or "Europe/Moscow"}


@app.put("/api/availability")
def put_availability(b: Slots, u=Depends(teacher)):
    t = u["tg_id"]
    tx([("DELETE FROM availability WHERE teacher_id=?", (t,))] +
       [("INSERT OR IGNORE INTO availability VALUES(?,?,?)", (t, w, h)) for w, h in b.slots if 0 <= w <= 6 and 0 <= h <= 23])
    return {"ok": True}


class Block(BaseModel):
    day: str
    from_hour: int
    to_hour: int  # не включая


async def warn_conflicts(t, name, day, hours):
    """Если закрыли время, где уже стоит урок, сообщаем менеджерам: урок надо перенести."""
    for b in q("SELECT start, student FROM bookings WHERE teacher_id=? AND status='active' AND start LIKE ?", (t, day + "%")):
        if int(b["start"][11:13]) in hours:
            await notify_managers(f"{name} закрыл(а) время, но там стоит урок: {fmt(b['start'])}, ученик {b['student']}. Нужен перенос.")


@app.post("/api/days_off")
async def add_day_off(b: DayOff, u=Depends(teacher)):
    day = date.fromisoformat(b.day).isoformat()
    run("INSERT OR IGNORE INTO days_off VALUES(?,?)", (u["tg_id"], day))
    await warn_conflicts(u["tg_id"], u["name"], day, range(24))
    return {"ok": True}


@app.post("/api/blocks")
async def add_block(b: Block, u=Depends(teacher)):
    day = date.fromisoformat(b.day).isoformat()
    hours = list(range(max(b.from_hour, 0), min(b.to_hour, 24)))
    if not hours:
        raise HTTPException(422, "Конец должен быть позже начала")
    tx([("INSERT OR IGNORE INTO blocks VALUES(?,?,?)", (u["tg_id"], day, h)) for h in hours])
    await warn_conflicts(u["tg_id"], u["name"], day, hours)
    return {"ok": True}


@app.delete("/api/blocks")
def del_block(day: str, from_hour: int, to_hour: int, u=Depends(teacher)):
    tx([("DELETE FROM blocks WHERE teacher_id=? AND day=? AND hour=?", (u["tg_id"], day, h)) for h in range(from_hour, to_hour)])
    return {"ok": True}


@app.delete("/api/days_off/{day}")
def del_day_off(day: str, u=Depends(teacher)):
    run("DELETE FROM days_off WHERE teacher_id=? AND day=?", (u["tg_id"], day))
    return {"ok": True}


@app.get("/api/free")
def free(day: str, u=Depends(manager)):
    d = date.fromisoformat(day)
    return [{"id": t["tg_id"], "name": t["name"], "hours": free_hours(t["tg_id"], d)}
            for t in q("SELECT tg_id, name FROM users WHERE role='teacher' ORDER BY name")]


@app.get("/api/bookings")
def bookings(u=Depends(user)):
    now = datetime.now(TZ).strftime("%Y-%m-%dT%H:00")
    sql = ("SELECT b.*, t.name teacher FROM bookings b JOIN users t ON t.tg_id=b.teacher_id "
           "WHERE b.status='active' AND b.start>=?")
    if u["role"] == "teacher":
        rows = q(sql + " AND b.teacher_id=? ORDER BY b.start", (now, u["tg_id"]))
    else:
        rows = q(sql + " ORDER BY b.start", (now,))
    for r in rows:
        r["start_utc"] = local_start(r["start"])
        if u["role"] == "teacher":
            r["local_start"] = teacher_local(r["start"], u.get("timezone") or "Europe/Moscow")
        else:
            r["local_start"] = None
    return rows


def get_booking(bid):
    rows = q("SELECT * FROM bookings WHERE id=? AND status='active'", (bid,))
    if not rows:
        raise HTTPException(404, "Запись не найдена")
    return rows[0]


@app.post("/api/bookings")
async def create_booking(b: BookIn, u=Depends(manager)):
    start = norm(b.start)
    if int(start[11:13]) not in free_hours(b.teacher_id, date.fromisoformat(start[:10])):
        raise HTTPException(409, "Это время не отмечено как свободное")
    try:
        bid = run("INSERT INTO bookings(teacher_id,start,student,note,created_by,changed_at) VALUES(?,?,?,?,?,?)",
                  (b.teacher_id, start, b.student, b.note, u["tg_id"], now_utc()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Слот уже занят")
    await notify(b.teacher_id, f"Новый урок: {fmt(start)}\nУченик: {b.student}" + (f"\n{b.note}" if b.note else ""), confirm_kb(bid))
    return {"id": bid}


@app.post("/api/bookings/{bid}/move")
async def move_booking(bid: int, b: MoveIn, u=Depends(manager)):
    old, start = get_booking(bid), norm(b.start)
    if int(start[11:13]) not in free_hours(old["teacher_id"], date.fromisoformat(start[:10])):
        raise HTTPException(409, "Это время не отмечено как свободное")
    try:
        run("UPDATE bookings SET start=?, confirmed=0, synced=0, reminded=0, changed_at=? WHERE id=?", (start, now_utc(), bid))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Слот уже занят")
    await notify(old["teacher_id"], f"Урок перенесён: {fmt(old['start'])} на {fmt(start)}\nУченик: {old['student']}", confirm_kb(bid))
    return {"ok": True}


@app.post("/api/bookings/{bid}/cancel")
async def cancel_booking(bid: int, u=Depends(manager)):
    old = get_booking(bid)
    run("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    await notify(old["teacher_id"], f"Урок отменён: {fmt(old['start'])}\nУченик: {old['student']}")
    return {"ok": True}


@app.post("/api/recurring-bookings")
async def create_recurring_bookings(b: RecurringBookingIn, u=Depends(manager)):
    if not b.student.strip():
        raise HTTPException(422, "Укажите ученика")
    if not b.weekdays or any(w < 0 or w > 6 for w in b.weekdays):
        raise HTTPException(422, "Выберите дни недели")
    if not 8 <= b.hour <= 21:
        raise HTTPException(422, "Время должно быть в диапазоне 08:00–21:00")
    if not 1 <= b.weeks <= 16:
        raise HTTPException(422, "Можно создать расписание на 1–16 недель")
    start_day = date.fromisoformat(b.start_day)
    if start_day < datetime.now(TZ).date():
        raise HTTPException(422, "Дата начала не может быть в прошлом")
    series_id = hashlib.sha256(f"{time.time_ns()}:{u['tg_id']}:{b.teacher_id}".encode()).hexdigest()[:16]
    teacher_rows = q("SELECT tg_id, name FROM users WHERE tg_id=? AND role='teacher'", (b.teacher_id,))
    if not teacher_rows:
        raise HTTPException(404, "Преподаватель не найден")
    run("INSERT INTO recurring_series(id, teacher_id, student, note, weekdays, hour, start_day, weeks, created_by) VALUES(?,?,?,?,?,?,?,?,?)",
        (series_id, b.teacher_id, b.student.strip(), b.note.strip(), json.dumps(sorted(set(b.weekdays))), b.hour, start_day.isoformat(), b.weeks, u["tg_id"]))
    created, conflicts = [], []
    for week in range(b.weeks):
        for weekday in sorted(set(b.weekdays)):
            day = start_day + timedelta(days=(weekday - start_day.weekday()) % 7 + week * 7)
            # Если стартовая дата уже попадает на выбранный день, он считается первой неделей.
            if day < start_day:
                continue
            start = f"{day.isoformat()}T{b.hour:02d}:00"
            if b.hour not in free_hours(b.teacher_id, day):
                conflicts.append({"start": start, "reason": "Слот недоступен или уже занят"})
                continue
            try:
                bid = run("INSERT INTO bookings(teacher_id,start,student,note,created_by,changed_at,recurrence_id) VALUES(?,?,?,?,?,?,?)",
                          (b.teacher_id, start, b.student.strip(), b.note.strip(), u["tg_id"], now_utc(), series_id))
                created.append({"id": bid, "start": start})
            except sqlite3.IntegrityError:
                conflicts.append({"start": start, "reason": "Слот уже занят"})
    if created:
        await notify(b.teacher_id, f"Добавлено регулярное расписание для ученика {b.student.strip()}: {len(created)} занятий. Первое занятие: {fmt(min(created, key=lambda x: x['start'])['start'])}. Проверьте расписание в приложении.")
    return {"series_id": series_id, "created": created, "conflicts": conflicts}


@app.post("/api/bookings/{bid}/synced")
def mark_synced(bid: int, u=Depends(manager)):
    get_booking(bid)
    run("UPDATE bookings SET synced=1 WHERE id=?", (bid,))
    return {"ok": True}
