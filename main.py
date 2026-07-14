# dialog_agent.py —— 对话中抽取关键信息 → 结合 strategies.json 检索 → 生成可执行步骤
import os, json, requests, datetime
from copy import deepcopy
from dotenv import load_dotenv

# === 1) 基础：读取 .env 并封装 Chat 调用 ===
load_dotenv()
API_KEY  = os.getenv("OPENAI_API_KEY")
BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.moonshot.cn/v1").rstrip("/")
MODEL    = os.getenv("OPENAI_MODEL") or "moonshot-v1-8k"

def chat_completion(messages, temperature=0.5, max_tokens=360, timeout=30):
    if not API_KEY:
        return "[占位] 未配置 OPENAI_API_KEY"
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        timeout=timeout
    )
    if r.status_code != 200:
        return f"[ERR {r.status_code}] {r.text[:200]}"
    return r.json()["choices"][0]["message"]["content"]

# === 2) 策略库加载（CSV转JSON后得到的 strategies.json） ===
def load_strategies(path="strategies.json"):
    return json.load(open(path, "r", encoding="utf-8")) if os.path.exists(path) else []

# === 3) 画像（问题/状态/类别/程度/限定）抽取 ===
STATE0 = {
  "goal": None,                              # 想达成什么：稳情绪/回专注/入睡/提神…
  "problem_category": None,                  # 情绪/注意/认知/行为/睡眠/社交/压力后反应
  "state_keywords": [],                      # 关键词：紧张/走神/反刍/困倦…
  "severity_score": None,                    # 0-10
  "time_available_min": None,                # 可用时间（分钟）
  "constraints": {                           # 限定条件（公共/能否出声/健康禁忌/资源）
    "speech_ok": None,                       # true/false
    "public_env": None,                      # true/false
    "health_limits": [],                     # ["asthma","heart","vertigo","photosensitive"]
    "resources": []                          # ["headphones","timer","window","water","desk","phone","notebook"]
  },
  "risk_flags": "green",                     # green/yellow/red
}

EXTRACTOR_SYS = """你是信息抽取器，仅返回一行JSON，不要解释。
字段：
goal, problem_category(情绪/注意/认知/行为/睡眠/社交/压力后反应),
state_keywords[], severity_score(0-10), time_available_min,
constraints:{speech_ok(boolean), public_env(boolean), health_limits[], resources[]},
risk_flags(green|yellow|red)
缺失用 null 或 []。
"""

ASK_ON_MISSING = {
  "goal": "此刻你最想达成的目标是？（如 稳情绪/回专注/入睡/提神）",
  "severity_score": "此刻不适/紧张 0-10 打几分？",
  "time_available_min": "你现在可用的时间大约几分钟？（只写数字）",
  "constraints.speech_ok": "现在方便出声吗？（true/false）",
  "constraints.public_env": "现在是在公共/会议等公开环境吗？（true/false）"
}

def extract_update(state:dict, user_text:str)->dict:
    """调用大模型从用户话语抽取画像；仅覆盖有值的字段"""
    raw = chat_completion([
        {"role":"system","content":EXTRACTOR_SYS},
        {"role":"user","content":user_text}
    ], temperature=0.2, max_tokens=240)
    new_state = deepcopy(state)
    try:
        data = json.loads(raw)
        # 顶层字段
        for k in ("goal","problem_category","state_keywords","severity_score","time_available_min","risk_flags"):
            if k in data and data[k] not in (None, "", []):
                new_state[k] = data[k]
        # constraints 合并
        if "constraints" in data and isinstance(data["constraints"], dict):
            for ck in ("speech_ok","public_env","health_limits","resources"):
                v = data["constraints"].get(ck, None)
                if v not in (None, "", []):
                    new_state["constraints"][ck] = v
    except Exception:
        pass  # 抽取失败不报错，维持现状
    return new_state

def first_missing_key(state:dict)->str|None:
    # 只问一个关键缺口，降低打扰
    order = ["goal","severity_score","time_available_min","constraints.speech_ok","constraints.public_env"]
    for key in order:
        if key == "constraints.speech_ok" and state["constraints"]["speech_ok"] in (None,""):
            return key
        if key == "constraints.public_env" and state["constraints"]["public_env"] in (None,""):
            return key
        if key in state and state[key] in (None, "", []):
            return key
    return None

