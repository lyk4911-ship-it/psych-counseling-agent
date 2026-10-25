"""SQLite-backed accounts, revocable logins and account-owned chat records.

Only password/token hashes are stored. Each operation opens a short-lived
connection; no database transaction is held open during an upstream model call.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    return f"pbkdf2_sha256$600000${salt}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class StoreError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class AccountStore:
    def __init__(self, path=None):
        root = Path(__file__).resolve().parent
        self.path = Path(path or os.getenv("APP_DB_PATH") or root / "data" / "accounts.sqlite3")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.initialize()
        # The database contains personal conversations. WAL sidecars inherit the
        # containing directory's permissions; keep that directory private too.
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_user ON auth_sessions(user_id);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(user_id, updated_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS message_conversation ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS chat_requests (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_owner ON memories(user_id, updated_at);
            """)

    @staticmethod
    def public_user(row):
        return {key: row[key] for key in ("id", "username", "display_name", "created_at")}

    def register(self, username, password, display_name):
        now = utc_now()
        user = {"id": str(uuid.uuid4()), "username": username.lower(),
                "display_name": display_name or username, "created_at": now}
        try:
            with self.connection() as db:
                db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                           (user["id"], user["username"], user["display_name"], hash_password(password), now))
        except sqlite3.IntegrityError as exc:
            raise StoreError(409, "这个用户名已被使用") from exc
        return user

    def authenticate(self, username, password):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        # Equal work for unknown usernames prevents an obvious timing oracle.
        encoded = row["password_hash"] if row else "pbkdf2_sha256$600000$missing$" + "0" * 64
        if not verify_password(password, encoded) or not row:
            raise StoreError(401, "用户名或密码不正确")
        return self.public_user(row)

    def create_login(self, user_id, ttl):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
            # Check the user still exists (e.g. deletion raced a login).
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise StoreError(401, "请重新登录")
            db.execute("INSERT INTO auth_sessions VALUES (?, ?, ?, ?)",
                       (token_hash(token), user_id, token_hash(csrf), time.time() + ttl))
        return token, csrf

    def login_user(self, token):
        if not token or len(token) > 256:
            raise StoreError(401, "请先登录")
        with self.connection() as db:
            row = db.execute("""SELECT users.*, auth_sessions.csrf_hash FROM users
                JOIN auth_sessions ON users.id=auth_sessions.user_id
                WHERE token_hash=? AND expires_at>?""", (token_hash(token), time.time())).fetchone()
        if not row:
            raise StoreError(401, "登录已过期，请重新登录")
        return self.public_user(row), row["csrf_hash"]

    def logout(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token),))

    def update_profile(self, user_id, display_name):
        with self.connection() as db:
            db.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
            return self.public_user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def require_password(self, db, user_id, password):
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise StoreError(403, "当前密码不正确")

    def change_password(self, user_id, current_password, new_password):
        encoded = hash_password(new_password)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_password(db, user_id, current_password)
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    def delete_user(self, user_id, password):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_password(db, user_id, password)
            db.execute("DELETE FROM users WHERE id=?", (user_id,))

    @staticmethod
    def owned_conversation(db, user_id, conversation_id):
        row = db.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",
                         (conversation_id, user_id)).fetchone()
        if not row:
            raise StoreError(404, "没有找到这段对话")
        return row

    def create_conversation(self, user_id, title="新对话"):
        now, conversation_id = utc_now(), str(uuid.uuid4())
        with self.connection() as db:
            db.execute("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                       (conversation_id, user_id, title, now, now))
        return {"id": conversation_id, "title": title, "created_at": now, "updated_at": now}

    def list_conversations(self, user_id, limit=100, offset=0):
        with self.connection() as db:
            rows = db.execute("""SELECT c.id,c.title,c.created_at,c.updated_at,
                (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) message_count,
                (SELECT content FROM messages m WHERE m.conversation_id=c.id ORDER BY id DESC LIMIT 1) last_message
                FROM conversations c WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                              (user_id, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def conversation(self, user_id, conversation_id, limit=100, before=None):
        with self.connection() as db:
            row = self.owned_conversation(db, user_id, conversation_id)
            messages = db.execute("""SELECT id,role,content,created_at FROM messages
                WHERE conversation_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?""",
                                  (conversation_id, before, before, limit + 1)).fetchall()
        return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
                "updated_at": row["updated_at"], "has_more": len(messages) > limit,
                "messages": [dict(m) for m in reversed(messages[:limit])]}

    def rename_conversation(self, user_id, conversation_id, title):
        with self.connection() as db:
            self.owned_conversation(db, user_id, conversation_id)
            db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))

    def delete_conversation(self, user_id, conversation_id):
        with self.connection() as db:
            self.owned_conversation(db, user_id, conversation_id)
            db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            self.clear_derived_state(db, user_id)

    def claim_turn(self, user_id, conversation_id, request_id, payload_hash):
        """A database lease serializes generation even across multiple workers."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned_conversation(db, user_id, conversation_id)
            previous = db.execute("SELECT * FROM chat_requests WHERE conversation_id=? AND request_id=?",
                                  (conversation_id, request_id)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise StoreError(409, "请求编号已用于另一条消息")
                return None, None, json.loads(previous["response_json"])
            if row["lease_until"] > time.time():
                raise StoreError(409, "这段对话正在回复，请稍后再试")
            lease = secrets.token_hex(16)
            db.execute("UPDATE conversations SET lease_token=?,lease_until=? WHERE id=?",
                       (lease, time.time() + 900, conversation_id))
        state = json.loads(row["state_json"])
        if not state:
            # Rebuild a bounded working context after derived state was cleared.
            # Raw history remains visible until the user deletes that history.
            with self.connection() as db:
                history = db.execute("SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 20", (conversation_id,)).fetchall()
            if history:
                state["_history"] = [{"role": m["role"], "content": m["content"], "ts": m["created_at"]} for m in reversed(history)]
        return state, lease, None

    def finish_turn(self, user_id, conversation_id, lease, request_id, payload_hash, user_text, response, proactive=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned_conversation(db, user_id, conversation_id)
            if row["lease_token"] != lease or row["lease_until"] <= time.time():
                raise StoreError(409, "对话已发生变化，请重新发送")
            now = utc_now()
            if not proactive:
                db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?,'user',?,?)",
                           (conversation_id, user_text, now))
            if response["reply"]:
                db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES (?,'assistant',?,?)",
                           (conversation_id, response["reply"], now))
            title = user_text[:30] if row["title"] == "新对话" and not proactive else row["title"]
            db.execute("UPDATE conversations SET title=?,state_json=?,updated_at=?,lease_token=NULL,lease_until=0 WHERE id=?",
                       (title, json.dumps(response["state"], ensure_ascii=False), now, conversation_id))
            db.execute("INSERT INTO chat_requests VALUES (?,?,?,?)",
                       (conversation_id, request_id, payload_hash, json.dumps(response, ensure_ascii=False)))

    def release_turn(self, conversation_id, lease):
        with self.connection() as db:
            db.execute("UPDATE conversations SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?",
                       (conversation_id, lease))

    def memories(self, user_id):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM memories WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [{k: row[k] for k in ("id", "content", "created_at", "updated_at")} for row in rows]

    def save_memory(self, user_id, content, memory_id=None):
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory_id:
                if not db.execute("SELECT 1 FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone():
                    raise StoreError(404, "没有找到这条记忆")
                db.execute("UPDATE memories SET content=?,updated_at=? WHERE id=?", (content, now, memory_id))
                self.clear_derived_state(db, user_id)
            else:
                if db.execute("SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)).fetchone()[0] >= 100:
                    raise StoreError(400, "最多保存 100 条记忆，请先整理旧记录")
                memory_id = str(uuid.uuid4())
                db.execute("INSERT INTO memories VALUES (?,?,?,?,?)", (memory_id, user_id, content, now, now))
        return memory_id

    def delete_memory(self, user_id, memory_id):
        with self.connection() as db:
            deleted = db.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)).rowcount
            if not deleted:
                raise StoreError(404, "没有找到这条记忆")
            # Invalidate hidden state/cache too, so deleting a memory cannot
            # accidentally reintroduce it via an earlier generated state.
            self.clear_derived_state(db, user_id)

    @staticmethod
    def clear_derived_state(db, user_id):
        db.execute("UPDATE conversations SET state_json='{}',lease_token=NULL,lease_until=0 WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM chat_requests WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)", (user_id,))

    def clear_history(self, user_id):
        with self.connection() as db:
            db.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))

    def memory_context(self, user_id, conversation_id, query):
        """Bounded, source-backed recall, not a claim of semantic memory.

        Manual memories plus relevant prior user messages. Chinese bigrams and
        Latin words work without an external vector service. Ambiguous references
        fall back to recent messages from other conversations.
        """
        manual = self.memories(user_id)
        with self.connection() as db:
            rows = db.execute("""SELECT m.content,m.created_at,c.title,c.id conversation_id
                FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE c.user_id=? AND m.role='user' ORDER BY m.id DESC LIMIT 300""", (user_id,)).fetchall()
        tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", query.lower()))
        for run in re.findall(r"[\u4e00-\u9fff]+", query):
            tokens.update(run[i:i+2] for i in range(len(run)-1))
        generic = {"上次", "之前", "那件", "件事", "这个", "那个", "今天", "怎么", "什么"}
        tokens -= generic
        ranked = []
        for index, row in enumerate(rows):
            if row["content"].startswith("/"):
                continue
            score = sum(token in row["content"].lower() for token in tokens)
            # The ordinary state already has the latest 20 messages. Retrieve
            # other sessions or older matched messages rather than duplicate it.
            if score or row["conversation_id"] != conversation_id:
                ranked.append((score, -index, dict(row)))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        recent = [item[2] for item in ranked[:6]]
        selected = sorted(manual, key=lambda m: sum(t in m["content"].lower() for t in tokens), reverse=True)[:12]
        return json.dumps({"user_confirmed_memories": selected,
                           "previous_user_messages": recent}, ensure_ascii=False)

    def export(self, user_id):
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise StoreError(404, "没有找到这个用户")
            user = self.public_user(row)
            conversations = []
            for row in db.execute("SELECT id,title,created_at,updated_at FROM conversations WHERE user_id=? ORDER BY created_at", (user_id,)):
                item = dict(row)
                item["messages"] = [dict(m) for m in db.execute("SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id", (row["id"],))]
                conversations.append(item)
            memories = [dict(m) for m in db.execute("SELECT id,content,created_at,updated_at FROM memories WHERE user_id=? ORDER BY updated_at", (user_id,))]
        return {"exported_at": utc_now(), "user": user, "conversations": conversations, "memories": memories}

    def admin_sessions(self):
        with self.connection() as db:
            rows = db.execute("""SELECT c.*,u.username,u.display_name,
                (SELECT content FROM messages m WHERE m.conversation_id=c.id AND role='user' ORDER BY id DESC LIMIT 1) last_user_msg,
                (SELECT content FROM messages m WHERE m.conversation_id=c.id AND role='assistant' ORDER BY id DESC LIMIT 1) last_reply
                FROM conversations c JOIN users u ON c.user_id=u.id ORDER BY updated_at DESC LIMIT 100""").fetchall()
        result = []
        for row in rows:
            state = json.loads(row["state_json"])
            result.append({"session_id": row["id"], "username": row["username"], "display_name": row["display_name"],
                           "title": row["title"], "updated_at": row["updated_at"], "last_user_msg": row["last_user_msg"],
                           "last_reply": row["last_reply"], "state": state, **{k: state.get(k) for k in
                           ("turns", "goal", "risk_flags", "severity_score", "problem_category", "state_keywords", "current_strategy")}})
        return result

    def admin_users(self):
        with self.connection() as db:
            rows = db.execute("""SELECT u.id,u.username,u.display_name,u.created_at,
                (SELECT COUNT(*) FROM conversations c WHERE c.user_id=u.id) conversation_count
                FROM users u ORDER BY created_at DESC LIMIT 500""").fetchall()
        return [dict(row) for row in rows]
