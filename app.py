"""
SpendWise — Flask Backend
Run: python app.py
API runs at http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime, date, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "spendwise-dev-secret-change-in-prod")
CORS(app, supports_credentials=True)

DB_PATH = "spendwise.db"

# ══════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT '$',
            lang        TEXT    NOT NULL DEFAULT 'en',
            xp          INTEGER NOT NULL DEFAULT 0,
            level       INTEGER NOT NULL DEFAULT 1,
            streak      INTEGER NOT NULL DEFAULT 0,
            last_log_date TEXT  DEFAULT NULL,
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            desc        TEXT    NOT NULL,
            amount      REAL    NOT NULL CHECK(amount > 0),
            category    TEXT    NOT NULL DEFAULT 'Other',
            type        TEXT    NOT NULL CHECK(type IN ('income','expense')),
            date        TEXT    NOT NULL,
            note        TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category    TEXT    NOT NULL,
            monthly_limit REAL  NOT NULL CHECK(monthly_limit > 0),
            UNIQUE(user_id, category)
        );

        CREATE TABLE IF NOT EXISTS badges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            badge_id    TEXT    NOT NULL,
            earned_at   TEXT    DEFAULT (datetime('now')),
            UNIQUE(user_id, badge_id)
        );
    """)

    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session["user_id"]


# ══════════════════════════════════════════════════════════════════════
#  GAMIFICATION HELPERS
# ══════════════════════════════════════════════════════════════════════

LEVELS = [
    {"name": "Novice Saver",   "xp": 0},
    {"name": "Budget Rookie",  "xp": 100},
    {"name": "Money Minder",   "xp": 300},
    {"name": "Thrift Expert",  "xp": 600},
    {"name": "Finance Wizard", "xp": 1000},
    {"name": "Wealth Master",  "xp": 1500},
]

BADGE_CHECKS = {
    "first":   lambda stats: stats["total_tx"] >= 1,
    "five":    lambda stats: stats["expense_tx"] >= 5,
    "streak3": lambda stats: stats["streak"] >= 3,
    "streak7": lambda stats: stats["streak"] >= 7,
    "budget":  lambda stats: stats["budget_count"] >= 1,
    "saver":   lambda stats: stats["total_income"] > stats["total_expense"] and stats["total_income"] > 0,
    "tenk":    lambda stats: stats["total_amount"] >= 10000,
    "diverse": lambda stats: stats["unique_categories"] >= 5,
}


def compute_level(xp: int) -> dict:
    level = 1
    for i, lv in enumerate(LEVELS):
        if xp >= lv["xp"]:
            level = i + 1
    level = min(level, len(LEVELS))
    cur_lv = LEVELS[level - 1]
    next_lv = LEVELS[level] if level < len(LEVELS) else None
    cur_xp = xp - cur_lv["xp"]
    total_xp = (next_lv["xp"] - cur_lv["xp"]) if next_lv else 1
    pct = min(round(cur_xp / total_xp * 100), 100) if total_xp > 0 else 100
    return {
        "level": level,
        "level_name": cur_lv["name"],
        "xp": xp,
        "pct": pct,
        "xp_current": cur_xp,
        "xp_total": total_xp,
    }


