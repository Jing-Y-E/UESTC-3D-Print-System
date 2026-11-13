import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("DB_PATH", "/app/data/test.db")
INITIAL_PRINTER_STOCK = 5000.0
SECRET_KEY = "3d-printer-demo-secret"
COOKIE_NAME = "session"
# 打印机 -> 所在地址
PRINTERS = {
    "打印机A": "教室1",
    "打印机B": "教室2",
    "打印机C": "实验室3",
}

STATUS_PENDING = "待审批"
STATUS_APPROVED = "已通过"
STATUS_REJECTED = "已拒绝"
STATUS_COMPLETED = "已完成"
FIX_REPAIRING = "维修中"
FIX_DONE = "维修成功"
TICKET_PENDING = "待解决"
TICKET_CONFIRM = "待确认"
TICKET_DONE = "已解决"
EXP_PENDING = "待审核"
EXP_APPROVED = "已通过"
EXP_REJECTED = "已拒绝"

APPROVE_FAIL_MSG = "⚠️ 审批失败：剩余耗材不足，无法通过该预约，请先补充耗材。"
TICKET_CREATED_MSG = "已上报管理员，请等待回复。"
EXP_CREATED_MSG = "感谢分享！您的经验已提交给管理员审核。"

# 写死的测试账号：账号 -> 密码 / 角色 / 学生资料
USERS = {
    "20261101": {
        "password": "123456",
        "role": "student",
        "name": "张三",
        "class_name": "计算机大类1班",
    },
    "20261102": {
        "password": "123456",
        "role": "student",
        "name": "李四",
        "class_name": "计算机大类2班",
    },
    "admin": {"password": "admin", "role": "admin", "name": "管理员", "class_name": ""},
}

# 系统内置初始方案
SOLUTIONS = {
    "翘边": "建议：1. 重新调平热床；2. 清洁热床表面并涂抹固体胶；3. 热床温度上调 5-10℃ 后重试。如果依然无法解决，请联系管理员维修。",
    "堵头": "建议：1. 升高喷头温度进行热拔；2. 用通针疏通；3. 检查耗材是否受潮。如果是设备老化，请点击管理员维修登记。",
    "拉丝": "建议：1. 提高回抽距离与回抽速度；2. 喷头温度下调 5-10℃；3. 打印后用热风快速处理残留拉丝。",
    "错层": "建议：1. 检查同步带张紧度并重新校准；2. 降低打印速度；3. 清理并润滑 Z 轴丝杆。",
    "悬垂": "建议：1. 喷头温度下调 5℃；2. 提高风扇转速；3. 为悬垂区域添加支撑并降低悬垂速度。",
    "不粘": "建议：1. 重新调平热床并清洁表面；2. 涂抹固体胶或更换 PEI 板；3. 降低首层速度、上调首层温度。",
}

app = FastAPI(title="3D 打印机预约系统")

