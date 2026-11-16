"""目标模板库（M1 的“拆解”先由模板 + 规则完成，LLM 只做润色/个性化）。

11 月接入 RAG 后，这里会变成检索库的一个子集：
embedding 检索 top-3 模板 → 注入 prompt → LLM 生成贴合用户目标的步骤。
先有可用的 baseline，才能证明 RAG 有用。
"""

from __future__ import annotations

TEMPLATES: dict[str, dict] = {
    "video": {
        "keywords": ["video", "短视频", "拍视频", "vlog", "youtube", "抖音", "b站"],
        "milestones": ["Positioning & topics", "Script & storyboard", "Shooting", "Editing & publishing"],
        "steps": [
            ("Positioning & topics", "Write one sentence: who is this video for and what problem it solves", "One target-audience line + one value proposition", 30, 1, "deep"),
            ("Positioning & topics", "List 3 topic ideas with a one-line opening hook each, then pick one", "Chosen topic + 3 hooks", 40, 1, "deep"),
            ("Script & storyboard", "Draft a 60-second script skeleton (3-second hook / middle / closing CTA)", "One-page script", 45, 2, "deep"),
            ("Script & storyboard", "Make a storyboard of 8–10 shots", "Storyboard", 45, 1, "deep"),
            ("Script & storyboard", "Study 3 reference videos and note reusable shots or edits", "3 reference notes", 30, 1, "research"),
            ("Shooting", "Test-shoot 3 clips on your phone to check lighting and audio", "3 usable clips", 40, 1, "deep"),
            ("Shooting", "Shoot all footage (split into two sessions if needed)", "All raw footage", 60, 2, "deep"),
            ("Editing & publishing", "Rough-cut to 60 seconds and add captions", "A watchable cut", 60, 2, "deep"),
            ("Editing & publishing", "Create the cover, title, and description", "Cover + publish copy", 30, 1, "admin"),
            ("Editing & publishing", "Publish and record the 24-hour stats", "Published link + stats screenshot", 20, 0, "admin"),
        ],
    },
    "learn": {
        "keywords": ["learn", "学", "skill", "技能", "course", "课程", "入门"],
        "milestones": ["Define & resources", "Basics", "Independent practice", "Output"],
        "steps": [
            ("Define & resources", "Write one concrete scenario of what you can do after learning this", "One verifiable capability statement", 25, 1, "deep"),
            ("Define & resources", "Find 2 beginner resources, compare, and pick one", "Chosen primary resource", 30, 1, "research"),
            ("Basics", "Finish the first 20-minute introductory tutorial", "One full run-through", 25, 1, "deep"),
            ("Basics", "List the 5 core concepts you must master", "A 5-concept list", 30, 1, "deep"),
            ("Basics", "Reproduce the tutorial's example (follow along once)", "A working/presentable artifact", 45, 1, "deep"),
            ("Independent practice", "Do it once without the tutorial", "An independently completed version", 45, 2, "deep"),
            ("Independent practice", "Build a small project of your own", "Your own small piece", 60, 2, "deep"),
            ("Independent practice", "Write down 3 sticking points and resolve each", "3 Q&A notes", 40, 1, "research"),
            ("Output", "Explain what you learned to someone in 3 sentences", "A clear explanation", 20, 1, "social"),
            ("Output", "Set your next-level goal", "One-line next goal", 20, 0, "admin"),
        ],
    },
    "sideproject": {
        "keywords": ["project", "side project", "app", "产品", "做一个小", "开发"],
        "milestones": ["Define", "Design", "Skeleton", "End-to-end", "Polish"],
        "steps": [
            ("Define", "One-sentence definition: who uses it and what problem it solves", "One-line product definition", 25, 1, "deep"),
            ("Define", "List the 3 must-have MVP features and cut everything else", "3-feature list", 30, 1, "deep"),
            ("Design", "Sketch the main flow (3–5 screens is enough)", "A flow diagram", 30, 1, "deep"),
            ("Skeleton", "Set up the empty project and deploy a hello world", "An accessible link", 45, 2, "deep"),
            ("Skeleton", "Build the data model and minimal storage", "Working schema", 45, 2, "deep"),
            ("End-to-end", "Get the first end-to-end path working with fake data", "One complete demo run", 60, 2, "deep"),
            ("End-to-end", "Wire up real input", "Real data flows through", 60, 2, "deep"),
            ("Polish", "Use it yourself end-to-end and note 5 issues", "5-issue list", 30, 1, "deep"),
            ("Polish", "Fix the 3 most painful issues", "Fix log", 60, 2, "deep"),
            ("Polish", "Have a friend try it and record feedback", "3 pieces of external feedback", 30, 1, "social"),
        ],
    },
    "fitness": {
        "keywords": ["健身", "fitness", "跑步", "run", "锻炼", "减脂", "练"],
        "milestones": ["Baseline", "Kickoff", "Consistency", "Ramp up"],
        "steps": [
            ("Baseline", "Measure your baseline (how many reps / how far you can run)", "A baseline record", 30, 1, "physical"),
            ("Baseline", "Set 3 fixed weekly time slots", "3 fixed slots", 20, 0, "admin"),
            ("Kickoff", "Session 1: keep it light, just finish", "One completion log", 30, 1, "physical"),
            ("Kickoff", "Note how your body reacts after training", "One body-feedback note", 15, 0, "admin"),
            ("Consistency", "Session 2: increase intensity 10% over last time", "One completion log", 35, 1, "physical"),
            ("Consistency", "Session 3: hold intensity, no need to improve", "One completion log", 35, 1, "physical"),
            ("Consistency", "Weekly review: which session you most wanted to skip and why", "One review conclusion", 20, 1, "deep"),
            ("Consistency", "Move training to the easiest-to-keep time slot", "Updated fixed slots", 15, 0, "admin"),
            ("Ramp up", "Complete 2 consecutive weeks without breaking", "Unbroken streak log", 40, 1, "physical"),
            ("Ramp up", "Add a small target (reps / weight / distance)", "A new quantified target", 20, 0, "admin"),
        ],
    },
    "piano": {
        "keywords": ["钢琴", "piano", "键盘乐器", "弹琴"],
        "milestones": ["Setup & baseline", "Posture & scales", "Reading & rhythm", "First piece", "Consolidation"],
        "steps": [
            ("Setup & baseline", "Decide how you'll practice (studio / digital / acoustic) and your weekly practice time", "A workable practice schedule", 25, 1, "admin"),
            ("Setup & baseline", "Learn the keyboard layout, posture, and basic hand shape; take a photo of your hand position", "A hand-position check photo", 20, 0, "physical"),
            ("Posture & scales", "C major scale: right hand, one octave, slowly", "Right hand plays one octave smoothly", 15, 1, "physical"),
            ("Posture & scales", "C major scale: left hand, one octave, slowly", "Left hand plays one octave smoothly", 15, 1, "physical"),
            ("Posture & scales", "C major scale hands together, metronome at 60", "One slow hands-together pass", 20, 2, "physical"),
            ("Reading & rhythm", "Learn the treble and bass clefs and find 5 notes", "Can name the position of 5 notes", 20, 1, "deep"),
            ("Reading & rhythm", "Practice 3 basic rhythm patterns (quarter / eighth / dotted)", "Clap 3 rhythms with a metronome", 20, 1, "deep"),
            ("First piece", "Pick a beginner piece (Twinkle Twinkle / Ode to Joy) and practice the right hand separately", "Right hand can play section A", 30, 1, "physical"),
            ("First piece", "Practice the left-hand chords / bass separately", "Left hand keeps up with section A", 30, 1, "physical"),
            ("First piece", "Play the first 8 bars hands together, slowly", "Can play the first 8 bars slowly", 30, 2, "physical"),
            ("First piece", "Play the whole piece and record it", "One complete recording", 30, 2, "physical"),
            ("Consolidation", "Listen to the recording, fix the most obvious issue, and pick the next piece", "One improvement note + next piece", 25, 1, "deep"),
        ],
    },
    "exam": {
        "keywords": ["复习", "考试", "exam", "final", "midterm", "quiz", "review", "期末", "期中", "备考", "刷题"],
        "milestones": ["Scope & diagnosis", "First-pass review", "Practice & error log", "Mock & gap check"],
        "steps": [
            ("Scope & diagnosis", "Collect the syllabus, exam scope, and past/sample papers into a resource list", "A resource list", 25, 0, "admin"),
            ("Scope & diagnosis", "List all topics from the syllabus and mark each: confident / shaky / unknown", "A topic diagnosis sheet", 30, 1, "deep"),
            ("First-pass review", "For the 2 weakest topics, review one core concept each", "Can explain 2 concepts in your own words", 40, 2, "deep"),
            ("First-pass review", "Active recall pass: close the book and outline key points per topic", "One recall outline per topic", 40, 2, "deep"),
            ("Practice & error log", "Do one past/practice paper under time limit", "One completed paper", 60, 2, "deep"),
            ("Practice & error log", "Organize wrong answers with cause and correct reasoning", "One-page error log", 30, 1, "deep"),
            ("Practice & error log", "Patch the 3 weakest knowledge points from the error log", "Can solve 3 points independently", 40, 2, "deep"),
            ("Practice & error log", "Redo a paper covering only previous errors and confusions", "Accuracy comparison", 45, 2, "deep"),
            ("Mock & gap check", "Run one full timed mock exam", "One complete mock", 60, 2, "deep"),
            ("Mock & gap check", "Final gap check: recite high-frequency points in 10 minutes", "A final checklist", 20, 1, "deep"),
        ],
    },
    "generic": {
        "keywords": [],
        "milestones": ["Define", "Explore", "First version", "Polish", "Wrap up"],
        "steps": [
            ("Define", "Write down the visible outcome when the goal is done", "One verifiable outcome statement", 25, 1, "deep"),
            ("Define", "Break out 3 milestone phases", "3 milestones", 30, 1, "deep"),
            ("Explore", "Identify the single biggest uncertainty", "One-line risk statement", 20, 1, "deep"),
            ("Explore", "Spend 30 minutes resolving that question", "One conclusion", 30, 1, "research"),
            ("First version", "Build the roughest possible first version", "A presentable v1", 60, 2, "deep"),
            ("First version", "Show it to someone for 5 minutes and collect 3 pieces of feedback", "3 pieces of feedback", 25, 1, "social"),
            ("Polish", "Revise based on feedback", "Second version", 60, 2, "deep"),
            ("Polish", "Finish v2 and self-review", "Self-review checklist", 45, 2, "deep"),
            ("Polish", "Check what's still missing to reach the goal", "Gap list", 25, 1, "deep"),
            ("Wrap up", "Wrap up and record reusable lessons", "3 lessons", 25, 1, "admin"),
        ],
    },
}


