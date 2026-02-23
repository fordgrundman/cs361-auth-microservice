import os
import time
import uuid
import sqlite3
from datetime import datetime, timezone

import bcrypt
import jwt
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

APP = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "auth.db")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
PORT = int(os.getenv("PORT", "5000"))

# Parse "APP_SECRETS=app1:secret1,app2:secret2"
def load_app_secrets():
    raw = os.getenv("APP_SECRETS", "").strip()
    secrets = {}
    if not raw:
        return secrets
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            continue
        app_id, secret = pair.split(":", 1)
        secrets[app_id.strip()] = secret.strip()
    return secrets

APP_SECRETS = load_app_secrets()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash BLOB NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # sessionId is the key used by downstream services
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        app_id TEXT NOT NULL,
        jwt_token TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def error(status_code: int, code: str, message: str, extra=None):
    payload = {"error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return jsonify(payload), status_code


def require_app_headers():
    app_id = request.headers.get("X-App-Id", "")
    app_secret = request.headers.get("X-App-Secret", "")
    if not app_id or not app_secret:
        return None, None, error(401, "missing_app_auth",
                                "Missing X-App-Id and/or X-App-Secret headers.")
    expected = APP_SECRETS.get(app_id)
    if expected is None:
        return None, None, error(401, "unknown_app", "Unknown appId.")
    if app_secret != expected:
        return None, None, error(401, "invalid_app_secret", "Invalid clientSecret for appId.")
    return app_id, app_secret, None


def hash_password(password: str) -> bytes:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt)


def verify_password(password: str, pw_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pw_hash)
    except Exception:
        return False


def make_access_token(user_id: int, username: str, app_id: str) -> str:
    claims = {
        "sub": str(user_id),
        "username": username,
        "appId": app_id,
        "iat": int(time.time()),
        # no "exp" on purpose
    }
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


@APP.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@APP.post("/signup")
def signup():
    """
    Signup user for a given appId.
    Requires app authentication headers.
    Body: { "username": "...", "password": "..." }
    Response: { userId, sessionId, token, appId }
    """
    app_id, _, err = require_app_headers()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return error(400, "invalid_input", "username and password are required.")

    if len(username) < 3 or len(password) < 8:
        return error(400, "invalid_syntax",
                     "Invalid username or password syntax.",
                     extra={"hint": "username>=3 chars, password>=8 chars"})

    conn = db_connect()
    cur = conn.cursor()

    # username uniqueness
    cur.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cur.fetchone() is not None:
        conn.close()
        return error(409, "username_exists", "Username already exists. Choose another.")

    pw_hash = hash_password(password)
    cur.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, pw_hash, now_iso())
    )
    user_id = cur.lastrowid

    session_id = str(uuid.uuid4())
    token = make_access_token(user_id, username, app_id)

    cur.execute(
        """INSERT INTO sessions (session_id, user_id, app_id, jwt_token, revoked, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (session_id, user_id, app_id, token, now_iso())
    )

    conn.commit()
    conn.close()

    return jsonify({
        "userId": user_id,
        "sessionId": session_id,
        "token": token,
        "appId": app_id
    }), 201


@APP.post("/login")
def login():
    """
    Login user for a given appId.
    Requires app authentication headers.
    Body: { "username": "...", "password": "..." }
    Response: { userId, sessionId, token, appId }
    """
    start = time.perf_counter()

    app_id, _, err = require_app_headers()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return error(400, "missing_fields", "username and password both required.")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return error(404, "account_not_found", "Account not found.")

    if not verify_password(password, row["password_hash"]):
        conn.close()
        return error(401, "invalid_credentials", "Invalid username or password.")

    user_id = int(row["id"])
    token = make_access_token(user_id, row["username"], app_id)
    session_id = str(uuid.uuid4())

    cur.execute(
        """INSERT INTO sessions (session_id, user_id, app_id, jwt_token, revoked, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (session_id, user_id, app_id, token, now_iso())
    )

    conn.commit()
    conn.close()

    elapsed = time.perf_counter() - start
    # soft performance signal (you can log or measure in video)
    return jsonify({
        "userId": user_id,
        "sessionId": session_id,
        "token": token,
        "appId": app_id,
        "timingSeconds": round(elapsed, 6)
    }), 200


@APP.post("/logout")
def logout():
    """
    Logout by revoking a sessionId.
    Requires app authentication headers.
    Body: { "sessionId": "..." }
    Response: { success: true }
    """
    start = time.perf_counter()

    app_id, _, err = require_app_headers()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    session_id = (body.get("sessionId") or "").strip()
    if not session_id:
        return error(400, "missing_session", "sessionId is required.")

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""SELECT session_id, revoked, app_id
                   FROM sessions WHERE session_id = ?""", (session_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return error(401, "unauthorized", "No JWT/session recognized for logout.")

    if row["app_id"] != app_id:
        conn.close()
        return error(403, "wrong_app", "sessionId does not belong to this appId.")

    if int(row["revoked"]) == 1:
        conn.close()
        return jsonify({"success": True, "alreadyRevoked": True}), 200

    cur.execute("""UPDATE sessions
                   SET revoked = 1, revoked_at = ?
                   WHERE session_id = ?""", (now_iso(), session_id))
    conn.commit()
    conn.close()

    elapsed = time.perf_counter() - start
    return jsonify({"success": True, "timingSeconds": round(elapsed, 6)}), 200


@APP.post("/introspect")
def introspect():
    """
    Session introspection for downstream apps:
    Requires app authentication headers.
    Body: { "sessionId": "..." }
    Response:
      - { active: true, userId, appId }
      - { active: false }
    """
    app_id, _, err = require_app_headers()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    session_id = (body.get("sessionId") or "").strip()
    if not session_id:
        return error(400, "missing_session", "sessionId is required.")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""SELECT user_id, app_id, jwt_token, revoked
                   FROM sessions WHERE session_id = ?""", (session_id,))
    row = cur.fetchone()
    conn.close()

    if row is None:
        return jsonify({"active": False}), 200

    if row["app_id"] != app_id:
        # don't leak that a session exists for other apps
        return jsonify({"active": False}), 200

    if int(row["revoked"]) == 1:
        return jsonify({"active": False}), 200

    # Verify token is still valid and signed
    token = row["jwt_token"]
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False})
    except Exception:
        return jsonify({"active": False, "reason": "invalid"}), 200

    return jsonify({
        "active": True,
        "userId": int(row["user_id"]),
        "appId": row["app_id"]
    }), 200


if __name__ == "__main__":
    init_db()
    APP.run(host="0.0.0.0", port=PORT, debug=True)