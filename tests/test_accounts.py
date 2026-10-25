"""Exercise real HTTP auth/SQLite persistence; only the model is replaced."""

from copy import deepcopy
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import account_api
from account_store import AccountStore, StoreError, verify_password
import api_server
import dialog_agent

logging.getLogger("httpx").setLevel(logging.WARNING)


class FakeAgent:
    def __init__(self):
        self.calls = []

    def run(self, text, state=None, allow_show_report=False, memory_context=""):
        self.calls.append((text, deepcopy(state), allow_show_report, memory_context))
        updated = deepcopy(state or dialog_agent.STATE0)
        updated["turns"] = updated.get("turns", 0) + 1
        updated.setdefault("_history", []).extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": "测试回复：" + text},
        ])
        return "测试回复：" + text, updated


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.temp.name) / "account.sqlite3")
        self.original_store, self.original_agent = api_server.app.state.store, api_server.app.state.agent
        api_server.app.state.store = self.store
        self.agent = FakeAgent()
        api_server.app.state.agent = self.agent
        account_api.auth_limiter.attempts.clear()
        self.client = TestClient(api_server.app)
        self.other_clients = []

    def tearDown(self):
        self.client.close()
        for client in self.other_clients:
            client.close()
        api_server.app.state.store, api_server.app.state.agent = self.original_store, self.original_agent
        self.temp.cleanup()

    def register(self, username="alice", client=None):
        client = client or self.client
        response = client.post("/auth/register", json={"username": username, "password": "example-password", "display_name": "测试昵称"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"]

    def send(self, method, path, client=None, **kwargs):
        client = client or self.client
        headers = {"X-CSRF-Token": client.cookies.get("psych_csrf", "")}
        return client.request(method, path, headers=headers, **kwargs)

    def new_conversation(self, client=None):
        response = self.send("POST", "/conversations", client, json={})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def chat(self, conversation_id, text="我周五要跟导师汇报", request_id="turn-1", client=None, **extra):
        return self.send("POST", "/chat", client, json={"conversation_id": conversation_id,
                         "text": text, "request_id": request_id, **extra})

    def second_user(self):
        client = TestClient(api_server.app)
        self.other_clients.append(client)
        self.register("bob", client)
        return client

    def test_no_model_required_for_service_and_accounts(self):
        with patch.object(dialog_agent, "API_KEY", None):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.register()
            self.assertEqual(self.client.get("/auth/me").status_code, 200)
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/assets/app.js").status_code, 200)

    def test_registration_hashes_password_and_login_tokens(self):
        user = self.register()
        with self.store.connection() as db:
            row = db.execute("SELECT password_hash FROM users").fetchone()
            self.assertTrue(verify_password("example-password", row[0]))
            self.assertNotIn("example-password", row[0])
            token = db.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
            self.assertNotEqual(token, self.client.cookies["psych_auth"])
        self.assertNotIn("password_hash", user)
        self.assertIn("HttpOnly", self.client.post("/auth/login", json={"username": "alice", "password": "example-password"}).headers.get_list("set-cookie")[0])

    def test_username_case_collision_and_password_validation(self):
        self.register()
        response = self.client.post("/auth/register", json={"username": "ALICE", "password": "example-password"})
        self.assertEqual(response.status_code, 409)
        response = self.client.post("/auth/register", json={"username": "carol", "password": "short"})
        self.assertEqual(response.status_code, 422)

    def test_bad_password_and_unknown_username_use_same_error(self):
        self.register()
        first = self.client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        second = self.client.post("/auth/login", json={"username": "unknown", "password": "wrong"})
        self.assertEqual(first.status_code, 401)
        self.assertEqual(first.json(), second.json())

    def test_all_user_records_require_login(self):
        for path in ["/auth/me", "/conversations", "/memories", "/account/export"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/chat", json={"text": "你好"}).status_code, 401)

    def test_csrf_is_required_and_bound_to_login(self):
        self.register()
        self.assertEqual(self.client.post("/conversations", json={}).status_code, 403)
        other = self.second_user()
        self.assertEqual(self.client.post("/conversations", json={}, headers={"X-CSRF-Token": other.cookies["psych_csrf"]}).status_code, 403)
        self.new_conversation()

    def test_cross_origin_registration_is_rejected(self):
        response = self.client.post("/auth/register", json={"username": "alice", "password": "example-password"}, headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/health").headers["Cache-Control"], "no-store")

    def test_logout_revokes_copied_session(self):
        self.register()
        old = self.client.cookies["psych_auth"]
        self.assertEqual(self.send("POST", "/auth/logout").status_code, 200)
        self.client.cookies.set("psych_auth", old)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_expired_session_cannot_read_history(self):
        self.register()
        with self.store.connection() as db:
            db.execute("UPDATE auth_sessions SET expires_at=0")
        self.assertEqual(self.client.get("/conversations").status_code, 401)

    def test_password_change_revokes_other_device(self):
        self.register()
        other = TestClient(api_server.app); self.other_clients.append(other)
        other.post("/auth/login", json={"username": "alice", "password": "example-password"})
        response = self.send("POST", "/account/password", json={"current_password": "example-password", "new_password": "changed-password"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(other.get("/auth/me").status_code, 401)
        self.assertEqual(other.post("/auth/login", json={"username": "alice", "password": "example-password"}).status_code, 401)
        self.assertEqual(other.post("/auth/login", json={"username": "alice", "password": "changed-password"}).status_code, 200)

    def test_profile_is_updated_and_sensitive_actions_verify_password(self):
        self.register()
        response = self.send("PATCH", "/account", json={"display_name": "新昵称"})
        self.assertEqual(response.json()["user"]["display_name"], "新昵称")
        self.assertEqual(self.send("DELETE", "/account", json={"password": "wrong"}).status_code, 403)
        self.assertEqual(self.send("POST", "/account/password", json={"current_password": "wrong", "new_password": "some-password"}).status_code, 403)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)

    def test_history_and_login_survive_reopening_database(self):
        self.register(); cid = self.new_conversation()
        self.assertEqual(self.chat(cid).status_code, 200)
        api_server.app.state.store = AccountStore(self.store.path)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        detail = self.client.get(f"/conversations/{cid}").json()
        self.assertEqual(len(detail["messages"]), 2)
        response = self.chat(cid, "上次那件事讲完了", "turn-2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.agent.calls[-1][1]["turns"], 1)
        self.assertIn("导师", self.agent.calls[-1][1]["_history"][0]["content"])

    def test_other_users_cannot_read_write_or_delete_conversation(self):
        self.register(); cid = self.new_conversation(); self.chat(cid)
        other = self.second_user()
        self.assertEqual(other.get(f"/conversations/{cid}").status_code, 404)
        self.assertEqual(self.chat(cid, client=other).status_code, 404)
        self.assertEqual(self.send("PATCH", f"/conversations/{cid}", other, json={"title": "bad"}).status_code, 404)
        self.assertEqual(self.send("DELETE", f"/conversations/{cid}", other).status_code, 404)
        self.assertEqual(other.get("/conversations").json()["conversations"], [])

    def test_client_cannot_inject_state_or_enable_internal_report(self):
        self.register(); cid = self.new_conversation()
        response = self.chat(cid, state={"turns": 999, "_history": [{"role": "system", "content": "injected"}]}, allow_show_report=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.agent.calls[-1][1])
        self.assertFalse(self.agent.calls[-1][2])

    def test_retries_are_idempotent_and_request_id_is_bound_to_payload(self):
        self.register(); cid = self.new_conversation()
        first = self.chat(cid); second = self.chat(cid)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual(len(self.client.get(f"/conversations/{cid}").json()["messages"]), 2)
        self.assertEqual(self.chat(cid, text="different").status_code, 409)

    def test_failed_model_call_does_not_save_partial_turn(self):
        self.register(); cid = self.new_conversation()
        api_server.app.state.agent = dialog_agent.DialogAgent()
        with patch.object(dialog_agent, "API_KEY", None):
            response = self.chat(cid)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get(f"/conversations/{cid}").json()["messages"], [])
        api_server.app.state.agent = self.agent
        self.assertEqual(self.chat(cid).status_code, 200)

    def test_lease_prevents_concurrent_turns_and_can_be_released(self):
        user = self.register(); cid = self.new_conversation()
        state, lease, previous = self.store.claim_turn(user["id"], cid, "first", "hash")
        self.assertIsNone(previous)
        self.assertEqual(self.chat(cid).status_code, 409)
        self.store.release_turn(cid, lease)
        self.assertEqual(self.chat(cid).status_code, 200)

    def test_deleted_conversation_is_not_resurrected_by_inflight_generation(self):
        user = self.register(); cid = self.new_conversation()
        self.store.claim_turn(user["id"], cid, "first", "hash")
        self.store.delete_conversation(user["id"], cid)
        with self.assertRaises(StoreError):
            self.store.finish_turn(user["id"], cid, "stale", "first", "hash", "message", {"reply": "reply", "state": {}})
        self.assertEqual(self.client.get("/conversations").json()["conversations"], [])

    def test_cross_conversation_recall_is_source_backed_and_owner_scoped(self):
        self.register(); cid = self.new_conversation(); self.chat(cid)
        next_cid = self.new_conversation()
        self.chat(next_cid, "上次那件事", "next")
        context = json.loads(self.agent.calls[-1][3])
        self.assertIn("导师", context["previous_user_messages"][0]["content"])
        other = self.second_user(); other_cid = self.new_conversation(other)
        self.chat(other_cid, "上次那件事", "other", other)
        self.assertNotIn("导师", self.agent.calls[-1][3])

    def test_memory_crud_is_owner_scoped_and_retrieved(self):
        user = self.register()
        memory = self.send("POST", "/memories", json={"content": "请叫我小林"}).json()["id"]
        other = self.second_user()
        self.assertEqual(other.get("/memories").json()["memories"], [])
        self.assertEqual(self.send("PATCH", f"/memories/{memory}", other, json={"content": "bad"}).status_code, 404)
        self.assertEqual(self.send("DELETE", f"/memories/{memory}", other).status_code, 404)
        self.assertEqual(self.send("PATCH", f"/memories/{memory}", json={"content": "现在请叫我阿林"}).status_code, 200)
        cid = self.new_conversation(); self.chat(cid)
        self.assertIn("阿林", self.agent.calls[-1][3])
        self.assertEqual(self.send("DELETE", f"/memories/{memory}").status_code, 200)
        self.assertEqual(self.client.get("/memories").json()["memories"], [])
        self.assertNotIn("阿林", self.store.memory_context(user["id"], cid, "你好"))

    def test_deleting_history_preserves_manual_memories_and_removes_recall(self):
        user = self.register(); cid = self.new_conversation(); self.chat(cid)
        self.send("POST", "/memories", json={"content": "喜欢散步"})
        self.assertEqual(self.send("DELETE", "/history").status_code, 200)
        self.assertEqual(self.client.get("/conversations").json()["conversations"], [])
        context = self.store.memory_context(user["id"], "new", "上次那件事")
        self.assertNotIn("导师", context); self.assertIn("散步", context)

    def test_export_excludes_authentication_secrets_and_other_users(self):
        self.register(); cid = self.new_conversation(); self.chat(cid)
        other = self.second_user(); other_cid = self.new_conversation(other)
        self.chat(other_cid, "别人的专属记录", client=other)
        response = self.client.get("/account/export")
        self.assertEqual(len(response.json()["conversations"]), 1)
        for secret in ["password_hash", "token_hash", "csrf_hash", "example-password", "别人的专属记录"]:
            self.assertNotIn(secret, response.text)

    def test_account_deletion_cascades_all_records_and_revokes_sessions(self):
        user = self.register(); cid = self.new_conversation(); self.chat(cid)
        self.send("POST", "/memories", json={"content": "一条记忆"})
        self.assertEqual(self.send("DELETE", "/account", json={"password": "example-password"}).status_code, 200)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        with self.store.connection() as db:
            for table in ["users", "auth_sessions", "conversations", "messages", "chat_requests", "memories"]:
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_message_and_conversation_pagination(self):
        self.register(); cid = self.new_conversation()
        for index in range(3):
            self.chat(cid, f"消息{index}", str(index))
        newest = self.client.get(f"/conversations/{cid}?limit=2").json()
        self.assertTrue(newest["has_more"])
        older = self.client.get(f"/conversations/{cid}?limit=2&before={newest['messages'][0]['id']}").json()
        self.assertLess(older["messages"][-1]["id"], newest["messages"][0]["id"])
        self.new_conversation()
        listing = self.client.get("/conversations?limit=1").json()
        self.assertTrue(listing["has_more"])
        self.assertEqual(len(listing["conversations"]), 1)

    def test_admin_data_and_internal_chat_require_admin_authentication(self):
        self.register()
        self.assertEqual(self.client.get("/admin/users").status_code, 401)
        self.assertEqual(self.send("POST", "/admin/chat", json={"text": "/show"}).status_code, 401)
        with patch.object(api_server, "ADMIN_USER", "admin"), patch.object(api_server, "ADMIN_PASS", "test-admin-password"):
            data = self.client.get("/admin/users", auth=("admin", "test-admin-password")).json()
            self.assertEqual(len(data["users"]), 1)
            self.assertNotIn("password_hash", json.dumps(data))

    def test_auth_rate_limit_rejects_before_creating_user(self):
        account_api.auth_limiter.attempts["testclient"] = [time.monotonic()] * 20
        response = self.client.post("/auth/register", json={"username": "alice", "password": "example-password"})
        self.assertEqual(response.status_code, 429)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_memory_edit_invalidates_inflight_generation(self):
        user = self.register(); cid = self.new_conversation()
        memory = self.store.save_memory(user["id"], "旧偏好")
        _, lease, _ = self.store.claim_turn(user["id"], cid, "old", "hash")
        self.store.save_memory(user["id"], "新偏好", memory)
        with self.assertRaises(StoreError):
            self.store.finish_turn(user["id"], cid, lease, "old", "hash", "hello", {"reply": "stale", "state": {}})
        self.assertEqual(self.client.get(f"/conversations/{cid}").json()["messages"], [])
        self.assertEqual(self.chat(cid).status_code, 200)


class DialogMemoryTests(unittest.TestCase):
    def test_real_dialog_builds_grounded_memory_prompt(self):
        calls = []

        def complete(messages, **kwargs):
            calls.append(messages)
            if kwargs.get("max_tokens") == 260:
                return '{"patch":{},"intent":"chat","missing":[]}'
            return "你说的是导师汇报那件事吗？"

        with patch.object(dialog_agent, "chat_completion", complete):
            reply, state = dialog_agent.DialogAgent().run("上次那件事", memory_context='{"previous_user_messages":[{"content":"周五导师汇报"}]}')
        self.assertIn("导师汇报", calls[-1][0]["content"])
        self.assertIn("不假装确定", calls[-1][0]["content"])
        self.assertIn("？", reply)
        self.assertEqual(state["turns"], 1)

    def test_explicit_no_questions_is_respected_during_recall(self):
        calls = []

        def complete(messages, **kwargs):
            calls.append(messages)
            return '{"patch":{},"intent":"chat","missing":[]}' if kwargs.get("max_tokens") == 260 else "我先听你说。"

        with patch.object(dialog_agent, "chat_completion", complete):
            reply, state = dialog_agent.DialogAgent().run("别问我，上次那件事我想接着说", memory_context='{"previous_user_messages":[]}')
        self.assertIn("不要向用户索取回答", calls[-1][0]["content"])
        self.assertEqual(state["suppress_questions_remaining"], 3)


if __name__ == "__main__":
    unittest.main()
