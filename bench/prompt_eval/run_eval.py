"""滚动续批 prompt 泛化性评测：S3 备考(7天) / S4 游泳(30天) × 变体 A(现状)/B(候选v2)。

每轮：构造 payload -> 调真实 LLM(temperature=0, json_object) -> 校验 milestone ∈ outline
-> 新步骤追加 todo -> 模拟全部完成(todo -> done, actual_min=est_min)。共 6 轮。
原始记录写入 bench/prompt_eval/s3_exam7_<A|B>.jsonl、s4_swim30_<A|B>.jsonl。

运行：环境配置和真实模型调用说明见本目录 README.md；数据库强制使用内存库。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ["GOALCAL_DB_URL"] = "sqlite:///:memory:"
os.environ["GOALCAL_PRODUCTION"] = "false"
os.environ.setdefault("GOALCAL_ENV_FILE", os.devnull)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from goalcal.config import settings  # noqa: E402  仅加载显式指定的环境文件
from goalcal.domain.refine import UPCOMING_SYSTEM  # noqa: E402  变体 A：线上版本

OUT_DIR = Path(__file__).resolve().parent

# 变体 B：候选 v2 system prompt（原样）
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

SCENARIOS = {
    "s3_exam7": {
        "goal_title": "复习机器学习",
        "outline": [
            "梳理考点与摸底自测",
            "分题型攻克核心概念与计算",
            "真题演练与错题归因",
            "限时模拟自测与查漏补缺",
        ],
        "initial_done": [
            {"title": "收集课件目录与往年真题，列出考点清单", "est_min": 30, "actual_min": 30},
            {"title": "按考点做10道摸底题并标记会/不熟/不会", "est_min": 40, "actual_min": 40},
        ],
        "days_left": [6, 5, 4, 3, 2, 1],
        # 第 1 轮起（1-indexed）的 miss_reasons；None 表示该轮为空
        "miss": {},
    },
    "s4_swim30": {
        "goal_title": "要学会自由泳",
        "outline": [
            "熟悉水性：呼吸控制与水中漂浮",
            "分解打腿与划手动作",
            "换气与手脚配合协调",
            "完整游进与距离耐力提升",
            "独立展示自由泳",
        ],
        "initial_done": [
            {"title": "查清附近两个泳池的开放时段、票价与浅水区情况", "est_min": 20, "actual_min": 20},
            {"title": "在陆上练习自由泳呼吸节奏（吸气2秒、水下呼气4秒）", "est_min": 15, "actual_min": 15},
        ],
        "days_left": [24, 21, 18, 15, 12, 9],
        # 第 2 轮起（1-indexed）持续 no_time（与产品 _miss_reasons 的累积语义一致，
        # 一次错过之后会一直出现在 payload 里），其余轮为空
        "miss": {2: ["no_time"], 3: ["no_time"], 4: ["no_time"], 5: ["no_time"], 6: ["no_time"]},
    },
}

ROUNDS = 6


def make_client() -> "OpenAI":
    """按 goalcal/llm/client.py 的代理处理：显式 HTTPS_PROXY 优先于 ALL_PROXY。"""
    from openai import DefaultHttpxClient, OpenAI
    from urllib.parse import urlsplit

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


def call_llm(client, system: str, payload: dict) -> tuple[str, int, str | None]:
    """调一次 LLM；失败重试 1 次。返回 (raw_text, retries, first_error)。"""
    content = json.dumps(payload, ensure_ascii=False)
    first_error = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content or "", attempt, first_error
        except Exception as exc:  # noqa: BLE001 - 评测需记录所有失败
            if first_error is None:
                first_error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                time.sleep(2)
    return "", 1, first_error


def build_payload(variant: str, scen: dict, done: list[dict], todo: list[dict], round_no: int) -> dict:
    payload = {
        "goal_title": scen["goal_title"],
        "outline": scen["outline"],
        "done_steps": [
            {"title": s["title"], "est_min": s["est_min"], "actual_min": s["actual_min"]}
            for s in done
        ],
        "remaining_steps": [s["title"] for s in todo],
        "miss_reasons": scen["miss"].get(round_no, []),
    }
    if variant == "B":
        phases = []
        for name in scen["outline"]:
            d = sum(1 for s in done if s["milestone"] == name)
            t = sum(1 for s in todo if s["milestone"] == name)
            phases.append({"name": name, "done_steps": d, "todo_steps": t})
        payload["days_left"] = scen["days_left"][round_no - 1]
        payload["phases"] = phases
    return payload


def run(scen_key: str, variant: str, client) -> None:
    scen = SCENARIOS[scen_key]
    system = UPCOMING_SYSTEM if variant == "A" else SYSTEM_B
    outline = scen["outline"]
    done = [
        {"title": s["title"], "est_min": s["est_min"], "actual_min": s["actual_min"],
         "milestone": outline[0], "gen_round": 0}
        for s in scen["initial_done"]
    ]
    todo: list[dict] = []
    out_path = OUT_DIR / f"{scen_key}_{variant}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for round_no in range(1, ROUNDS + 1):
            payload = build_payload(variant, scen, done, todo, round_no)
            raw, retries, first_error = call_llm(client, system, payload)
            parsed, violations, generated = None, [], []
            if raw:
                try:
                    parsed = json.loads(raw)
                    for s in parsed.get("steps") or []:
                        title = str(s.get("title", "")).strip()
                        ms = str(s.get("milestone", "")).strip()
                        est = s.get("est_min")
                        if title:
                            generated.append({"title": title, "milestone": ms, "est_min": est})
                    bad = [g["milestone"] for g in generated if g["milestone"] not in outline]
                    if bad:
                        violations.append(f"milestone_not_in_outline: {bad}")
                    if not generated:
                        violations.append("no_valid_steps")
                except json.JSONDecodeError as exc:
                    violations.append(f"json_decode_error: {exc}")
            else:
                violations.append(f"llm_call_failed: {first_error}")
            record = {
                "round": round_no,
                "payload": payload,
                "response": raw,
                "retries": retries,
                "first_error": first_error,
                "violations": violations,
                "generated": generated,
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"{scen_key} {variant} round {round_no}: "
                  f"{len(generated)} steps, retries={retries}, violations={violations or 'ok'}",
                  flush=True)
            if not generated:
                break  # LLM 连续失败，中止该场景（已记录）
            # 新步骤追加 todo，再模拟用户全部完成
            for i, g in enumerate(generated):
                todo.append({"title": g["title"], "est_min": g["est_min"] or 30,
                             "actual_min": g["est_min"] or 30,
                             "milestone": g["milestone"], "gen_round": round_no})
            done.extend(todo)
            todo = []


def main() -> None:
    if not settings.llm_api_key:
        raise SystemExit("LLM_API_KEY 未配置")
    print(f"model={settings.llm_model} base_url={settings.llm_base_url}", flush=True)
    client = make_client()
    for scen_key in ("s3_exam7", "s4_swim30"):
        for variant in ("A", "B"):
            run(scen_key, variant, client)
    print("done", flush=True)


if __name__ == "__main__":
    main()