def update_streak(conn, user_id: int):
    """Update streak based on today's date. Returns new streak value."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT streak, last_log_date FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    streak = row["streak"]
    last = row["last_log_date"]

    if last == today:
        return streak  # Already logged today, no change
    elif last == yesterday:
        streak += 1
    else:
        streak = 1

    conn.execute(
        "UPDATE users SET streak = ?, last_log_date = ? WHERE id = ?",
        (streak, today, user_id)
    )
    return streak


def gain_xp(conn, user_id: int, amount: int) -> dict:
    """Add XP, recompute level, return updated gamification state."""
    conn.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (amount, user_id))
    row = conn.execute("SELECT xp FROM users WHERE id = ?", (user_id,)).fetchone()
    new_xp = row["xp"]
    lv = compute_level(new_xp)
    conn.execute("UPDATE users SET level = ? WHERE id = ?", (lv["level"], user_id))
    return lv


def get_user_stats(conn, user_id: int) -> dict:
    """Collect stats needed for badge checks."""
    rows = conn.execute(
        "SELECT type, amount, category FROM transactions WHERE user_id = ?",
        (user_id,)
    ).fetchall()
    total_income = sum(r["amount"] for r in rows if r["type"] == "income")
    total_expense = sum(r["amount"] for r in rows if r["type"] == "expense")
    expense_tx = sum(1 for r in rows if r["type"] == "expense")
    categories = {r["category"] for r in rows}
    budget_count = conn.execute(
        "SELECT COUNT(*) FROM budgets WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    streak = conn.execute(
        "SELECT streak FROM users WHERE id = ?", (user_id,)
    ).fetchone()["streak"]

    return {
        "total_tx": len(rows),
        "expense_tx": expense_tx,
        "total_income": total_income,
        "total_expense": total_expense,
        "total_amount": total_income + total_expense,
        "unique_categories": len(categories),
        "budget_count": budget_count,
        "streak": streak,
    }


def check_and_award_badges(conn, user_id: int) -> list[str]:
    """Check all badge conditions and award any newly earned badges. Returns list of new badge IDs."""
    stats = get_user_stats(conn, user_id)
    already = {r["badge_id"] for r in conn.execute(
        "SELECT badge_id FROM badges WHERE user_id = ?", (user_id,)
    ).fetchall()}

    new_badges = []
    for badge_id, check in BADGE_CHECKS.items():
        if badge_id not in already and check(stats):
            conn.execute(
                "INSERT OR IGNORE INTO badges (user_id, badge_id) VALUES (?, ?)",
                (user_id, badge_id)
            )
            new_badges.append(badge_id)

    return new_badges


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — SERVE APP
# ══════════════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return render_template("index.html")


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.get_json()
    name     = data.get("name", "").strip()
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")
    currency = data.get("currency", "$")
    lang     = data.get("lang", "en")

    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, password, currency, lang) VALUES (?, ?, ?, ?, ?)",
            (name, email, generate_password_hash(password), currency, lang)
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        session["user_id"] = user["id"]
        return jsonify(_user_payload(user)), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already registered"}), 409
    finally:
        conn.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")

    # Demo shortcut
    if email == "demo@spendwise.app" and password == "demo1234":
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            # Create demo user on first login
            conn.execute(
                "INSERT INTO users (name, email, password, currency, xp, streak) VALUES (?,?,?,?,?,?)",
                ("Demo User", email, generate_password_hash("demo1234"), "$", 240, 3)
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            _seed_demo_data(conn, user["id"])
            conn.commit()
        session["user_id"] = user["id"]
        conn.close()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        return jsonify(_user_payload(user))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password"], password):
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = user["id"]
    return jsonify(_user_payload(user))


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
@login_required
def me():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (current_user_id(),)).fetchone()
    conn.close()
    if not user:
        session.clear()
        return jsonify({"error": "User not found"}), 404
    return jsonify(_user_payload(user))


def _user_payload(user) -> dict:
    lv = compute_level(user["xp"])
    return {
        "id":       user["id"],
        "name":     user["name"],
        "email":    user["email"],
        "currency": user["currency"],
        "lang":     user["lang"],
        "xp":       user["xp"],
        "level":    lv["level"],
        "level_name": lv["level_name"],
        "xp_pct":   lv["pct"],
        "xp_current": lv["xp_current"],
        "xp_total": lv["xp_total"],
        "streak":   user["streak"],
        "last_log_date": user["last_log_date"],
    }


def _seed_demo_data(conn, user_id: int):
    today = date.today().isoformat()
    transactions = [
        ("Grocery Store",     82.50, "Food",          "expense", today, "Weekly groceries"),
        ("Monthly Salary",  3200.00, "Income",         "income",  today, ""),
        ("Netflix",           15.99, "Entertainment",  "expense", today, "Monthly sub"),
        ("Uber Ride",         12.40, "Transport",      "expense", today, ""),
        ("Gym Membership",    45.00, "Health",         "expense", today, ""),
        ("Coffee Shop",        5.60, "Food",           "expense", today, "Latte"),
        ("Electricity Bill",  78.00, "Utilities",      "expense", today, ""),
    ]
    for tx in transactions:
        conn.execute(
            "INSERT INTO transactions (user_id, desc, amount, category, type, date, note) VALUES (?,?,?,?,?,?,?)",
            (user_id, *tx)
        )
    budgets = [
        ("Food", 300), ("Transport", 150), ("Entertainment", 100),
        ("Shopping", 200), ("Health", 100)
    ]
    for cat, limit in budgets:
        conn.execute(
            "INSERT OR REPLACE INTO budgets (user_id, category, monthly_limit) VALUES (?,?,?)",
            (user_id, cat, limit)
        )


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/transactions", methods=["GET"])
@login_required
def list_transactions():
    uid = current_user_id()
    search   = request.args.get("search", "")
    cat      = request.args.get("category", "")
    tx_type  = request.args.get("type", "")
    month    = request.args.get("month", "")   # e.g. "2026-06"

    query = "SELECT * FROM transactions WHERE user_id = ?"
    params = [uid]

    if search:
        query += " AND (desc LIKE ? OR note LIKE ? OR category LIKE ?)"
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if cat:
        query += " AND category = ?"
        params.append(cat)
    if tx_type in ("income", "expense"):
        query += " AND type = ?"
        params.append(tx_type)
    if month:
        query += " AND strftime('%Y-%m', date) = ?"
        params.append(month)

    query += " ORDER BY date DESC, id DESC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transactions", methods=["POST"])
@login_required
def add_transaction():
    uid  = current_user_id()
    data = request.get_json()

    desc     = data.get("desc", "").strip()
    amount   = float(data.get("amount", 0))
    category = data.get("category", "Other").strip()
    tx_type  = "income" if category == "Income" else "expense"
    tx_date  = data.get("date", date.today().isoformat())
    note     = data.get("note", "").strip()

    if not desc:
        return jsonify({"error": "Description is required"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    conn = get_db()

    cur = conn.execute(
        "INSERT INTO transactions (user_id, desc, amount, category, type, date, note) VALUES (?,?,?,?,?,?,?)",
        (uid, desc, amount, category, tx_type, tx_date, note)
    )
    tx_id = cur.lastrowid

    # Streak
    new_streak = update_streak(conn, uid)

    # XP: income = 15, expense = 10
    xp_gain = 15 if tx_type == "income" else 10
    lv = gain_xp(conn, uid, xp_gain)

    # Badges
    new_badges = check_and_award_badges(conn, uid)

    conn.commit()
    tx_row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()

    return jsonify({
        "transaction": dict(tx_row),
        "xp_gain":    xp_gain,
        "gamification": lv,
        "streak":     new_streak,
        "new_badges": new_badges,
    }), 201


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
@login_required
def delete_transaction(tx_id):
    uid = current_user_id()
    conn = get_db()
    result = conn.execute(
        "DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, uid)
    )
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": tx_id})


@app.route("/api/transactions/<int:tx_id>", methods=["PUT"])
@login_required
def update_transaction(tx_id):
    uid  = current_user_id()
    data = request.get_json()

    desc     = data.get("desc", "").strip()
    amount   = float(data.get("amount", 0))
    category = data.get("category", "Other").strip()
    tx_type  = "income" if category == "Income" else "expense"
    tx_date  = data.get("date", date.today().isoformat())
    note     = data.get("note", "").strip()

    if not desc or amount <= 0:
        return jsonify({"error": "Invalid data"}), 400

    conn = get_db()
    result = conn.execute(
        "UPDATE transactions SET desc=?, amount=?, category=?, type=?, date=?, note=? WHERE id=? AND user_id=?",
        (desc, amount, category, tx_type, tx_date, note, tx_id, uid)
    )
    conn.commit()
    if result.rowcount == 0:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — BUDGETS
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/budgets", methods=["GET"])
@login_required
def list_budgets():
    uid = current_user_id()
    conn = get_db()
    rows = conn.execute("SELECT * FROM budgets WHERE user_id = ?", (uid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/budgets", methods=["POST"])
@login_required
def set_budget():
    uid  = current_user_id()
    data = request.get_json()
    category = data.get("category", "").strip()
    limit    = float(data.get("monthly_limit", 0))

    if not category or limit <= 0:
        return jsonify({"error": "Category and positive limit required"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO budgets (user_id, category, monthly_limit) VALUES (?,?,?) "
        "ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
        (uid, category, limit)
    )

    # Award budget badge + XP
    new_badges = check_and_award_badges(conn, uid)
    lv = gain_xp(conn, uid, 30)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM budgets WHERE user_id = ? AND category = ?", (uid, category)
    ).fetchone()
    conn.close()
    return jsonify({
        "budget": dict(row),
        "xp_gain": 30,
        "gamification": lv,
        "new_badges": new_badges,
    }), 201


@app.route("/api/budgets/<category>", methods=["DELETE"])
@login_required
def delete_budget(category):
    uid = current_user_id()
    conn = get_db()
    conn.execute("DELETE FROM budgets WHERE user_id = ? AND category = ?", (uid, category))
    conn.commit()
    conn.close()
    return jsonify({"deleted": category})


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — SUMMARY / INSIGHTS
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/summary", methods=["GET"])
@login_required
def summary():
    uid = current_user_id()
    conn = get_db()

    # All-time totals
    totals = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS total_income,
          COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS total_expense,
          COUNT(*) AS total_tx
        FROM transactions WHERE user_id = ?
    """, (uid,)).fetchone()

    # Monthly breakdown (last 6 months)
    monthly = conn.execute("""
        SELECT
          strftime('%Y-%m', date) AS month,
          COALESCE(SUM(CASE WHEN type='income'  THEN amount ELSE 0 END),0) AS income,
          COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense
        FROM transactions WHERE user_id = ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """, (uid,)).fetchall()

    # Category breakdown (expenses, this month)
    this_month = date.today().strftime("%Y-%m")
    categories = conn.execute("""
        SELECT category, SUM(amount) AS total
        FROM transactions
        WHERE user_id = ? AND type='expense' AND strftime('%Y-%m', date) = ?
        GROUP BY category ORDER BY total DESC
    """, (uid, this_month)).fetchall()

    # Budget utilisation this month
    budgets = conn.execute("SELECT * FROM budgets WHERE user_id = ?", (uid,)).fetchall()
    budget_status = []
    for b in budgets:
        spent = conn.execute("""
            SELECT COALESCE(SUM(amount),0) AS s FROM transactions
            WHERE user_id=? AND category=? AND type='expense' AND strftime('%Y-%m',date)=?
        """, (uid, b["category"], this_month)).fetchone()["s"]
        pct = round(spent / b["monthly_limit"] * 100, 1) if b["monthly_limit"] > 0 else 0
        budget_status.append({
            "category":      b["category"],
            "monthly_limit": b["monthly_limit"],
            "spent":         spent,
            "pct":           pct,
            "status":        "danger" if pct >= 100 else "warn" if pct >= 75 else "safe",
        })

    conn.close()

    monthly_list = []
    for r in reversed(list(monthly)):
        inc = r["income"]; exp = r["expense"]
        monthly_list.append({
            "month": r["month"], "income": inc, "expense": exp,
            "net": round(inc - exp, 2),
            "savings_rate": round((inc - exp) / inc * 100, 1) if inc > 0 else 0,
        })

    return jsonify({
        "totals": {
            "income":  totals["total_income"],
            "expense": totals["total_expense"],
            "net":     round(totals["total_income"] - totals["total_expense"], 2),
            "tx_count": totals["total_tx"],
        },
        "monthly":        monthly_list,
        "categories":     [dict(r) for r in categories],
        "budget_status":  budget_status,
    })


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — GAMIFICATION
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/gamification", methods=["GET"])
@login_required
def gamification():
    uid = current_user_id()
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    badges = conn.execute(
        "SELECT badge_id, earned_at FROM badges WHERE user_id = ?", (uid,)
    ).fetchall()

    stats = get_user_stats(conn, uid)
    conn.close()

    lv = compute_level(user["xp"])

    # Build challenges progress
    challenges = [
        {"id": "log5",    "name": "Log 5 Expenses",  "reward": 50,  "progress": stats["expense_tx"],   "goal": 5},
        {"id": "streak3", "name": "3 Day Streak",     "reward": 75,  "progress": stats["streak"],        "goal": 3},
        {"id": "budget1", "name": "Set 1 Budget",     "reward": 30,  "progress": stats["budget_count"],  "goal": 1},
        {"id": "income",  "name": "Log Income",       "reward": 40,  "progress": min(stats["total_tx"] - stats["expense_tx"], 1), "goal": 1},
    ]

    return jsonify({
        "level":       lv["level"],
        "level_name":  lv["level_name"],
        "xp":          user["xp"],
        "xp_pct":      lv["pct"],
        "xp_current":  lv["xp_current"],
        "xp_total":    lv["xp_total"],
        "streak":      user["streak"],
        "last_log_date": user["last_log_date"],
        "badges":      [{"id": b["badge_id"], "earned_at": b["earned_at"]} for b in badges],
        "challenges":  challenges,
        "stats":       stats,
    })


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — USER PREFERENCES
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/user/preferences", methods=["PATCH"])
@login_required
def update_preferences():
    uid  = current_user_id()
    data = request.get_json()
    currency = data.get("currency")
    lang     = data.get("lang")

    updates = []
    params  = []
    if currency:
        updates.append("currency = ?"); params.append(currency)
    if lang:
        updates.append("lang = ?"); params.append(lang)
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    params.append(uid)
    conn = get_db()
    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return jsonify(_user_payload(user))


