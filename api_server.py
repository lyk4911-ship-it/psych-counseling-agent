# -*- coding: utf-8 -*-
"""Account-owned persistent chat API and separate administrator tools."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from account_api import User, get_store, router as account_router
from account_store import AccountStore, StoreError
from dialog_agent import DialogAgent, ModelServiceError, PROACTIVE_MARKER, API_KEY
from show_memory import append_show_report, read_all_entries


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
TESTER_TOKEN = (os.getenv("TESTER_TOKEN") or "").strip()
ADMIN_USER = (os.getenv("ADMIN_USER") or "").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "").strip()
_security = HTTPBasic()


def _require_admin(credentials: HTTPBasicCredentials = Depends(_security)):
    if not ADMIN_USER or not ADMIN_PASS:
        raise HTTPException(503, "管理员账号未配置，请设置 ADMIN_USER / ADMIN_PASS")
    valid_user = secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
    valid_password = secrets.compare_digest(credentials.password.encode(), ADMIN_PASS.encode())
    if not (valid_user and valid_password):
        raise HTTPException(401, "认证失败", headers={"WWW-Authenticate": "Basic"})


class ChatRequest(BaseModel):
    text: str = Field(default="", max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=64)
    session_id: Optional[str] = Field(default=None, max_length=64)
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    # Legacy tester fields are accepted, but public /chat never trusts them.
    state: Optional[Dict[str, Any]] = None
    allow_show_report: bool = False
    tester_token: Optional[str] = Field(default=None, max_length=256)
    memory_user_label: Optional[str] = Field(default=None, max_length=60)
    proactive_ping: bool = False


class ChatResponse(BaseModel):
    reply: str
    state: Dict[str, Any]
    session_id: str
    conversation_id: str
    proactive_skipped: bool = False


app = FastAPI(title="Psych Companion API", version="0.2.0")
app.state.store = AccountStore()
app.state.agent = DialogAgent()
app.include_router(account_router)
_ROOT = Path(__file__).resolve().parent
app.mount("/assets", StaticFiles(directory=_ROOT / "web"), name="assets")

# Serve the browser UI from this server. Explicit CORS origins are optional for
# other clients; credentialed wildcard origins are intentionally unsupported.
_cors_origins = [value.strip().rstrip("/") for value in os.getenv("CORS_ORIGINS", "").split(",") if value.strip() and value.strip() != "*"]
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-CSRF-Token"])


@app.middleware("http")
async def browser_security(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin.rstrip("/") not in [expected, *_cors_origins]:
            return JSONResponse({"detail": "不允许来自其他站点的请求"}, status_code=403)
        try:
            if int(request.headers.get("content-length", "0")) > 512_000:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "无效的请求长度"}, status_code=400)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(StoreError)
async def store_error(request: Request, exc: StoreError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(ModelServiceError)
async def model_error(request: Request, exc: ModelServiceError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse((_ROOT / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
def health(store=Depends(get_store)):
    with store.connection() as db:
        db.execute("SELECT 1")
    return {"status": "healthy", "model_configured": bool(API_KEY)}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User, request: Request, store=Depends(get_store)):
    if req.conversation_id and req.session_id and req.conversation_id != req.session_id:
        raise HTTPException(422, "对话编号不一致")
    conversation_id = req.conversation_id or req.session_id
    if not conversation_id:
        conversation_id = store.create_conversation(user["id"])["id"]
    user_text = PROACTIVE_MARKER if req.proactive_ping or req.text.strip() == PROACTIVE_MARKER else req.text.strip()
    request_id = req.request_id or str(uuid.uuid4())
    payload_hash = hashlib.sha256(json.dumps({"text": user_text}, ensure_ascii=False).encode()).hexdigest()
    state, lease, previous = store.claim_turn(user["id"], conversation_id, request_id, payload_hash)
    if previous is not None:
        return previous
    try:
        # Hidden state comes only from this user's database, never req.state.
        memory_context = store.memory_context(user["id"], conversation_id, user_text)
        reply, new_state = request.app.state.agent.run(user_text, state or None,
                                                      allow_show_report=False,
                                                      memory_context=memory_context)
        response = ChatResponse(reply=reply or "", state=new_state, session_id=conversation_id,
                                conversation_id=conversation_id,
                                proactive_skipped=not bool((reply or "").strip()) and user_text == PROACTIVE_MARKER)
        store.finish_turn(user["id"], conversation_id, lease, request_id, payload_hash, user_text,
                          response.model_dump(), proactive=user_text == PROACTIVE_MARKER)
        return response
    finally:
        store.release_turn(conversation_id, lease)


# Administrator tests remain stateless and explicitly separated from user data.
@app.post("/admin/chat", response_model=ChatResponse)
def admin_chat(req: ChatRequest, request: Request, _=Depends(_require_admin)):
    allow_show = req.allow_show_report and (not TESTER_TOKEN or secrets.compare_digest((req.tester_token or "").encode(), TESTER_TOKEN.encode()))
    session_id = req.session_id or str(uuid.uuid4())
    reply, state = request.app.state.agent.run(req.text, req.state, allow_show_report=allow_show)
    if allow_show and req.text.strip().lower().startswith("/show"):
        append_show_report(req.memory_user_label or "", session_id, reply)
    return ChatResponse(reply=reply or "", state=state, session_id=session_id, conversation_id=session_id)


@app.get("/admin/users")
def admin_users(_=Depends(_require_admin), store=Depends(get_store)):
    return {"users": store.admin_users()}


@app.get("/admin/users/{user_id}/export")
def admin_user_export(user_id: str, _=Depends(_require_admin), store=Depends(get_store)):
    return store.export(user_id)


# ======================================================
# =============== 管理员实时观察界面 ====================
# ======================================================

@app.get("/admin/sessions")
def admin_list_sessions(_=Depends(_require_admin), store=Depends(get_store)):
    """获取所有会话摘要（供管理员轮询或首次加载）"""
    return {"sessions": store.admin_sessions()}


@app.get("/admin/memory")
def admin_memory_list(_=Depends(_require_admin)):
    """长期记忆库：仅含历次 /show 报告（JSON）"""
    return {"entries": read_all_entries()}


@app.get("/admin/memory-page", response_class=HTMLResponse)
def admin_memory_page(_=Depends(_require_admin)):
    """长期记忆库浏览页（仅 /show 内容）"""
    return _MEMORY_HTML

@app.get("/admin/stream")
async def admin_stream(request: Request, _=Depends(_require_admin), store=Depends(get_store)):
    """Poll persistent state so restarts and multiple workers stay consistent."""
    async def event_stream():
        previous = None
        while not await request.is_disconnected():
            rows = await run_in_threadpool(store.admin_sessions)
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            if signature != previous:
                yield 'data: {"changed":true}\n\n'
                previous = signature
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(_=Depends(_require_admin)):
    """管理员实时观察界面"""
    return _ADMIN_HTML


@app.get("/tester", response_class=HTMLResponse)
def tester_dashboard(_=Depends(_require_admin)):
    """测试者后台：可输入 /show 生成咨询师报告；用户端无此权限"""
    return _TESTER_HTML


_ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>管理员 - 用户状态实时观察</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 16px; background: #1a1a2e; color: #eee; }
    h1 { margin: 0 0 16px; font-size: 1.25rem; }
    .status { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 12px; margin-left: 8px; }
    .status.live { background: #22c55e; color: #fff; }
    .status.off { background: #6b7280; color: #fff; }
    .sessions { display: grid; gap: 12px; }
    .card { background: #16213e; border-radius: 12px; padding: 16px; border: 1px solid #2d3748; }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .card-id { font-family: monospace; font-size: 11px; color: #94a3b8; }
    .card-time { font-size: 12px; color: #64748b; }
    .risk { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
    .risk.green { background: #14532d; color: #86efac; }
    .risk.yellow { background: #713f12; color: #fde047; }
    .risk.red { background: #7f1d1d; color: #fca5a5; }
    .meta { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; font-size: 13px; }
    .meta span { background: #0f172a; padding: 4px 8px; border-radius: 6px; }
    .msg { font-size: 12px; color: #94a3b8; margin-top: 8px; max-height: 60px; overflow: hidden; text-overflow: ellipsis; }
    .empty { text-align: center; color: #64748b; padding: 40px; }
  </style>
</head>
<body>
  <h1>用户状态实时观察 <span id="status" class="status off">连接中</span></h1>
  <p style="font-size:13px;margin:0 0 12px"><a href="/admin/memory-page" style="color:#60a5fa">长期记忆库（/show）</a> · <a href="/tester" style="color:#60a5fa">测试者后台</a></p>
  <div id="sessions" class="sessions"></div>
  <h2 style="font-size:1rem;margin-top:24px">注册账户</h2>
  <div id="users" class="sessions"></div>

  <script>
    const sessionsEl = document.getElementById("sessions");
    const statusEl = document.getElementById("status");

    function escapeHtml(value) {
      const element = document.createElement("span");
      element.textContent = String(value == null ? "" : value);
      return element.innerHTML;
    }

    function renderSession(raw) {
      const s = Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)]));
      const risk = ["green", "yellow", "red"].includes(raw.risk_flags) ? raw.risk_flags : "green";
      return `
        <div class="card">
          <div class="card-header">
            <span class="card-id">${s.display_name} · ${s.username} · ${s.title} · ${(s.session_id || "").slice(0, 8)}...</span>
            <span class="card-time">${(s.updated_at || "").replace("T", " ").slice(0, 19)}</span>
          </div>
          <div class="meta">
            <span>轮次: ${s.turns || 0}</span>
            <span class="risk ${risk}">风险: ${s.risk_flags || "green"}</span>
            ${s.goal ? `<span>目标: ${s.goal}</span>` : ""}
            ${s.problem_category ? `<span>类别: ${s.problem_category}</span>` : ""}
            ${s.severity_score != null ? `<span>程度: ${s.severity_score}</span>` : ""}
            ${(raw.state_keywords || []).length ? `<span>关键词: ${escapeHtml(raw.state_keywords.join(", "))}</span>` : ""}
          </div>
          ${s.last_user_msg ? `<div class="msg">用户: ${s.last_user_msg}</div>` : ""}
          ${raw.current_strategy ? `<div class="msg">当前策略: ${escapeHtml(raw.current_strategy.strategy_id || JSON.stringify(raw.current_strategy))}</div>` : ""}
        </div>
      `;
    }

    function render(sessions) {
      if (!sessions || sessions.length === 0) {
        sessionsEl.innerHTML = '<div class="empty">暂无活跃会话，等待用户接入...</div>';
        return;
      }
      sessionsEl.innerHTML = sessions.map(renderSession).join("");
    }

    async function fetchSessions() {
      try {
        const r = await fetch("/admin/sessions");
        const d = await r.json();
        render(d.sessions || []);
        const usersResponse = await fetch("/admin/users");
        const usersData = await usersResponse.json();
        const usersEl = document.getElementById("users");
        usersEl.replaceChildren();
        for (const user of usersData.users || []) {
          const card = document.createElement("div");
          card.className = "card";
          const label = document.createElement("p");
          label.textContent = `${user.display_name} · ${user.username} · ${user.conversation_count} 段对话`;
          const exportLink = document.createElement("a");
          exportLink.href = `/admin/users/${encodeURIComponent(user.id)}/export`;
          exportLink.textContent = "查看 / 导出账户记录";
          card.append(label, exportLink);
          usersEl.appendChild(card);
        }
      } catch (e) {
        console.error(e);
      }
    }

    function connectSSE() {
      const es = new EventSource("/admin/stream");
      es.onopen = () => { statusEl.textContent = "实时"; statusEl.className = "status live"; };
      es.onerror = () => { statusEl.textContent = "断开"; statusEl.className = "status off"; es.close(); setTimeout(connectSSE, 3000); };
      es.onmessage = (e) => {
        try {
          const s = JSON.parse(e.data);
          fetchSessions();
        } catch (_) {}
      };
    }

    fetchSessions();
    connectSSE();
    setInterval(fetchSessions, 5000);
  </script>
</body>
</html>
"""

