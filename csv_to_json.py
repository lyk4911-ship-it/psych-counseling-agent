import csv, json, sys, re

csv_path = sys.argv[1] if len(sys.argv) > 1 else "strategies.csv"
json_path = sys.argv[2] if len(sys.argv) > 2 else "strategies.json"

# ------------------------
# 自动识别“多值字段”函数
# ------------------------
def split_multi(s):
    if not s:
        return []
    s = str(s)
    # 支持：中文分号；英文分号；逗号（避免破坏英文句子，只对简单用例）
    if ";" in s or "；" in s:
        s = s.replace("；", ";")
        return [x.strip() for x in s.split(";") if x.strip()]
    return s  # 返回原值（可能是单值，也可能是句子）


# ------------------------
# 尝试数字化（int/float）函数
# ------------------------
def try_num(v):
    if v is None:
        return v
    v = str(v).strip()
    if v == "":
        return v
    # float 最通用
    try:
        return float(v)
    except:
        return v  # 保留原样


# ------------------------
# 主转换流程
# ------------------------
rows = []

with open(csv_path, encoding="utf-8") as f:
    reader = csv.DictReader(f)

    for r in reader:
        item = {}

        for k, v in r.items():

            # 1) 多值字段 → list
            if v and (";" in v or "；" in v):
                item[k] = split_multi(v)
                continue

            # 2) 数字段 → float（策略库 duration_min/dosage… 情感库也可能有）
            # 统一做数值尝试
            nv = try_num(v)
            item[k] = nv

        rows.append(item)

# 输出 JSON
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)

print(f"已导出 -> {json_path} （共 {len(rows)} 条）")