# === 4) 与策略库的结合：过滤 + 打分检索 ===
def category_map(strategy_category:str)->str:
    """把策略库里的 category 映射到问题大类"""
    mapping = {
        "生理调节":"情绪", "注意分配":"注意", "认知改变":"认知",
        "任务结构化":"行为", "情绪诱导":"情绪",
        "环境调节":"情绪", "社会调节":"社交", "元认知":"认知"
    }
    return mapping.get(strategy_category, "通用")

def match_score(row:dict, s:dict)->int:
    # 强过滤：公共/禁语音
    delivery = row.get("delivery_mode","") or ""
    if s["constraints"].get("public_env") or s["constraints"].get("speech_ok") is False:
        if "语音" in delivery:
            return -999
    # 强过滤：健康禁忌
    row_contra = row.get("contraindications","") or ""
    for h in (s["constraints"].get("health_limits") or []):
        if h and h in row_contra:
            return -999
    score = 0
    # 类别对齐
    if s.get("problem_category") and category_map(row.get("category","")) == s["problem_category"]:
        score += 3
    # 关键词命中
    for k in (s.get("state_keywords") or []):
        for f in ("name","target_state","notes","category"):
            v = row.get(f,"")
            if isinstance(v, list): v = ";".join(v)
            if k and str(v).find(k) >= 0:
                score += 1
    # 时间窗
    t = s.get("time_available_min")
    if t and row.get("duration_min"):
        try:
            if float(row["duration_min"]) <= float(t) + 0.5:
                score += 2
        except: pass
    return score

def retrieve(strategies:list, s:dict, k:int=3)->list:
    ranked = sorted(strategies, key=lambda r: match_score(r, s), reverse=True)
    return [r for r in ranked if match_score(r, s) > -999][:k]

def format_plan(rows:list)->str:
    if not rows: return "（未命中策略，先给通用低风险建议）"
    lines = []
    for r in rows:
        lines.append(f"- {r.get('name','')}｜{r.get('delivery_mode','')}｜剂量:{r.get('dosage','')}｜时长:{r.get('duration_min','')}min")
    return "\n".join(lines)

# === 5) 把一切串起来：主循环 ===
def run():
    strategies = load_strategies("strategies.json")
    state = deepcopy(STATE0)
    print("主动式心理调控 · 对话（/exit 退出）")

    while True:
        user = input("\n你：").strip()
        if user.lower() in ("exit","/exit","quit"): break

        # (1) 抽取并更新画像
        state = extract_update(state, user)

        # 风险管控：red 直接安全指引
        if state.get("risk_flags") == "red":
            print("助手：我检测到高风险内容。请立刻联系可信的人或专业支持；同时做 4-4-6 呼吸 1 分钟，保持在有人处。若胸痛/呼吸困难，请立即就医。")
            continue

        # (2) 若缺关键字段，只问一个问题
        miss = first_missing_key(state)
        if miss:
            print("助手：", ASK_ON_MISSING.get(miss, f"补充一下 {miss}？"))
            continue

        # (3) 用当前画像检索策略，生成方案
        top = retrieve(strategies, state, k=3)
        plan = format_plan(top)

        planner_prompt = (
            f"用户状态：{json.dumps(state, ensure_ascii=False)}\n"
            f"候选策略（仅供参考，若不合适可忽略）：\n{plan}\n"
            "请按‘复述+共情 → 选择逻辑 → 步骤 → 结尾追问(仅1个)’的顺序输出。"
            "追问示例：‘这些步骤里哪一步最方便先做？’或‘你更想要快(≤2min)还是稳(5–10min)？’"
        )

        reply = chat_completion(
            [{"role":"system","content": ASSISTANT_SYS},
            {"role":"user","content": planner_prompt}],
            temperature=0.6, max_tokens=420
        )


        print("助手：", reply)

        # (4) 记录日志
        os.makedirs("logs", exist_ok=True)
        with open("logs/session.jsonl","a",encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "user": user, "state": state, "plan": plan, "reply": reply
            }, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    run()