# ══════════════════════════════════════════════════════════════════════
#  ROUTES — NOTIFICATIONS (budget alerts)
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/notifications", methods=["GET"])
@login_required
def notifications():
    uid = current_user_id()
    this_month = date.today().strftime("%Y-%m")
    conn = get_db()

    budgets = conn.execute("SELECT * FROM budgets WHERE user_id = ?", (uid,)).fetchall()
    alerts = []
    for b in budgets:
        spent = conn.execute("""
            SELECT COALESCE(SUM(amount),0) AS s FROM transactions
            WHERE user_id=? AND category=? AND type='expense' AND strftime('%Y-%m',date)=?
        """, (uid, b["category"], this_month)).fetchone()["s"]
        pct = round(spent / b["monthly_limit"] * 100, 1) if b["monthly_limit"] > 0 else 0
        if pct >= 100:
            alerts.append({
                "type": "danger",
                "title": f"Budget exceeded: {b['category']}",
                "body": f"You've spent {pct}% of your {b['category']} budget (${spent:.2f} / ${b['monthly_limit']:.2f})",
            })
        elif pct >= 75:
            alerts.append({
                "type": "warn",
                "title": f"Budget warning: {b['category']}",
                "body": f"You've used {pct}% of your {b['category']} budget.",
            })

    conn.close()
    return jsonify(alerts)


# ══════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    print("\n✅  SpendWise API running at http://localhost:5000\n")
    print("   Demo credentials: demo@spendwise.app / demo1234\n")
    app.run(debug=True, port=5000)
