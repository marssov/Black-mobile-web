import os
import time
import secrets
from datetime import datetime
from typing import Optional

import aiomysql
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "black-mobile-super-secret-key-2026"),
    max_age=7200,
)

templates = Jinja2Templates(directory="templates")
templates.env.globals["project_name"] = os.getenv("PROJECT_NAME", "BLACK MOBILE")
templates.env.globals["project_short"] = os.getenv("PROJECT_SHORT", "BM")
templates.env.globals["logo_url"] = os.getenv("LOGO_URL", "")

OWNER_NICK = os.getenv("OWNER_NICK", "Mars_Capone")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "HrK2138DgFi0")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

LOGIN_FAILS: dict[str, list[float]] = {}


def fmt_num(val) -> str:
    try:
        return f"{int(val):,}".replace(",", " ")
    except Exception:
        return "0"


def fmt_date(ts) -> str:
    try:
        t = int(ts)
        if t <= 0:
            return "—"
        if t >= 2147483647:
            return "Навсегда"
        return datetime.fromtimestamp(t).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts or "—")


templates.env.filters["fmt_num"] = fmt_num
templates.env.filters["fmt_date"] = fmt_date


async def get_mysql():
    conn = await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        db=os.getenv("DB_NAME"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()


class _MysqlConnCtx:
    def __init__(self):
        self._gen = None

    async def __aenter__(self):
        self._gen = get_mysql()
        return await self._gen.__anext__()

    async def __aexit__(self, *args):
        if self._gen:
            await self._gen.aclose()


class _MysqlPoolWrapper:
    def acquire(self):
        return _MysqlConnCtx()


async def get_mysql_pool():
    return _MysqlPoolWrapper()


@app.on_event("startup")
async def init_db():
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS web_admins (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nickname VARCHAR(64) UNIQUE NOT NULL,
                    password_plain VARCHAR(64) NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'moder',
                    prefix VARCHAR(48) NOT NULL DEFAULT 'Мл. админ',
                    prefix_color VARCHAR(20) NOT NULL DEFAULT 'gray',
                    session_ver INT NOT NULL DEFAULT 1,
                    last_ip VARCHAR(45) DEFAULT '',
                    created_at INT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS web_invites (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    code VARCHAR(32) UNIQUE NOT NULL,
                    target_nick VARCHAR(64) DEFAULT '',
                    role VARCHAR(20) NOT NULL DEFAULT 'moder',
                    prefix VARCHAR(48) NOT NULL DEFAULT 'Мл. админ',
                    prefix_color VARCHAR(20) NOT NULL DEFAULT 'gray',
                    created_at INT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            await cur.execute("SELECT id FROM web_admins WHERE nickname = %s", (OWNER_NICK,))
            if not await cur.fetchone():
                await cur.execute(
                    "INSERT INTO web_admins (nickname, password_plain, role, prefix, prefix_color, created_at) "
                    "VALUES (%s, %s, 'owner', 'Владелец', 'purple', %s)",
                    (OWNER_NICK, OWNER_PASSWORD, int(time.time())),
                )
            else:
                await cur.execute(
                    "UPDATE web_admins SET password_plain = %s, role = 'owner', prefix = 'Владелец', prefix_color = 'purple' WHERE nickname = %s",
                    (OWNER_PASSWORD, OWNER_NICK),
                )


def check_bruteforce(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 1800]
    LOGIN_FAILS[ip] = attempts
    return len(attempts) >= 5


def record_fail(ip: str):
    LOGIN_FAILS.setdefault(ip, []).append(time.time())


async def get_current_admin(request: Request) -> Optional[dict]:
    uid = request.session.get("admin_id")
    sver = request.session.get("session_ver")
    if not uid:
        return None
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM web_admins WHERE id = %s", (uid,))
            adm = await cur.fetchone()
            if not adm or adm["session_ver"] != sver:
                request.session.clear()
                return None
            return adm


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    adm = await get_current_admin(request)
    return RedirectResponse("/overview" if adm else "/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "new_pass": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, nickname: str = Form(...), secret: str = Form(...)):
    ip = request.client.host if request.client else "0.0.0.0"
    if check_bruteforce(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Слишком много неверных попыток. Доступ с вашего IP заблокирован на 30 минут.", "new_pass": None}
        )

    nick = nickname.strip()
    sec = secret.strip()
    pool = await get_mysql_pool()

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 1. Проверка существующего администратора
            await cur.execute("SELECT * FROM web_admins WHERE nickname = %s", (nick,))
            adm = await cur.fetchone()
            if adm:
                if adm["password_plain"] == sec:
                    LOGIN_FAILS.pop(ip, None)
                    await cur.execute("UPDATE web_admins SET last_ip = %s WHERE id = %s", (ip, adm["id"]))
                    request.session["admin_id"] = adm["id"]
                    request.session["session_ver"] = adm["session_ver"]
                    return RedirectResponse("/overview", status_code=302)
                else:
                    record_fail(ip)
                    return templates.TemplateResponse(
                        request, "login.html",
                        {"error": "Неверный личный пароль доступа.", "new_pass": None}
                    )

            # 2. Проверка одноразового инвайт-кода
            await cur.execute("SELECT * FROM web_invites WHERE code = %s", (sec,))
            inv = await cur.fetchone()
            if inv:
                if inv["target_nick"] and inv["target_nick"].lower() != nick.lower():
                    return templates.TemplateResponse(
                        request, "login.html",
                        {"error": f"Этот код предназначен только для {inv['target_nick']}.", "new_pass": None}
                    )
                personal_pass = "BM-" + secrets.token_hex(3).upper()
                await cur.execute(
                    "INSERT INTO web_admins (nickname, password_plain, role, prefix, prefix_color, last_ip, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (nick, personal_pass, inv["role"], inv["prefix"], inv["prefix_color"], ip, int(time.time())),
                )
                new_id = cur.lastrowid
                await cur.execute("DELETE FROM web_invites WHERE id = %s", (inv["id"],))
                request.session["admin_id"] = new_id
                request.session["session_ver"] = 1
                request.session["just_registered_pass"] = personal_pass
                return RedirectResponse("/overview", status_code=302)

    record_fail(ip)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Неверный пароль или недействительный одноразовый код.", "new_pass": None}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

@app.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    just_pass = request.session.pop("just_registered_pass", None)
    day_ago = int(time.time()) - 86400
    stats = {"total_users": 0, "new_24h": 0, "active_24h": 0, "admins_count": 0}
    recent_logins = []

    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COUNT(*) AS c FROM accounts")
            stats["total_users"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE reg_time >= %s", (day_ago,))
            stats["new_24h"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE last_login >= %s", (day_ago,))
            stats["active_24h"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE admin > 0")
            stats["admins_count"] = (await cur.fetchone())["c"]
            await cur.execute(
                "SELECT id, name, level, last_ip, last_login FROM accounts ORDER BY last_login DESC LIMIT 15"
            )
            recent_logins = await cur.fetchall()

    return templates.TemplateResponse(request, "overview.html", {
        "admin": admin,
        "active_tab": "overview",
        "just_pass": just_pass,
        "stats": stats,
        "recent_logins": recent_logins,
    })


@app.get("/checks", response_class=HTMLResponse)
async def checks_page(request: Request):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    big_transfers = []
    multi_ips = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM money_audit_log WHERE ABS(amount) >= 5000000 ORDER BY time DESC LIMIT 20"
            )
            big_transfers = await cur.fetchall()
            await cur.execute(
                "SELECT reg_ip, COUNT(*) AS cnt, GROUP_CONCAT(name SEPARATOR ', ') AS names "
                "FROM accounts WHERE reg_ip != '' GROUP BY reg_ip HAVING cnt >= 3 ORDER BY cnt DESC LIMIT 15"
            )
            multi_ips = await cur.fetchall()

    return templates.TemplateResponse(request, "checks.html", {
        "admin": admin,
        "active_tab": "checks",
        "big_transfers": big_transfers,
        "multi_ips": multi_ips,
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, cat: str = "money"):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    items = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if cat == "admin":
                await cur.execute("SELECT * FROM aclogs ORDER BY id DESC LIMIT 30")
            elif cat == "bans":
                await cur.execute("SELECT * FROM ban_list ORDER BY id DESC LIMIT 30")
            else:
                cat = "money"
                await cur.execute("SELECT * FROM money_audit_log ORDER BY time DESC LIMIT 30")
            items = await cur.fetchall()

    return templates.TemplateResponse(request, "logs.html", {
        "admin": admin,
        "active_tab": "logs",
        "cat": cat,
        "items": items,
    })


@app.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    game_admins = []
    leaders = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, name, admin, level, last_login, last_ip FROM accounts WHERE admin > 0 ORDER BY admin DESC, id ASC"
            )
            game_admins = await cur.fetchall()
            try:
                await cur.execute("SELECT * FROM leaders ORDER BY id ASC LIMIT 30")
                leaders = await cur.fetchall()
            except Exception:
                leaders = []

    return templates.TemplateResponse(request, "staff.html", {
        "admin": admin,
        "active_tab": "staff",
        "game_admins": game_admins,
        "leaders": leaders,
    })


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    members = []
    invites = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM web_admins ORDER BY id ASC")
            members = await cur.fetchall()
            if admin["role"] == "owner":
                await cur.execute("SELECT * FROM web_invites ORDER BY id DESC")
                invites = await cur.fetchall()

    return templates.TemplateResponse(request, "team.html", {
        "admin": admin,
        "active_tab": "team",
        "members": members,
        "invites": invites,
    })


@app.post("/team/invite")
async def team_create_invite(
    request: Request,
    target_nick: str = Form(""),
    role: str = Form("moder"),
    prefix_preset: str = Form("Тех. специалист"),
    custom_prefix: str = Form(""),
    prefix_color: str = Form("cyan"),
):
    admin = await get_current_admin(request)
    if not admin or admin["role"] != "owner":
        return RedirectResponse("/team", status_code=302)

    final_prefix = custom_prefix.strip() if custom_prefix.strip() else prefix_preset
    code = "BM-" + secrets.token_hex(2).upper() + "-" + secrets.token_hex(2).upper()

    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO web_invites (code, target_nick, role, prefix, prefix_color, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (code, target_nick.strip(), role, final_prefix, prefix_color, int(time.time())),
            )
    return RedirectResponse("/team", status_code=302)


@app.post("/team/manage")
async def team_manage(
    request: Request,
    action: str = Form(...),
    target_id: int = Form(...),
    prefix: str = Form(""),
    prefix_color: str = Form("blue"),
    role: str = Form("moder"),
):
    admin = await get_current_admin(request)
    if not admin or admin["role"] != "owner":
        return RedirectResponse("/team", status_code=302)

    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if action == "del_invite":
                await cur.execute("DELETE FROM web_invites WHERE id = %s", (target_id,))
            elif action == "delete_member":
                await cur.execute("DELETE FROM web_admins WHERE id = %s AND role != 'owner'", (target_id,))
            elif action == "reset_pass":
                new_pass = "BM-" + secrets.token_hex(3).upper()
                await cur.execute(
                    "UPDATE web_admins SET password_plain = %s, session_ver = session_ver + 1 WHERE id = %s AND role != 'owner'",
                    (new_pass, target_id),
                )
            elif action == "update_prefix":
                await cur.execute(
                    "UPDATE web_admins SET prefix = %s, prefix_color = %s, role = %s WHERE id = %s AND role != 'owner'",
                    (prefix.strip() or "Админ", prefix_color, role, target_id),
                )
    return RedirectResponse("/team", status_code=302)

@app.get("/players", response_class=HTMLResponse)
async def players_page(request: Request, q: str = "", msg: str = ""):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    q_clean = q.strip()
    player = None
    twinks = []
    old_names = []
    real_donated = 0
    bans_count = 0
    is_banned = False
    recent_money = []

    if q_clean:
        pool = await get_mysql_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if q_clean.isdigit():
                    await cur.execute("SELECT * FROM accounts WHERE id = %s LIMIT 1", (int(q_clean),))
                else:
                    await cur.execute("SELECT * FROM accounts WHERE name = %s LIMIT 1", (q_clean,))
                player = await cur.fetchone()

                if not player and "." in q_clean:
                    await cur.execute("SELECT * FROM accounts WHERE reg_ip = %s OR last_ip = %s LIMIT 1", (q_clean, q_clean))
                    player = await cur.fetchone()

                if not player:
                    try:
                        await cur.execute("SELECT uid FROM change_names WHERE name = %s LIMIT 1", (q_clean,))
                        cn = await cur.fetchone()
                        if cn:
                            await cur.execute("SELECT * FROM accounts WHERE id = %s LIMIT 1", (cn["uid"],))
                            player = await cur.fetchone()
                    except Exception:
                        pass

                if player:
                    pid = player["id"]
                    await cur.execute(
                        "SELECT id, name, level, money FROM accounts WHERE (reg_ip = %s OR last_ip = %s) AND id != %s AND %s != '' LIMIT 15",
                        (player["reg_ip"], player["last_ip"], pid, player["last_ip"]),
                    )
                    twinks = await cur.fetchall()
                    try:
                        await cur.execute("SELECT name FROM change_names WHERE uid = %s ORDER BY id DESC LIMIT 15", (pid,))
                        old_names = await cur.fetchall()
                    except Exception:
                        old_names = []
                    try:
                        await cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE user_id = %s AND status = 1", (pid,))
                        real_donated = int((await cur.fetchone())["s"])
                    except Exception:
                        real_donated = 0
                    await cur.execute("SELECT COUNT(*) AS c FROM ban_list WHERE user_id = %s", (pid,))
                    bans_count = (await cur.fetchone())["c"]
                    now_ts = int(time.time())
                    await cur.execute("SELECT id FROM ban_list WHERE user_id = %s AND ban_time > %s LIMIT 1", (pid, now_ts))
                    is_banned = bool(await cur.fetchone())
                    await cur.execute("SELECT * FROM money_audit_log WHERE uid = %s ORDER BY time DESC LIMIT 20", (pid,))
                    recent_money = await cur.fetchall()

    return templates.TemplateResponse(request, "players.html", {
        "admin": admin,
        "active_tab": "players",
        "q": q_clean,
        "msg": msg,
        "player": player,
        "twinks": twinks,
        "old_names": old_names,
        "real_donated": real_donated,
        "bans_count": bans_count,
        "is_banned": is_banned,
        "recent_money": recent_money,
    })


@app.post("/players/action")
async def player_action(
    request: Request,
    player_id: int = Form(...),
    action_type: str = Form(...),
    value: int = Form(0),
    reason: str = Form("Выдано через веб-панель"),
):
    admin = await get_current_admin(request)
    if not admin or admin["role"] not in ["admin", "owner"]:
        return RedirectResponse(f"/players?q={player_id}", status_code=302)

    pool = await get_mysql_pool()
    now_ts = int(time.time())
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id, name, last_ip FROM accounts WHERE id = %s", (player_id,))
            target = await cur.fetchone()
            if not target:
                return RedirectResponse(f"/players?q={player_id}", status_code=302)

            if action_type == "money":
                await cur.execute("UPDATE accounts SET money = money + %s WHERE id = %s", (value, player_id))
            elif action_type == "rub":
                await cur.execute("UPDATE accounts SET rub = rub + %s WHERE id = %s", (value, player_id))
            elif action_type == "admin":
                await cur.execute("UPDATE accounts SET admin = %s WHERE id = %s", (max(0, value), player_id))
            elif action_type == "ban":
                ban_until = now_ts + max(1, value) * 86400
                await cur.execute(
                    "INSERT INTO ban_list (user_id, time, ban_time, ip, description, admin) VALUES (%s, %s, %s, %s, %s, %s)",
                    (player_id, now_ts, ban_until, target["last_ip"] or "", reason[:32], admin["nickname"][:24]),
                )
            elif action_type == "permban":
                await cur.execute(
                    "INSERT INTO ban_list (user_id, time, ban_time, ip, description, admin) VALUES (%s, %s, %s, %s, %s, %s)",
                    (player_id, now_ts, 2147483647, target["last_ip"] or "", reason[:32], admin["nickname"][:24]),
                )
            elif action_type == "unban":
                await cur.execute("DELETE FROM ban_list WHERE user_id = %s", (player_id,))
            elif action_type == "wipe":
                await cur.execute("UPDATE accounts SET money = 0, bank = 0, rub = 0 WHERE id = %s", (player_id,))
                await cur.execute("DELETE FROM ownable_cars WHERE owner_id = %s", (player_id,))
                await cur.execute("UPDATE houses SET owner_id = 0 WHERE owner_id = %s", (player_id,))
                await cur.execute("UPDATE business SET owner_id = 0 WHERE owner_id = %s", (player_id,))

            await cur.execute(
                "INSERT INTO aclogs (name, type, data, reason, date) VALUES (%s, %s, %s, %s, %s)",
                (admin["nickname"], f"web_{action_type}", f"{target['name']} (#{player_id}) val={value}", reason[:64], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

    return RedirectResponse(f"/players?q={player_id}&msg=ok", status_code=302)

@app.get("/economy", response_class=HTMLResponse)
async def economy_page(request: Request):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    eco = {"total_cash": 0, "total_bank": 0, "total_dp": 0}
    top_rich = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COALESCE(SUM(money),0) AS cash, COALESCE(SUM(bank),0) AS bank, COALESCE(SUM(rub),0) AS dp FROM accounts")
            r = await cur.fetchone()
            eco = {"total_cash": int(r["cash"]), "total_bank": int(r["bank"]), "total_dp": int(r["dp"])}
            await cur.execute("SELECT id, name, level, (money + bank) AS total_wealth FROM accounts ORDER BY total_wealth DESC LIMIT 15")
            top_rich = await cur.fetchall()

    return templates.TemplateResponse(request, "economy.html", {
        "admin": admin,
        "active_tab": "economy",
        "eco": eco,
        "top_rich": top_rich,
    })


@app.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request, q: str = ""):
    admin = await get_current_admin(request)
    if not admin:
        return RedirectResponse("/login", status_code=302)

    q_clean = q.strip()
    answer = None

    if q_clean:
        pool = await get_mysql_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT id, name, level, (money + bank) AS wealth, rub FROM accounts ORDER BY wealth DESC LIMIT 10")
                top_rich = await cur.fetchall()
                await cur.execute("SELECT id, name, level, (money + bank) AS wealth, reg_ip FROM accounts WHERE level <= 3 AND (money + bank) >= 1000000 ORDER BY wealth DESC LIMIT 10")
                rich_new = await cur.fetchall()
                await cur.execute("SELECT player_name, counterparty_name, amount, description FROM money_audit_log ORDER BY time DESC LIMIT 15")
                last_money = await cur.fetchall()
                await cur.execute("SELECT reg_ip, COUNT(*) AS cnt, GROUP_CONCAT(name SEPARATOR ', ') AS names FROM accounts WHERE reg_ip != '' GROUP BY reg_ip HAVING cnt >= 3 ORDER BY cnt DESC LIMIT 10")
                twink_ips = await cur.fetchall()

                mentioned_player = None
                for word in q_clean.replace("?", "").replace(",", "").split():
                    if "_" in word or word.isdigit():
                        if word.isdigit():
                            await cur.execute("SELECT id, name, level, money, bank, rub, reg_ip, last_ip FROM accounts WHERE id = %s LIMIT 1", (int(word),))
                        else:
                            await cur.execute("SELECT id, name, level, money, bank, rub, reg_ip, last_ip FROM accounts WHERE name = %s LIMIT 1", (word,))
                        mentioned_player = await cur.fetchone()
                        if mentioned_player:
                            break

        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            answer = "Ключ GROQ_API_KEY не найден в настройках Render."
        else:
            context_text = (
                f"Топ-10 богатейших игроков: {top_rich}\n"
                f"Богатые новички (1-3 LVL с балансом > 1кк): {rich_new}\n"
                f"Последние 15 денежных операций: {last_money}\n"
                f"Подозрительные IP с >=3 аккаунтами: {twink_ips}\n"
                f"Упомянутый игрок: {mentioned_player}"
            )
            system_prompt = (
                "Ты — главный ИИ-следователь RP-проекта BLACK MOBILE. "
                "Отвечай строго на русском языке, чётко и понятным текстом без SQL-кода. "
                "Опирайся только на переданные данные из базы сервера."
            )
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }
                payload = {
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Данные сервера:\n{context_text}\n\nВопрос: {q_clean}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 900,
                }
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post("https://groq.com", headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        answer = data["choices"][0]["message"]["content"]
                    else:
                        answer = f"Сервер ИИ вернул статус {resp.status_code}: {resp.text[:180]}"
            except Exception as e:
                answer = f"Ошибка соединения с ИИ: {e}"

    return templates.TemplateResponse(request, "ai.html", {
        "admin": admin,
        "active_tab": "ai",
        "q": q_clean,
        "answer": answer,
    })
