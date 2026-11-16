"""滚动续批 prompt 泛化性评测：S5 短视频(项目型) / S6 吉他(技能型) × 变体 A(现状)/B(候选v2)。

用法：
    python bench/prompt_eval/run_eval_s5_s6.py

输出（每行一轮，含 payload 与原始 response）：
    bench/prompt_eval/s5_video30_A.jsonl
    bench/prompt_eval/s5_video30_B.jsonl
    bench/prompt_eval/s6_guitar45_A.jsonl
    bench/prompt_eval/s6_guitar45_B.jsonl

协议：done=首批步骤，todo=[]；每轮构造 payload → 调 LLM（失败重试 1 次）→
校验 milestone ∈ outline → 新 steps 追加 todo → 全部完成（actual_min=est_min）→ 下一轮，共 6 轮。
miss_reasons 始终为空。

GOALCAL_DB_URL 仅为绕过 config 的 production 绝对路径校验（本评测不触库），
在 import goalcal.config 之前覆盖环境变量，不修改任何文件。
"""

from __future__ import annotations

import json
import os
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

os.environ["GOALCAL_DB_URL"] = "sqlite:////Users/macmini/projects/GoalCal/var/goalcal-production.db"
sys.path.insert(0, "/Users/macmini/projects/GoalCal")

from goalcal.config import settings  # noqa: E402  自动加载 .env
from goalcal.domain.refine import UPCOMING_SYSTEM  # noqa: E402  变体 A：线上版本

OUT_DIR = Path(__file__).resolve().parent
ROUNDS = 6

