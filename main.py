import os
import time
from datetime import datetime
import aiomysql
import aiosqlite
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "bm_secret_key_change_me_2026")
)
templates = Jinja2Templates(directory="templates")
templates.env.globals["project_name"] = os.getenv("PROJECT_NAME", "BLACK MOBILE")
templates.env.globals["project_short"] = os.getenv("PROJECT_SHORT", "BM")
templates.env.globals["logo_url"] = os.getenv("LOGO_URL", "")

ACCESS_PASSWORD = os.getenv("ADMIN_ACCESS_PASSWORD", "BM2026")
OWNER_NICK = os.getenv("OWNER_NICK", "Artem_Popikov")
SQLITE_DB = os.getenv("SQLITE_PATH", "web_panel.db")


@app.on_event("startup")
async def startup():
    async with aiosqlite.connect(SQLITE_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS web_admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT UNIQUE NOT NULL,
                ip TEXT,
                user_agent TEXT,
                role TEXT DEFAULT 'moder',
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
        """)
        await db.commit()


async def get_mysql():
    conn = await aiomysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        db=os.getenv("DB_NAME", "gs356095"),
        charset="utf8mb4",
        cursorclass=aiomysql.DictCursor,
        autocommit=True
    )
    try:
        yield conn
    finally:
        conn.close()


def fmt_num(val):
    if val is None:
        return "0"
    return f"{int(val):,}".replace(",", " ")


def fmt_date(ts):
    if not ts or int(ts) == 0:
        return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")


templates.env.filters["fmt_num"] = fmt_num
templates.env.filters["fmt_date"] = fmt_date


async def get_current_admin(request: Request):
    nick = request.session.get("nickname")
    if not nick:
        return None
    async with aiosqlite.connect(SQLITE_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM web_admins WHERE nickname = ?", (nick,))
        row = await cur.fetchone()
        return dict(row) if row else None


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    admin = await get_current_admin(request)
    if admin and admin["status"] == "approved":
        return RedirectResponse("/players", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "admin": admin,
        "error": None
    })


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    nickname: str = Form(...),
    password: str = Form(...)
):
    nickname = nickname.strip()
    if password != ACCESS_PASSWORD:
        return templates.TemplateResponse(request, "login.html", {
            "admin": None,
            "error": "Неверный секретный пароль доступа"
        })

    client_ip = request.headers.get("x-forwarded-for", request.client.host)
    ua = request.headers.get("user-agent", "")[:120]

    async with aiosqlite.connect(SQLITE_DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM web_admins WHERE nickname = ?", (nickname,))
        existing = await cur.fetchone()

        if not existing:
            is_owner = (nickname.lower() == OWNER_NICK.lower())
            status = "approved" if is_owner else "pending"
            role = "owner" if is_owner else "moder"
            await db.execute(
                "INSERT INTO web_admins (nickname, ip, user_agent, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (nickname, client_ip, ua, role, status, int(time.time()))
            )
            await db.commit()
        else:
            await db.execute("UPDATE web_admins SET ip = ? WHERE nickname = ?", (client_ip, nickname))
            await db.commit()

    request.session["nickname"] = nickname
    return RedirectResponse("/login", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
async def root():
    return RedirectResponse("/players", status_code=303)


@app.get("/players", response_class=HTMLResponse)
async def players_page(request: Request, q: str = "", db=Depends(get_mysql)):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
        return RedirectResponse("/login", status_code=303)

    player = None
    twinks = []
    old_names = []
    real_donated = 0
    bans_count = 0
    recent_money = []
    server_online = False

    async with db.cursor() as cur:
        await cur.execute("SELECT seen_at FROM laird_heartbeat LIMIT 1")
        hb = await cur.fetchone()
        if hb and (int(time.time()) - int(hb["seen_at"]) < 120):
            server_online = True

        q_clean = q.strip()
        if q_clean:
            if q_clean.isdigit():
                await cur.execute("SELECT * FROM accounts WHERE id = %s LIMIT 1", (int(q_clean),))
            else:
                await cur.execute(
                    "SELECT * FROM accounts WHERE name = %s OR reg_ip = %s OR last_ip = %s LIMIT 1",
                    (q_clean, q_clean, q_clean)
                )
            player = await cur.fetchone()

            if not player and not q_clean.isdigit():
                await cur.execute("SELECT owner_id FROM change_names WHERE name = %s LIMIT 1", (q_clean,))
                cn = await cur.fetchone()
                if cn:
                    await cur.execute("SELECT * FROM accounts WHERE id = %s LIMIT 1", (cn["owner_id"],))
                    player = await cur.fetchone()

            if player:
                uid = player["id"]

                await cur.execute(
                    """SELECT id, name, level, money, last_login FROM accounts
                       WHERE id != %s AND (
                           (reg_ip != '' AND (reg_ip = %s OR reg_ip = %s)) OR
                           (last_ip != '' AND (last_ip = %s OR last_ip = %s))
                       ) LIMIT 10""",
                    (uid, player["reg_ip"], player["last_ip"], player["reg_ip"], player["last_ip"])
                )
                twinks = await cur.fetchall()

                await cur.execute("SELECT name, time FROM change_names WHERE owner_id = %s ORDER BY id DESC", (uid,))
                old_names = await cur.fetchall()

                await cur.execute("SELECT COALESCE(SUM(sum), 0) AS total FROM payments WHERE user_id = %s AND status > 0", (uid,))
                pay_row = await cur.fetchone()
                real_donated = pay_row["total"] if pay_row else 0

                await cur.execute("SELECT COUNT(*) AS cnt FROM ban_list WHERE user_id = %s", (uid,))
                ban_row = await cur.fetchone()
                bans_count = ban_row["cnt"] if ban_row else 0

                await cur.execute(
                    """SELECT * FROM money_audit_log
                       WHERE uid = %s OR counterparty_uid = %s
                       ORDER BY id DESC LIMIT 15""",
                    (uid, uid)
                )
                recent_money = await cur.fetchall()

    async with aiosqlite.connect(SQLITE_DB) as sdb:
        cur = await sdb.execute("SELECT COUNT(*) FROM web_admins WHERE status = 'pending'")
        row_cnt = await cur.fetchone()
        pending_count = row_cnt[0] if row_cnt else 0

    return templates.TemplateResponse(request, "players.html", {
        "admin": admin,
        "active_tab": "players",
        "pending_count": pending_count,
        "q": q_clean,
        "player": player,
        "twinks": twinks,
        "old_names": old_names,
        "real_donated": real_donated,
        "bans_count": bans_count,
        "recent_money": recent_money,
        "server_online": server_online
    })


@app.post("/players/action")
async def player_action(
    request: Request,
    player_id: int = Form(...),
    action_type: str = Form(...),
    value: int = Form(0),
    reason: str = Form("Выдано через веб-панель"),
    db=Depends(get_mysql)
):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved" or admin["role"] == "moder":
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    now_ts = int(time.time())
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with db.cursor() as cur:
        await cur.execute("SELECT name FROM accounts WHERE id = %s", (player_id,))
        target = await cur.fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Игрок не найден")

        if action_type == "money":
            await cur.execute("UPDATE accounts SET money = money + %s WHERE id = %s", (value, player_id))
            log_text = f"Выдал {value} вирт игроку {target['name']} (веб-панель)"
        elif action_type == "rub":
            await cur.execute("UPDATE accounts SET rub = rub + %s WHERE id = %s", (value, player_id))
            log_text = f"Выдал {value} DP игроку {target['name']} (веб-панель)"
        elif action_type == "admin":
            await cur.execute("UPDATE accounts SET admin = %s WHERE id = %s", (value, player_id))
            log_text = f"Установил {value} уровень админки игроку {target['name']} (веб-панель)"
        elif action_type == "ban":
            ban_until = now_ts + (value * 86400)
            await cur.execute(
                "INSERT INTO ban_list (user_id, time, ban_time, ip, description, admin) VALUES (%s, %s, %s, '', %s, %s)",
                (player_id, now_ts, ban_until, reason[:32], admin["nickname"][:24])
            )
            log_text = f"Забанил {target['name']} на {value} дн. Причина: {reason}"
        else:
            log_text = "Неизвестное действие"

        await cur.execute(
            "INSERT INTO aclogs (type, date, name, reason, data) VALUES (%s, %s, %s, %s, %s)",
            ("WEB_PANEL", date_str, admin["nickname"][:24], reason[:32], log_text[:128])
        )

    return RedirectResponse(f"/players?q={player_id}", status_code=303)


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
        return RedirectResponse("/login", status_code=303)

    async with aiosqlite.connect(SQLITE_DB) as sdb:
        sdb.row_factory = aiosqlite.Row
        cur = await sdb.execute("SELECT * FROM web_admins WHERE status = 'pending' ORDER BY id DESC")
        pending = [dict(r) for r in await cur.fetchall()]

        cur = await sdb.execute("SELECT * FROM web_admins WHERE status != 'pending' ORDER BY id ASC")
        members = [dict(r) for r in await cur.fetchall()]

    return templates.TemplateResponse(request, "team.html", {
        "admin": admin,
        "active_tab": "team",
        "pending_count": len(pending),
        "pending": pending,
        "members": members
    })


@app.post("/team/decide")
async def team_decide(
    request: Request,
    target_id: int = Form(...),
    decision: str = Form(...),
    role: str = Form("moder")
):
    admin = await get_current_admin(request)
    if not admin or admin["role"] != "owner":
        raise HTTPException(status_code=403, detail="Только владелец может управлять доступом")

    async with aiosqlite.connect(SQLITE_DB) as sdb:
        if decision == "approve":
            await sdb.execute("UPDATE web_admins SET status = 'approved', role = ? WHERE id = ?", (role, target_id))
        elif decision == "reject":
            await sdb.execute("UPDATE web_admins SET status = 'rejected' WHERE id = ?", (target_id,))
        elif decision == "delete":
            await sdb.execute("DELETE FROM web_admins WHERE id = ? AND role != 'owner'", (target_id,))
        await sdb.commit()

    return RedirectResponse("/team", status_code=303)
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
async def get_pending_count() -> int:
    async with aiosqlite.connect(SQLITE_DB) as sdb:
        cur = await sdb.execute("SELECT COUNT(*) FROM web_admins WHERE status = 'pending'")
        row = await cur.fetchone()
        return row[0] if row else 0


@app.get("/overview", response_class=HTMLResponse)
async def overview_page(request: Request):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
        return RedirectResponse("/login", status_code=302)

    day_ago = int(time.time()) - 86400
    stats = {"total_users": 0, "new_24h": 0, "active_24h": 0, "admins_count": 0}
    recent_logins = []

    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COUNT(*) AS c FROM users")
            stats["total_users"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM users WHERE reg_time >= %s", (day_ago,))
            stats["new_24h"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM users WHERE last_login >= %s", (day_ago,))
            stats["active_24h"] = (await cur.fetchone())["c"]
            await cur.execute("SELECT COUNT(*) AS c FROM users WHERE admin > 0")
            stats["admins_count"] = (await cur.fetchone())["c"]
            await cur.execute(
                "SELECT id, name, level, last_ip, last_login FROM users ORDER BY last_login DESC LIMIT 15"
            )
            recent_logins = await cur.fetchall()

    return templates.TemplateResponse(request, "overview.html", {
        "admin": admin,
        "active_tab": "overview",
        "pending_count": await get_pending_count(),
        "stats": stats,
        "recent_logins": recent_logins
    })


@app.get("/checks", response_class=HTMLResponse)
async def checks_page(request: Request):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
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
                "FROM users WHERE reg_ip != '' GROUP BY reg_ip HAVING cnt >= 3 ORDER BY cnt DESC LIMIT 15"
            )
            multi_ips = await cur.fetchall()

    return templates.TemplateResponse(request, "checks.html", {
        "admin": admin,
        "active_tab": "checks",
        "pending_count": await get_pending_count(),
        "big_transfers": big_transfers,
        "multi_ips": multi_ips
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, cat: str = "money"):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
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
        "pending_count": await get_pending_count(),
        "cat": cat,
        "items": items
    })

@app.get("/economy", response_class=HTMLResponse)
async def economy_page(request: Request):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
        return RedirectResponse("/login", status_code=302)

    eco = {"total_cash": 0, "total_bank": 0, "total_dp": 0}
    top_rich = []
    pool = await get_mysql_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT COALESCE(SUM(money),0) AS cash, COALESCE(SUM(bank),0) AS bank, COALESCE(SUM(rub),0) AS dp FROM users"
            )
            r = await cur.fetchone()
            eco = {"total_cash": int(r["cash"]), "total_bank": int(r["bank"]), "total_dp": int(r["dp"])}
            await cur.execute(
                "SELECT id, name, level, (money + bank) AS total_wealth FROM users ORDER BY total_wealth DESC LIMIT 15"
            )
            top_rich = await cur.fetchall()

    return templates.TemplateResponse(request, "economy.html", {
        "admin": admin,
        "active_tab": "economy",
        "pending_count": await get_pending_count(),
        "eco": eco,
        "top_rich": top_rich
    })


@app.get("/ai", response_class=HTMLResponse)
async def ai_page(request: Request, q: str = "", preset: str = ""):
    admin = await get_current_admin(request)
    if not admin or admin["status"] != "approved":
        return RedirectResponse("/login", status_code=302)

    report = None
    q_clean = q.strip()
    pool = await get_mysql_pool()

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if preset == "rich_newbies":
                await cur.execute(
                    "SELECT id, name, level, (money + bank) AS wealth FROM users WHERE level <= 3 AND (money + bank) >= 1000000 ORDER BY wealth DESC LIMIT 15"
                )
                rows_db = await cur.fetchall()
                report = {
                    "title": "Богатые новички (1–3 LVL)",
                    "summary": f"Найдено {len(rows_db)} игроков низкого уровня с балансом от 1 000 000 ₽. Рекомендуется проверить источники получения средств.",
                    "rows": [{"title": f"{r['name']} (#{r['id']})", "sub": f"{r['level']} LVL", "tag": f"{int(r['wealth']):,} ₽".replace(",", " ")} for r in rows_db]
                }
            elif preset == "big_transfers":
                day_ago = int(time.time()) - 86400
                await cur.execute(
                    "SELECT * FROM money_audit_log WHERE time >= %s ORDER BY ABS(amount) DESC LIMIT 15", (day_ago,)
                )
                rows_db = await cur.fetchall()
                report = {
                    "title": "Крупнейшие переводы за 24 часа",
                    "summary": f"Топ-{len(rows_db)} самых крупных денежных операций на сервере за последние сутки.",
                    "rows": [{"title": f"{r['player_name']} → {r['counterparty_name'] or '—'}", "sub": r['description'] or 'Операция', "tag": f"{int(r['amount']):,} ₽".replace(",", " ")} for r in rows_db]
                }
            elif preset == "twinks":
                await cur.execute(
                    "SELECT reg_ip, COUNT(*) AS cnt, GROUP_CONCAT(name SEPARATOR ', ') AS names FROM users WHERE reg_ip != '' GROUP BY reg_ip HAVING cnt >= 3 ORDER BY cnt DESC LIMIT 15"
                )
                rows_db = await cur.fetchall()
                report = {
                    "title": "Сетки мультиаккаунтов по IP",
                    "summary": f"Обнаружено {len(rows_db)} IP-адресов с 3 и более зарегистрированными аккаунтами.",
                    "rows": [{"title": f"IP {r['reg_ip']}", "sub": r['names'][:60], "tag": f"{r['cnt']} акк."} for r in rows_db]
                }
            elif q_clean:
                if q_clean.isdigit():
                    await cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (int(q_clean),))
                else:
                    await cur.execute("SELECT * FROM users WHERE name = %s LIMIT 1", (q_clean,))
                u = await cur.fetchone()
                if u:
                    await cur.execute("SELECT COUNT(*) AS c FROM ban_list WHERE user_id = %s", (u["id"],))
                    bans = (await cur.fetchone())["c"]
                    await cur.execute("SELECT COUNT(*) AS c FROM users WHERE (reg_ip = %s OR last_ip = %s) AND id != %s", (u["reg_ip"], u["last_ip"], u["id"]))
                    twinks_cnt = (await cur.fetchone())["c"]
                    risk = "ВЫСОКИЙ" if (bans > 0 or twinks_cnt >= 2) else "НИЗКИЙ"
                    report = {
                        "title": f"Досье на {u['name']} (#{u['id']})",
                        "summary": f"Уровень риска: {risk}.\nУровень аккаунта: {u['level']} LVL · Общий капитал: {int(u['money'] + u['bank']):,} ₽.\nСвязанных твинков по IP: {twinks_cnt} · Блокировок в истории: {bans}.".replace(",", " "),
                        "rows": []
                    }
                else:
                    report = {"title": "Результат поиска", "summary": f"Игрок «{q_clean}» не найден в базе данных.", "rows": []}

    return templates.TemplateResponse(request, "ai.html", {
        "admin": admin,
        "active_tab": "ai",
        "pending_count": await get_pending_count(),
        "q": q_clean,
        "report": report
    })
