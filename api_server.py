# api_server.py —— 把 dialog_agent 封成一个 HTTP 接口（干净版）
# -*- coding: utf-8 -*-

import os
import uvicorn
import traceback
import asyncio
import json
import uuid
from datetime import datetime
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import secrets
import logging

from dialog_agent import DialogAgent, log_turn, PROACTIVE_MARKER
from show_memory import append_show_report, read_all_entries

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 若设置，则仅当请求中 tester_token 与本值一致时，才允许 allow_show_report（生产环境建议必设）
TESTER_TOKEN = (os.getenv("TESTER_TOKEN") or "").strip()

# 管理员后台 HTTP Basic Auth（必须在 .env 中设置，否则拒绝访问）
ADMIN_USER = (os.getenv("ADMIN_USER") or "").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "").strip()

_security = HTTPBasic()

def _require_admin(credentials: HTTPBasicCredentials = Depends(_security)):
    if not ADMIN_USER or not ADMIN_PASS:
        raise HTTPException(status_code=503, detail="管理员账号未配置，请在 .env 设置 ADMIN_USER / ADMIN_PASS")
    ok = (
        secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
        and secrets.compare_digest(credentials.password.encode(), ADMIN_PASS.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )

class ChatRequest(BaseModel):
    text: str = ""
    state: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None  # 会话ID，前端生成并持久化，用于管理员追踪
    allow_show_report: bool = False  # 仅测试者后台应传 True；用户端须为 False
    tester_token: Optional[str] = None  # 与 .env 中 TESTER_TOKEN 一致时才允许生成 /show 报告
    # 长期记忆库：仅在与 /show 报告一并写入；填「用户3」或昵称等，便于检索
    memory_user_label: Optional[str] = None
    # 为 True 时表示「页面空闲触发的主动一句」，服务端映射为内部标记，不当作用户正文
    proactive_ping: bool = False


def _effective_allow_show(req: ChatRequest) -> bool:
    if not req.allow_show_report:
        return False
    if not TESTER_TOKEN:
        return True
    return (req.tester_token or "").strip() == TESTER_TOKEN

class ChatResponse(BaseModel):
    reply: str
    state: Dict[str, Any]
    session_id: str  # 返回会话ID（若请求未带则返回新生成的）
    proactive_skipped: bool = False  # 主动一句因冷却等原因未生成时为 True

# 会话存储：供管理员实时观察（最多保留100个会话，LRU淘汰）
SESSION_STORE: OrderedDict = OrderedDict()
SESSION_STORE_MAX = 100

# SSE 订阅者队列：用于实时推送会话更新
_admin_subscribers: List[asyncio.Queue] = []

def _broadcast_session_update(update: dict):
    """向所有已连接的管理员推送会话更新"""
    for q in _admin_subscribers:
        try:
            q.put_nowait(update)
        except asyncio.QueueFull:
            pass

def _update_session_store(session_id: str, user_text: str, new_state: dict, reply: str):
    """更新会话存储并广播"""
    s = new_state or {}
    summary = {
        "session_id": session_id,
        "last_user_msg": (user_text or "")[:80],
        "last_reply": (reply or "")[:80],
        "turns": s.get("turns", 0),
        "goal": s.get("goal"),
        "problem_category": s.get("problem_category"),
        "severity_score": s.get("severity_score"),
        "risk_flags": s.get("risk_flags"),
        "state_keywords": (s.get("state_keywords") or [])[:5],
        "current_strategy": s.get("current_strategy"),
        "phase": s.get("phase"),
        "updated_at": datetime.now().isoformat(),
        "state": s,  # 完整状态供管理员查看
    }
    SESSION_STORE[session_id] = summary
    SESSION_STORE.move_to_end(session_id)
    if len(SESSION_STORE) > SESSION_STORE_MAX:
        SESSION_STORE.popitem(last=False)
    _broadcast_session_update(summary)

app = FastAPI(title="Psych Agent API", version="0.1.0")

# CORS：先全开放，后面可以收紧到你的前端域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = DialogAgent()

_FRONTEND_HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")

@app.get("/", response_class=HTMLResponse)
def root():
    """直接提供前端页面，用户访问根路径即可使用"""
    try:
        with open(_FRONTEND_HTML_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        # 将前端 API 地址替换为相对路径，避免用户手动配置
        html = html.replace(
            'return "http://127.0.0.1:8000/chat";',
            'return location.origin + "/chat";'
        )
        return html
    except FileNotFoundError:
        return HTMLResponse('<p>index.html 未找到，请确认文件存在于服务根目录。</p>', status_code=500)

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    处理用户对话请求
    - 添加异常处理和错误日志
    - 自动记录对话日志
    - 更新会话存储供管理员观察
    """
    try:
        if req.proactive_ping or (req.text or "").strip() == PROACTIVE_MARKER:
            user_text = PROACTIVE_MARKER
        else:
            user_text = req.text or ""
        user_state = req.state or None
        session_id = req.session_id or str(uuid.uuid4())
        allow_show = _effective_allow_show(req)
        if req.allow_show_report and not allow_show:
            logger.warning("拒绝无效的 allow_show_report（tester_token 不匹配或未提供）")

        # 调用对话引擎（用户端不传 allow_show_report，无法使用 /show）
        reply, new_state = agent.run(
            user_text,
            user_state,
            allow_show_report=allow_show,
        )
        
        # 记录对话日志
        try:
            log_turn(user_text, new_state, reply)
        except Exception as log_err:
            logger.warning(f"日志记录失败: {log_err}", exc_info=True)
        
        # 更新会话存储并推送给管理员
        try:
            _update_session_store(session_id, user_text, new_state, reply)
        except Exception as store_err:
            logger.warning(f"会话存储失败: {store_err}", exc_info=True)

        # 长期记忆库：仅持久化 /show 报告全文（不存普通聊天）
        if (
            allow_show
            and (user_text or "").strip().lower().startswith("/show")
            and "该指令为内部测试功能" not in (reply or "")
        ):
            try:
                append_show_report(
                    (req.memory_user_label or "").strip(),
                    session_id,
                    reply or "",
                )
            except Exception as mem_err:
                logger.warning(f"长期记忆写入失败: {mem_err}", exc_info=True)

        proactive_skipped = (not (reply or "").strip()) and user_text == PROACTIVE_MARKER
        return ChatResponse(
            reply=reply or "",
            state=new_state,
            session_id=session_id,
            proactive_skipped=proactive_skipped,
        )
        
    except ValueError as e:
        # 配置错误等业务异常
        logger.error(f"业务错误: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"请求参数错误: {str(e)}")
        
    except Exception as e:
        # 未知异常，记录详细错误信息
        error_detail = traceback.format_exc()
        logger.error(f"处理请求时发生异常: {e}\n{error_detail}", exc_info=True)
        
        # 返回友好的错误信息，不暴露内部细节
        raise HTTPException(
            status_code=500,
            detail="服务暂时不可用，请稍后重试。如问题持续，请联系管理员。"
        )

# ======================================================
# =============== 管理员实时观察界面 ====================
# ======================================================

@app.get("/admin/sessions")
def admin_list_sessions(_=Depends(_require_admin)):
    """获取所有会话摘要（供管理员轮询或首次加载）"""
    return {"sessions": list(SESSION_STORE.values())}


@app.get("/admin/memory")
def admin_memory_list(_=Depends(_require_admin)):
    """长期记忆库：仅含历次 /show 报告（JSON）"""
    return {"entries": read_all_entries()}


@app.get("/admin/memory-page", response_class=HTMLResponse)
def admin_memory_page(_=Depends(_require_admin)):
    """长期记忆库浏览页（仅 /show 内容）"""
    return _MEMORY_HTML

@app.get("/admin/stream")
async def admin_stream(_=Depends(_require_admin)):
    """SSE 实时推送：每当有会话更新时推送给管理员"""
    async def event_stream():
        queue = asyncio.Queue()
        _admin_subscribers.append(queue)
        try:
            while True:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(update, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in _admin_subscribers:
                _admin_subscribers.remove(queue)

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

  <script>
    const sessionsEl = document.getElementById("sessions");
    const statusEl = document.getElementById("status");

    function renderSession(s) {
      const risk = (s.risk_flags || "green").toLowerCase();
      return `
        <div class="card">
          <div class="card-header">
            <span class="card-id">${(s.session_id || "").slice(0, 8)}...</span>
            <span class="card-time">${(s.updated_at || "").replace("T", " ").slice(0, 19)}</span>
          </div>
          <div class="meta">
            <span>轮次: ${s.turns || 0}</span>
            <span class="risk ${risk}">风险: ${s.risk_flags || "green"}</span>
            ${s.goal ? `<span>目标: ${s.goal}</span>` : ""}
            ${s.problem_category ? `<span>类别: ${s.problem_category}</span>` : ""}
            ${s.severity_score != null ? `<span>程度: ${s.severity_score}</span>` : ""}
            ${(s.state_keywords || []).length ? `<span>关键词: ${s.state_keywords.join(", ")}</span>` : ""}
          </div>
          ${s.last_user_msg ? `<div class="msg">用户: ${s.last_user_msg}</div>` : ""}
          ${s.current_strategy ? `<div class="msg">当前策略: ${s.current_strategy.strategy_id || JSON.stringify(s.current_strategy)}</div>` : ""}
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
        const resp = await fetch(base + "/chat", {
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