# 变体 B（候选 v2）：system prompt 原样
B_SYSTEM = """You are GoalCal's rolling planner.
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

SCENARIOS = {
    "s5_video30": {
        "goal_title": "Publish my first short video",
        "outline": [
            "Lock the concept",
            "Script and storyboard",
            "Film and edit",
            "Set up channel and publish",
            "Review and plan video two",
        ],
        "initial_done": [
            {"title": "List three video ideas and pick one", "est_min": 20},
            {"title": "Write a one-sentence promise for the chosen idea", "est_min": 15},
        ],
        "days_left": [24, 21, 18, 15, 12, 9],
    },
    "s6_guitar45": {
        "goal_title": "learn guitar",
        "outline": [
            "Setup & first chords",
            "Chord transitions & strumming",
            "Song repertoire build",
            "Full-song polish & dynamics",
            "Demo performance prep",
        ],
        "initial_done": [
            {"title": "Tune the guitar and learn the open Em and Am chords", "est_min": 30},
            {"title": "Set up a practice space and a 15-minute daily slot", "est_min": 15},
        ],
        "days_left": [40, 35, 30, 25, 20, 15],
    },
}


def make_client():
    """与 goalcal/llm/client.py 一致的代理处理。"""
    from openai import DefaultHttpxClient, OpenAI

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    host = urlsplit(settings.llm_base_url).hostname
    http_client = (
        DefaultHttpxClient(proxy=proxy, timeout=60)
        if proxy and host not in {"localhost", "127.0.0.1", "::1"}
        else None
    )
    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=60,
        http_client=http_client,
    )


def call_llm(client, system: str, payload: dict) -> tuple[str | None, list[str]]:
    """返回 (原始 content, 错误记录)。失败重试 1 次。"""
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content or "", errors
        except Exception as exc:  # noqa: BLE001 - 评测脚本要记录所有失败
            errors.append(f"attempt{attempt}: {type(exc).__name__}: {exc}")
            if attempt == 1:
                time.sleep(3)
    return None, errors


def build_payload(variant: str, scenario: dict, done: list[dict], todo: list[dict], round_idx: int) -> dict:
    payload = {
        "goal_title": scenario["goal_title"],
        "outline": scenario["outline"],
        "done_steps": [
            {"title": d["title"], "est_min": d["est_min"], "actual_min": d["actual_min"]}
            for d in done
        ],
        "remaining_steps": [t["title"] for t in todo],
        "miss_reasons": [],
    }
    if variant == "B":
        payload["days_left"] = scenario["days_left"][round_idx]
        payload["phases"] = [
            {
                "name": name,
                "done_steps": sum(1 for d in done if d["milestone"] == name),
                "todo_steps": sum(1 for t in todo if t["milestone"] == name),
            }
            for name in scenario["outline"]
        ]
    return payload


def next_phase_after(outline: list[str], done: list[dict]) -> str:
    """模仿生产 _next_phase：最后一个 done 阶段的下一阶段（违规归一用）。"""
    last_ms = done[-1]["milestone"] if done else ""
    if last_ms in outline:
        return outline[min(outline.index(last_ms) + 1, len(outline) - 1)]
    return outline[0]


def run_scenario(client, scenario_key: str, variant: str) -> Path:
    scenario = SCENARIOS[scenario_key]
    outline = scenario["outline"]
    system = UPCOMING_SYSTEM if variant == "A" else B_SYSTEM
    done = [
        {**d, "actual_min": d["est_min"], "milestone": outline[0]}
        for d in scenario["initial_done"]
    ]
    todo: list[dict] = []
    out_path = OUT_DIR / f"{scenario_key}_{variant}.jsonl"
    generated_titles: list[str] = []

    with out_path.open("w", encoding="utf-8") as f:
        for round_idx in range(ROUNDS):
            payload = build_payload(variant, scenario, done, todo, round_idx)
            content, errors = call_llm(client, system, payload)

            record = {
                "round": round_idx + 1,
                "variant": variant,
                "scenario": scenario_key,
                "payload": payload,
                "response": {"raw": content, "errors": errors},
            }

            steps: list[dict] = []
            if content is None:
                record["response"]["parse_error"] = "llm_call_failed"
            else:
                try:
                    data = json.loads(content)
                    raw_steps = data.get("steps") or []
                    violations = []
                    for s in raw_steps:
                        ms = str(s.get("milestone", "")).strip()
                        title = str(s.get("title", "")).strip()
                        est = int(s.get("est_min", 30))
                        if ms not in outline:
                            violations.append({"milestone": ms, "title": title})
                            ms = next_phase_after(outline, done)  # 生产行为：归一到下一阶段
                        steps.append({"milestone": ms, "title": title, "est_min": est})
                    record["response"]["steps"] = steps
                    record["response"]["milestone_violations"] = violations
                    dup_pairs = [
                        (t, prev)
                        for t in [s["title"] for s in steps]
                        for prev in generated_titles
                        if SequenceMatcher(None, t.lower(), prev.lower()).ratio() > 0.75
                    ]
                    record["response"]["near_dup_vs_previous"] = dup_pairs
                except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
                    record["response"]["parse_error"] = f"{type(exc).__name__}: {exc}"

            generated_titles.extend(s["title"] for s in record["response"].get("steps", []))
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

            # 模拟用户全部完成：todo（含本轮新步骤）→ done
            todo.extend(record["response"].get("steps", []))
            for t in todo:
                t.setdefault("actual_min", t["est_min"])
            done.extend(todo)
            todo = []
            print(f"{scenario_key} {variant} round {round_idx + 1}: "
                  f"{len(record['response'].get('steps', []))} steps, "
                  f"errors={len(errors)}, "
                  f"violations={len(record['response'].get('milestone_violations', []))}")

    return out_path


def main() -> None:
    if not settings.llm_api_key:
        raise SystemExit("LLM_API_KEY 未配置（.env）")
    print(f"model={settings.llm_model} base_url={settings.llm_base_url}")
    client = make_client()
    for scenario_key in SCENARIOS:
        for variant in ("A", "B"):
            path = run_scenario(client, scenario_key, variant)
            print(f"wrote {path}")


if __name__ == "__main__":
    main()
