"""汇总 S5/S6 的 4 份 JSONL 记录 → bench/prompt_eval/metrics_s5_s6.json 并打印摘要。

用法：
    python bench/prompt_eval/analyze_s5_s6.py

指标：每轮步骤（milestone/est_min）、近似重复标题对（difflib ratio>0.75）、
各阶段首次开启轮次、JSON 合法性、语言一致性（英文目标含 CJK 即为违规）。
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
FILES = [
    "s5_video30_A.jsonl",
    "s5_video30_B.jsonl",
    "s6_guitar45_A.jsonl",
    "s6_guitar45_B.jsonl",
]

CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def analyze(path: Path) -> dict:
    rounds = load(path)
    outline = rounds[0]["payload"]["outline"]
    generated: list[dict] = []
    per_round: list[dict] = []
    phase_first_open: dict[str, str] = {outline[0]: "initial"}
    json_ok = 0
    language_violations: list[str] = []

    for rec in rounds:
        steps = rec["response"].get("steps")
        if steps is not None:
            json_ok += 1
        steps = steps or []
        for s in steps:
            if s["milestone"] not in phase_first_open:
                phase_first_open[s["milestone"]] = f"round {rec['round']}"
            if CJK.search(s["title"]):
                language_violations.append(s["title"])
        generated.extend(steps)
        per_round.append(
            {
                "round": rec["round"],
                "days_left": rec["payload"].get("days_left"),
                "steps": [
                    {"milestone": s["milestone"], "title": s["title"], "est_min": s["est_min"]}
                    for s in steps
                ],
                "milestone_violations": rec["response"].get("milestone_violations", []),
                "errors": rec["response"].get("errors", []),
                "parse_error": rec["response"].get("parse_error"),
            }
        )

    titles = [g["title"] for g in generated]
    dup_pairs: list[dict] = []
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            ratio = SequenceMatcher(None, titles[i].lower(), titles[j].lower()).ratio()
            if ratio > 0.75:
                dup_pairs.append({"a": titles[i], "b": titles[j], "ratio": round(ratio, 3)})

    return {
        "file": path.name,
        "outline": outline,
        "rounds_total": len(rounds),
        "json_ok": json_ok,
        "language_violations": language_violations,
        "phase_first_open": phase_first_open,
        "phases_never_opened": [p for p in outline if p not in phase_first_open],
        "near_dup_pairs_count": len(dup_pairs),
        "near_dup_pairs": dup_pairs,
        "per_round": per_round,
    }


def main() -> None:
    metrics = [analyze(OUT_DIR / name) for name in FILES]
    out = OUT_DIR / "metrics_s5_s6.json"
    out.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    for m in metrics:
        print(f"== {m['file']} ==  json_ok={m['json_ok']}/{m['rounds_total']}  "
              f"dup_pairs={m['near_dup_pairs_count']}  lang_viol={len(m['language_violations'])}")
        print("  phases:", m["phase_first_open"])
        for pr in m["per_round"]:
            titles = [f"[{s['milestone'][:14]}] {s['title']} ({s['est_min']}m)" for s in pr["steps"]]
            print(f"  r{pr['round']} (days_left={pr['days_left']}): " + " | ".join(titles))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