def pick_template(text: str) -> str:
    lowered = text.lower()
    best_key = "generic"
    best_score = 0
    for key, tpl in TEMPLATES.items():
        if key == "generic":
            continue
        # 更具体的关键词优先：'钢琴' 应该赢过 '学'，'短视频' 应该赢过 '视频'
        score = sum(len(kw) for kw in tpl["keywords"] if kw.lower() in lowered)
        if score > best_score:
            best_key = key
            best_score = score
    return best_key


def build_steps(text: str, complexity: str = "full") -> tuple[str, list[dict]]:
    key = pick_template(text)
    raw_steps = full_steps(key)
    step_defs = _select_steps(raw_steps, complexity)
    return key, step_defs


def full_steps(key: str) -> list[dict]:
    """模板的完整有序步骤（不按复杂度抽减），供滚动拆解按批取。"""
    tpl = TEMPLATES.get(key, TEMPLATES["generic"])
    return [
        {
            "seq": i,
            "milestone": milestone,
            "title": title,
            "outcome": outcome,
            "est_min": est,
            "energy": energy,
            "kind": kind,
        }
        for i, (milestone, title, outcome, est, energy, kind) in enumerate(tpl["steps"], 1)
    ]


def first_batch(steps: list[dict], min_n: int = 2, max_n: int = 3) -> list[dict]:
    """取一批：优先同一阶段的连续步骤，数量限制在 min_n–max_n。"""
    if not steps:
        return []
    first_ms = steps[0]["milestone"]
    batch = [s for s in steps if s["milestone"] == first_ms][:max_n]
    if len(batch) < min_n:
        batch = steps[:min_n]
    return [dict(s) for s in batch]


def _select_steps(steps: list[dict], complexity: str) -> list[dict]:
    """按复杂度调整步骤数量；保留首尾，均匀抽掉中间步骤。"""
    target = {"simple": 6, "standard": 8, "complex": 12}.get(complexity)
    if target is None or target >= len(steps):
        return steps
    if target <= 1:
        return steps[:1]

    indexes = [round(i * (len(steps) - 1) / (target - 1)) for i in range(target)]
    indexes = sorted(set(indexes))
    selected = [steps[i] for i in indexes]
    for seq, step in enumerate(selected, 1):
        step["seq"] = seq
    return selected



def milestones(template_key: str) -> list[str]:
    return TEMPLATES.get(template_key, TEMPLATES["generic"])["milestones"]
