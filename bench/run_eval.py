"""意图/实体解析评测。

用法：
    python bench/run_eval.py --parser rules
    python bench/run_eval.py --parser llm
    python bench/run_eval.py --parser both --out bench/results.csv

输出：逐条结果 + 汇总准确率，直接贴进中期报告第 3 节。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from goalcal.llm.parser import parse  # noqa: E402
from goalcal.llm.rules import parse_with_rules  # noqa: E402

CASES = ROOT / "bench" / "cases_intents.jsonl"


def load_cases() -> list[dict]:
    with CASES.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def check(case: dict, action) -> tuple[bool, list[str]]:
    exp = case["expect"]
    problems: list[str] = []

    if action.intent != exp["intent"]:
        problems.append(f"intent {action.intent} != {exp['intent']}")

    if "start" in exp and action.start is not None:
        want = datetime.fromisoformat(exp["start"]).replace(tzinfo=action.start.tzinfo)
        if abs((action.start - want).total_seconds()) > 90 * 60:
            problems.append(f"start {action.start:%m-%d %H:%M} != {want:%m-%d %H:%M}")
    elif "start" in exp and action.start is None:
        problems.append("start missing")

    if "duration_min" in exp and action.duration_min != exp["duration_min"]:
        problems.append(f"duration {action.duration_min} != {exp['duration_min']}")

    if "deadline" in exp and (action.deadline is None or action.deadline.isoformat() != exp["deadline"]):
        problems.append(f"deadline {action.deadline} != {exp['deadline']}")

    if "state" in exp and action.state != exp["state"]:
        problems.append(f"state {action.state} != {exp['state']}")

    title_needle = exp.get("title_contains")
    if title_needle and title_needle.lower() not in (action.title or "").lower():
        problems.append(f"title '{action.title}' missing '{title_needle}'")

    return (not problems), problems


def run(parser_name: str, cases: list[dict]) -> list[dict]:
    rows = []
    for case in cases:
        now = datetime.fromisoformat(case["now"])
        if parser_name == "rules":
            action = parse_with_rules(case["text"], now).normalized(now)
        else:
            action, _ = parse(case["text"], now, use_llm=True)
        ok, problems = check(case, action)
        rows.append(
            {
                "id": case["id"],
                "text": case["text"],
                "expected": json.dumps(case["expect"], ensure_ascii=False),
                "got_intent": action.intent,
                "got_start": action.start.isoformat() if action.start else "",
                "got_deadline": action.deadline.isoformat() if action.deadline else "",
                "pass": "PASS" if ok else "FAIL",
                "problems": "; ".join(problems),
            }
        )
    return rows


def summarize(rows: list[dict], parser_name: str) -> None:
    total = len(rows)
    passed = sum(r["pass"] == "PASS" for r in rows)
    print(f"\n[{parser_name}] {passed}/{total} = {passed / total:.1%}")
    fails = [r for r in rows if r["pass"] == "FAIL"]
    for r in fails:
        print(f"  ✗ {r['id']}: {r['text']}  → {r['problems']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parser", choices=["rules", "llm", "both"], default="rules")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cases = load_cases()
    names = ["rules", "llm"] if args.parser == "both" else [args.parser]
    all_rows: list[dict] = []
    for name in names:
        rows = run(name, cases)
        for r in rows:
            r["parser"] = name
        summarize(rows, name)
        all_rows.extend(rows)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
