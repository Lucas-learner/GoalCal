"""滚动续批 prompt 泛化性评测：S1(半马30天) / S2(半马180天) × A(现状) / B(v2)。

只打 LLM、不触库。每轮把生成的步骤全部标记完成（actual_min=est_min）后进入
下一轮，共 6 轮。原始记录（round / payload / response）写入本目录 JSONL。

注意：文件名带 marathon 前缀，避免与并行的其它评测脚本冲突。

运行：.venv/bin/python bench/prompt_eval/run_marathon_eval.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# 评测隔离于开发与生产数据库。
os.environ["GOALCAL_DB_URL"] = "sqlite:///:memory:"
os.environ["GOALCAL_PRODUCTION"] = "false"
os.environ.setdefault("GOALCAL_ENV_FILE", os.devnull)

from goalcal.config import settings  # noqa: E402
from goalcal.domain.refine import UPCOMING_SYSTEM  # noqa: E402

HERE = Path(__file__).resolve().parent

SYSTEM_B = """You are GoalCal's rolling planner.
The goal has a phase outline; earlier steps are done and the user needs the next
batch. Plan the NEXT 2-3 concrete steps, continuing the current phase or opening
the next one. Return ONE JSON object:
{"steps":[{"milestone":"...","title":"...","est_min":30,"energy":1,"kind":"deep"}]}

Rules:
1. milestone must be one of the given outline phases.
2. Each step is one observable action of 5-60 minutes; the first step must be
   doable immediately (lower the barrier if the user was stuck or short on time).
3. Calibrate BOTH ways with the history: if actual minutes exceeded estimates, or
   steps were missed for no_time/tired, keep the new steps small; if steps were
   done easily and on time, raise the duration, intensity, or scope.
4. Budget the calendar: days_left / remaining phases ≈ days per phase. When the
   current phase has consumed its share, open the next phase and fold unfinished
   work into it; do not let one phase expand to fill all remaining time.
5. Repetition is allowed, standing still is not: a repeated drill must state what
   changes this time (more distance/duration/intensity, a higher quality bar, or
   an explicit consolidation purpose). Never emit a step essentially identical
   to an existing one.
6. Use the same language as the goal title. Output only JSON, no markdown.
"""

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
    """与 goalcal/llm/client.py 一致：显式 HTTPS_PROXY 优先，避免 ALL_PROXY 探测失败。"""
    from openai import DefaultHttpxClient, OpenAI

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    host = urlsplit(settings.llm_base_url).hostname
    http_client = None
    if proxy and host not in {"localhost", "127.0.0.1", "::1"}:
        http_client = DefaultHttpxClient(proxy=proxy, timeout=60)
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                  timeout=60, http_client=http_client)


def call_llm(client, system: str, payload: dict, rec: dict) -> dict:
    """temperature=0.0 + json_object；失败重试 1 次并把错误写进记录。"""
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
        except Exception as exc:  # noqa: BLE001 - 评测要把失败原样记下来
            last_exc = exc
            rec.setdefault("errors", []).append({
                "attempt": attempt,
                "type": type(exc).__name__,
                "error": str(exc)[:300],
            })
            time.sleep(2 * attempt)
    raise last_exc  # type: ignore[misc]


def normalize_steps(data: dict) -> tuple[list[dict], list[str]]:
    """按生产同款口径归一化（est_min 夹到 [5,90]），返回 (steps, 违规 milestone)。"""
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


def run(scenario: str, variant: str, days_left_list: list[int]) -> Path:
    system = UPCOMING_SYSTEM if variant == "A" else SYSTEM_B
    done = [dict(s) for s in FIRST_BATCH]
    todo: list[dict] = []
    client = make_client()
    out = HERE / f"{scenario}_{variant}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for i, days_left in enumerate(days_left_list, start=1):
            payload = {
                "goal_title": GOAL_TITLE,
                "outline": OUTLINE,
                "done_steps": [
                    {"title": s["title"], "est_min": s["est_min"],
                     "actual_min": s["actual_min"]}
                    for s in done
                ],
                "remaining_steps": [s["title"] for s in todo],
                "miss_reasons": [],
            }
            if variant == "B":
                payload["days_left"] = days_left
                payload["phases"] = phase_stats(done, todo)
            rec = {"round": i, "payload": payload, "response": None,
                   "steps": [], "milestone_violations": [], "errors": []}
            data = call_llm(client, system, payload, rec)
            rec["response"] = data
            steps, violations = normalize_steps(data)
            rec["steps"] = steps
            rec["milestone_violations"] = violations
            # 模拟用户把本批和遗留待办全部完成，进入下一轮。
            todo.extend(steps)
            for s in todo:
                done.append({**s, "actual_min": s["est_min"]})
            todo = []
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{scenario}_{variant} round {i}: {len(steps)} steps "
                  f"(violations={len(violations)}, errors={len(rec['errors'])})",
                  flush=True)
    return out


def main() -> None:
    print(f"model={settings.llm_model} base_url={settings.llm_base_url} "
          f"key_set={bool(settings.llm_api_key)}")
    for scenario, days_left_list in SCENARIOS.items():
        for variant in ("A", "B"):
            out = run(scenario, variant, days_left_list)
            print(f"wrote {out.name} ({out.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
