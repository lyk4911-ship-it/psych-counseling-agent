# show_memory.py —— 长期记忆库：仅持久化 /show 咨询师报告（不存普通聊天记录）
# -*- coding: utf-8 -*-

import os
import json
import threading
from datetime import datetime
LOG_DIR = "logs"
MEMORY_FILE = os.path.join(LOG_DIR, "show_memory.jsonl")
_lock = threading.Lock()


def append_show_report(
    user_label: str,
    session_id: str,
    report_text: str,
) -> None:
    """
    追加一条 /show 报告记录（一行 JSON）。
    user_label: 测试者填写的「用户几」或昵称，用于检索。
    """
    label = (user_label or "").strip() or f"用户-{session_id[:8]}"
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user_label": label,
        "session_id": session_id,
        "report": report_text,
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _lock:
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(line)


def read_all_entries(max_lines: int = 500) -> list:
    """读取最近若干条（从文件末尾向前读，简单实现：读全文件取最后 N 行）"""
    if not os.path.exists(MEMORY_FILE):
        return []
    with _lock:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    out = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
