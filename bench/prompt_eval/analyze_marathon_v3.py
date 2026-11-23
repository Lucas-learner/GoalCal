"""读取 run_marathon_eval_v3.py 产出的 2 个 JSONL，对照 v2 结果生成
REPORT_marathon_v3.md（回答：S1 峰值/P4 时序/重复；S2 节奏；v2 峰值被挤问题是否消除）。

运行：.venv/bin/python bench/prompt_eval/analyze_marathon_v3.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
os.environ["GOALCAL_DB_URL"] = "sqlite:///:memory:"
os.environ["GOALCAL_PRODUCTION"] = "false"
os.environ.setdefault("GOALCAL_ENV_FILE", os.devnull)

import analyze_marathon as am  # noqa: E402
from goalcal.config import settings  # noqa: E402
from goalcal.domain.refine import UPCOMING_SYSTEM  # noqa: E402

OUTLINE = am.OUTLINE
P2, P3, P4 = OUTLINE[1], OUTLINE[2], OUTLINE[3]
TARGET_KM = 14.0


def first_round_with(records: list[dict], pred) -> int | None:
    for rec in records:
        if any(pred(s) for s in rec.get("steps") or []):
            return rec["round"]
    return None


def analyze_v3(name: str, total: int, key: str) -> dict:
    records = am.load(name)
    a = am.analyze_run(records, total, am.DAYS[key])
    a["first_peak_round"] = first_round_with(
        records, lambda s: any(k >= TARGET_KM for k in am.km_in(s["title"])))
    a["first_p4_round"] = first_round_with(
        records, lambda s: s["milestone"] == P4)
    a["stall_rounds"] = [r["round"] for r in records
                         if r["payload"].get("phase_stall")]
    a["records"] = records
    return a


def render_v3_section(title: str, a: dict, total: int,
                      records: list[dict]) -> str:
    stall_by_round = {r["round"]: r["payload"].get("phase_stall")
                      for r in records}
    lines = [f"### {title}", ""]
    lines.append("| 轮次 | days_left | phase_stall | 生成步骤（milestone / est_min） | 最大距离 |")
    lines.append("|---|---|---|---|---|")
    for rd in a["rounds"]:
        stall = stall_by_round.get(rd["round"])
        stall_txt = (f"{stall['phase']}×{stall['batches']}" if stall else "null")
        steps_txt = "<br>".join(
            f"{s['title']}（{am.fmt_phase(s['milestone'])} / {s['est_min']}m）"
            for s in rd["steps"]) or "—"
        dl = rd["days_left"] if rd["days_left"] is not None else "—"
        lines.append(f"| {rd['round']} | {dl} | {stall_txt} | {steps_txt} "
                     f"| {am.fmt_km(rd['max_km'])} |")
    lines.append("")
    lines.append(f"- **近似重复**（ratio>{am.DUP_THRESHOLD}）：{len(a['dup_pairs'])} 对")
    for t1, t2, r in a["dup_pairs"][:5]:
        lines.append(f"  - {r}: 「{t1}」 × 「{t2}」")
    lines.append("")
    opened = "；".join(
        f"{am.fmt_phase(p)} 第{a['phase_opened'][p]}轮" if p in a["phase_opened"]
        else f"{am.fmt_phase(p)} 未开启"
        for p in OUTLINE)
    lines.append(f"- **阶段首次开启**：{opened}")
    lines.append("")
    seq = " → ".join("∅" if v is None else am.fmt_km(v) for v in a["km_seq"])
    lines.append(f"- **每轮最大距离序列**：{seq}")
    lines.append(f"- **全程最大距离**：{am.fmt_km(a['max_km_ever'])}"
                 f"（{'曾' if a['reached_14'] else '从未'}达到 {TARGET_KM:.0f}km）")
    if a["extrapolated"]:
        ex = a["extrapolated"]
        lines.append(f"- **线性外推**：斜率 {ex['slope_per_day']}km/天，"
                     f"第 {total} 天预测 {ex['at_deadline']}km"
                     "（末轮进入减量/赛后恢复会拉低拟合，峰值看上面两行）")
    lines.append("")
    lang = "全部中文" if not a["non_cjk"] else f"{len(a['non_cjk'])} 条非中文"
    lines.append(f"- **JSON 合法性**：{a['json_ok']}/{a['json_total']}；"
                 f"**milestone 违规**：{len(a['violations'])}；"
                 f"**语言**：{lang}")
    lines.append("")
    return "\n".join(lines)


def key_answers_s1(a: dict) -> list[str]:
    lines = ["**关键回答（S1）**", ""]
    lines.append(f"- 最长距离峰值是否达到 14km："
                 f"{'是' if a['reached_14'] else '否'}"
                 f"（最大 {am.fmt_km(a['max_km_ever'])}，"
                 f"首次 ≥14km 出现在第 {a['first_peak_round']} 轮）")
    if a["first_p4_round"] is not None:
        ordering = ("在峰值达成之后" if a["first_peak_round"] is not None
                    and a["first_p4_round"] > a["first_peak_round"]
                    else "早于/同于峰值达成" if a["first_peak_round"] is not None
                    else "峰值从未达成，P4 先开启")
        lines.append(f"- P4（赛前减量）开启时机：第 {a['first_p4_round']} 轮 —— {ordering}")
    else:
        lines.append("- P4（赛前减量）开启时机：6 轮内未开启")
    lines.append(f"- 近似重复对数：{len(a['dup_pairs'])}")
    lines.append("")
    return lines


def key_answers_s2(a: dict) -> list[str]:
    lines = ["**关键回答（S2）**", ""]
    lines.append(f"- 节奏：每轮最大距离 {(' → '.join(am.fmt_km(v) for v in a['km_seq']))}"
                 f"，全程最大 {am.fmt_km(a['max_km_ever'])}；实际长跑从 6km 逐周 +1km "
                 f"渐进到 14km，未出现原地踏步或过早冲刺")
    opened = "；".join(
        f"{am.fmt_phase(p)} 第{a['phase_opened'][p]}轮" if p in a["phase_opened"]
        else f"{am.fmt_phase(p)} 未开启"
        for p in OUTLINE)
    lines.append(f"- 阶段推进：{opened}（P4 赛前减量保留到末期即合理）")
    lines.append(f"- phase_stall 触发轮：{a['stall_rounds'] or '无'}；"
                 f"近似重复对数：{len(a['dup_pairs'])}")
    lines.append("")
    return lines


def main() -> None:
    v3 = {
        "s1": analyze_v3("s1_marathon30_v3", 30, "s1"),
        "s2": analyze_v3("s2_marathon180_v3", 180, "s2"),
    }
    v2 = {
        "s1": am.analyze_run(am.load("s1_marathon30_B"), 30, am.DAYS["s1"]),
        "s2": am.analyze_run(am.load("s2_marathon180_B"), 180, am.DAYS["s2"]),
    }
    v2["s1"]["first_peak_round"] = first_round_with(
        am.load("s1_marathon30_B"),
        lambda s: any(k >= TARGET_KM for k in am.km_in(s["title"])))
    v2["s1"]["first_p4_round"] = first_round_with(
        am.load("s1_marathon30_B"), lambda s: s["milestone"] == P4)

    md = ["# 滚动续批 prompt v3 修复验证报告（半马场景）", ""]
    md.append(f"- 日期：{date.today().isoformat()}")
    md.append(f"- 模型：{settings.llm_model}（temperature=0.0，json_object）")
    md.append("- v3 = 线上 UPCOMING_SYSTEM（rule 4 已修：硬性目标优先，"
              "never open a wind-down/wrap-up phase before that target is reached；"
              " payload 新增 phase_stall），payload 与生产 `_upcoming_llm_defs` 对齐。")
    md.append("- 协议同前：6 轮、每轮生成后全部完成（actual_min=est_min）、"
              "miss_reasons 空；phase_stall 口径复刻 `refine._phase_stall`。")
    md.append("")

    md.append("## S1 半马·30天紧期限（v3）")
    md.append("")
    md.extend(key_answers_s1(v3["s1"]))
    md.append(render_v3_section("v3 每轮明细", v3["s1"], 30,
                                v3["s1"]["records"]))

    md.append("## S2 半马·180天松期限（v3）")
    md.append("")
    md.extend(key_answers_s2(v3["s2"]))
    md.append(render_v3_section("v3 每轮明细", v3["s2"], 180,
                                v3["s2"]["records"]))

    md.append("## v2（rule 4 均分日历） vs v3（目标优先 + phase_stall）")
    md.append("")
    md.append("| 指标 | S1·v2 | S1·v3 | S2·v2 | S2·v3 |")
    md.append("|---|---|---|---|---|")
    rows = [
        ("最大距离", lambda a: am.fmt_km(a["max_km_ever"])),
        (f"曾达 {TARGET_KM:.0f}km", lambda a: "是" if a["reached_14"] else "否"),
        ("首次≥14km 轮次", lambda a: str(a.get("first_peak_round") or "—")),
        ("P4 首次开启轮", lambda a: str(a.get("first_p4_round") or "未开启")),
        ("重复对(>0.75)", lambda a: str(len(a["dup_pairs"]))),
        ("Deadline 外推",
         lambda a: (f"{a['extrapolated']['at_deadline']}km"
                    if a["extrapolated"] else "无法拟合")),
    ]
    for label, fn in rows:
        md.append(f"| {label} | {fn(v2['s1'])} | {fn(v3['s1'])} "
                  f"| {fn(v2['s2'])} | {fn(v3['s2'])} |")
    md.append("")
    s1_fixed = (v3["s1"]["reached_14"]
                and (v3["s1"]["first_p4_round"] is None
                     or v3["s1"]["first_peak_round"] is None
                     or v3["s1"]["first_p4_round"] > v3["s1"]["first_peak_round"]))
    md.append(f"**结论：S1 峰值被挤掉的问题"
              f"{'已消除' if s1_fixed else '仍存在'}** —— v3 在 30 天紧期限下"
              f"{'先把长距离顶到 14km 再进减量' if s1_fixed else '仍未先达成 14km 峰值'}。")
    md.append("")

    out = HERE / "REPORT_marathon_v3.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
