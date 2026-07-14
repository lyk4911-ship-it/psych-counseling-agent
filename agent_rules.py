# agent_rules.py —— 对话模式分类、提问门槛、微介入（规则透明，可手调）
# -*- coding: utf-8 -*-

import json
import os
import random
import re
from typing import Any, Dict, List, Optional, Tuple

# 与 dialog_agent 中 risk 对齐：高风险词优先
_RISK_PATTERNS = [
    r"不想活", r"结束生命", r"自杀", r"自残", r"死了算了", r"割腕", r"跳楼",
    r"活不下去", r"去死", r"想死",
]

# 生涯/决策探索（可编辑）
_CAREER_KEYWORDS = [
    "考研", "考公", "选专业", "offer", "转行", "跳槽", "秋招", "春招", "简历",
    "职业规划", "方向", "选哪个", "该不该", "要不要换", "读博", "出国", "体制内",
    "大厂", "考编", "实习", "赛道", "行业", "岗位",
]

# 普通信息咨询
_INFO_KEYWORDS = [
    "是什么", "什么意思", "怎么查", "官网", "报名", "截止时间", "流程", "条件",
    "分数线", "参考书", "科目", "大纲", "链接", "在哪看", "通知",
]

# 用户明确拒绝被问 / 要求直接输出
_NO_QUESTION_PHRASES = [
    "不要问我", "别问我", "别问", "不要反问", "少问点", "你直接说", "直接说",
    "多说一点", "多讲一点", "展开说说", "别打岔", "别绕", "给点实在的",
    "不要问那么多", "不用问", "回答我就行",
]

# 微介入触发（轻量关键词，可配 emotion_rules.json 覆盖）
_MICRO_TRIGGERS: List[Tuple[str, str]] = [
    (r"热门|最火|大家都在", "热门和适合常常是两回事，先想清楚你更看重什么，再去看热度。"),
    (r"卷|内卷", "有时候「卷」描述的是环境，不一定是你真正想要的目标。"),
    (r"不知道要什么|很迷茫|没有方向", "方向常常是在试了几件小事之后变清晰的，不必一次想透。"),
    (r"信息太多|越看越焦虑|刷不完", "信息焦虑时，先定一个「今晚只解决一个问题」，比接着刷更有效。"),
    (r"别人都说|他们都", "「别人」的标准可以听，但决策最后还是得回到你自己的约束和目标。"),
]

_EMOTION_RULES_CACHE: Optional[Dict[str, Any]] = None


def load_emotion_rules(path: str = "emotion_rules.json") -> Dict[str, Any]:
    """【新增】加载可选情绪/模式规则 JSON，便于不改代码调规则。"""
    global _EMOTION_RULES_CACHE
    if _EMOTION_RULES_CACHE is not None:
        return _EMOTION_RULES_CACHE
    if not os.path.exists(path):
        _EMOTION_RULES_CACHE = {}
        return _EMOTION_RULES_CACHE
    try:
        with open(path, "r", encoding="utf-8") as f:
            _EMOTION_RULES_CACHE = json.load(f)
    except Exception:
        _EMOTION_RULES_CACHE = {}
    return _EMOTION_RULES_CACHE


def classify_conversation_mode(user_text: str, state: Dict[str, Any]) -> str:
    """
    【新增】规则版对话模式。返回值：risk | career | info | emotion
    后续可接 LLM：在此函数内先算 rules_mode，再可选覆盖。
    """
    t = (user_text or "").strip()
    if not t:
        return state.get("conversation_mode") or "emotion"

    # 1) 风险优先（与画像一致时仍抬高）
    for p in _RISK_PATTERNS:
        if re.search(p, t):
            return "risk"
    if state.get("risk_flags") == "red":
        return "risk"

    # 2) 生涯/决策（先于 info，避免「考研报名」被当成纯信息）
    if any(k in t for k in _CAREER_KEYWORDS):
        return "career"

    # 3) 信息咨询：短句、偏事实查询
    if any(k in t for k in _INFO_KEYWORDS) and len(t) < 160:
        if not any(k in t for k in ["难过", "焦虑", "睡不着", "崩溃", "压力", "想哭", "抑郁"]):
            return "info"

    rules = load_emotion_rules()
    extra_career = (rules.get("career_keywords") or []) if isinstance(rules, dict) else []
    if extra_career and any(k in t for k in extra_career):
        return "career"

    # 4) 默认情绪支持
    return "emotion"


def user_requests_no_questions(user_text: str) -> bool:
    """用户是否表达「别问我 / 直接说」等。"""
    t = (user_text or "").strip()
    return any(p in t for p in _NO_QUESTION_PHRASES)