_MEMORY_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>长期记忆库（/show 报告）</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; padding: 16px; background: #0f172a; color: #e2e8f0; }
    h1 { font-size: 1.1rem; }
    .hint { color: #94a3b8; font-size: 13px; margin-bottom: 16px; }
    .card { background: #1e293b; border-radius: 8px; padding: 12px; margin-bottom: 12px; border: 1px solid #334155; }
    .meta { font-size: 12px; color: #94a3b8; margin-bottom: 8px; }
    .report { white-space: pre-wrap; font-size: 13px; line-height: 1.5; color: #cbd5e1; max-height: 400px; overflow-y: auto; }
    a { color: #60a5fa; }
  </style>
</head>
<body>
  <h1>长期记忆库</h1>
  <p class="hint">仅保存测试者执行 <code>/show</code> 时生成的咨询师报告；不保存普通聊天。数据文件：<code>logs/show_memory.jsonl</code></p>
  <p><a href="/admin">← 管理员观察</a> · <a href="/tester">测试者后台</a></p>
  <div id="list"></div>
  <script>
    async function load() {
      const r = await fetch("/admin/memory");
      const d = await r.json();
      const entries = (d.entries || []).slice().reverse();
      const el = document.getElementById("list");
      if (!entries.length) {
        el.innerHTML = "<p class=hint>暂无记录</p>";
        return;
      }
      el.innerHTML = entries.map(e => `
        <div class="card">
          <div class="meta">${e.ts || ""} · ${escapeHtml(e.user_label || "")} · session ${(e.session_id || "").slice(0,8)}…</div>
          <div class="report">${escapeHtml(e.report || "")}</div>
        </div>
      `).join("");
    }
    function escapeHtml(s) {
      const d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    }
    load();
    setInterval(load, 30000);
  </script>
</body>
</html>
"""

_TESTER_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>测试者后台</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; padding: 16px; background: #0f172a; color: #e2e8f0; }
    h1 { font-size: 1.1rem; margin: 0 0 8px; }
    .hint { font-size: 13px; color: #94a3b8; margin-bottom: 16px; }
    .warn { background: #422006; color: #fde68a; padding: 8px 12px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; }
    label { display: block; font-size: 12px; color: #94a3b8; margin-bottom: 4px; }
    input, textarea { width: 100%; max-width: 640px; padding: 8px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
    textarea { min-height: 72px; font-family: monospace; font-size: 12px; }
    .messages { max-height: 320px; overflow-y: auto; margin: 12px 0; padding: 8px; background: #1e293b; border-radius: 8px; }
    .msg { margin: 6px 0; font-size: 14px; white-space: pre-wrap; }
    .msg.user { color: #93c5fd; }
    .msg.assistant { color: #cbd5e1; }
    .row { display: flex; gap: 8px; align-items: center; margin-top: 8px; flex-wrap: wrap; }
    button { padding: 8px 16px; border-radius: 8px; border: none; background: #3b82f6; color: #fff; cursor: pointer; }
    button:disabled { opacity: 0.5; }
    a { color: #60a5fa; }
  </style>
</head>
<body>
  <h1>测试者后台</h1>
  <p class="hint">此处可发送 <code>/show</code> 生成咨询师结构化报告（用户聊天页已禁止该指令）。若服务端配置了 <code>TESTER_TOKEN</code>，请在下方填写。</p>
  <div class="warn">请勿向最终用户公开本页面地址；生产环境务必设置环境变量 <code>TESTER_TOKEN</code>。</div>
  <label>TESTER_TOKEN（可选，与服务端 .env 一致）</label>
  <input type="password" id="testerToken" placeholder="未配置则可留空" autocomplete="off" />
  <label style="margin-top:12px">长期记忆标签（生成 /show 时写入）：填「用户3」或昵称</label>
  <input type="text" id="memoryUserLabel" placeholder="例如：用户1、张三（会存入 logs/show_memory.jsonl）" />
  <label style="margin-top:12px">可选：粘贴用户端 state（JSON），用于接续同一会话</label>
  <textarea id="stateJson" placeholder='留空则从空画像开始；可从浏览器 Network 响应里复制 state'></textarea>
  <div class="row">
    <button type="button" id="applyState">应用 state</button>
    <a href="/admin">← 管理员观察</a>
    <a href="/admin/memory-page">长期记忆库</a>
  </div>
  <div class="messages" id="messages"></div>
  <label>输入（可输入 /show）</label>
  <div class="row">
    <input id="input" style="flex:1;min-width:200px" placeholder="输入消息或 /show" />
    <button id="send">发送</button>
  </div>
  <script>
    const base = "";
    let agentState = null;
    let sessionId = localStorage.getItem("psych_tester_session") || (crypto.randomUUID?.() || "t-" + Date.now());
    localStorage.setItem("psych_tester_session", sessionId);

    const messagesEl = document.getElementById("messages");
    const inputEl = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const tokenEl = document.getElementById("testerToken");
    const memoryLabelEl = document.getElementById("memoryUserLabel");
    memoryLabelEl.value = localStorage.getItem("psych_memory_label") || "";
    memoryLabelEl.addEventListener("change", () => localStorage.setItem("psych_memory_label", memoryLabelEl.value));

    function addMessage(role, text) {
      const d = document.createElement("div");
      d.className = "msg " + role;
      d.textContent = (role === "user" ? "测试者: " : "助手: ") + text;
      messagesEl.appendChild(d);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    document.getElementById("applyState").onclick = () => {
      const raw = document.getElementById("stateJson").value.trim();
      if (!raw) { agentState = null; addMessage("assistant", "已清空 state"); return; }
      try {
        agentState = JSON.parse(raw);
        addMessage("assistant", "已应用 state");
      } catch (e) {
        alert("JSON 解析失败");
      }
    };

    async function sendMessage() {
      const text = inputEl.value.trim();
      if (!text) return;
      addMessage("user", text);
      inputEl.value = "";
      sendBtn.disabled = true;
      const body = {
        text,
        state: agentState,
        session_id: sessionId,
        allow_show_report: true,
        tester_token: tokenEl.value.trim() || null,
        memory_user_label: memoryLabelEl.value.trim() || null
      };
      try {
        const resp = await fetch(base + "/admin/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        agentState = data.state;
        if (data.session_id) sessionId = data.session_id;
        addMessage("assistant", data.reply || "");
      } catch (e) {
        console.error(e);
        addMessage("assistant", "请求失败: " + e.message);
      } finally {
        sendBtn.disabled = false;
        inputEl.focus();
      }
    }

    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
