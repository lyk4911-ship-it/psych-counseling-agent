# dialog_agent.py —— 两库两阶段设计：
#   阶段1（分析库）：辅助构建用户画像 - 模拟咨询师在谈话中理解用户的过程
#   阶段2（策略库）：选择干预策略 - 模拟咨询师从专业策略库中选择最合适的策略
# -*- coding: utf-8 -*-

import os, json, re, random, time, requests
import datetime as dt
import threading
from copy import deepcopy
from collections import OrderedDict
from dotenv import load_dotenv
from typing import Dict, Any, Optional, List, Tuple

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agent_rules import (
    apply_suppression_after_reply,
    apply_user_suppression_trigger,
    classify_conversation_mode,
    listening_mode_addon,
    maybe_add_micro_intervention,
    mode_system_addon,
    should_ask_question,
    update_assistant_question_streak,
)

# ======================================================
# =============== 环境基础配置 ==========================
# ======================================================

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("❌ 未检测到 OPENAI_API_KEY，请在 .env 中设置。")

BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
# 官方 DeepSeek API：deepseek-chat = DeepSeek-V3.2（非思考）；deepseek-reasoner = V3.2 思考模式
MODEL = os.getenv("OPENAI_MODEL") or "deepseek-chat"

RATE_PER_MIN = float(os.getenv("RATE_PER_MIN") or 8)
RATE_BURST   = float(os.getenv("RATE_BURST") or 2)

AUTO_CHAT     = (os.getenv("AUTO_TALK") == "1")
AUTO_INTERVAL = int(os.getenv("AUTO_TALK_INTERVAL") or 4)

# 历史保留轮数（每轮2条：user+assistant）
HISTORY_TURNS_KEEP = int(os.getenv("HISTORY_TURNS_KEEP") or 10)

# 前端空闲时「像微信好友一样」主动发一条；服务端冷却（秒）
PROACTIVE_MARKER = "__PROACTIVE__"
PROACTIVE_COOLDOWN_SEC = float(os.getenv("PROACTIVE_COOLDOWN_SEC") or 180)

# ✅ 策略周期刷新：每几轮自动推断一个“当前最合适策略”供后台看
STRATEGY_REFRESH_EVERY = int(os.getenv("STRATEGY_REFRESH_EVERY") or 3)

STRATEGY_PATH        = "strategies.json"         # 策略库：干预策略列表（用于给用户提供方法）
ANALYSIS_LIB_PATH    = "情感分析库.json"          # 分析库：分析评估策略（辅助LLM理解用户状态并提取画像）
LOG_DIR              = "logs"
LOG_FILE             = os.path.join(LOG_DIR, "session.jsonl")

# ======================================================
# =============== HTTP 会话 + 缓存 ======================
# ======================================================

SESSION = requests.Session()
SESSION.mount("http://", HTTPAdapter(max_retries=Retry(
    total=2, backoff_factor=0.2,
    status_forcelist=[500, 502, 503, 504], allowed_methods=["POST"]
)))
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=2, backoff_factor=0.2,
    status_forcelist=[500, 502, 503, 504], allowed_methods=["POST"]
)))

_RESP_CACHE = OrderedDict()
_CACHE_CAP = 64
_CACHE_LOCK = threading.Lock()  # ✅ 保护缓存并发访问

def _cache_get(key):
    """线程安全的缓存读取"""
    with _CACHE_LOCK:
        if key in _RESP_CACHE:
            _RESP_CACHE.move_to_end(key)
            return _RESP_CACHE[key]
        return None

def _cache_put(key, val):
    """线程安全的缓存写入"""
    with _CACHE_LOCK:
        _RESP_CACHE[key] = val
        _RESP_CACHE.move_to_end(key)
        if len(_RESP_CACHE) > _CACHE_CAP:
            _RESP_CACHE.popitem(last=False)

# ======================================================
# =============== 令牌桶限流 ============================
# ======================================================

