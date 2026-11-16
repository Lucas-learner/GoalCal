"""滚动续批 prompt v3 修复验证：S1(半马30天) / S2(半马180天)。

system prompt 直接从代码导入（线上版本，rule 4 已含硬性目标保护 + phase_stall）。
payload 与生产 _upcoming_llm_defs 对齐：goal_title / outline / days_left /
phases / phase_stall / done_steps / remaining_steps / miss_reasons。
phase_stall：第 1 轮为 null；之后若最近 4 步同一阶段且后续还有未拆步骤的阶段，
填 {"phase": <阶段名>, "batches": 2}（与 refine._phase_stall 同口径），否则 null。

运行：.venv/bin/python bench/prompt_eval/run_marathon_eval_v3.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, "/Users/macmini/projects/GoalCal")
os.environ["GOALCAL_DB_URL"] = "sqlite:////Users/macmini/projects/GoalCal/var/goalcal-production.db"

from goalcal.config import settings  # noqa: E402
from goalcal.domain.refine import UPCOMING_SYSTEM  # noqa: E402

HERE = Path(__file__).resolve().parent

GOAL_TITLE = "跑半程马拉松"
OUTLINE = [
    "基础评估与建立跑步节奏",
    "逐步拉长距离（最长跑到14公里）",
    "专项强化与配速适应",
    "赛前减量与比赛执行",
]

FIRST_BATCH = [
    {"title": "做一次20分钟走跑交替测试并记录数据", "est_min": 30,
     "actual_min": 30, "milestone": OUTLINE[0]},
    {"title": "排出每周三次的训练日程并备齐跑鞋装备", "est_min": 20,
     "actual_min": 20, "milestone": OUTLINE[0]},
]

SCENARIOS = {
    "s1_marathon30": [24, 21, 18, 15, 12, 9],
    "s2_marathon180": [170, 155, 140, 125, 110, 95],
}


def make_client():
    """与 goalcal/llm/client.py 一致：显式 HTTPS_PROXY 优先。"""
    from openai import DefaultHttpxClient, OpenAI

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    host = urlsplit(settings.llm_base_url).hostname
    http_client = None
    if proxy and host not in {"localhost", "127.0.0.1", "::1"}:
        http_client = DefaultHttpxClient(proxy=proxy, timeout=60)
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                  timeout=60, http_client=http_client)


def call_llm(client, system: str, payload: dict, rec: dict) -> dict:
    """temperature=0.0 + json_object；失败重试 1 次并记录。"""
    user = json.dumps(payload, ensure_ascii=False)
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            return json.loads(content)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            rec.setdefault("errors", []).append({
                "attempt": attempt, "type": type(exc).__name__,
                "error": str(exc)[:300],
            })
            time.sleep(2 * attempt)
    raise last_exc  # type: ignore[misc]


def normalize_steps(data: dict) -> tuple[list[dict], list[str]]:
    """按生产同款口径归一化（est_min 夹到 [5,90]）。"""
    steps, violations = [], []
    for s in data.get("steps") or []:
        ms = str(s.get("milestone", "")).strip()[:120]
        title = str(s.get("title", "")).strip()[:200]
        if not title:
            continue
        try:
            est = int(s.get("est_min", 30))
        except (TypeError, ValueError):
            est = 30
        if ms not in OUTLINE:
            violations.append(ms)
        steps.append({"milestone": ms, "title": title,
                      "est_min": max(5, min(est, 90))})
    return steps[:3], violations


def phase_stats(done: list[dict], todo: list[dict]) -> list[dict]:
    return [
        {"name": name,
         "done_steps": sum(1 for s in done if s["milestone"] == name),
         "todo_steps": sum(1 for s in todo if s["milestone"] == name)}
        for name in OUTLINE
    ]


def phase_stall(done: list[dict], todo: list[dict]) -> dict | None:
    """复刻 refine._phase_stall：最近 4 步同阶段、非末阶段、后续阶段有未拆步骤。"""
    seq = sorted(done + todo, key=lambda s: s["seq"])
    recent = [s["milestone"] for s in seq[-4:]]
    if len(recent) < 4 or len(set(recent)) != 1:
        return None
    current = recent[0]
    if current not in OUTLINE or OUTLINE.index(current) >= len(OUTLINE) - 1:
        return None
    stepped = {s["milestone"] for s in seq}
    if all(name in stepped for name in OUTLINE[OUTLINE.index(current) + 1:]):
        return None
    return {"phase": current, "batches": 2}


def run(scenario: str, days_left_list: list[int]) -> Path:
    done = [dict(s, seq=i + 1) for i, s in enumerate(FIRST_BATCH)]
    todo: list[dict] = []
    client = make_client()
    out = HERE / f"{scenario}_v3.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for i, days_left in enumerate(days_left_list, start=1):
            stall = None if i == 1 else phase_stall(done, todo)
            payload = {
                "goal_title": GOAL_TITLE,
                "outline": OUTLINE,
                "days_left": days_left,
                "phases": phase_stats(done, todo),
                "phase_stall": stall,
                "done_steps": [
                    {"title": s["title"], "est_min": s["est_min"],
                     "actual_min": s["actual_min"]}
                    for s in sorted(done, key=lambda s: s["seq"])
                ],
                "remaining_steps": [s["title"] for s in todo],
                "miss_reasons": [],
            }
            rec = {"round": i, "payload": payload, "response": None,
                   "steps": [], "milestone_violations": [], "errors": []}
            data = call_llm(client, UPCOMING_SYSTEM, payload, rec)
            rec["response"] = data
            steps, violations = normalize_steps(data)
            rec["steps"] = steps
            rec["milestone_violations"] = violations
            todo.extend(dict(s, seq=len(done) + len(todo) + k + 1)
                        for k, s in enumerate(steps))
            for s in todo:
                done.append({**s, "actual_min": s["est_min"]})
            todo = []
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{scenario}_v3 round {i}: stall={stall['phase'] if stall else None} "
                  f"steps={len(steps)} violations={len(violations)} "
                  f"errors={len(rec['errors'])}", flush=True)
    return out


def main() -> None:
    print(f"model={settings.llm_model} base_url={settings.llm_base_url} "
          f"key_set={bool(settings.llm_api_key)}")
    for scenario, days_left_list in SCENARIOS.items():
        out = run(scenario, days_left_list)
        print(f"wrote {out.name} ({out.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
