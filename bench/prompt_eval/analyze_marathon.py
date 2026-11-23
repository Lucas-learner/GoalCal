"""读取 run_marathon_eval.py 产出的 4 个马拉松 JSONL，生成 REPORT_marathon.md。

运行：.venv/bin/python bench/prompt_eval/analyze_marathon.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

sys.path.insert(0, str(ROOT))
os.environ["GOALCAL_DB_URL"] = "sqlite:///:memory:"
os.environ["GOALCAL_PRODUCTION"] = "false"
os.environ.setdefault("GOALCAL_ENV_FILE", os.devnull)
from goalcal.config import settings  # noqa: E402

DAYS = {"s1": [24, 21, 18, 15, 12, 9], "s2": [170, 155, 140, 125, 110, 95]}
RUNS = [  # (文件后缀, 场景名, 场景键, 总天数)
    ("s1_marathon30_A", "S1 半马·30天紧期限", "s1", 30),
    ("s1_marathon30_B", "S1 半马·30天紧期限", "s1", 30),
    ("s2_marathon180_A", "S2 半马·180天松期限", "s2", 180),
    ("s2_marathon180_B", "S2 半马·180天松期限", "s2", 180),
]

OUTLINE = [
    "基础评估与建立跑步节奏",
    "逐步拉长距离（最长跑到14公里）",
    "专项强化与配速适应",
    "赛前减量与比赛执行",
]

KM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:公里|千米|km|KM|Km)")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
DUP_THRESHOLD = 0.75


def load(name: str) -> list[dict]:
    path = HERE / f"{name}.jsonl"
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def km_in(title: str) -> list[float]:
    return [float(m.group(1)) for m in KM_RE.finditer(title)]


def linfit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return my - b * mx, b


def analyze_run(records: list[dict], total_days: int,
                days_left_list: list[int]) -> dict:
    rounds = []
    all_titles: list[str] = ["做一次20分钟走跑交替测试并记录数据",
                             "排出每周三次的训练日程并备齐跑鞋装备"]
    violations: list[dict] = []
    json_ok = 0
    non_cjk: list[str] = []
    km_all: list[float] = []
    phase_opened: dict[str, int | str] = {OUTLINE[0]: 0}  # 0 = 初始批次已开启

    for rec in records:
        steps = rec.get("steps") or []
        if rec.get("response") is not None:
            json_ok += 1
        for ms in rec.get("milestone_violations") or []:
            violations.append({"round": rec["round"], "milestone": ms})
        for s in steps:
            all_titles.append(s["title"])
            if not CJK_RE.search(s["title"]):
                non_cjk.append(s["title"])
            km_all.extend(km_in(s["title"]))
            ms = s["milestone"]
            if ms not in phase_opened:
                phase_opened[ms] = rec["round"]
        rounds.append({
            "round": rec["round"],
            "days_left": rec["payload"].get("days_left"),
            "steps": steps,
            "max_km": max((k for s in steps for k in km_in(s["title"])),
                          default=None),
        })

    # 近似重复：全部步骤标题（含 2 条种子）两两比较。
    dup_pairs = []
    for i in range(len(all_titles)):
        for j in range(i + 1, len(all_titles)):
            r = SequenceMatcher(None, all_titles[i], all_titles[j]).ratio()
            if r > DUP_THRESHOLD:
                dup_pairs.append((all_titles[i], all_titles[j], round(r, 3)))

    # 距离外推：x=已过天数（两变体都用场景日历；A 只是 payload 里没给模型），
    # y=该轮标题中的最大公里数（正则，计划/确认类步骤里的数字也会被计入）。
    xs, ys = [], []
    for rd in rounds:
        dl = rd["days_left"] if rd["days_left"] is not None \
            else days_left_list[rd["round"] - 1]
        if rd["max_km"] is not None:
            xs.append(float(total_days - dl))
            ys.append(rd["max_km"])
    fit = linfit(xs, ys)
    extrapolated = None
    if fit:
        a, b = fit
        extrapolated = {"slope_per_day": round(b, 4),
                        "at_deadline": round(a + b * total_days, 2)}

    return {
        "rounds": rounds,
        "json_ok": json_ok,
        "json_total": len(records),
        "dup_pairs": dup_pairs,
        "phase_opened": phase_opened,
        "violations": violations,
        "non_cjk": non_cjk,
        "km_seq": [rd["max_km"] for rd in rounds],
        "max_km_ever": max(km_all) if km_all else None,
        "reached_14": any(k >= 14 for k in km_all),
        "extrapolated": extrapolated,
        "errors": [e for rec in records for e in rec.get("errors") or []],
    }


def fmt_phase(name: str) -> str:
    short = {"基础评估与建立跑步节奏": "P1 基础评估",
             "逐步拉长距离（最长跑到14公里）": "P2 拉长距离",
             "专项强化与配速适应": "P3 专项强化",
             "赛前减量与比赛执行": "P4 赛前减量"}
    return short.get(name, name)


def fmt_km(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}".rstrip("0").rstrip(".") + "km"


def render_section(title: str, a: dict, total_days: int) -> str:
    lines = [f"### {title}", ""]
    lines.append("| 轮次 | days_left | 生成步骤（milestone / est_min） | 最大距离 |")
    lines.append("|---|---|---|---|")
    for rd in a["rounds"]:
        steps_txt = "<br>".join(
            f"{s['title']}（{fmt_phase(s['milestone'])} / {s['est_min']}m）"
            for s in rd["steps"]) or "—"
        dl = rd["days_left"] if rd["days_left"] is not None else "—"
        lines.append(f"| {rd['round']} | {dl} | {steps_txt} | {fmt_km(rd['max_km'])} |")
    lines.append("")

    n_dup = len(a["dup_pairs"])
    lines.append(f"- **近似重复**（ratio>{DUP_THRESHOLD}）：{n_dup} 对")
    for t1, t2, r in a["dup_pairs"][:5]:
        lines.append(f"  - {r}: 「{t1}」 × 「{t2}」")
    lines.append("")

    opened = "；".join(
        f"{fmt_phase(p)} 第{a['phase_opened'][p]}轮" if p in a["phase_opened"]
        else f"{fmt_phase(p)} 未开启"
        for p in OUTLINE)
    lines.append(f"- **阶段首次开启**：{opened}")
    lines.append("")

    seq = " → ".join("∅" if v is None else fmt_km(v) for v in a["km_seq"])
    lines.append(f"- **每轮最大距离序列**：{seq}")
    lines.append(f"- **全程出现的最大距离**：{fmt_km(a['max_km_ever'])}"
                 f"（{'曾' if a['reached_14'] else '从未'}达到大纲的 14km）")
    if a["extrapolated"]:
        ex = a["extrapolated"]
        reach = "能" if ex["at_deadline"] >= 14 else "不能"
        lines.append(
            f"- **线性外推**：观测斜率 {ex['slope_per_day']}km/天，"
            f"第 {total_days} 天预测 {ex['at_deadline']}km —— {reach}到大纲的 14km"
            "（注：进入赛前减量后距离回落会拉低拟合，需结合上一条判断）")
    else:
        lines.append("- **线性外推**：距离数据点不足（<2），无法拟合")
    lines.append("")

    lines.append(f"- **JSON 合法性**：{a['json_ok']}/{a['json_total']} 轮成功解析")
    if a["non_cjk"]:
        lines.append(f"- **语言一致性**：{len(a['non_cjk'])} 条非中文标题")
    else:
        lines.append("- **语言一致性**：全部中文 ✅")
    for t in a["non_cjk"][:3]:
        lines.append(f"  - 非中文：「{t}」")
    if a["violations"]:
        lines.append(f"- **milestone 违规**：{len(a['violations'])} 处 "
                     f"（如 {[v['milestone'] for v in a['violations'][:3]]}）")
    else:
        lines.append("- **milestone 违规**：0 ✅")
    if a["errors"]:
        lines.append(f"- **LLM 调用错误**：{len(a['errors'])} 次（已重试 1 次）")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    results = {name: analyze_run(load(name), total, DAYS[key])
               for name, _, key, total in RUNS}

    md = ["# 滚动续批 prompt 泛化性评测报告（半马场景）", ""]
    md.append(f"- 日期：{date.today().isoformat()}")
    md.append(f"- 模型：{settings.llm_model}（temperature=0.0，json_object）")
    md.append("- 方法：每场景×变体模拟 6 轮；每轮 LLM 生成 2–3 步后全部标记完成"
              "（actual_min=est_min）进入下一轮；miss_reasons 恒为空。")
    md.append("- 变体 A=线上 UPCOMING_SYSTEM（无天数信息）；变体 B=v2"
              "（payload 增加 days_left + phases 统计，规则 3/4/5 加强）。")
    md.append("- 距离口径：正则提取标题中的公里数，取每轮最大值；计划/确认类步骤"
              "里的公里数也会被计入（如「计划从6公里到10公里」），可能高于实际跑量。")
    md.append("- 外推口径：按已过天数（两变体同一张日历）最小二乘拟合，外推至"
              "第 30/180 天与大纲 14km 对比；近似重复池=全部步骤标题（含 2 条种子）。")
    md.append("")

    for key, s_name, total in (("s1", "S1 半马·30天紧期限", 30),
                               ("s2", "S2 半马·180天松期限", 180)):
        md.append(f"## {s_name}")
        md.append("")
        md.append(render_section("变体 A（现状 UPCOMING_SYSTEM）",
                                 results[f"{key}_marathon{total}_A"], total))
        md.append(render_section("变体 B（候选 v2）",
                                 results[f"{key}_marathon{total}_B"], total))

    md.append("## 横向结论")
    md.append("")
    md.append("| 场景×变体 | 重复对(>0.75) | P2/P3/P4 开启轮 | 最大距离 | 曾达14km | Deadline 外推 |")
    md.append("|---|---|---|---|---|---|")
    for name, scenario, _, total in RUNS:
        a = results[name]
        op = a["phase_opened"]

        def oround(p: str) -> str:
            v = op.get(p, "—")
            return f"第{v}轮" if isinstance(v, int) else "未开启"

        ex = (f"{a['extrapolated']['at_deadline']}km"
              if a["extrapolated"] else "无法拟合")
        md.append(f"| {scenario} · {'A' if name.endswith('_A') else 'B'} "
                  f"| {len(a['dup_pairs'])} "
                  f"| {oround(OUTLINE[1])}/{oround(OUTLINE[2])}/{oround(OUTLINE[3])} "
                  f"| {fmt_km(a['max_km_ever'])} "
                  f"| {'是' if a['reached_14'] else '否'} "
                  f"| {ex} |")
    md.append("")

    out = HERE / "REPORT_marathon.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
