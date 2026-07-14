#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
使用 5 个随机 MBTI 模拟用户，在同一生活情境下与 DialogAgent 对话，
并在内容质量持续下降时自动终止对话，保存完整日志与摘要。
"""

import argparse
import json
import os
import random
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from dialog_agent import DialogAgent, STATE0, chat_completion


SCENARIO = (
    "你是大四学生，下周要交毕业论文初稿，今晚还要和实习导师开会。"
    "你担心自己准备不够、睡眠不足、时间安排混乱，想和朋友聊聊怎么稳住状态。"
)

MBTI_TYPES = [
    "INTJ", "INTP", "ENTJ", "ENTP",
    "INFJ", "INFP", "ENFJ", "ENFP",
    "ISTJ", "ISFJ", "ESTJ", "ESFJ",
    "ISTP", "ISFP", "ESTP", "ESFP",
]


@dataclass
class QualityDecision:
    score: float
    decline_streak: int
    should_stop: bool
    reason: str
    dimensions: Dict[str, float]


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def persona_prompt(mbti: str, scenario: str, history: List[Dict[str, str]], turn_idx: int) -> str:
    history_lines = []
    for it in history[-8:]:
        role = "你" if it["role"] == "user" else "对方"
        history_lines.append(f"{role}：{it['content']}")
    history_text = "\n".join(history_lines) if history_lines else "（暂无历史）"
    return (
        f"你在扮演一个真实用户，MBTI={mbti}。\n"
        f"生活情境：{scenario}\n"
        "目标：继续和朋友式心理支持助手聊天，表达真实想法、困惑、反应。\n"
        "限制：只输出1-3句用户口语，不要写旁白，不要解释自己在扮演，不要输出JSON。\n"
        f"当前轮次：{turn_idx}\n"
        f"最近对话：\n{history_text}\n"
        "请输出你下一句会说的话。"
    )


def generate_user_turn(mbti: str, scenario: str, history: List[Dict[str, str]], turn_idx: int) -> str:
    raw = chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "你是用户模拟器。根据指定 MBTI 倾向自然发言。"
                    "不用刻板标签，不要每次都重复场景。"
                ),
            },
            {"role": "user", "content": persona_prompt(mbti, scenario, history, turn_idx)},
        ],
        temperature=0.9,
        max_tokens=180,
    )
    out = (raw or "").strip().replace("\n", " ")
    if not out:
        out = "我现在脑子有点乱，感觉事情都挤在一起了。"
    return out


def parse_quality_json(text: str) -> Dict:
    text = (text or "").strip()
    if not text:
        return {}
    l = text.find("{")
    r = text.rfind("}")
    if l == -1 or r == -1 or r <= l:
        return {}
    try:
        return json.loads(text[l : r + 1])
    except Exception:
        return {}


def evaluate_quality(
    mbti: str,
    scenario: str,
    history: List[Dict[str, str]],
    last_score: float,
    decline_streak: int,
    min_turns_before_stop: int,
    decline_delta: float,
    stop_streak: int,
) -> QualityDecision:
    recent = history[-10:]
    conv = "\n".join(
        [f"{'用户' if x['role']=='user' else '助手'}：{x['content']}" for x in recent]
    )
    prompt = (
        f"场景：{scenario}\n"
        f"人格：{mbti}\n"
        f"最近对话：\n{conv}\n\n"
        "请评估最近这条助手回复的内容质量（0-10），标准：相关性、具体性、推进度、非重复性、自然度。\n"
        "只输出 JSON："
        '{"score":0-10,"dimensions":{"relevance":0-10,"specificity":0-10,"progress":0-10,"non_repetition":0-10,"naturalness":0-10},"reason":"<=30字"}'
    )
    raw = chat_completion(
        [
            {"role": "system", "content": "你是严格的对话质检员，只输出JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=180,
    )
    data = parse_quality_json(raw)
    score = float(data.get("score") or 5.0)
    score = max(0.0, min(10.0, score))
    dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
    reason = str(data.get("reason") or "无")

    new_decline_streak = decline_streak
    if score <= (last_score - decline_delta):
        new_decline_streak += 1
    else:
        new_decline_streak = 0

    turns_done = len([x for x in history if x["role"] == "assistant"])
    should_stop = turns_done >= min_turns_before_stop and new_decline_streak >= stop_streak
    if should_stop:
        reason = f"质量连续下降({new_decline_streak}轮): {reason}"

    return QualityDecision(
        score=score,
        decline_streak=new_decline_streak,
        should_stop=should_stop,
        reason=reason,
        dimensions=dimensions,
    )


def run_one_simulation(
    agent: DialogAgent,
    mbti: str,
    scenario: str,
    max_turns: int,
    min_turns_before_stop: int,
    decline_delta: float,
    stop_streak: int,
) -> Tuple[List[Dict], Dict]:
    state = deepcopy(STATE0)
    history: List[Dict[str, str]] = []
    records: List[Dict] = []
    last_score = 10.0
    decline_streak = 0
    stop_reason = "达到最大轮数"

    opener = (
        f"我最近真的有点扛不住了。{scenario}"
        "我知道该做事，但就是越想越乱。"
    )
    user_text = opener

    for turn_idx in range(1, max_turns + 1):
        assistant_text, state = agent.run(user_text, state, allow_show_report=False)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})

        q = evaluate_quality(
            mbti=mbti,
            scenario=scenario,
            history=history,
            last_score=last_score,
            decline_streak=decline_streak,
            min_turns_before_stop=min_turns_before_stop,
            decline_delta=decline_delta,
            stop_streak=stop_streak,
        )
        last_score = q.score
        decline_streak = q.decline_streak

        records.append(
            {
                "turn": turn_idx,
                "ts": iso_now(),
                "mbti": mbti,
                "scenario": scenario,
                "user": user_text,
                "assistant": assistant_text,
                "quality_score": q.score,
                "quality_dimensions": q.dimensions,
                "decline_streak": q.decline_streak,
                "quality_reason": q.reason,
            }
        )

        if q.should_stop:
            stop_reason = q.reason
            break

        user_text = generate_user_turn(mbti, scenario, history, turn_idx + 1)

    summary = {
        "mbti": mbti,
        "scenario": scenario,
        "turns": len(records),
        "avg_quality": round(
            sum(x["quality_score"] for x in records) / max(1, len(records)),
            2,
        ),
        "last_quality": records[-1]["quality_score"] if records else None,
        "stop_reason": stop_reason,
        "end_ts": iso_now(),
    }
    return records, summary


def write_jsonl(path: str, rows: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 5 个随机 MBTI 与 DialogAgent 的模拟对话并落盘日志。")
    parser.add_argument("--count", type=int, default=5, help="模拟人数，默认 5")
    parser.add_argument("--max-turns", type=int, default=12, help="每场最多轮数，默认 12")
    parser.add_argument("--min-turns-before-stop", type=int, default=4, help="至少聊多少轮后才允许因降质停止")
    parser.add_argument("--decline-delta", type=float, default=0.8, help="判定降质的最小降分幅度")
    parser.add_argument("--stop-streak", type=int, default=2, help="连续降质多少轮后停止")
    parser.add_argument("--output-dir", type=str, default="simulation_logs", help="输出目录")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    count = max(1, min(args.count, len(MBTI_TYPES)))
    selected = random.sample(MBTI_TYPES, k=count)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.output_dir, f"mbti_run_{run_id}")
    ensure_dir(out_dir)

    agent = DialogAgent()
    all_summary = []

    for mbti in selected:
        records, summary = run_one_simulation(
            agent=agent,
            mbti=mbti,
            scenario=SCENARIO,
            max_turns=args.max_turns,
            min_turns_before_stop=args.min_turns_before_stop,
            decline_delta=args.decline_delta,
            stop_streak=args.stop_streak,
        )
        write_jsonl(os.path.join(out_dir, f"{mbti}.jsonl"), records)
        all_summary.append(summary)
        print(f"[{mbti}] turns={summary['turns']} avg={summary['avg_quality']} stop={summary['stop_reason']}")

    summary_obj = {
        "run_id": run_id,
        "created_at": iso_now(),
        "scenario": SCENARIO,
        "selected_mbti": selected,
        "settings": {
            "max_turns": args.max_turns,
            "min_turns_before_stop": args.min_turns_before_stop,
            "decline_delta": args.decline_delta,
            "stop_streak": args.stop_streak,
            "seed": args.seed,
        },
        "results": all_summary,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary_obj, f, ensure_ascii=False, indent=2)

    print(f"\n完成：日志已输出到 {out_dir}")


if __name__ == "__main__":
    main()