class TokenBucket:
    """线程安全的令牌桶限流器"""
    def __init__(self, rate_per_min=30, burst=5):
        self.rate = float(rate_per_min)
        self.allowance = float(rate_per_min)
        self.burst = float(burst)
        self.last = time.time()
        self._lock = threading.Lock()  # ✅ 保护限流器状态

    def consume(self, cost=1):
        """线程安全的令牌消耗"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last
            self.last = now
            self.allowance = min(
                self.rate + self.burst,
                self.allowance + elapsed * (self.rate / 60.0)
            )
            if self.allowance >= cost:
                self.allowance -= cost
                return True
            return False

    def wait_time(self, cost=1):
        """计算等待时间（需要锁保护读取allowance）"""
        with self._lock:
            if self.allowance >= cost:
                return 0.0
            missing = cost - self.allowance
            return missing / (self.rate / 60.0)

    def set_rate(self, new_rate):
        """线程安全的速率调整"""
        with self._lock:
            new_rate = max(1.0, float(new_rate))
            self.rate = (self.rate * 0.7) + (new_rate * 0.3)

TB = TokenBucket(rate_per_min=RATE_PER_MIN, burst=RATE_BURST)

def _maybe_throttle():
    """限流控制（线程安全）"""
    if not TB.consume(1):
        t = TB.wait_time(1)
        time.sleep(min(max(t, 0.05), 3.0))
        TB.consume(1)

# ======================================================
# =============== Chat API（DeepSeek） ==================
# ======================================================

def chat_completion(messages, temperature=0.6, max_tokens=300, timeout=30):
    """
    统一的对话接口封装：带缓存、限流、重试、429自适应。
    返回：字符串（模型输出 or 错误字符串）
    """
    try:
        cache_key = json.dumps(
            {"m": messages, "t": temperature, "mx": max_tokens},
            ensure_ascii=False, sort_keys=True
        )
        cached = _cache_get(cache_key)
        if cached:
            return cached
    except Exception:
        cache_key = None

    _maybe_throttle()

    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    attempts, max_attempts, backoff = 0, 4, 1.0

    while True:
        attempts += 1
        try:
            r = SESSION.post(
                url,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload, timeout=timeout
            )
        except Exception as e:
            if attempts >= max_attempts:
                return f"[ERR] {e}"
            time.sleep(backoff)
            backoff = min(backoff * 2, 8)
            continue

        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            wait_s = float(ra) if ra else (backoff + random.random())
            TB.set_rate(TB.rate * 0.7)
            if attempts >= max_attempts:
                return "[ERR429] 过载"
            time.sleep(wait_s)
            continue

        if r.status_code == 200:
            try:
                out = r.json()["choices"][0]["message"]["content"]
            except Exception:
                out = "（解析错误）"
            if cache_key:
                _cache_put(cache_key, out)
            return out

        if 500 <= r.status_code < 600:
            if attempts >= max_attempts:
                return f"[ERR{r.status_code}] {r.text[:200]}"
            time.sleep(backoff)
            backoff = min(backoff * 2, 12)
            continue

        return f"[HTTP{r.status_code}] {r.text[:200]}"

# ======================================================
# =============== 资源加载 ===============================
# ======================================================

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


# ======================================================
# =============== 提示词 =================================
# ======================================================

# 【替换】主对话系统：主动式心理调控 + 少追问 + 好友感
FULL_CHAT_SYS = (
  "你是聪明、克制、像靠谱朋友一样的对话助手，优先接住对方，不急着引导对方回答你。\n"
  "语气：自然口语，别像客服或教科书；不必每轮都共情，别堆套话。\n"
  "长度：通常 2～5 句，优先短句，像即时聊天。\n"
  "\n"
  "【提问纪律】\n"
  "- 默认不问，尤其在情绪支持场景里不要把话题抛回给用户。\n"
  "- 若系统提示「本轮禁止提问」，则不要向用户索取回答，也不要用「更像哪种 / 有没有 / 要不要 / 是不是 / 不如你」这类问句句式。\n"
  "- 用户若在倾诉事实，先接住内容，允许停在陪伴和理解，不必每轮都推进。\n"
  "\n"
  "【主动但轻】\n"
  "- 可以偶尔帮对方整理一句，但不要把聊天变成带任务的引导流程。\n"
  "- 不要每轮都给建议或任务清单；很多时候一句接住就够了。\n"
  "\n"
  "【模式】你会收到【当前模式】说明，请按该模式调整重心（信息/生涯/情绪/风险）。\n"
)

ANALYSIS_SYS = (
  "你是心理咨询师的画像构建助手。你的任务是基于对话内容和分析库中的专业评估方法，理解用户状态并构建画像。\n"
  "分析库提供了专业的心理学评估工具和方法，参考这些工具来理解用户的话语。\n"
  "只输出一行严格JSON，不要解释。\n"
  "输出格式：\n"
  "{\n"
  ' "patch": { ... },\n'
  ' "intent": "chat|plan|emergency",\n'
  ' "confidence": 0.0-1.0,\n'
  ' "missing": ["字段名", ...]\n'
  "}\n"
  "patch字段允许：goal, problem_category, state_keywords[], severity_score(0-10), time_available_min,\n"
  "constraints:{speech_ok, public_env, health_limits[], resources[]}, risk_flags(green|yellow|red)\n"
  "规则：参考分析库中的专业方法，只填你有把握的；不确定就不要填/填null。\n"
)

STRATEGY_PICK_SYS = (
  "你是心理咨询师的策略选择助手。你的任务是基于已构建的用户画像，从专业策略库中选择最合适的干预策略。\n"
  "这模拟了咨询师在裁决阶段，从所有专业策略中选择最符合用户当前状态的方法。\n"
  "只输出一行严格JSON，不要解释：\n"
  "{\n"
  '  "strategy_id": "...",\n'
  '  "why": ["...","..."],\n'
  '  "confidence": 0.0-1.0\n'
  "}\n"
  "要求：严格基于画像与候选策略字段；不要编造对话事实。\n"
)

PLAN_REPLY_SYS = (
  "你是微信好友风格的「微行动」助手。根据【画像】与【已选策略】回答。\n"
  "结构优先：\n"
  "1）若对方在选方向/做决策：先用 2～4 句给出「判断维度 / 取舍标准 / 怎么比」（不要空泛鸡汤）。\n"
  "2）再给 1～2 条以「·」开头的、≤2 分钟可完成的小步（口语、非术语）。\n"
  "3）若系统允许提问，最后用 1 句轻问；若系统写明「禁止提问」，则用一句陈述收束，不要用问号。\n"
  "禁止诊断、禁止贴标签；不要一上来列三四条待办。\n"
)

SHOW_SYS = (
  "你是后台记录员，为开发者/督导整理「基于证据的会谈摘要」，不是给来访者看的诊断书。\n"
  "语言平实，少用空泛咨询术语；不要把正常的生涯纠结过度心理化。\n"
  "\n"
  "严格按下面 5 段输出，每段独立成段，段首必须带编号与标题（照抄）：\n"
  "1. 已观察到的内容（只写对话里明确出现的事实与表述，可短列表，勿臆测）\n"
  "2. 合理推测（基于上条，标注「推测」并说明依据）\n"
  "3. 尚待确认（列出若知道会更好判断的信息点，不要装作已确认）\n"
  "4. 风险等级（低/中/高 + 一句依据；无依据则写「依据不足，保守标为中」并说明）\n"
  "5. 下一步建议（对 AI 对话可怎么做、何时建议真人协助；分条，短句）\n"
  "\n"
  "约束：\n"
  "- 无证据不写「抑郁/人格」等标签；\n"
  "- 信息不足时在第 2、3 段明确写清；\n"
  "- 总篇幅适中，不要堆砌术语。\n"
)

EMERGENCY_SYS = (
    "你是心理支持后台摘要助手，读者是专业咨询师。\n"
    "请基于对话片段，生成：\n"
    "1）3-5 行核心摘要\n"
    "2）2-4 行 AI 建议关注点\n"
    "禁止诊断、禁止治疗建议、禁止专业名词堆砌。"
)

PROACTIVE_SYS = (
    "你是对方的微信好友。对方刚才聊过几句，现在有一阵子没发消息了，你想发一条很短的、自然的关心或接话。\n"
    "要求：总共 1～2 句，总字数不超过 60 字；不要用问号结尾；不要布置任务；不要心理分析腔或说教；"
    "不要提「咨询师」「治疗」；像真朋友随手打一行字。"
)

PROACTIVE_QC_DENY = (
    "咨询", "诊断", "治疗", "抑郁症", "焦虑症", "精神障碍", "心理疾病",
    "建议你", "综上所述", "首先", "其次", "此外，", "根据你的", "作为你的",
    "心理健康", "情绪管理", "医疗资源", "专业帮助", "心理热线", "本平台",
    "用户您", "尊敬的用户",
)

PROACTIVE_FALLBACKS = [
    "我在呢，你刚才说的我记着了。",
    "先喘口气也行，不急着马上想清楚。",
    "要是还想聊，我都在。",
    "刚才聊的那些，我在这儿听着呢。",
]

STATE0 = {
  "goal": None,
  "problem_category": None,
  "state_keywords": [],
  "severity_score": None,
  "time_available_min": None,
  "constraints": {
      "speech_ok": None,
      "public_env": None,
      "health_limits": [],
      "resources": []
  },
  "risk_flags": "green",
  "phase": "rapport",
  "turns": 0,
  "_history": [],
  "current_strategy": None,
  "last_plan": None,
  "conversation_mode": "emotion",
  "suppress_questions_remaining": 0,
  "assistant_question_streak": 0,
  "_last_micro_turn": None,
  "last_proactive_unix": None,
}

AUTO_MESSAGES = [
  "我在呢，你刚才说的我记着了。",
  "先喘口气也行，不急着马上想清楚。",
]

def _safe_json_load(s: str) -> Dict[str, Any]:
    try:
        m = re.search(r"\{.*\}", s, flags=re.S)
        json_str = m.group(0) if m else "{}"
        return json.loads(json_str)
    except Exception:
        return {}

def humanize(t: str) -> str:
    t = (t or "").strip()
    rep = {"此外，": "", "综上": "", "因此，": "所以，", "建议你": "不妨试试"}
    for k, v in rep.items():
        t = t.replace(k, v)
    return t

def _normalize_text_for_compare(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[，。！？、,.!?；;：:·\-—\[\]\(\)\"'“”‘’]", "", t)
    return t

def _split_sentences(t: str) -> List[str]:
    if not t:
        return []
    parts = re.split(r"[。！？\n]+", t)
    return [p.strip() for p in parts if p and p.strip()]

def _char_jaccard(a: str, b: str) -> float:
    sa = set(_normalize_text_for_compare(a))
    sb = set(_normalize_text_for_compare(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))

def _recent_assistant_texts(state: Dict[str, Any], n: int = 3) -> List[str]:
    h = state.get("_history") or []
    out = []
    for it in reversed(h):
        if isinstance(it, dict) and it.get("role") == "assistant" and it.get("content"):
            out.append(str(it["content"]))
        if len(out) >= n:
            break
    return out

def _extract_scene_hint(user_text: str) -> str:
    t = user_text or ""
    keys = [
        "毕业论文", "导师", "开会", "汇报", "实习", "睡眠不足", "时间安排",
        "焦虑", "拖延", "复习", "考试", "面试", "找工作",
    ]
    for k in keys:
        if k in t:
            return k
    t = t.strip()
    return t[:12] if t else "这件事"

def _pick_user_topic_keyword(user_text: str) -> Optional[str]:
    keys = [
        "毕业论文", "论文", "导师", "开会", "会议", "汇报", "PPT",
        "初稿", "数据", "方法论", "睡眠", "时间安排", "焦虑", "卡住",
    ]
    for k in keys:
        if k in (user_text or ""):
            return k
    return None

def _ensure_topic_mirroring(reply: str, user_text: str) -> str:
    return (reply or "").strip()

def _personalize_first_turn(reply: str, user_text: str) -> str:
    return (reply or "").strip()

def _remove_repetitive_lines(reply: str, state: Dict[str, Any]) -> str:
    cur_lines = _split_sentences(reply)
    if not cur_lines:
        return (reply or "").strip()
    recent = _recent_assistant_texts(state, n=3)
    if not recent:
        return (reply or "").strip()
    filtered = []
    for ln in cur_lines:
        sim = max((_char_jaccard(ln, r) for r in recent), default=0.0)
        if sim < 0.82:
            filtered.append(ln)
    if not filtered:
        filtered = [cur_lines[0]]
    return "。".join(filtered).rstrip("。") + "。"

def _strip_question_marks_if_needed(reply: str, should_ask: bool) -> str:
    if should_ask:
        return (reply or "").strip()
    t = (reply or "").strip()
    t = t.replace("？", "。").replace("?", "。")
    t = re.sub(r"[。]{2,}", "。", t)
    return t.strip()

def _strip_question_like_lines_if_needed(reply: str, should_ask: bool) -> str:
    if should_ask:
        return (reply or "").strip()
    t = (reply or "").strip()
    if not t:
        return t
    lines = [x.strip() for x in t.split("\n") if x.strip()]
    deny_patterns = [
        r"更像哪种", r"有没有", r"要不要", r"是不是", r"不如你",
        r"你现在.*像", r"你手边有", r"你打算", r"你会不会",
        r"你愿不愿意", r"要不", r"愿意说说", r"说说看", r"聊聊看",
        r"讲讲", r"展开说", r"如果你想说", r"你可以说说",
    ]
    kept = []
    for ln in lines:
        if any(re.search(p, ln) for p in deny_patterns):
            continue
        kept.append(ln)
    if kept:
        return "\n".join(kept).strip()
    sents = _split_sentences(t)
    if sents:
        return sents[0] + "。"
    return "我在这儿，先陪你待一会儿。"

def _compress_overlong_reply(reply: str) -> str:
    return (reply or "").strip()

def _postprocess_reply(reply: str, state: Dict[str, Any], user_text: str, should_ask: bool) -> str:
    t = (reply or "").strip()
    if not t:
        return t
    t = _strip_question_marks_if_needed(t, should_ask)
    t = _strip_question_like_lines_if_needed(t, should_ask)
    return t.strip()

def _merge_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if state is None:
        return deepcopy(STATE0)
    merged = deepcopy(STATE0)
    merged.update(state)
    merged.setdefault("constraints", deepcopy(STATE0["constraints"]))
    for ck in STATE0["constraints"]:
        merged["constraints"].setdefault(ck, deepcopy(STATE0["constraints"][ck]))
    merged.setdefault("_history", [])
    for k in ("conversation_mode", "suppress_questions_remaining", "assistant_question_streak", "_last_micro_turn", "last_proactive_unix"):
        merged.setdefault(k, deepcopy(STATE0[k]) if k in STATE0 else None)
    return merged

def _append_history(state: Dict[str, Any], role: str, content: str):
    h = state.get("_history") or []
    h.append({"role": role, "content": content, "ts": dt.datetime.now().isoformat(timespec="seconds")})
    keep = max(2, HISTORY_TURNS_KEEP * 2)
    if len(h) > keep:
        h = h[-keep:]
    state["_history"] = h

def _history_to_messages(state: Dict[str, Any]) -> List[Dict[str, str]]:
    msgs = []
    for it in (state.get("_history") or []):
        if isinstance(it, dict) and it.get("role") in ("user", "assistant") and it.get("content"):
            msgs.append({"role": it["role"], "content": it["content"]})
    return msgs

def _history_to_transcript(state: Dict[str, Any], max_items: int = 80) -> str:
    h = state.get("_history") or []
    h = h[-max_items:]
    lines = []
    for it in h:
        ts = it.get("ts", "")
        role = "用户" if it.get("role") == "user" else "助手"
        lines.append(f"[{ts}] {role}：{it.get('content','')}")
    return "\n".join(lines) if lines else "（暂无对话记录）"

def _dedupe_list(items: List, max_len: int = 20) -> List:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[-max_len:]

def _apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(patch, dict):
        return state
    for k in ("goal", "problem_category", "severity_score", "time_available_min", "risk_flags"):
        if k in patch and patch.get(k) not in (None, "", []):
            state[k] = patch.get(k)
    if "state_keywords" in patch and isinstance(patch.get("state_keywords"), list) and patch["state_keywords"]:
        cur = state.get("state_keywords") or []
        add = [x for x in patch["state_keywords"] if x and isinstance(x, str)]
        state["state_keywords"] = _dedupe_list(cur + add, 20)
    c = patch.get("constraints")
    if isinstance(c, dict):
        state.setdefault("constraints", deepcopy(STATE0["constraints"]))
        for ck in ("speech_ok", "public_env"):
            if ck in c and c.get(ck) not in (None, ""):
                state["constraints"][ck] = c.get(ck)
        for ck in ("health_limits", "resources"):
            if ck in c and isinstance(c.get(ck), list) and c.get(ck):
                cur = state["constraints"].get(ck) or []
                state["constraints"][ck] = _dedupe_list(cur + [x for x in c.get(ck) if x], 20)
    return state

def category_map(cat):
    return {
        "生理调节": "情绪", "注意分配": "注意", "认知改变": "认知",
        "任务结构化": "行为", "情绪诱导": "情绪", "环境调节": "情绪",
        "社会调节": "社交", "元认知": "认知"
    }.get(cat, "通用")

def match_score(row, s):
    if s.get("constraints", {}).get("public_env") or s.get("constraints", {}).get("speech_ok") is False:
        if "语音" in (row.get("delivery_mode") or ""):
            return -999
    hl = s.get("constraints", {}).get("health_limits") or []
    contraind = row.get("contraindications") or ""
    if any(h and h in contraind for h in hl):
        return -999
    score = 0
    if s.get("problem_category"):
        if category_map(row.get("category", "")) == s["problem_category"]:
            score += 3
    for kw in s.get("state_keywords") or []:
        fields = [row.get("name", ""), row.get("target_state", ""), row.get("notes", "")]
        if any(kw and (kw in (f or "")) for f in fields):
            score += 1
    t = s.get("time_available_min")
    if t:
        try:
            if float(row.get("duration_min", 99)) <= float(t) + 0.5:
                score += 2
        except Exception:
            pass
    return score

def preselect_candidates(strategies: List[Dict[str, Any]], state: Dict[str, Any], k: int = 12) -> List[Dict[str, Any]]:
    scored = []
    for r in strategies:
        sc = match_score(r, state)
        if sc > -999:
            scored.append((sc, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, seen = [], set()
    for sc, r in scored:
        nm = r.get("name", "")
        if nm and nm not in seen:
            seen.add(nm)
            out.append(r)
        if len(out) >= k:
            break
    return out

def _format_strategies(rows: List[Dict[str, Any]]) -> str:
    lines = []
    for r in rows:
        lines.append(
            f"- {r.get('strategy_id')} | {r.get('name')} | {r.get('category')} | "
            f"{r.get('delivery_mode')} | {r.get('duration_min')}min | "
            f"target={r.get('target_state')} | contraind={r.get('contraindications')}"
        )
    return "\n".join(lines) if lines else "（无候选策略）"

def llm_analyze_patch(analysis_lib: Any, state: Dict[str, Any], user_text: str) -> Dict[str, Any]:
    hist_msgs = _history_to_messages(state)
    lib_str = json.dumps(analysis_lib, ensure_ascii=False)
    if len(lib_str) > 6000:
        lib_str = lib_str[:6000] + "…（截断）"
    prompt = (
        f"【分析库】\n{lib_str}\n\n"
        f"【当前画像state】\n{json.dumps({k: state.get(k) for k in ['goal','problem_category','state_keywords','severity_score','time_available_min','constraints','risk_flags','phase']}, ensure_ascii=False)}\n\n"
        f"【对话历史】\n{json.dumps(hist_msgs, ensure_ascii=False)}\n\n"
        f"【用户最新一句】\n{user_text}\n\n"
        "请按规则输出 JSON。"
    )
    raw = chat_completion(
        [{"role": "system", "content": ANALYSIS_SYS},
         {"role": "user", "content": prompt}],
        temperature=0.1, max_tokens=260
    )
    return _safe_json_load(raw)

def llm_pick_current_strategy(strategies: List[Dict[str, Any]], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not strategies:
        return None
    candidates = strategies
    if len(strategies) > 60:
        candidates = preselect_candidates(strategies, state, k=12)
    cand_text = _format_strategies(candidates)
    persona = {k: state.get(k) for k in ["goal","problem_category","state_keywords","severity_score","time_available_min","constraints","risk_flags"]}
    prompt = (
        f"【画像】{json.dumps(persona, ensure_ascii=False)}\n"
        f"【候选策略】\n{cand_text}\n\n"
        "请选择当前最贴合的一条 strategy_id。"
    )
    raw = chat_completion(
        [{"role": "system", "content": STRATEGY_PICK_SYS},
         {"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=160
    )
    dec = _safe_json_load(raw)
    if not dec.get("strategy_id"):
        return None
    return {
        "strategy_id": dec.get("strategy_id"),
        "why": dec.get("why") if isinstance(dec.get("why"), list) else [],
        "confidence": float(dec.get("confidence") or 0.0),
        "ts": dt.datetime.now().isoformat(timespec="seconds")
    }

def llm_show_report(state: Dict[str, Any]) -> str:
    transcript = _history_to_transcript(state, max_items=120)
    tags = {
        "state_keywords": state.get("state_keywords"),
        "goal": state.get("goal"),
        "problem_category": state.get("problem_category"),
        "severity_score": state.get("severity_score"),
        "constraints": state.get("constraints"),
        "risk_flags": state.get("risk_flags"),
        "current_strategy": state.get("current_strategy"),
        "last_plan": state.get("last_plan"),
    }
    prompt = (
        f"【完整历史对话记录】\n{transcript}\n\n"
        f"【系统侧标签/画像提示（可参考，不得捏造）】\n{json.dumps(tags, ensure_ascii=False)}\n\n"
        "请按结构输出报告。"
    )
    def ok_format(text: str) -> bool:
        t = (text or "").strip()
        need = ["1.", "2.", "3.", "4.", "5."]
        return all(x in t for x in need) and ("已观察" in t or "观察" in t)
    raw = chat_completion(
        [{"role":"system","content": SHOW_SYS},
         {"role":"user","content": prompt}],
        temperature=0.2, max_tokens=900
    )
    raw = (raw or "").strip()
    if ok_format(raw):
        return raw
    repair_sys = (
        SHOW_SYS +
        "\n\n【重要】你刚才的输出不符合五段结构。现在必须重写："
        "只输出报告正文，必须包含 1.～5. 五个标题段落；"
        "不要套旧版「近期聊天概括/主要心理问题」四段模板。"
    )
    raw2 = chat_completion(
        [{"role":"system","content": repair_sys},
         {"role":"user","content": prompt}],
        temperature=0.1, max_tokens=900
    )
    raw2 = (raw2 or "").strip()
    if ok_format(raw2):
        return raw2
    return "（/show 输出未按五段格式生成：模型服从性不足或接口异常。）\n\n原始输出：\n" + raw[:600]

def build_emergency_report(state):
    transcript = _history_to_transcript(state, max_items=80)
    persona = {
        "goal": state.get("goal"),
        "problem_category": state.get("problem_category"),
        "severity_score": state.get("severity_score"),
        "time_available_min": state.get("time_available_min"),
        "constraints": state.get("constraints"),
        "state_keywords": state.get("state_keywords"),
    }
    prompt = (
        f"【对话片段】\n{transcript}\n\n"
        f"【自动画像】\n{json.dumps(persona, ensure_ascii=False)}"
    )
    summary = chat_completion(
        [{"role": "system", "content": EMERGENCY_SYS},
         {"role": "user", "content": prompt}],
        temperature=0.4, max_tokens=420
    )
    out = []
    out.append("====== 🔴 紧急摘要报告 ======")
    out.append(f"风险级别：{state.get('risk_flags')}")
    out.append("")
    out.append((summary or "").strip())
    out.append("")
    out.append("【画像要点】")
    out.append(json.dumps(persona, ensure_ascii=False))
    out.append("====================================")
    return "\n".join(out)

def log_turn(user, state, reply):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "user": user,
            "state": state,
            "reply": reply
        }, ensure_ascii=False) + "\n")

def _sanitize_proactive_line(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    s = s.replace("\n", " ").strip()
    for p in ("「", "『", '"', "'", "“", "”"):
        s = s.strip(p)
    for prefix in (
        "好的，", "好的。", "输出：", "一条：", "主动消息：", "消息：", "回复：",
    ):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
    s = s.replace("**", "").replace("*", "").replace("`", "")
    while s and s[-1] in "?？":
        s = s[:-1].rstrip()
    if len(s) > 72:
        s = s[:72].rstrip()
    return s

def _proactive_passes_qc(line: str) -> bool:
    if not line or len(line) < 4:
        return False
    if line.startswith("[") or "[ERR" in line or "[HTTP" in line:
        return False
    low = line.lower()
    if any(x in line for x in PROACTIVE_QC_DENY):
        return False
    if line.count("？") + line.count("?") > 0:
        return False
    if "http://" in low or "https://" in low:
        return False
    if line.count("。") > 3 or line.count("；") > 2:
        return False
    return True

def _run_proactive_friend_line(state: Dict[str, Any]) -> Tuple[str, bool]:
    now = time.time()
    last = state.get("last_proactive_unix")
    if last is not None and (now - float(last)) < PROACTIVE_COOLDOWN_SEC:
        return "", True
    if int(state.get("turns", 0)) < 1:
        return "", True
    transcript = _history_to_transcript(state, max_items=24)
    if len(transcript.strip()) < 12:
        return "", True
    messages = [
        {"role": "system", "content": PROACTIVE_SYS},
        {"role": "user", "content": f"【最近对话】\n{transcript}\n\n请只输出一条主动消息正文，不要标题或引号。"},
    ]
    line = ""
    try:
        raw = chat_completion(messages, temperature=0.75, max_tokens=120)
        line = _sanitize_proactive_line(humanize(raw or ""))
    except Exception:
        line = ""
    if not line or not _proactive_passes_qc(line):
        line = random.choice(PROACTIVE_FALLBACKS)
    hist = state.get("_history") or []
    last_a = None
    for it in reversed(hist):
        if isinstance(it, dict) and it.get("role") == "assistant":
            last_a = (it.get("content") or "").strip()
            break
    if last_a and last_a == line:
        alts = [x for x in PROACTIVE_FALLBACKS if x != line]
        line = random.choice(alts or PROACTIVE_FALLBACKS)
    state["last_proactive_unix"] = now
    return line, False

def maybe_auto_message(state, intent):
    if not AUTO_CHAT:
        return None
    if intent == "plan":
        return None
    if int(state.get("suppress_questions_remaining", 0)) > 0:
        return None
    if state.get("turns", 0) > 0 and state["turns"] % AUTO_INTERVAL == 0:
        return random.choice(AUTO_MESSAGES)
    return None

class DialogAgent:
    def __init__(self):
        self.strategies   = load_json(STRATEGY_PATH, [])
        self.analysis_lib = load_json(ANALYSIS_LIB_PATH, {})

    def run(
        self,
        user_text: str,
        state: Optional[Dict[str, Any]] = None,
        allow_show_report: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        state = _merge_state(state)
        user = (user_text or "").strip()
        if user.lower() in ("/reset", "reset"):
            ns = deepcopy(STATE0)
            return "好～我把画像清空了。你想从哪儿开始说起？", ns
        if user.lower().startswith("/show"):
            if not allow_show_report:
                return (
                    "该指令为内部测试功能，用户端不可用。请在测试者后台使用「生成咨询师报告」。",
                    state,
                )
            report = llm_show_report(state)
            return report, state
        if user == PROACTIVE_MARKER:
            reply, skipped = _run_proactive_friend_line(state)
            if skipped:
                return "", state
            _append_history(state, "assistant", reply)
            return reply, state
        if not user:
            return "你先随便说点啥，我在这儿听着。", state
        _append_history(state, "user", user)
        apply_user_suppression_trigger(state, user)
        analysis = llm_analyze_patch(self.analysis_lib, state, user)
        patch   = analysis.get("patch") if isinstance(analysis, dict) else None
        intent  = (analysis.get("intent") if isinstance(analysis, dict) else None) or "chat"
        missing = analysis.get("missing") if isinstance(analysis, dict) else []
        if isinstance(patch, dict):
            state = _apply_patch(state, patch)
        if state.get("risk_flags") == "red" or intent == "emergency":
            rep = build_emergency_report(state)
            safe = humanize(
                "我感觉你现在真的很难受，而且有危险信号。\n"
                "请尽快联系信任的人，或当地专业热线/急救电话。"
            )
            reply = rep + "\n\n" + safe
            _append_history(state, "assistant", reply)
            return reply, state
        state["turns"] = int(state.get("turns", 0)) + 1
        mode = classify_conversation_mode(user, state)
        state["conversation_mode"] = mode
        should_ask = should_ask_question(user, state, mode)
        if STRATEGY_REFRESH_EVERY > 0:
            enough = bool((state.get("state_keywords") or []) or state.get("problem_category") or state.get("goal"))
            if enough and state["turns"] % STRATEGY_REFRESH_EVERY == 0:
                try:
                    cur = llm_pick_current_strategy(self.strategies, state)
                    if cur:
                        state["current_strategy"] = cur
                except Exception:
                    pass
        reply = None
        if intent == "plan":
            if not state.get("current_strategy"):
                cur = llm_pick_current_strategy(self.strategies, state)
                if cur:
                    state["current_strategy"] = cur
            chosen_id = (state.get("current_strategy") or {}).get("strategy_id")
            chosen = None
            if chosen_id:
                for s in self.strategies:
                    if s.get("strategy_id") == chosen_id:
                        chosen = s
                        break
            persona = {k: state.get(k) for k in ["goal","problem_category","state_keywords","severity_score","time_available_min","constraints","risk_flags"]}
            plan_sys = (
                PLAN_REPLY_SYS
                + "\n"
                + mode_system_addon(mode)
                + (
                    "\n【本轮】禁止在结尾使用问号或反问。"
                    if not should_ask
                    else "\n【本轮】若需收尾，最多一句轻问。"
                )
            )
            messages = [{"role": "system", "content": plan_sys}]
            hist_msgs = _history_to_messages(state)
            if len(hist_msgs) > 16:
                hist_msgs = hist_msgs[-16:]
            if len(hist_msgs) > 0:
                messages.extend(hist_msgs)
            prompt = (
                f"【画像】{json.dumps(persona, ensure_ascii=False)}\n"
                f"【已选策略】{json.dumps(chosen or {'strategy_id': chosen_id}, ensure_ascii=False)}\n"
                "请基于对话历史和以上信息，输出给用户看的回复。"
            )
            messages.append({"role": "user", "content": prompt})
            raw = chat_completion(
                messages,
                temperature=0.55, max_tokens=220
            )
            reply = humanize(raw)
            reply = maybe_add_micro_intervention(user, mode, reply, state)
            reply = _postprocess_reply(reply, state, user, should_ask)
            update_assistant_question_streak(state, reply)
            apply_suppression_after_reply(state)
            if chosen_id:
                state["last_plan"] = {
                    "strategy_id": chosen_id,
                    "ts": dt.datetime.now().isoformat(timespec="seconds")
                }
            state["phase"] = "followup"
            _append_history(state, "assistant", reply)
            return reply, state
        if reply is None:
            brief = {
                "goal": state.get("goal"),
                "problem_category": state.get("problem_category"),
                "severity": state.get("severity_score"),
                "keywords": (state.get("state_keywords") or [])[:6],
                "constraints": state.get("constraints"),
                "missing": missing[:3] if isinstance(missing, list) else []
            }
            ask_line = (
                "【本轮输出】若确有必要，最多 1 个高质量追问；否则不要问。"
                if should_ask
                else "【本轮输出】不要向用户索取回答；不要用问句句式收尾；允许只接住、不推进。"
            )
            listen = ""
            if mode == "emotion" and len(user) > 100:
                listen = "\n" + listening_mode_addon()
            system_with_context = "\n\n".join(
                [
                    FULL_CHAT_SYS,
                    mode_system_addon(mode) + listen,
                    f"【画像提示（仅参考，不要暴露给用户）】\n{json.dumps(brief, ensure_ascii=False)}",
                    ask_line,
                ]
            )
            messages = [{"role": "system", "content": system_with_context}]
            hist_msgs = _history_to_messages(state)
            if len(hist_msgs) > 0:
                if len(hist_msgs) > 20:
                    hist_msgs = hist_msgs[-20:]
                messages.extend(hist_msgs)
            reply = chat_completion(
                messages,
                temperature=0.6, max_tokens=220
            )
            reply = humanize(reply)
            reply = maybe_add_micro_intervention(user, mode, reply, state)
            reply = _postprocess_reply(reply, state, user, should_ask)
            update_assistant_question_streak(state, reply)
            apply_suppression_after_reply(state)
        auto = maybe_auto_message(state, intent)
        if auto:
            reply = (reply or "") + "\n" + humanize(auto)
        if not reply:
            reply = "我在呢，你接着说。"
        _append_history(state, "assistant", reply)
        return reply, state

def run():
    agent = DialogAgent()
    state = deepcopy(STATE0)
    print("主动式心理调控 · 对话已启动（输入 /exit 退出，/show 生成咨询师报告，/reset 清空）")
    while True:
        user = input("\n你：").strip()
        if user.lower() in ("/exit", "exit", "quit"):
            break
        reply, state = agent.run(user, state, allow_show_report=True)
        print("助手：", reply)
        log_turn(user, state, reply)

if __name__ == "__main__":
    run()