def should_ask_question(
    user_text: str,
    state: Dict[str, Any],
    mode: str,
    response_plan: Optional[str] = None,
) -> bool:
    """
    【新增】提问门槛：只有「问了能明显提高判断质量」时才允许问。
    response_plan 预留（如将来接规划模块）。
    """
    if state.get("suppress_questions_remaining", 0) > 0:
        return False
    if user_requests_no_questions(user_text):
        return False

    t = (user_text or "").strip()

    # 信息型短问句：用户要答案，少追问
    if mode == "info" and len(t) < 200:
        return False

    # 生涯模式：仅在「二选一/选哪个」等真歧义时问一句；否则给框架、少问
    if mode == "career":
        return bool(re.search(r"还是|哪个好|二选一|选哪个|A还是B", t))

    # 高风险交给流程，少问
    if mode == "risk":
        return False

    # 情绪支持默认不追问，定位更像倾听者而非引导者。
    if mode == "emotion":
        return False

    # 其他模式也尽量克制。
    if int(state.get("assistant_question_streak", 0)) >= 1:
        return False
    return False


def _pick_micro_from_rules(user_text: str) -> Optional[str]:
    """从内置与 emotion_rules.json 合并触发句。"""
    t = user_text or ""
    rules = load_emotion_rules()
    extra = rules.get("micro_triggers") if isinstance(rules, dict) else None
    pairs: List[Tuple[str, str]] = list(_MICRO_TRIGGERS)
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict) and item.get("pattern") and item.get("line"):
                pairs.append((str(item["pattern"]), str(item["line"])))

    for pattern, line in pairs:
        try:
            if re.search(pattern, t):
                return line
        except re.error:
            continue
    return None


def maybe_add_micro_intervention(
    user_text: str,
    mode: str,
    draft_reply: str,
    state: Dict[str, Any],
) -> str:
    """
    【新增】在特定情形下追加 1 句微介入（克制、非说教）。
    同一轮最多一次；与 mode 冲突时不加（如纯 info 且用户只查事实）。
    """
    if mode == "info" and len((user_text or "").strip()) < 100:
        if not re.search(r"迷茫|纠结|不知道", user_text or ""):
            return draft_reply

    last_ts = state.get("_last_micro_turn")
    cur_turn = int(state.get("turns", 0))
    if last_ts is not None and cur_turn - int(last_ts) < 2:
        return draft_reply

    line = _pick_micro_from_rules(user_text)
    if not line:
        return draft_reply

    # 避免与正文重复
    if line[:12] in (draft_reply or ""):
        return draft_reply

    if random.random() > 0.45:
        return draft_reply

    state["_last_micro_turn"] = cur_turn
    return (draft_reply or "").rstrip() + "\n" + line


def mode_system_addon(mode: str) -> str:
    """拼到主 system 后的模式说明（中文）。"""
    m = {
        "info": "【当前模式：信息咨询】优先直接回答事实与步骤，少反问；不要心理化。",
        "career": "【当前模式：生涯/决策探索】先给判断框架（维度、标准、取舍），再给少量行动；不要一上来列三四条任务清单。",
        "emotion": "【当前模式：情绪支持】以倾听和接住感受为主，不主动追问，不急着拆题，不把对话推向任务化。",
        "risk": "【当前模式：高风险支持】安全第一，少说教、少追问细节。",
    }
    return m.get(mode, m["emotion"])


def listening_mode_addon() -> str:
    """【新增】轻陪伴补充：与主 prompt 叠加，用于偏倾听的一轮（可选）。"""
    return (
        "【倾听/轻陪伴】本段以听清楚为主，不急于给建议；"
        "若用户只是在倾诉，不必总结大道理。"
    )


def apply_user_suppression_trigger(state: Dict[str, Any], user_text: str) -> None:
    """用户表达「别问我」时，拉高抑制轮数。"""
    if user_requests_no_questions(user_text):
        state["suppress_questions_remaining"] = max(
            4, int(state.get("suppress_questions_remaining", 0))
        )


def apply_suppression_after_reply(state: Dict[str, Any]) -> None:
    """每轮助手输出后递减抑制计数。"""
    r = int(state.get("suppress_questions_remaining", 0))
    if r > 0:
        state["suppress_questions_remaining"] = r - 1


def update_assistant_question_streak(state: Dict[str, Any], reply: str) -> None:
    """根据本轮回复是否含明显提问，更新追问连击计数。"""
    c = reply or ""
    n = c.count("？") + c.count("?")
    if n >= 1 and len(c) < 800:
        state["assistant_question_streak"] = int(state.get("assistant_question_streak", 0)) + 1
    else:
        state["assistant_question_streak"] = 0


def count_question_like_in_assistant(state: Dict[str, Any]) -> int:
    """粗略统计上一轮助手是否以提问为主（用于 streak）。"""
    h = state.get("_history") or []
    if len(h) < 2:
        return 0
    last = h[-1]
    if last.get("role") != "assistant":
        return 0
    c = last.get("content") or ""
    return 1 if ("？" in c or "?" in c) and c.count("？") + c.count("?") >= 1 else 0
