"""读取 bench/prompt_eval/s3_exam7_<A|B>.jsonl 与 s4_swim30_<A|B>.jsonl，
生成 REPORT_exam_swim.md 的量化部分。

指标：每轮步骤列表(milestone/est_min)、近似重复对(difflib ratio>0.75)、
各阶段首次开启轮、S3 第 6 轮落点、JSON 合法性、语言一致性。

注意：bench/prompt_eval/ 有并行任务共用，本脚本文件名与产出报告名均为
exam_swim 专属，避免互相覆盖。
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from itertools import combinations
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

SCEN = {
    "s3_exam7": {
        "label": "S3 备考·7天极紧（复习机器学习）",
        "outline": [
            "梳理考点与摸底自测",
            "分题型攻克核心概念与计算",
            "真题演练与错题归因",
            "限时模拟自测与查漏补缺",
        ],
        "initial": ["收集课件目录与往年真题，列出考点清单",
                    "按考点做10道摸底题并标记会/不熟/不会"],
    },
    "s4_swim30": {
        "label": "S4 游泳·30天技能型（要学会自由泳）",
        "outline": [
            "熟悉水性：呼吸控制与水中漂浮",
            "分解打腿与划手动作",
            "换气与手脚配合协调",
            "完整游进与距离耐力提升",
            "独立展示自由泳",
        ],
        "initial": ["查清附近两个泳池的开放时段、票价与浅水区情况",
                    "在陆上练习自由泳呼吸节奏（吸气2秒、水下呼气4秒）"],
    },
}
VARIANTS = {"A": "现状（线上 UPCOMING_SYSTEM）", "B": "候选 v2（时间预算+重复规则）"}
DUP_THRESHOLD = 0.75


def is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def load(run_key: str) -> list[dict]:
    with (OUT_DIR / f"{run_key}.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def dup_pairs(titles: list[str]) -> list[tuple[str, str, float]]:
    pairs = []
    for a, b in combinations(titles, 2):
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r > DUP_THRESHOLD:
            pairs.append((a, b, round(r, 3)))
    return pairs


def analyze(run_key: str) -> dict:
    scen_key, variant = run_key.rsplit("_", 1)
    scen = SCEN[scen_key]
    rounds = load(run_key)
    all_titles = list(scen["initial"])
    gen_titles = []
    per_round = []
    phase_open: dict[str, int | None] = {p: None for p in scen["outline"]}
    json_ok, lang_bad, violations = 0, [], []
    for rec in rounds:
        gen = rec["generated"]
        per_round.append([
            {"title": g["title"], "milestone": g["milestone"], "est_min": g["est_min"]}
            for g in gen
        ])
        for g in gen:
            all_titles.append(g["title"])
            gen_titles.append(g["title"])
            if g["milestone"] in phase_open and phase_open[g["milestone"]] is None:
                phase_open[g["milestone"]] = rec["round"]
            if not is_cjk(g["title"]):
                lang_bad.append(g["title"])
        if rec["response"]:
            try:
                json.loads(rec["response"])
                json_ok += 1
            except json.JSONDecodeError:
                violations.append(f"round{rec['round']}: json_decode_error")
        if rec["violations"]:
            violations.append(f"round{rec['round']}: {rec['violations']}")
    return {
        "scen_key": scen_key, "variant": variant,
        "label": scen["label"],
        "per_round": per_round,
        "phase_open": phase_open,
        "round6_milestones": sorted({g["milestone"] for g in rounds[-1]["generated"]}) if rounds else [],
        "pairs_all": dup_pairs(all_titles),
        "pairs_gen": dup_pairs(gen_titles),
        "n_rounds": len(rounds),
        "json_ok": json_ok,
        "lang_bad": lang_bad,
        "violations": violations,
        "retries_total": sum(r["retries"] for r in rounds),
    }


def fmt_steps(steps: list[dict]) -> str:
    if not steps:
        return "  （无）"
    out = []
    for i, s in enumerate(steps, 1):
        ms = s["milestone"]
        short = ms if len(ms) <= 14 else ms[:13] + "…"
        out.append(f"  {i}. [{short}] {s['title']}（{s['est_min']}m）")
    return "\n".join(out)


def section(res: dict) -> str:
    scen = SCEN[res["scen_key"]]
    lines = [f"### {res['label']} × 变体 {res['variant']}（{VARIANTS[res['variant']]}）", ""]
    lines.append(f"- 轮数：{res['n_rounds']}；JSON 合法：{res['json_ok']}/{res['n_rounds']}；"
                 f"LLM 重试合计：{res['retries_total']}；违规：{res['violations'] or '无'}")
    lines.append(f"- 语言不一致步骤：{res['lang_bad'] or '无'}")
    lines.append("")
    lines.append("每轮生成步骤：")
    for i, steps in enumerate(res["per_round"], 1):
        lines.append(f"- 第 {i} 轮：")
        lines.append(fmt_steps(steps))
    lines.append("")
    lines.append("各阶段首次开启轮（生成的步骤首次落到该阶段；阶段 1 初始已有完成步骤）：")
    for p in scen["outline"]:
        r = res["phase_open"][p]
        lines.append(f"- {p}：{'第 ' + str(r) + ' 轮' if r else '从未'}")
    if res["scen_key"] == "s3_exam7":
        lines.append(f"- 第 6 轮（考前 1 天）步骤所在阶段：{res['round6_milestones']}")
    lines.append("")
    lines.append(
        f"近似重复标题对（difflib ratio>{DUP_THRESHOLD}）：生成步骤之间 {len(res['pairs_gen'])} 对；"
        f"含初始步骤共 {len(res['pairs_all'])} 对。")
    for a, b, r in (res["pairs_gen"][:5] or res["pairs_all"][:5]):
        lines.append(f"  - ratio={r}：「{a}」 ↔ 「{b}」")
    lines.append("")
    return "\n".join(lines)


def model_line() -> str:
    os.environ["GOALCAL_DB_URL"] = "sqlite:////Users/macmini/projects/GoalCal/var/goalcal-production.db"
    sys.path.insert(0, "/Users/macmini/projects/GoalCal")
    from goalcal.config import settings
    return f"`{settings.llm_model}` @ {settings.llm_base_url}，temperature=0.0，response_format=json_object"


def main() -> None:
    results = {}
    for scen_key in SCEN:
        for variant in ("A", "B"):
            results[f"{scen_key}_{variant}"] = analyze(f"{scen_key}_{variant}")
    report = OUT_DIR / "REPORT_exam_swim.md"
    with report.open("w", encoding="utf-8") as fh:
        fh.write("# 滚动续批 prompt 泛化性评测：备考(S3) × 游泳(S4) × 变体 A/B\n\n")
        fh.write(f"LLM：{model_line()}；每变体 6 轮滚动模拟（每轮生成 2-3 步并模拟全部完成）。\n\n")
        for key in ("s3_exam7_A", "s3_exam7_B", "s4_swim30_A", "s4_swim30_B"):
            fh.write(section(results[key]))
        fh.write("## 关键判断\n\n（待填）\n")
    for key, res in results.items():
        print(f"== {key}: rounds={res['n_rounds']} json_ok={res['json_ok']} "
              f"dup_gen={len(res['pairs_gen'])} dup_all={len(res['pairs_all'])} "
              f"viol={res['violations'] or '-'} retries={res['retries_total']}")
        print("   phase_open:", res["phase_open"])
        if res["scen_key"] == "s3_exam7":
            print("   round6:", res["round6_milestones"])


if __name__ == "__main__":
    main()
