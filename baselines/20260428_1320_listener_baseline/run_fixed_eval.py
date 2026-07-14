#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
固定话术回归评测：
1) 使用固定 MBTI 用户脚本驱动 DialogAgent
2) 生成每轮评分与总体评分
3) 输出结果并追加到历史榜单（便于每次改代码后对比）
"""

import argparse
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Tuple

from dialog_agent import DialogAgent, STATE0


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_fixture(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[，。！？、,.!?；;：:·\-—\[\]\(\)\"'“”‘’]", "", t)
    return t


def char_jaccard(a: str, b: str) -> float:
    sa, sb = set(normalize_text(a)), set(normalize_text(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))


def keyword_overlap_score(user_text: str, reply: str) -> float:
    """
    相关性评分（修订）：
    - 场景关键词命中
    - 用户-回复字符相似度（弱语义近似）
    """
    keywords = [
        "论文", "导师", "开会", "会议", "PPT", "初稿", "进度", "焦虑",
        "睡眠", "时间", "计划", "数据", "方法论", "卡住", "汇报", "框架",
        "拖延", "紧张", "问题", "明天", "今晚",
    ]
    topic_hit = 0
    for kw in keywords:
        if kw in user_text and kw in reply:
            topic_hit += 1
    kw_score = min(10.0, 4.8 + topic_hit * 0.85)
    sim_score = min(10.0, 3.5 + char_jaccard(user_text, reply) * 12.0)
    return (0.62 * kw_score) + (0.38 * sim_score)


def comfort_score(reply: str) -> float:
    """
    用户是否会觉得这段回复读起来舒服、松一点。
    """
    score = 7.1
    good = [
        "嗯", "是啊", "确实", "挺", "会", "难免", "我在", "先不用",
        "没事", "正常", "磨人", "不容易", "能理解", "接得住",
    ]
    bad = [
        "首先", "其次", "综上所述", "建议您", "应该", "必须", "立刻",
        "你需要", "你得", "你必须", "请立即",
    ]
    for w in good:
        if w in reply:
            score += 0.18
    for w in bad:
        if w in reply:
            score -= 0.45
    if len(reply) > 260:
        score -= 0.7
    if "·" in reply:
        score -= 0.35
    return max(0.0, min(10.0, score))


def naturalness_score(reply: str) -> float:
    score = 8.8
    for bad in ["综上所述", "首先", "其次", "建议您", "尊敬的用户", "本平台"]:
        if bad in reply:
            score -= 1.4
    n = len(reply)
    if n > 320:
        score -= 1.2
    elif n > 240:
        score -= 0.7
    return max(0.0, min(10.0, score))


def chat_flow_score(reply: str) -> float:
    """
    像不像真实聊天，而不是像文章/流程。
    """
    score = 7.0
    line_count = len([x for x in reply.split("\n") if x.strip()])
    sent_count = len([x for x in re.split(r"[。！？]", reply) if x.strip()])
    if line_count <= 3:
        score += 0.35
    if 1 <= sent_count <= 4:
        score += 0.45
    if len(reply) > 280:
        score -= 0.9
    if re.search(r"\d+[\.、]|1）|2）|3）", reply):
        score -= 0.8
    if "·" in reply:
        score -= 0.35
    if re.search(r"【.*】", reply):
        score -= 0.8
    return max(0.0, min(10.0, score))


def listener_score(user_text: str, reply: str) -> float:
    """
    像不像一个会听的人，而不是一个急着处理问题的人。
    """
    score = 6.8
    emotion_words = ["烦", "累", "乱", "慌", "焦虑", "卡住", "难受", "磨人", "压着"]
    mirrored = 0
    for w in emotion_words:
        if w in user_text and w in reply:
            mirrored += 1
    score += min(1.0, mirrored * 0.28)
    if re.search(r"能理解|正常|会这样|确实|不容易|挺磨人", reply):
        score += 0.55
    if re.search(r"先|然后|接下来|现在就|试试|不妨|可以先|今晚|明天", reply):
        score -= 0.45
    if "·" in reply:
        score -= 0.4
    return max(0.0, min(10.0, score))


def likability_score(reply: str) -> float:
    """
    用户会不会愿意继续和它聊。
    """
    score = 6.9
    warm = ["我在", "没事", "慢慢来", "先这样也行", "不用急", "嗯", "是啊"]
    stiff = ["建议您", "应当", "综上所述", "根据你的情况", "作为你的"]
    for w in warm:
        if w in reply:
            score += 0.22
    for w in stiff:
        if w in reply:
            score -= 0.7
    if len(reply) > 240:
        score -= 0.45
    return max(0.0, min(10.0, score))


def repetition_penalty(reply: str, prev_replies: List[str]) -> float:
    if not prev_replies:
        return 0.0
    sim = max((char_jaccard(reply, x) for x in prev_replies[-3:]), default=0.0)
    if sim >= 0.86:
        return 2.2
    if sim >= 0.78:
        return 1.3
    if sim >= 0.70:
        return 0.7
    return 0.0


def question_penalty(reply: str) -> float:
    qn = reply.count("?") + reply.count("？")
    if qn >= 3:
        return 1.3
    if qn == 2:
        return 0.8
    return 0.0


def pseudo_question_penalty(reply: str) -> float:
    """
    没有问号但本质在向用户索取回答/推进。
    """
    patterns = [
        r"更像哪种", r"有没有", r"要不要", r"是不是", r"要不", r"不如你",
        r"愿意说说", r"说说看", r"聊聊看", r"讲讲", r"展开说",
        r"你现在.*像", r"你打算", r"你会不会", r"你手边有",
    ]
    hits = sum(1 for p in patterns if re.search(p, reply))
    if hits >= 2:
        return 1.4
    if hits == 1:
        return 0.7
    return 0.0


def pressure_penalty(reply: str) -> float:
    """
    是否给用户明显压力/控制感。
    """
    penalty = 0.0
    if re.search(r"你需要|你得|你必须|立刻|马上|现在就|赶紧", reply):
        penalty += 1.0
    if "·" in reply:
        penalty += 0.3
    if re.search(r"先.*然后|接下来", reply):
        penalty += 0.35
    return penalty


def robotic_penalty(reply: str) -> float:
    """
    是否像在跑流程/像客服，而不是像朋友。
    """
    penalty = 0.0
    patterns = [
        r"综上所述", r"首先", r"其次", r"此外", r"根据你的情况",
        r"作为你的", r"本平台", r"建议您", r"【.*】",
    ]
    hits = sum(1 for p in patterns if re.search(p, reply))
    if hits >= 2:
        penalty += 1.4
    elif hits == 1:
        penalty += 0.7
    if len(reply) > 320:
        penalty += 0.5
    return penalty


def score_turn(user_text: str, reply: str, prev_replies: List[str]) -> Dict[str, Any]:
    relevance = keyword_overlap_score(user_text, reply)
    naturalness = naturalness_score(reply)
    comfort = comfort_score(reply)
    flow = chat_flow_score(reply)
    listener = listener_score(user_text, reply)
    likability = likability_score(reply)
    rep_pen = repetition_penalty(reply, prev_replies)
    q_pen = question_penalty(reply)
    pseudo_q_pen = pseudo_question_penalty(reply)
    pressure_pen = pressure_penalty(reply)
    robotic_pen = robotic_penalty(reply)
    human_likeness = (0.28 * naturalness) + (0.24 * flow) + (0.24 * listener) + (0.24 * likability)
    overall = (
        (0.10 * relevance)
        + (0.30 * comfort)
        + (0.22 * flow)
        + (0.18 * listener)
        + (0.12 * likability)
        + (0.08 * naturalness)
        - rep_pen
        - q_pen
        - pseudo_q_pen
        - pressure_pen
        - robotic_pen
    )
    final = max(0.0, min(10.0, overall))
    human_likeness = max(0.0, min(10.0, human_likeness - rep_pen - q_pen - pseudo_q_pen - robotic_pen))
    final = max(0.0, min(10.0, final))
    return {
        "score": round(final, 2),
        "human_likeness_score": round(human_likeness, 2),
        "dimensions": {
            "relevance": round(relevance, 2),
            "comfort": round(comfort, 2),
            "chat_flow": round(flow, 2),
            "listener": round(listener, 2),
            "likability": round(likability, 2),
            "naturalness": round(naturalness, 2),
            "repetition_penalty": round(rep_pen, 2),
            "question_penalty": round(q_pen, 2),
            "pseudo_question_penalty": round(pseudo_q_pen, 2),
            "pressure_penalty": round(pressure_pen, 2),
            "robotic_penalty": round(robotic_pen, 2),
        },
    }


def run_script(agent: DialogAgent, mbti: str, user_turns: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    state = deepcopy(STATE0)
    rows: List[Dict[str, Any]] = []
    prev_replies: List[str] = []

    for idx, user in enumerate(user_turns, start=1):
        reply, state = agent.run(user, state, allow_show_report=False)
        s = score_turn(user, reply, prev_replies)
        prev_replies.append(reply)
        rows.append(
            {
                "turn": idx,
                "ts": iso_now(),
                "mbti": mbti,
                "user": user,
                "assistant": reply,
                "score": s["score"],
                "human_likeness_score": s["human_likeness_score"],
                "score_dimensions": s["dimensions"],
            }
        )

    avg_score = round(sum(r["score"] for r in rows) / max(1, len(rows)), 2)
    avg_human = round(sum(r["human_likeness_score"] for r in rows) / max(1, len(rows)), 2)
    summary = {
        "mbti": mbti,
        "turns": len(rows),
        "avg_score": avg_score,
        "avg_human_likeness_score": avg_human,
        "min_score": min((r["score"] for r in rows), default=None),
        "max_score": max((r["score"] for r in rows), default=None),
    }
    return rows, summary


def write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_history(history_path: str, run_summary: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(history_path))
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(run_summary, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="固定 MBTI 话术回归评测")
    parser.add_argument("--fixture", type=str, default="eval_fixtures/fixed_mbti_scripts.json", help="固定脚本文件")
    parser.add_argument("--output-dir", type=str, default="simulation_logs", help="输出目录")
    parser.add_argument("--history-file", type=str, default="simulation_logs/fixed_eval_history.jsonl", help="历史榜单文件")
    args = parser.parse_args()

    fixture = load_fixture(args.fixture)
    scripts = fixture.get("scripts") or {}
    if not isinstance(scripts, dict) or not scripts:
        raise ValueError("fixture scripts 为空或格式错误")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.output_dir, f"fixed_eval_{run_id}")
    ensure_dir(out_dir)

    agent = DialogAgent()
    summaries: List[Dict[str, Any]] = []

    for mbti, user_turns in scripts.items():
        if not isinstance(user_turns, list) or not user_turns:
            continue
        rows, summary = run_script(agent, mbti, user_turns)
        write_jsonl(os.path.join(out_dir, f"{mbti}.jsonl"), rows)
        summaries.append(summary)
        print(
            f"[{mbti}] turns={summary['turns']} avg={summary['avg_score']} "
            f"human={summary['avg_human_likeness_score']}"
        )

    overall = round(sum(s["avg_score"] for s in summaries) / max(1, len(summaries)), 2)
    overall_human = round(sum(s["avg_human_likeness_score"] for s in summaries) / max(1, len(summaries)), 2)
    result = {
        "run_id": run_id,
        "created_at": iso_now(),
        "fixture": args.fixture,
        "overall_avg_score": overall,
        "overall_human_likeness_score": overall_human,
        "results": summaries,
    }

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    append_history(args.history_file, result)
    print(f"\n完成：{out_dir}")
    print(f"overall_avg_score={overall}")
    print(f"overall_human_likeness_score={overall_human}")


if __name__ == "__main__":
    main()