# 静态文件：校徽等图片放在 main.py 同目录（容器内为 /app）
APP_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建库建表：数据库不存在时自动创建，每台打印机独立初始化 5000 克耗材。"""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            booking_time TEXT NOT NULL,
            printer TEXT NOT NULL,
            consumable REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '待审批',
            owner TEXT NOT NULL DEFAULT '',
            duration TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(bookings)")}
    if "consumable" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN consumable REAL NOT NULL DEFAULT 0")
    if "status" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN status TEXT NOT NULL DEFAULT '待审批'")
    if "owner" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
    if "duration" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN duration TEXT NOT NULL DEFAULT ''")
    if "note" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN note TEXT NOT NULL DEFAULT ''")

    # 打印机独立耗材
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS printer_stock (
            printer TEXT PRIMARY KEY,
            remaining REAL NOT NULL
        )
        """
    )
    for printer in PRINTERS:
        conn.execute(
            "INSERT OR IGNORE INTO printer_stock (printer, remaining) VALUES (?, ?)",
            (printer, INITIAL_PRINTER_STOCK),
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            issue TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '维修中',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("UPDATE maintenance SET status = ? WHERE status = '已修复'", (FIX_DONE,))

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            question_keyword TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '待解决',
            admin_reply TEXT NOT NULL DEFAULT '',
            follow_up TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    ticket_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "follow_up" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN follow_up TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            reply TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS student_experiences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            problem TEXT NOT NULL,
            solution TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '待审核',
            feedback TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    # 问答社区：废弃旧 qa_board，改为 questions + answers
    conn.execute("DROP TABLE IF EXISTS qa_board")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asker_name TEXT NOT NULL,
            question_content TEXT NOT NULL,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            answerer_name TEXT NOT NULL,
            answer_content TEXT NOT NULL,
            likes INTEGER NOT NULL DEFAULT 0,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    answer_cols = {row["name"] for row in conn.execute("PRAGMA table_info(answers)")}
    if "likes" not in answer_cols:
        conn.execute("ALTER TABLE answers ADD COLUMN likes INTEGER NOT NULL DEFAULT 0")
    if "is_pinned" not in answer_cols:
        conn.execute("ALTER TABLE answers ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
    # 点赞记录：谁给哪条回答点了赞（支持再次点击取消）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS answer_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            answer_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (answer_id, user_id)
        )
        """
    )
    like_cols = {row["name"] for row in conn.execute("PRAGMA table_info(answer_likes)")}
    if "user_id" not in like_cols:
        # 兼容旧表（voter 字段）：迁移为 user_id
        conn.execute("ALTER TABLE answer_likes RENAME TO answer_likes_old")
        conn.execute(
            """
            CREATE TABLE answer_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                answer_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (answer_id, user_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO answer_likes (answer_id, user_id, created_at)
            SELECT answer_id, voter, created_at FROM answer_likes_old
            """
        )
        conn.execute("DROP TABLE answer_likes_old")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_answer_likes_user "
        "ON answer_likes (answer_id, user_id)"
    )
    conn.commit()
    conn.close()


init_db()


STYLE = """
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f4f7f9; color: #1f2937; }
  h1 { font-size: 22px; }
  h2 { font-size: 16px; margin: 0 0 12px; color: #1e293b; }
  .navbar { position: sticky; top: 0; z-index: 20; display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; padding: 12px 24px; background: #fff; box-shadow: 0 2px 10px rgba(0, 0, 0, 0.06); }
  .nav-brand { font-size: 16px; font-weight: 700; color: #1e293b; }
  .nav-right { display: flex; align-items: center; gap: 10px; font-size: 14px; color: #475569; flex-wrap: wrap; }
  .nav-right b { color: #1e293b; }
  .nav-link { padding: 6px 12px; border-radius: 8px; background: #eff6ff; color: #2563eb; font-size: 13px; text-decoration: none; }
  .nav-link:hover { background: #dbeafe; }
  .nav-logo { width: 30px; height: 30px; border-radius: 50%; }
  .nav-org { font-size: 13px; font-weight: 600; color: #334155; letter-spacing: 0.3px; }
  .container { max-width: 1000px; margin: 24px auto; padding: 0 16px 60px; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05); padding: 20px; margin-bottom: 20px; }
  form { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  input, select, textarea, button { padding: 9px 12px; font-size: 14px; border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; font-family: inherit; color: #1f2937; }
  button { background: #2563eb; color: #fff; border: none; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  button.ok { background: #16a34a; }
  button.ok:hover { background: #15803d; }
  button.no { background: #dc2626; }
  button.no:hover { background: #b91c1c; }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; }
  .stat-card { flex: 1 1 220px; border: 1px solid #eef2f7; border-radius: 10px; padding: 14px; background: #fbfdff; }
  .stat-card .name { font-weight: 600; }
  .stat-card .num { font-size: 20px; font-weight: 600; color: #2563eb; margin-top: 6px; }
  .stat-card .sub { color: #64748b; font-size: 13px; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px; }
  th, td { padding: 12px 10px; text-align: left; border: none; border-bottom: 1px solid #f1f3f5; vertical-align: top; }
  th { background: #f8f9fa; font-weight: 600; color: #334155; }
  tbody tr:nth-child(even) { background: #fafbfc; }
  tbody tr:hover { background: #eef5ff; }
  .empty { color: #94a3b8; padding: 12px 8px; }
  .tag { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .tag.pending { background: #fef3c7; color: #92400e; }
  .tag.approved { background: #dcfce7; color: #166534; }
  .tag.rejected { background: #fee2e2; color: #991b1b; }
  .tag.repairing { background: #fef3c7; color: #92400e; }
  .tag.fixed { background: #dcfce7; color: #166534; }
  .tag.b-pending { background: #ffedd5; color: #c2410c; }
  .tag.b-approved { background: #dcfce7; color: #166534; }
  .tag.b-completed { background: #d1fae5; color: #065f46; }
  .tag.b-rejected { background: #fee2e2; color: #991b1b; }
  .tag.confirm { background: #dbeafe; color: #1e40af; }
  .qa { border-bottom: 1px solid #e5e5e5; padding: 10px 0; font-size: 14px; }
  .qa .q { margin: 4px 0; }
  .qa .meta { color: #888; font-size: 13px; }
  .qa .answer-box { background: #fff7ed; border-left: 3px solid #ea580c; border-radius: 4px; padding: 8px 10px; margin-top: 6px; }
  details { border-bottom: 1px solid #e5e5e5; padding: 8px 0; font-size: 14px; }
  details summary { cursor: pointer; font-weight: 600; }
  details .ans { margin: 8px 0 0 10px; }
  details .ans .meta { color: #888; font-size: 12px; }
  .link-btn { background: none; border: none; color: #999; font-size: 12px; padding: 0 4px; cursor: pointer; font-family: inherit; }
  .link-btn:hover { color: #2563eb; background: none; }
  .link-btn.danger:hover { color: #dc2626; }
  .toggle-btn { background: none; border: none; color: #666; font-size: 13px; padding: 0 4px; cursor: pointer; text-decoration: underline; font-family: inherit; }
  .toggle-btn:hover { background: none; color: #2563eb; }
  .like-btn { background: none; border: none; padding: 0 2px; cursor: pointer; font-size: 14px; filter: grayscale(1); opacity: 0.55; transition: filter 0.2s, opacity 0.2s, transform 0.2s; }
  .like-btn:hover { background: none; filter: grayscale(0.4); opacity: 0.85; }
  .like-btn.liked { filter: grayscale(1) sepia(1) saturate(6) hue-rotate(190deg) brightness(0.95); opacity: 1; transform: scale(1.08); }
  .like-btn:disabled { cursor: default; }
  .like-count { color: #94a3b8; font-size: 12px; margin-left: 2px; }
  .like-count.liked { color: #2563eb; font-weight: 600; }
  .btn { display: inline-block; padding: 7px 12px; background: #16a34a; color: #fff; border-radius: 8px; font-size: 13px; text-decoration: none; }
  .btn:hover { background: #15803d; }
  td .actions { display: flex; gap: 6px; }
  .login { max-width: 320px; margin: 80px auto; }
  .login form { flex-direction: column; align-items: stretch; }
  .login input, .login select, .login button { width: 100%; box-sizing: border-box; }
  .error { color: #dc2626; font-size: 14px; }
  .alert { background: #fee2e2; color: #991b1b; border-radius: 6px; padding: 10px 12px; font-size: 14px; margin: 12px 0 0; }
  .alert.ok { background: #dcfce7; color: #166534; }
  .info { background: #eff6ff; color: #1d4ed8; border-radius: 6px; padding: 10px 12px; font-size: 14px; margin: 8px 0 0; }
  .answer { background: #f0f9ff; border-left: 3px solid #2563eb; border-radius: 4px; padding: 10px 12px; font-size: 14px; margin-top: 8px; }
  .answer .src { display: block; font-weight: 600; margin-bottom: 4px; color: #1d4ed8; }
  .answer.ai { background: #f5f3ff; border-left-color: #7c3aed; }
  .answer.ai .src { color: #6d28d9; }
  .answer.exp { background: #fff7ed; border-left-color: #ea580c; }
  .answer.exp .src { color: #c2410c; }
  .batch { display: flex; gap: 8px; margin-top: 8px; }
  .hint { color: #888; font-size: 13px; margin: 6px 0; }
  .qa-new { color: #9ca3af; font-size: 12px; font-weight: 400; margin-left: 4px; }
  td form { margin: 0; }
  td input[name="reply"] { width: 150px; }
  .review-form { flex-direction: column; align-items: stretch; gap: 6px; }
  .review-form input, .review-form select { width: 150px; }
"""

LOGIN_STYLE = """
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #e5e7eb;
    background: radial-gradient(circle at 18% 18%, #1e3a8a 0%, #0f172a 48%, #2e1065 100%);
    background-attachment: fixed;
  }
  body::before {
    content: "";
    position: fixed;
    inset: 0;
    background:
      radial-gradient(circle at 82% 78%, rgba(59, 130, 246, 0.28), transparent 42%),
      radial-gradient(circle at 12% 86%, rgba(168, 85, 247, 0.22), transparent 45%);
    pointer-events: none;
  }
  .login {
    position: relative;
    width: 100%;
    max-width: 360px;
    padding: 36px 32px 32px;
    text-align: center;
    border-radius: 22px;
    background: rgba(255, 255, 255, 0.08);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid rgba(255, 255, 255, 0.18);
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5), inset 0 0 0 1px rgba(255, 255, 255, 0.04);
  }
  .login img { display: block; margin: 0 auto 15px; }
  .login .title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 1px;
    color: #e0f2fe;
    text-shadow: 0 0 8px rgba(56, 189, 248, 0.85), 0 0 22px rgba(99, 102, 241, 0.65);
  }
  .login .subtitle {
    margin: 10px 0 24px;
    font-size: 13px;
    letter-spacing: 0.5px;
    color: #a5b4fc;
  }
  .login form { display: flex; flex-direction: column; gap: 14px; }
  .login input, .login select {
    width: 100%;
    padding: 10px 4px;
    font-size: 14px;
    font-family: inherit;
    color: #f8fafc;
    background: transparent;
    border: none;
    border-bottom: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 0;
    outline: none;
    transition: border-color 0.25s, box-shadow 0.25s;
  }
  .login input::placeholder { color: rgba(226, 232, 240, 0.45); }
  .login select option { color: #0f172a; }
  .login input:focus, .login select:focus {
    border-bottom-color: #38bdf8;
    box-shadow: 0 10px 14px -10px rgba(56, 189, 248, 0.85);
  }
  .login button {
    margin-top: 10px;
    padding: 11px;
    border: none;
    border-radius: 12px;
    font-size: 15px;
    font-weight: 600;
    font-family: inherit;
    color: #fff;
    cursor: pointer;
    background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
    box-shadow: 0 8px 20px rgba(59, 130, 246, 0.35);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }
  .login button:hover {
    transform: translateY(-2px);
    box-shadow: 0 12px 28px rgba(139, 92, 246, 0.55), 0 0 18px rgba(56, 189, 248, 0.5);
  }
  .login button:active { transform: translateY(0); }
  .error { margin: 0 0 12px; color: #fca5a5; font-size: 13px; }
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 - 3D 打印机预约系统</title>
<style>{LOGIN_STYLE}</style>
</head>
<body>
  <div class="login">
    <img src="/static/logo.png" alt="电子科技大学校徽" style="width: 100px; height: 100px; border-radius: 50%; object-fit: cover; transform: scale(1.15); box-shadow: 0 0 20px rgba(0, 150, 255, 0.8); margin-bottom: 20px;">
    <div class="title">3D 打印机预约系统</div>
    <div class="subtitle">电子科技大学 (UESTC) 英才科协</div>
    {ERROR}
    <form method="post" action="/login">
      <select name="role" required>
        <option value="student">学生登录</option>
        <option value="admin">管理员登录</option>
      </select>
      <input name="account" placeholder="账号" maxlength="50" required autofocus>
      <input name="password" type="password" placeholder="密码" maxlength="50" required>
      <button type="submit">登录</button>
    </form>
  </div>
</body>
</html>"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>3D 打印机预约</title>
<style>{STYLE}</style>
</head>
<body>
  <nav class="navbar">
    <div class="nav-brand">3D 打印机预约系统</div>
    <div class="nav-right">
      {PROFILE}
      <a class="nav-link" href="/logout">退出登录</a>
      <img class="nav-logo" src="/static/logo.png" alt="校徽">
      <span class="nav-org">电子科技大学英才科协</span>
    </div>
  </nav>
  <main class="container">
  {TIP}
  {CONTENT}
  </main>
  <script>
  (function () {
    var SCROLL_KEY = "scrollPos";
    document.addEventListener("submit", function () {
      try {
        sessionStorage.setItem(SCROLL_KEY, String(window.scrollY || window.pageYOffset || 0));
      } catch (e) {}
    }, true);
    window.addEventListener("DOMContentLoaded", function () {
      try {
        var saved = sessionStorage.getItem(SCROLL_KEY);
        if (saved !== null) {
          sessionStorage.removeItem(SCROLL_KEY);
          window.scrollTo(0, parseInt(saved, 10) || 0);
        }
      } catch (e) {}
    });
  })();
  </script>
</body>
</html>"""


# ---------- 会话 ----------


def sign(account):
    return hmac.new(SECRET_KEY.encode(), account.encode(), hashlib.sha256).hexdigest()


def get_session(request):
    token = request.cookies.get(COOKIE_NAME, "")
    if "." not in token:
        return None
    account, signature = token.rsplit(".", 1)
    if account not in USERS or not hmac.compare_digest(signature, sign(account)):
        return None
    user = USERS[account]
    return {
        "account": account,
        "role": user["role"],
        "name": user["name"],
        "class_name": user["class_name"],
    }


def login_page(error=""):
    err = f'<p class="error">{escape(error)}</p>' if error else ""
    return LOGIN_PAGE.replace("{LOGIN_STYLE}", LOGIN_STYLE).replace("{ERROR}", err)


def render_page(session, content, tip=""):
    if session["role"] == "admin":
        profile = "欢迎你，<b>管理员</b>"
    else:
        profile = (
            f'欢迎你，<b>{escape(session["name"])}</b>'
            f'（{escape(session["class_name"])}）'
        )
    return (
        PAGE.replace("{STYLE}", STYLE)
        .replace("{PROFILE}", profile)
        .replace("{TIP}", tip)
        .replace("{CONTENT}", content)
    )


# ---------- 小工具 ----------


def fmt_grams(value):
    return "%g" % round(float(value), 2)


def fmt_num(value):
    return "%g" % round(float(value), 1)


def booking_status_tag(status):
    css = {
        STATUS_PENDING: "b-pending",
        STATUS_APPROVED: "b-approved",
        STATUS_COMPLETED: "b-completed",
        STATUS_REJECTED: "b-rejected",
    }
    return f'<span class="tag {css.get(status, "b-pending")}">{escape(status)}</span>'


def maintenance_status_tag(status):
    css = "repairing" if status == FIX_REPAIRING else "fixed"
    return f'<span class="tag {css}">{escape(status)}</span>'


def ticket_status_tag(status):
    css = {
        TICKET_PENDING: "pending",
        TICKET_CONFIRM: "confirm",
        TICKET_DONE: "approved",
    }.get(status, "pending")
    return f'<span class="tag {css}">{escape(status)}</span>'


def experience_status_tag(status):
    css = {EXP_PENDING: "pending", EXP_APPROVED: "approved", EXP_REJECTED: "rejected"}
    return f'<span class="tag {css.get(status, "pending")}">{escape(status)}</span>'


# ---------- 打印机独立耗材 ----------


def get_printer_remaining(conn, printer):
    row = conn.execute(
        "SELECT remaining FROM printer_stock WHERE printer = ?", (printer,)
    ).fetchone()
    return row["remaining"] if row else 0.0


def get_printer_used(conn, printer):
    row = conn.execute(
        "SELECT COALESCE(SUM(consumable), 0) AS used FROM bookings "
        "WHERE status IN (?, ?) AND printer = ?",
        (STATUS_APPROVED, STATUS_COMPLETED, printer),
    ).fetchone()
    return row["used"] if row else 0.0


def get_printer_recent_used(conn, printer, days=7):
    """近 days 天（含今天）该打印机已通过 / 已完成预约的耗材合计。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(consumable), 0) AS used FROM bookings "
        "WHERE status IN (?, ?) AND printer = ? "
        "AND created_at >= datetime('now', 'localtime', ?)",
        (STATUS_APPROVED, STATUS_COMPLETED, printer, f"-{days} days"),
    ).fetchone()
    return row["used"] if row else 0.0


def deduct_printer_stock(conn, printer, grams):
    remaining = max(0.0, get_printer_remaining(conn, printer) - grams)
    conn.execute(
        "UPDATE printer_stock SET remaining = ? WHERE printer = ?", (round(remaining, 2), printer)
    )


def printer_states(conn):
    """返回 {设备: 故障描述}，只统计仍在维修中的记录。"""
    rows = conn.execute(
        "SELECT device, issue FROM maintenance WHERE status = ? ORDER BY id DESC",
        (FIX_REPAIRING,),
    ).fetchall()
    broken = {}
    for row in rows:
        broken.setdefault(row["device"], row["issue"])
    return broken


def printer_label(printer):
    address = PRINTERS.get(printer, "")
    return f"{printer}（{address}）" if address else printer


def printer_options(broken):
    options = ""
    for printer in PRINTERS:
        label = escape(printer_label(printer))
        if printer in broken:
            options += f'<option value="{escape(printer)}" disabled>{label}（维修中）</option>'
        else:
            options += f'<option value="{escape(printer)}">{label}</option>'
    return options


def owner_class(owner):
    user = USERS.get(owner)
    return user["class_name"] if user and user["class_name"] else "—"


def owner_name(owner):
    user = USERS.get(owner)
    return user["name"] if user else owner


def msg_block(msg, ok=False):
    if not msg:
        return ""
    css = "alert ok" if ok else "alert"
    return f'<p class="{css}">{escape(msg)}</p>'


# ---------- 智能耗材预测 ----------


def smart_tip(conn):
    """任意一台打印机剩余低于 300 克时，按该机近 7 天日均消耗预测耗尽天数。"""
    tips = ""
    for printer in PRINTERS:
        remaining = get_printer_remaining(conn, printer)
        if remaining >= 300:
            continue
        daily = get_printer_recent_used(conn, printer, 7) / 7.0
        if daily > 0:
            days = fmt_num(remaining / daily)
            detail = f"按近 7 天消耗速度，预计 {days} 天后耗尽"
        else:
            detail = "近 7 天暂无消耗记录"
        tips += (
            f'<p class="alert">⚠️ 智能预测：{printer_label(printer)}仅剩 '
            f"{fmt_grams(remaining)} 克，{detail}，请及时补充！</p>"
        )
    return tips


# ---------- 空闲时段推荐 ----------


def suggest_slot(conn):
    """统计明天各打印机的预约，返回最空闲的 1 小时时段。"""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT booking_time, printer FROM bookings WHERE status IN (?, ?)",
        (STATUS_PENDING, STATUS_APPROVED),
    ).fetchall()

    counts = {hour: 0 for hour in range(8, 20)}
    for row in rows:
        slot = slot_range(row["booking_time"])
        if slot is None or row["printer"] not in PRINTERS:
            continue
        day, start, end = slot
        if day != tomorrow:
            continue
        for hour in counts:
            if start < (hour + 1) * 60 and hour * 60 < end:
                counts[hour] += 1

    best = min(counts, key=lambda hour: (counts[hour], hour))
    return tomorrow, best, counts[best]


def slot_range(text):
    """把 '2026-11-13 08:00-10:00' 解析为 (日期, 开始分钟, 结束分钟)。"""
    if not text or " " not in text:
        return None
    day, times = text.split(" ", 1)
    if "-" in times:
        start, end = times.split("-", 1)
    else:
        start, end = times, ""

    def to_minutes(value):
        parts = value.strip().split(":")
        if len(parts) < 2:
            return None
        try:
            return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None

    start_min = to_minutes(start)
    if start_min is None:
        return None
    end_min = to_minutes(end) if end else start_min + 60
    if end_min is None:
        return None
    return day, start_min, end_min


def ranges_overlap(a, b):
    return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]


def has_time_conflict(conn, printer, slot_text, statuses, exclude_id=None):
    """同一打印机、同一天，时间段存在交集即视为冲突。"""
    target = slot_range(slot_text)
    if target is None:
        return False
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT id, booking_time FROM bookings WHERE printer = ? AND status IN ({marks})",
        (printer, *statuses),
    ).fetchall()
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        current = slot_range(row["booking_time"])
        if current is not None and ranges_overlap(target, current):
            return True
    return False


# ---------- AI 助手：多方方案合并 ----------


def find_key(conn, question):
    """在系统方案、知识库、已通过经验中找匹配关键字。"""
    candidates = list(SOLUTIONS.keys())
    for row in conn.execute("SELECT keyword FROM knowledge"):
        if row["keyword"]:
            candidates.append(row["keyword"])
    for row in conn.execute(
        "SELECT problem FROM student_experiences WHERE status = ?", (EXP_APPROVED,)
    ):
        if row["problem"]:
            candidates.append(row["problem"])

    for key in candidates:
        if key and key in question:
            return key
    return None


def matched_solutions(conn, key, question):
    """返回 [(来源, 内容)]，包含系统建议、管理员回复、同学经验。"""
    results = []

    if key in SOLUTIONS:
        results.append(("【系统建议】", SOLUTIONS[key]))

    for row in conn.execute("SELECT keyword, reply FROM knowledge ORDER BY id DESC"):
        keyword = row["keyword"]
        if keyword and (
            keyword == key or keyword in question or (key and key in keyword)
        ):
            results.append(("【管理员回复】", row["reply"]))

    for row in conn.execute(
        "SELECT problem, solution FROM student_experiences WHERE status = ? ORDER BY id DESC",
        (EXP_APPROVED,),
    ):
        problem = row["problem"]
        if problem and (
            problem == key or problem in question or (key and key in problem)
        ):
            results.append(("【同学经验分享】", row["solution"]))

    return results


def answer_card(source, text):
    css = "answer"
    if source == "【管理员回复】":
        css = "answer ai"
    elif source == "【同学经验分享】":
        css = "answer exp"
    return f'<div class="{css}"><span class="src">{escape(source)}</span>{escape(text)}</div>'


def report_form(keyword, question):
    return f"""<form method="post" action="/ticket">
    <input type="hidden" name="keyword" value="{escape(keyword)}">
    <input type="hidden" name="detail" id="ticket_detail">
    <button class="no" type="button" onclick="var d = prompt('请详细描述您的故障现象或需要的帮助。'); if (d && d.trim()) {{ document.getElementById('ticket_detail').value = d; this.form.submit(); }}">未解决，上报管理员</button>
  </form>"""


# ---------- 同学互助问答板 ----------


def qa_board_html(conn, qa_keyword="", is_admin=False, viewer="", voter=""):
    if qa_keyword:
        questions = conn.execute(
            """
            SELECT id, asker_name, question_content, is_pinned, created_at
            FROM questions WHERE question_content LIKE ?
            ORDER BY is_pinned DESC, id DESC
            """,
            ("%" + qa_keyword + "%",),
        ).fetchall()
    else:
        questions = conn.execute(
            """
            SELECT id, asker_name, question_content, is_pinned, created_at
            FROM questions ORDER BY is_pinned DESC, id DESC
            """
        ).fetchall()

    if not questions:
        if qa_keyword:
            items = '<p class="empty">没有找到包含该关键词的问题。</p>'
        else:
            items = '<p class="empty">暂无提问，快来问第一个问题吧。</p>'
    else:
        items = ""
        liked_ids = {
            row["answer_id"]
            for row in conn.execute(
                "SELECT answer_id FROM answer_likes WHERE user_id = ?", (voter,)
            )
        }
        for question in questions:
            question_id = question["id"]
            asker = escape(question["asker_name"])
            content = escape(question["question_content"])
            pin_tag = '<span class="tag pending">置顶</span> ' if question["is_pinned"] else ""
            answers = conn.execute(
                """
                SELECT id, answerer_name, answer_content, likes, is_pinned, created_at
                FROM answers WHERE question_id = ?
                ORDER BY is_pinned DESC, likes DESC, created_at ASC, id ASC
                """,
                (question_id,),
            ).fetchall()

            new_hint = ""
            if (
                viewer
                and question["asker_name"] == viewer
                and any(a["answerer_name"] != viewer for a in answers)
            ):
                new_hint = '<span class="qa-new">（有同学回答了您）</span>'

            items += (
                "<details>"
                f"<summary>{pin_tag}{asker}提问：{content}（{len(answers)}个回答）{new_hint}</summary>"
            )

            if answers:
                for answer in answers:
                    answerer = escape(answer["answerer_name"])
                    text = escape(answer["answer_content"])
                    created = escape(answer["created_at"])
                    answer_id = answer["id"]
                    likes = answer["likes"]
                    liked = answer_id in liked_ids
                    like_cls = "like-btn liked" if liked else "like-btn"
                    like_title = ' title="取消点赞"' if liked else ' title="点赞"'
                    count_cls = "like-count liked" if liked else "like-count"
                    pin_mark = (
                        '<span class="tag pending">置顶回答</span> '
                        if answer["is_pinned"]
                        else ""
                    )
                    items += (
                        '<div class="ans">'
                        f"{pin_mark}<b>{answerer}</b>：{text} "
                        f'<span class="meta">{created}</span> '
                        f'<button type="button" class="{like_cls}" data-id="{answer_id}"{like_title} onclick="likeAnswer(this)">👍</button>'
                        f'<span class="{count_cls}" id="like-count-{answer_id}">{likes}</span>'
                    )
                    if is_admin:
                        pin_label = "取消置顶" if answer["is_pinned"] else "置顶"
                        items += (
                            '<form method="post" action="/qa_pin_answer" style="display:inline">'
                            f'<input type="hidden" name="id" value="{answer_id}">'
                            f'<button class="link-btn" type="submit">[{pin_label}]</button>'
                            "</form>"
                            '<form method="post" action="/qa_delete_answer" style="display:inline">'
                            f'<input type="hidden" name="id" value="{answer_id}">'
                            '<button class="link-btn danger" type="submit">[删除]</button>'
                            "</form>"
                        )
                    items += "</div>"
            else:
                items += '<p class="empty">暂无回答，快来抢答。</p>'

            items += (
                '<form method="post" action="/qa_answer">'
                f'<input type="hidden" name="id" value="{question_id}">'
                '<input name="answer" placeholder="我来回答…" maxlength="500" required>'
                '<label class="hint"><input type="checkbox" name="anonymous"> 匿名回答</label>'
                '<button type="submit">提交回答</button>'
                "</form>"
            )

            if is_admin:
                pin_label = "取消置顶" if question["is_pinned"] else "置顶"
                items += (
                    '<div class="actions">'
                    '<form method="post" action="/qa_pin" style="display:inline">'
                    f'<input type="hidden" name="id" value="{question_id}">'
                    f'<button class="link-btn" type="submit">[{pin_label}]</button>'
                    "</form>"
                    '<form method="post" action="/qa_delete" style="display:inline">'
                    f'<input type="hidden" name="id" value="{question_id}">'
                    '<button class="link-btn danger" type="submit">[删除]</button>'
                    "</form>"
                    "</div>"
                )

            items += "</details>"

    ask_form = ""
    if not is_admin:
        ask_form = """
  <form method="post" action="/qa_ask">
    <input name="question" placeholder="向同学提问，例如：PEI 板用什么清洁？" maxlength="500" required>
    <label class="hint"><input type="checkbox" name="anonymous"> 匿名提问</label>
    <button type="submit">向同学提问</button>
  </form>"""

    toggle_js = (
        "var ds = document.querySelectorAll('details'); var openAll = true; "
        "for (var i = 0; i < ds.length; i++) { if (!ds[i].open) { openAll = false; break; } } "
        "for (var i = 0; i < ds.length; i++) { ds[i].open = !openAll; } "
        "this.textContent = openAll ? '展开全部问题' : '折叠全部问题';"
    )
    clear_link = '<a href="/">清空搜索</a>' if qa_keyword else ""

    search_form = f"""
  <form method="get" action="/">
    <input name="qa_keyword" value="{escape(qa_keyword)}" placeholder="搜索问题关键词" maxlength="100">
    <button type="submit">搜索</button>
    <button type="button" class="toggle-btn" onclick="{toggle_js}">折叠全部问题</button>
    {clear_link}
  </form>"""

    title = "问答社区管理" if is_admin else "同学互助问答"

    like_js = (
        "<script>"
        "function likeAnswer(btn){"
        "var id=btn.getAttribute('data-id');"
        "if(btn.disabled){return;}"
        "btn.disabled=true;"
        "fetch('/qa_like',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
        "body:'id='+encodeURIComponent(id)})"
        ".then(function(r){return r.json();})"
        ".then(function(data){"
        "var liked=data.status==='liked';"
        "btn.classList.toggle('liked',liked);btn.title=liked?'取消点赞':'点赞';"
        "var s=document.getElementById('like-count-'+id);"
        "if(s){s.textContent=data.new_likes;s.classList.toggle('liked',liked);}"
        "btn.disabled=false;})"
        ".catch(function(){btn.disabled=false;});"
        "}"
        "</script>"
    )

    return f"""
  <div class="card">
  <h2>{title}</h2>
{search_form}
{ask_form}
  {items}
  {like_js}
  </div>
"""


# ---------- 学生页面 ----------


def student_assistant(conn, session, reply_html=""):
    options = "".join(f'<option value="{key}">{key}</option>' for key in SOLUTIONS)
    tickets = conn.execute(
        """
        SELECT id, question_keyword, detail, status, admin_reply, created_at
        FROM tickets WHERE student_id = ? ORDER BY id DESC
        """,
        (session["account"],),
    ).fetchall()
    experiences = conn.execute(
        """
        SELECT problem, solution, status, feedback, created_at
        FROM student_experiences WHERE student_id = ? ORDER BY id DESC
        """,
        (session["account"],),
    ).fetchall()

    if tickets:
        body = ""
        for row in tickets:
            if row["status"] == TICKET_CONFIRM:
                ticket_id = row["id"]
                action = (
                    '<td><div class="actions">'
                    '<form method="post" action="/ticket_feedback">'
                    f'<input type="hidden" name="id" value="{ticket_id}">'
                    '<input type="hidden" name="action" value="resolved">'
                    '<button class="ok" type="submit">✅ 问题已解决</button>'
                    "</form>"
                    f'<button class="no" type="button" onclick="unresolveTicket({ticket_id})">❌ 问题未解决</button>'
                    "</div></td>"
                )
            else:
                action = '<td class="empty">—</td>'
            body += (
                "<tr>"
                f"<td>{escape(row['question_keyword'])}</td>"
                f"<td>{escape(row['detail']) or '—'}</td>"
                f"<td>{ticket_status_tag(row['status'])}</td>"
                f"<td>{escape(row['admin_reply']) or '等待管理员回复'}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                f"{action}"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="6">暂无提问记录</td></tr>'

    if experiences:
        exp_body = ""
        for row in experiences:
            exp_body += (
                "<tr>"
                f"<td>{escape(row['problem'])}</td>"
                f"<td>{escape(row['solution'])}</td>"
                f"<td>{experience_status_tag(row['status'])}</td>"
                f"<td>{escape(row['feedback']) or '—'}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                "</tr>"
            )
    else:
        exp_body = '<tr><td class="empty" colspan="5">暂无分享记录</td></tr>'

    return f"""
  <div class="card">
  <h2>故障排查小助手</h2>
  <p class="hint">选择故障类型或输入问题关键字，系统会同时展示系统建议、管理员回复和同学经验。</p>
  <form method="post" action="/assistant">
    <select name="keyword">
      <option value="">故障类型（可选）</option>
      {options}
    </select>
    <input name="detail" placeholder="或输入问题关键字，例如：模型翘边" maxlength="200">
    <button type="submit">提问</button>
  </form>
  {reply_html}
  </div>

  <div class="card">
  <h2>我的问题记录</h2>
  <table>
    <thead>
      <tr><th>问题关键字</th><th>详细描述</th><th>状态</th><th>管理员回复</th><th>提交时间</th><th>操作</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>

  <div class="card">
  <h2>分享修复经验</h2>
  <form method="post" action="/experience">
    <input name="problem" placeholder="故障问题（或关键词）" maxlength="100" required>
    <input name="solution" placeholder="我的解决办法" maxlength="200" required>
    <button type="submit">提交分享</button>
  </form>
  </div>

  <div class="card">
  <h2>我的分享记录</h2>
  <table>
    <thead>
      <tr><th>故障问题</th><th>解决办法</th><th>审核状态</th><th>管理员反馈</th><th>提交时间</th></tr>
    </thead>
    <tbody>
      {exp_body}
    </tbody>
  </table>
  <script>
function unresolveTicket(id) {{
  var d = prompt('请描述一下还有哪里没有解决？（必须填写）');
  if (!d || !d.trim()) {{ alert('必须填写补充说明才能提交哦'); return; }}
  var f = document.createElement('form');
  f.method = 'post';
  f.action = '/ticket_feedback';
  var add = function (n, v) {{ var i = document.createElement('input'); i.type = 'hidden'; i.name = n; i.value = v; f.appendChild(i); }};
  add('id', id);
  add('action', 'unresolved');
  add('follow_up', d.trim());
  document.body.appendChild(f);
  f.submit();
}}
  </script>
  </div>
"""


def student_content(session, msg="", ok=False, suggest=False, reply_html="", qa_keyword=""):
    conn = connect()
    broken = printer_states(conn)
    rows = conn.execute(
        """
        SELECT id, booking_time, printer, consumable, status, duration, note, created_at
        FROM bookings WHERE owner = ? ORDER BY id DESC
        """,
        (session["account"],),
    ).fetchall()
    stocks = [(p, get_printer_remaining(conn, p)) for p in PRINTERS]

    booking_date = ""
    start_value = ""
    end_value = ""
    suggestion = ""
    if suggest:
        day, hour, count = suggest_slot(conn)
        booking_date = day
        start_value = f"{hour:02d}:00"
        end_value = f"{hour + 1:02d}:00"
        part = "下午" if hour >= 12 else "上午"
        suggestion = (
            f'<p class="info">建议您预约明天{part} {hour:02d}:00-{hour + 1:02d}:00，'
            f"此时段最空闲（已有 {count} 条约预约），已自动填入时间。</p>"
        )

    repairs = conn.execute(
        "SELECT device, issue, status, created_at FROM maintenance ORDER BY id DESC"
    ).fetchall()
    assistant = student_assistant(conn, session, reply_html)
    qa = qa_board_html(
        conn, qa_keyword, viewer=session["name"], voter=session["account"]
    )
    conn.close()

    stock_text = " ｜ ".join(f"{printer_label(p)} 剩余 {fmt_grams(g)} 克" for p, g in stocks)

    content = f"""
{msg_block(msg, ok)}
  <div class="card">
  <h2>提交预约</h2>
  <p class="hint">各打印机耗材：{stock_text}</p>
  <form method="post" action="/book">
    <input name="booking_date" type="date" value="{booking_date}" min="2026-01-01" max="2099-12-31" required>
    <input name="start_time" type="time" value="{start_value}" required>
    <span class="hint">至</span>
    <input name="end_time" type="time" value="{end_value}" required>
    <select name="printer" required>
      {printer_options(broken)}
    </select>
    <input name="consumable" type="number" step="0.1" min="0" placeholder="预计使用耗材(克)" required>
    <button type="submit">提交预约</button>
  </form>
  <form method="get" action="/">
    <input type="hidden" name="suggest" value="1">
    <button type="submit">🤖 帮我找空闲时段</button>
  </form>
  {suggestion}
  </div>
"""

    if rows:
        body = ""
        for row in rows:
            action = "—"
            if row["status"] == STATUS_APPROVED:
                booking_id = row["id"]
                action = f'<a class="btn" href="/complete?id={booking_id}">登记任务完成</a>'
            body += (
                "<tr>"
                f"<td>{escape(row['booking_time'].replace('T', ' '))}</td>"
                f"<td>{escape(row['printer'])}</td>"
                f"<td>{fmt_grams(row['consumable'])}</td>"
                f"<td>{booking_status_tag(row['status'])}</td>"
                f"<td>{escape(row['duration']) or '—'}</td>"
                f"<td>{escape(row['note']) or '—'}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                f"<td>{action}</td>"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="8">暂无预约记录</td></tr>'

    content += f"""
  <div class="card">
  <h2>我的预约</h2>
  <table>
    <thead>
      <tr><th>预约时间</th><th>打印机</th><th>耗材(克)</th><th>状态</th><th>实际耗时</th><th>成果备注</th><th>创建时间</th><th>操作</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>

  <div class="card">
  <h2>设备维修状态</h2>
"""

    for printer in PRINTERS:
        if printer in broken:
            content += (
                f'<p class="error">{escape(printer)}：设备维修中，暂不可用'
                f"（{escape(broken[printer])}）</p>"
            )

    if repairs:
        body = ""
        for row in repairs:
            body += (
                "<tr>"
                f"<td>{escape(row['device'])}</td>"
                f"<td>{escape(row['issue'])}</td>"
                f"<td>{maintenance_status_tag(row['status'])}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="4">暂无维修记录，所有设备正常</td></tr>'

    content += f"""
  <table>
    <thead>
      <tr><th>设备名</th><th>故障描述</th><th>维修状态</th><th>记录时间</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>
"""
    content += assistant
    content += qa
    return content


# ---------- 管理员页面 ----------


def admin_content(session, msg="", ok=False, qa_keyword=""):
    conn = connect()
    rows = conn.execute(
        """
        SELECT id, name, booking_time, printer, consumable, status, owner, duration, note, created_at
        FROM bookings ORDER BY id DESC
        """
    ).fetchall()
    repairs = conn.execute(
        "SELECT id, device, issue, status, created_at FROM maintenance ORDER BY id DESC"
    ).fetchall()
    tickets = conn.execute(
        """
        SELECT id, student_id, question_keyword, detail, status, follow_up, created_at
        FROM tickets WHERE status IN (?, ?) ORDER BY id DESC
        """,
        (TICKET_PENDING, TICKET_CONFIRM),
    ).fetchall()
    experiences = conn.execute(
        """
        SELECT id, student_id, problem, solution, created_at
        FROM student_experiences WHERE status = ? ORDER BY id DESC
        """,
        (EXP_PENDING,),
    ).fetchall()
    stocks = [(p, get_printer_remaining(conn, p), get_printer_used(conn, p)) for p in PRINTERS]
    qa = qa_board_html(
        conn, qa_keyword, is_admin=True, voter=session["account"]
    )
    conn.close()
    device_options = "".join(f'<option value="{p}">{p}</option>' for p in PRINTERS)
    pending = sum(1 for row in rows if row["status"] == STATUS_PENDING)

    batch = ""
    if pending:
        batch = f"""
  <div class="batch">
    <form method="post" action="/review_all">
      <input type="hidden" name="action" value="approve">
      <button class="ok" type="submit">一键全部通过</button>
    </form>
    <form method="post" action="/review_all">
      <input type="hidden" name="action" value="reject">
      <button class="no" type="submit">一键全部拒绝</button>
    </form>
  </div>
"""

    alerts = ""
    open_tickets = sum(1 for row in tickets if row["status"] == TICKET_PENDING)
    if open_tickets:
        alerts += f'<p class="alert">⚠️ 您有 {open_tickets} 个待解决的故障问题。</p>'
    if experiences:
        alerts += f'<p class="alert">⚠️ 您有 {len(experiences)} 条待审核的学生经验。</p>'

    stock_cards = ""
    for printer, remaining, used in stocks:
        stock_cards += f"""
    <div class="stat-card">
      <div class="name">{escape(printer)}</div>
      <div class="sub">地址：{escape(PRINTERS.get(printer, "—"))}</div>
      <div class="num">剩余 {fmt_grams(remaining)} 克</div>
      <div class="sub">已用 {fmt_grams(used)} 克</div>
    </div>
"""

    content = f"""
{alerts}
{msg_block(msg, ok)}
  <div class="card">
  <h2>打印机耗材统计</h2>
  <div class="stats">
{stock_cards}
  </div>
  </div>

  <div class="card">
  <h2>全部预约记录（待审批 {pending} 条）</h2>
{batch}"""

    if rows:
        body = ""
        for row in rows:
            if row["status"] == STATUS_PENDING:
                actions = (
                    '<td><div class="actions">'
                    '<form method="post" action="/review">'
                    f'<input type="hidden" name="id" value="{row["id"]}">'
                    '<input type="hidden" name="action" value="approve">'
                    '<button class="ok" type="submit">通过</button>'
                    "</form>"
                    '<form method="post" action="/review">'
                    f'<input type="hidden" name="id" value="{row["id"]}">'
                    '<input type="hidden" name="action" value="reject">'
                    '<button class="no" type="submit">拒绝</button>'
                    "</form>"
                    "</div></td>"
                )
            else:
                actions = '<td class="empty">已处理</td>'

            body += (
                "<tr>"
                f"<td>{escape(row['name'])}</td>"
                f"<td>{escape(owner_class(row['owner']))}</td>"
                f"<td>{escape(row['booking_time'].replace('T', ' '))}</td>"
                f"<td>{escape(row['printer'])}</td>"
                f"<td>{fmt_grams(row['consumable'])}</td>"
                f"<td>{booking_status_tag(row['status'])}</td>"
                f"<td>{escape(row['duration']) or '—'}</td>"
                f"<td>{escape(row['note']) or '—'}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                f"{actions}"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="10">暂无预约记录</td></tr>'

    content += f"""
  <table>
    <thead>
      <tr><th>姓名</th><th>班级</th><th>预约时间</th><th>打印机</th><th>耗材(克)</th><th>状态</th><th>实际耗时</th><th>成果备注</th><th>创建时间</th><th>操作</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>

  <div class="card">
  <h2>设备维修</h2>
  <form method="post" action="/maintenance">
    <select name="device" required>
      <option value="">请选择设备</option>
      {device_options}
    </select>
    <input name="issue" placeholder="故障描述" maxlength="200" required>
    <button type="submit">提交维修记录</button>
  </form>
"""

    if repairs:
        body = ""
        for row in repairs:
            if row["status"] == FIX_REPAIRING:
                action = (
                    '<td><form method="post" action="/repair">'
                    f'<input type="hidden" name="id" value="{row["id"]}">'
                    '<button class="ok" type="submit">标记为已维修</button>'
                    "</form></td>"
                )
            else:
                action = '<td class="empty">—</td>'

            body += (
                "<tr>"
                f"<td>{escape(row['device'])}</td>"
                f"<td>{escape(row['issue'])}</td>"
                f"<td>{maintenance_status_tag(row['status'])}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                f"{action}"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="5">暂无维修记录</td></tr>'

    content += f"""
  <table>
    <thead>
      <tr><th>设备名</th><th>故障描述</th><th>维修状态</th><th>记录时间</th><th>操作</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>

  <div class="card">
  <h2>待处理问题（{len(tickets)} 条）</h2>
"""

    if tickets:
        body = ""
        for row in tickets:
            if row["status"] == TICKET_PENDING:
                reply_cell = (
                    "<td>"
                    '<form method="post" action="/ticket_reply">'
                    f'<input type="hidden" name="id" value="{row["id"]}">'
                    '<input name="reply" placeholder="回复内容" maxlength="200" required>'
                    '<button type="submit">回复</button>'
                    "</form>"
                    "</td>"
                )
            else:
                reply_cell = '<td class="empty">等待学生确认</td>'
            detail_text = escape(row["detail"]) or "—"
            if row["follow_up"]:
                detail_text += (
                    f'<div class="hint">学生补充：{escape(row["follow_up"])}</div>'
                )
            body += (
                "<tr>"
                f"<td>{escape(owner_name(row['student_id']))}</td>"
                f"<td>{escape(row['question_keyword'])}</td>"
                f"<td>{detail_text}</td>"
                f"<td>{ticket_status_tag(row['status'])}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                f"{reply_cell}"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="6">暂无待处理问题</td></tr>'

    content += f"""
  <table>
    <thead>
      <tr><th>学生</th><th>问题关键字</th><th>详细描述</th><th>状态</th><th>提交时间</th><th>回复</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>

  <div class="card">
  <h2>待审核的学生经验（{len(experiences)} 条）</h2>
"""

    if experiences:
        body = ""
        for row in experiences:
            body += (
                "<tr>"
                f"<td>{escape(owner_name(row['student_id']))}</td>"
                f"<td>{escape(row['problem'])}</td>"
                f"<td>{escape(row['solution'])}</td>"
                f"<td>{escape(row['created_at'])}</td>"
                "<td>"
                '<form method="post" action="/experience_review">'
                f'<input type="hidden" name="id" value="{row["id"]}">'
                '<input type="hidden" name="action" value="approve">'
                '<button class="ok" type="submit">通过</button>'
                "</form>"
                '<form class="review-form" method="post" action="/experience_review">'
                f'<input type="hidden" name="id" value="{row["id"]}">'
                '<input type="hidden" name="action" value="reject">'
                '<select name="reason">'
                '<option value="反馈错误">反馈错误</option>'
                '<option value="信息不匹配">信息不匹配</option>'
                '<option value="其他">其他（自定义）</option>'
                "</select>"
                '<input name="custom" placeholder="自定义原因" maxlength="50">'
                '<button class="no" type="submit">拒绝</button>'
                "</form>"
                "</td>"
                "</tr>"
            )
    else:
        body = '<tr><td class="empty" colspan="5">暂无待审核经验</td></tr>'

    content += f"""
  <table>
    <thead>
      <tr><th>学生</th><th>故障问题</th><th>解决办法</th><th>提交时间</th><th>审核</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
  </div>
"""
    content += qa
    return content


# ---------- 路由 ----------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = get_session(request)
    if session is None:
        return HTMLResponse(login_page())

    params = request.query_params
    msg = params.get("msg", "")
    ok = params.get("ok") == "1"
    suggest = params.get("suggest") == "1"
    qa_keyword = params.get("qa_keyword", "").strip()

    conn = connect()
    tip = smart_tip(conn)
    conn.close()

    if session["role"] == "student":
        content = student_content(
            session, msg=msg, ok=ok, suggest=suggest, qa_keyword=qa_keyword
        )
    else:
        content = admin_content(
            session, msg=msg, ok=ok, qa_keyword=qa_keyword
        )

    return HTMLResponse(render_page(session, content, tip))


@app.post("/login")
async def login(request: Request):
    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    role = data.get("role", ["student"])[0].strip()
    account = data.get("account", [""])[0].strip()
    password = data.get("password", [""])[0]

    user = USERS.get(account)
    if (
        user is None
        or user["role"] != role
        or not hmac.compare_digest(user["password"], password)
    ):
        return HTMLResponse(login_page("账号、密码或登录身份不正确"))

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=f"{account}.{sign(account)}",
        httponly=True,
        max_age=12 * 3600,
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/book")
async def book(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    # 手工解析表单，避免额外依赖 python-multipart
    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)

    booking_date = data.get("booking_date", [""])[0].strip()
    start_time = data.get("start_time", [""])[0].strip()
    end_time = data.get("end_time", [""])[0].strip()
    printer = data.get("printer", [""])[0].strip()
    consumable_raw = data.get("consumable", ["0"])[0].strip()

    try:
        consumable = max(0.0, float(consumable_raw or 0))
    except ValueError:
        consumable = 0.0

    if not booking_date or not start_time or not end_time or printer not in PRINTERS:
        return RedirectResponse(url="/", status_code=303)

    booking_time = f"{booking_date} {start_time}-{end_time}"
    target = slot_range(booking_time)
    if target is None or target[2] <= target[1]:
        return RedirectResponse(
            url="/?msg=" + quote("⚠️ 结束时间必须晚于开始时间，请调整时间。"),
            status_code=303,
        )

    conn = connect()

    # 该打印机耗材不足直接拦截
    remaining = get_printer_remaining(conn, printer)
    if consumable > remaining:
        conn.close()
        return RedirectResponse(
            url="/?msg="
            + quote(
                f"⚠️ 材料不足，{printer_label(printer)}当前仅剩 {fmt_grams(remaining)} 克，"
                "请减少用量或选择其他打印机。"
            ),
            status_code=303,
        )

    if printer in printer_states(conn):
        conn.close()
        return RedirectResponse(
            url="/?msg=" + quote("该打印机正在维修中，暂不可用。"), status_code=303
        )

    # 同一打印机当天时段重叠即拦截（已完成/已拒绝不占用时间）
    if has_time_conflict(
        conn, printer, booking_time, (STATUS_PENDING, STATUS_APPROVED)
    ):
        conn.close()
        return RedirectResponse(
            url="/?msg=" + quote("⚠️ 该时段与已有预约冲突，请调整时间。"), status_code=303
        )

    conn.execute(
        """
        INSERT INTO bookings (name, booking_time, printer, consumable, status, owner, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session["name"],
            booking_time,
            printer,
            consumable,
            STATUS_PENDING,
            session["account"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.get("/complete", response_class=HTMLResponse)
async def complete_page(request: Request):
    session = get_session(request)
    if session is None:
        return HTMLResponse(login_page())
    if session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    booking_id = request.query_params.get("id", "").strip()
    conn = connect()
    row = None
    if booking_id.isdigit():
        row = conn.execute(
            """
            SELECT id, booking_time, printer, consumable, status
            FROM bookings WHERE id = ? AND owner = ?
            """,
            (int(booking_id), session["account"]),
        ).fetchone()
    conn.close()

    if row is None or row["status"] != STATUS_APPROVED:
        return RedirectResponse(
            url="/?msg=" + quote("只有已通过的预约才能登记完成。"), status_code=303
        )

    content = f"""
  <div class="card">
  <h2>登记打印任务完成</h2>
  <p class="hint">
    {escape(printer_label(row['printer']))} ｜
    预约时间 {escape(row['booking_time'].replace('T', ' '))} ｜
    预计耗材 {fmt_grams(row['consumable'])} 克
  </p>
  <form method="post" action="/complete">
    <input type="hidden" name="id" value="{row['id']}">
    <input name="duration" placeholder="实际耗时，例如：2小时" maxlength="50" required>
    <textarea name="note" rows="3" placeholder="打印成果备注，例如：打印了一个小黄鸭，完美成功" maxlength="200" required></textarea>
    <button type="submit">提交登记</button>
  </form>
  <p class="hint"><a href="/">返回首页</a></p>
  </div>
"""
    return HTMLResponse(render_page(session, content))


@app.post("/complete")
async def complete_booking(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    booking_id = data.get("id", [""])[0].strip()
    duration = data.get("duration", [""])[0].strip()
    note = data.get("note", [""])[0].strip()

    if booking_id.isdigit():
        conn = connect()
        row = conn.execute(
            "SELECT status FROM bookings WHERE id = ? AND owner = ?",
            (int(booking_id), session["account"]),
        ).fetchone()
        if row and row["status"] == STATUS_APPROVED:
            conn.execute(
                "UPDATE bookings SET status = ?, duration = ?, note = ? WHERE id = ?",
                (STATUS_COMPLETED, duration, note, int(booking_id)),
            )
            conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("已登记任务完成，感谢您的记录！"), status_code=303
    )


@app.post("/review")
async def review(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)

    booking_id = data.get("id", [""])[0].strip()
    action = data.get("action", [""])[0].strip()

    if booking_id.isdigit() and action in ("approve", "reject"):
        conn = connect()
        row = conn.execute(
            "SELECT id, status, printer, consumable, booking_time FROM bookings WHERE id = ?",
            (int(booking_id),),
        ).fetchone()

        # 只有待审批的记录才能处理，避免重复扣减库存
        if row and row["status"] == STATUS_PENDING:
            if action == "approve":
                needed = float(row["consumable"])
                # 同一打印机、同一天的时间段不能与已通过的预约重叠
                if has_time_conflict(
                    conn,
                    row["printer"],
                    row["booking_time"],
                    (STATUS_APPROVED,),
                    exclude_id=int(booking_id),
                ):
                    conn.close()
                    return RedirectResponse(
                        url="/?msg="
                        + quote("⚠️ 无法同时通过：该时间段已安排了其他任务。"),
                        status_code=303,
                    )
                # 审批前校验该打印机的独立库存
                if needed > get_printer_remaining(conn, row["printer"]):
                    conn.close()
                    return RedirectResponse(
                        url="/?msg=" + quote(APPROVE_FAIL_MSG), status_code=303
                    )
                deduct_printer_stock(conn, row["printer"], needed)
                conn.execute(
                    "UPDATE bookings SET status = ? WHERE id = ?",
                    (STATUS_APPROVED, int(booking_id)),
                )
            else:
                conn.execute(
                    "UPDATE bookings SET status = ? WHERE id = ?",
                    (STATUS_REJECTED, int(booking_id)),
                )
            conn.commit()
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/review_all")
async def review_all(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    action = data.get("action", [""])[0].strip()

    conn = connect()
    rows = conn.execute(
        "SELECT id, printer, consumable, booking_time FROM bookings WHERE status = ?",
        (STATUS_PENDING,),
    ).fetchall()

    if not rows:
        conn.close()
        return RedirectResponse(
            url="/?msg=" + quote("当前没有待审批的预约。"), status_code=303
        )

    if action == "approve":
        # 先校验时间冲突：既要比已通过记录，也要比本批次内部
        accepted = {}
        for row in rows:
            current = slot_range(row["booking_time"])
            if has_time_conflict(
                conn, row["printer"], row["booking_time"], (STATUS_APPROVED,)
            ) or (
                current is not None
                and any(
                    ranges_overlap(current, other)
                    for other in accepted.get(row["printer"], [])
                )
            ):
                conn.close()
                return RedirectResponse(
                    url="/?msg=" + quote("⚠️ 无法同时通过：该时间段已安排了其他任务。"),
                    status_code=303,
                )
            if current is not None:
                accepted.setdefault(row["printer"], []).append(current)

        need = {}
        for row in rows:
            need[row["printer"]] = need.get(row["printer"], 0.0) + float(row["consumable"])

        # 批量审批前按打印机逐台校验库存
        for printer, amount in need.items():
            if amount > get_printer_remaining(conn, printer):
                conn.close()
                return RedirectResponse(
                    url="/?msg=" + quote(APPROVE_FAIL_MSG), status_code=303
                )

        for printer, amount in need.items():
            deduct_printer_stock(conn, printer, amount)
        conn.execute(
            "UPDATE bookings SET status = ? WHERE status = ?",
            (STATUS_APPROVED, STATUS_PENDING),
        )
        conn.commit()
        conn.close()
        return RedirectResponse(
            url="/?ok=1&msg=" + quote(f"已一键通过 {len(rows)} 条预约。"), status_code=303
        )

    if action == "reject":
        conn.execute(
            "UPDATE bookings SET status = ? WHERE status = ?",
            (STATUS_REJECTED, STATUS_PENDING),
        )
        conn.commit()
        conn.close()
        return RedirectResponse(
            url="/?ok=1&msg=" + quote(f"已一键拒绝 {len(rows)} 条预约。"), status_code=303
        )

    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/maintenance")
async def add_maintenance(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)

    device = data.get("device", [""])[0].strip()
    issue = data.get("issue", [""])[0].strip()

    if device and issue:
        conn = connect()
        conn.execute(
            "INSERT INTO maintenance (device, issue, status, created_at) VALUES (?, ?, ?, ?)",
            (device, issue, FIX_REPAIRING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/repair")
async def finish_repair(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    repair_id = data.get("id", [""])[0].strip()

    if repair_id.isdigit():
        conn = connect()
        conn.execute(
            "UPDATE maintenance SET status = ? WHERE id = ? AND status = ?",
            (FIX_DONE, int(repair_id), FIX_REPAIRING),
        )
        conn.commit()
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/assistant")
async def assistant(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    keyword = data.get("keyword", [""])[0].strip()
    detail = data.get("detail", [""])[0].strip()
    question = detail or keyword

    if not question:
        return RedirectResponse(url="/", status_code=303)

    conn = connect()
    key = keyword or find_key(conn, question)
    solutions = matched_solutions(conn, key, question)
    tip = smart_tip(conn)
    conn.close()

    if solutions:
        reply_html = "".join(answer_card(source, text) for source, text in solutions)
        reply_html += report_form(key or question[:20], question)
    else:
        reply_html = (
            '<p class="info">未找到匹配的解决方案，是否上报给管理员？</p>'
            + report_form(keyword or question[:20], question)
        )

    content = student_content(session, reply_html=reply_html)
    return HTMLResponse(render_page(session, content, tip))


@app.post("/ticket")
async def create_ticket_route(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    keyword = data.get("keyword", [""])[0].strip()
    detail = data.get("detail", [""])[0].strip()

    if not keyword and not detail:
        return RedirectResponse(url="/", status_code=303)

    conn = connect()
    conn.execute(
        """
        INSERT INTO tickets (student_id, question_keyword, detail, status, admin_reply, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session["account"],
            keyword or detail[:20],
            detail,
            TICKET_PENDING,
            "",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote(TICKET_CREATED_MSG), status_code=303
    )


@app.post("/ticket_reply")
async def ticket_reply(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    ticket_id = data.get("id", [""])[0].strip()
    reply = data.get("reply", [""])[0].strip()

    if ticket_id.isdigit() and reply:
        conn = connect()
        row = conn.execute(
            "SELECT question_keyword FROM tickets WHERE id = ? AND status = ?",
            (int(ticket_id), TICKET_PENDING),
        ).fetchone()
        if row:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE tickets SET admin_reply = ?, status = ? WHERE id = ?",
                (reply, TICKET_CONFIRM, int(ticket_id)),
            )
            # AI 知识沉淀：关键字 + 管理员回复，同关键字保留最新一条
            conn.execute("DELETE FROM knowledge WHERE keyword = ?", (row["question_keyword"],))
            conn.execute(
                "INSERT INTO knowledge (keyword, reply, created_at) VALUES (?, ?, ?)",
                (row["question_keyword"], reply, now),
            )
            conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("已回复该工单，等待学生确认。"), status_code=303
    )


@app.post("/ticket_feedback")
async def ticket_feedback(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    ticket_id = data.get("id", [""])[0].strip()
    action = data.get("action", [""])[0].strip()
    follow_up = data.get("follow_up", [""])[0].strip()

    if action == "unresolved" and not follow_up:
        return RedirectResponse(
            url="/?msg=" + quote("必须填写补充说明才能提交哦"), status_code=303
        )

    if ticket_id.isdigit() and action in ("resolved", "unresolved"):
        conn = connect()
        row = conn.execute(
            "SELECT id FROM tickets WHERE id = ? AND student_id = ? AND status = ?",
            (int(ticket_id), session["account"], TICKET_CONFIRM),
        ).fetchone()
        if row:
            if action == "resolved":
                conn.execute(
                    "UPDATE tickets SET status = ? WHERE id = ?",
                    (TICKET_DONE, int(ticket_id)),
                )
            else:
                conn.execute(
                    "UPDATE tickets SET status = ?, follow_up = ? WHERE id = ?",
                    (TICKET_PENDING, follow_up, int(ticket_id)),
                )
            conn.commit()
        conn.close()

    msg = "感谢反馈！" if action == "resolved" else "已通知管理员，请耐心等待。"
    return RedirectResponse(url="/?ok=1&msg=" + quote(msg), status_code=303)


@app.post("/experience")
async def add_experience(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    problem = data.get("problem", [""])[0].strip()
    solution = data.get("solution", [""])[0].strip()

    if problem and solution:
        conn = connect()
        conn.execute(
            """
            INSERT INTO student_experiences (student_id, problem, solution, status, feedback, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session["account"],
                problem,
                solution,
                EXP_PENDING,
                "",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        conn.close()

    return RedirectResponse(url="/?ok=1&msg=" + quote(EXP_CREATED_MSG), status_code=303)


@app.post("/experience_review")
async def review_experience(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    exp_id = data.get("id", [""])[0].strip()
    action = data.get("action", [""])[0].strip()
    reason = data.get("reason", ["信息不匹配"])[0].strip()
    custom = data.get("custom", [""])[0].strip()

    if exp_id.isdigit() and action in ("approve", "reject"):
        if action == "approve":
            status, feedback = EXP_APPROVED, "已通过审核，感谢分享！"
        else:
            if reason == "其他" or not reason:
                reason = custom or "信息不匹配"
            elif custom:
                reason = f"{reason}：{custom}"
            status, feedback = EXP_REJECTED, reason

        conn = connect()
        conn.execute(
            "UPDATE student_experiences SET status = ?, feedback = ? WHERE id = ? AND status = ?",
            (status, feedback, int(exp_id), EXP_PENDING),
        )
        conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("已完成经验审核，结果已同步给学生。"), status_code=303
    )


@app.post("/qa_ask")
async def qa_ask(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "student":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    question = data.get("question", [""])[0].strip()
    anonymous = data.get("anonymous", [""])[0] == "on"

    if question:
        asker = "匿名同学" if anonymous else session["name"]
        conn = connect()
        conn.execute(
            """
            INSERT INTO questions (asker_name, question_content, is_pinned, created_at)
            VALUES (?, ?, 0, ?)
            """,
            (asker, question, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("问题已发布到互助问答板。"), status_code=303
    )


@app.post("/qa_answer")
async def qa_answer(request: Request):
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    data = parse_qs(raw)
    question_id = data.get("id", [""])[0].strip()
    answer = data.get("answer", [""])[0].strip()
    anonymous = data.get("anonymous", [""])[0] == "on"

    if question_id.isdigit() and answer:
        if session["role"] == "admin":
            answerer = "管理员"
        else:
            answerer = "匿名同学" if anonymous else session["name"]
        conn = connect()
        exists = conn.execute(
            "SELECT id FROM questions WHERE id = ?", (int(question_id),)
        ).fetchone()
        if exists:
            conn.execute(
                """
                INSERT INTO answers (question_id, answerer_name, answer_content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(question_id),
                    answerer,
                    answer,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("回答已发布，感谢互助！"), status_code=303
    )


@app.post("/qa_like")
async def qa_like(request: Request):
    session = get_session(request)
    if session is None:
        return JSONResponse({"error": "请先登录"}, status_code=401)

    raw = (await request.body()).decode("utf-8")
    answer_id = parse_qs(raw).get("id", [""])[0].strip()
    if not answer_id.isdigit():
        return JSONResponse({"status": "error", "new_likes": 0}, status_code=400)

    aid = int(answer_id)
    user_id = session["account"]
    conn = connect()
    row = conn.execute("SELECT likes FROM answers WHERE id = ?", (aid,)).fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"status": "error", "new_likes": 0}, status_code=404)

    existing = conn.execute(
        "SELECT id FROM answer_likes WHERE answer_id = ? AND user_id = ?",
        (aid, user_id),
    ).fetchone()

    if existing:
        # 已赞过：取消点赞
        conn.execute("DELETE FROM answer_likes WHERE id = ?", (existing["id"],))
        conn.execute(
            "UPDATE answers SET likes = CASE WHEN likes > 0 THEN likes - 1 ELSE 0 END "
            "WHERE id = ?",
            (aid,),
        )
        status = "unliked"
    else:
        # 未赞过：新增点赞
        conn.execute(
            "INSERT INTO answer_likes (answer_id, user_id, created_at) VALUES (?, ?, ?)",
            (aid, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute("UPDATE answers SET likes = likes + 1 WHERE id = ?", (aid,))
        status = "liked"

    conn.commit()
    new_likes = conn.execute(
        "SELECT likes FROM answers WHERE id = ?", (aid,)
    ).fetchone()["likes"]
    conn.close()
    return JSONResponse({"status": status, "new_likes": new_likes, "likes": new_likes})


@app.post("/qa_pin_answer")
async def qa_pin_answer(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    answer_id = parse_qs(raw).get("id", [""])[0].strip()

    if answer_id.isdigit():
        conn = connect()
        conn.execute(
            "UPDATE answers SET is_pinned = 1 - is_pinned WHERE id = ?", (int(answer_id),)
        )
        conn.commit()
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/qa_pin")
async def qa_pin(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    qa_id = parse_qs(raw).get("id", [""])[0].strip()

    if qa_id.isdigit():
        conn = connect()
        conn.execute(
            "UPDATE questions SET is_pinned = 1 - is_pinned WHERE id = ?", (int(qa_id),)
        )
        conn.commit()
        conn.close()

    return RedirectResponse(url="/", status_code=303)


@app.post("/qa_delete")
async def qa_delete(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    qa_id = parse_qs(raw).get("id", [""])[0].strip()

    if qa_id.isdigit():
        conn = connect()
        conn.execute(
            "DELETE FROM answer_likes WHERE answer_id IN (SELECT id FROM answers WHERE question_id = ?)",
            (int(qa_id),),
        )
        conn.execute("DELETE FROM answers WHERE question_id = ?", (int(qa_id),))
        conn.execute("DELETE FROM questions WHERE id = ?", (int(qa_id),))
        conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("已删除该问题及其全部回答。"), status_code=303
    )


@app.post("/qa_delete_answer")
async def qa_delete_answer(request: Request):
    session = get_session(request)
    if session is None or session["role"] != "admin":
        return RedirectResponse(url="/", status_code=303)

    raw = (await request.body()).decode("utf-8")
    answer_id = parse_qs(raw).get("id", [""])[0].strip()

    if answer_id.isdigit():
        conn = connect()
        conn.execute(
            "DELETE FROM answer_likes WHERE answer_id = ?", (int(answer_id),)
        )
        conn.execute("DELETE FROM answers WHERE id = ?", (int(answer_id),))
        conn.commit()
        conn.close()

    return RedirectResponse(
        url="/?ok=1&msg=" + quote("已删除该回答。"), status_code=303
    )
