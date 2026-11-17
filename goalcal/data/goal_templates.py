"""目标模板库（M1 的“拆解”先由模板 + 规则完成，LLM 只做润色/个性化）。

支持英文（en）与简体中文（zh）双语模板；未传 lang 时默认保持英文（兼容既有领域测试）。
"""

from __future__ import annotations

from ..i18n import normalize_lang

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
        "zh_milestones": ["定位与选题", "脚本与分镜", "拍摄素材", "剪辑与发布"],
        "zh_steps": [
            ("定位与选题", "写一句话明确视频面向谁以及解决什么问题", "一条清晰的受众与价值定位", 30, 1, "deep"),
            ("定位与选题", "列出 3 个选题和开头钩子，从中选定 1 个", "选定选题与 3 个开头钩子", 40, 1, "deep"),
            ("脚本与分镜", "写出 60 秒脚本骨架（前 3 秒钩子、主体、结尾引导）", "一页完整脚本骨架", 45, 2, "deep"),
            ("脚本与分镜", "整理 8–10 个镜头的分镜清单", "一版分镜表", 45, 1, "deep"),
            ("脚本与分镜", "拆解 3 条对标视频，记录可复用的镜头或剪辑节奏", "3 条对标拆解笔记", 30, 1, "research"),
            ("拍摄素材", "用手机试拍 3 段，检查光线、构图和收音", "3 段可用试拍片段", 40, 1, "deep"),
            ("拍摄素材", "完成全部正片素材拍摄", "全部原始拍摄素材", 60, 2, "deep"),
            ("剪辑与发布", "粗剪到 60 秒以内并加上字幕", "一条完整可看成片", 60, 2, "deep"),
            ("剪辑与发布", "制作封面并撰写标题与简介文案", "封面图与发布文案", 30, 1, "admin"),
            ("剪辑与发布", "发布成片并记录 24 小时基础数据", "发布链接与数据截图", 20, 0, "admin"),
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
        "zh_milestones": ["定目标与选资料", "基础入门", "独立练习", "总结输出"],
        "zh_steps": [
            ("定目标与选资料", "写下学会后能独立完成的一个具体场景", "一条可验证的能力标准", 25, 1, "deep"),
            ("定目标与选资料", "对比 2 份入门资料并选定 1 份主学材料", "选定一份主学资料", 30, 1, "research"),
            ("基础入门", "看完或学完前 20 分钟的基础引导", "完成首次通览入门", 25, 1, "deep"),
            ("基础入门", "整理必须掌握的 5 个核心概念", "一份 5 个核心概念清单", 30, 1, "deep"),
            ("基础入门", "跟着教程完整复现一遍基础示例", "一个可运行或可展示的结果", 45, 1, "deep"),
            ("独立练习", "脱离教程独立重做一遍核心练习", "一版独立完成的作品", 45, 2, "deep"),
            ("独立练习", "做一个属于自己的小型实战作品", "一个个人小项目或作品", 60, 2, "deep"),
            ("独立练习", "记下 3 个卡点并逐个查清解决", "3 条问题与解法笔记", 40, 1, "research"),
            ("总结输出", "用 3 句话向他人讲清楚这次学到的核心", "一段清晰复述内容", 20, 1, "social"),
            ("总结输出", "定下下一阶段的进阶目标", "一行下一阶段目标", 20, 0, "admin"),
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
        "zh_milestones": ["明确定义", "流程设计", "搭建骨架", "打通主流程", "打磨上线"],
        "zh_steps": [
            ("明确定义", "用一句话定义清楚谁来用、解决什么问题", "一句话产品定位", 25, 1, "deep"),
            ("明确定义", "列出 MVP 最核心的 3 个功能并砍掉其余需求", "3 项核心功能清单", 30, 1, "deep"),
            ("流程设计", "画出核心交互流程的 3–5 个关键页面草图", "一份页面主流程草图", 30, 1, "deep"),
            ("搭建骨架", "初始化空项目并跑通最简部署访问", "一个可访问的空白站点", 45, 2, "deep"),
            ("搭建骨架", "建立核心数据模型与最小存储结构", "可用的数据结构", 45, 2, "deep"),
            ("打通主流程", "先用模拟数据跑通第一条端到端主链路", "一次完整演示链路", 60, 2, "deep"),
            ("打通主流程", "接入真实输入与持久化写入", "真实数据流转正常", 60, 2, "deep"),
            ("打磨上线", "自己从头到尾试用一遍并记下 5 个问题", "5 条自测问题清单", 30, 1, "deep"),
            ("打磨上线", "优先修复最影响体验的 3 个问题", "3 项核心问题修复完成", 60, 2, "deep"),
            ("打磨上线", "找 1 位朋友试用并记录 3 条真实反馈", "3 条外部试用反馈", 30, 1, "social"),
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
        "zh_milestones": ["测出基线", "轻松起步", "稳定节奏", "逐步加量"],
        "zh_steps": [
            ("测出基线", "做一次基础摸底测试并记录当前体能基线", "一条真实体能基线记录", 30, 1, "physical"),
            ("测出基线", "固定每周最容易坚持的 3 个运动时段", "3 个固定运动时段", 20, 0, "admin"),
            ("轻松起步", "完成第 1 次轻量训练，以完成为主不求强度", "一条首次训练完成记录", 30, 1, "physical"),
            ("轻松起步", "记录训练后的体感与疲劳恢复情况", "一条身体反馈记录", 15, 0, "admin"),
            ("稳定节奏", "完成第 2 次训练，比上次小幅提升约一成", "一条训练完成记录", 35, 1, "physical"),
            ("稳定节奏", "完成第 3 次训练，保持稳定强度即可", "一条训练完成记录", 35, 1, "physical"),
            ("稳定节奏", "每周复盘最想跳过哪次训练及原因", "一条节奏调整结论", 20, 1, "deep"),
            ("稳定节奏", "把训练调整到最容易落地的时段", "更新后的固定时段", 15, 0, "admin"),
            ("逐步加量", "连续完成两周不断档训练", "连续两周打卡记录", 40, 1, "physical"),
            ("逐步加量", "设定下一个小幅量化目标", "新的量化训练目标", 20, 0, "admin"),
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
        "zh_milestones": ["准备与定节奏", "手型与音阶", "识谱与节奏", "首支小曲", "巩固录音"],
        "zh_steps": [
            ("准备与定节奏", "确认练琴方式与每周固定练琴时段", "一份可执行的练琴计划", 25, 1, "admin"),
            ("准备与定节奏", "熟悉琴键排列、坐姿与基本手型", "一张手型自检照片", 20, 0, "physical"),
            ("手型与音阶", "右手慢练 C 大调一个八度音阶", "右手流畅弹完一个八度", 15, 1, "physical"),
            ("手型与音阶", "左手慢练 C 大调一个八度音阶", "左手流畅弹完一个八度", 15, 1, "physical"),
            ("手型与音阶", "用 60 拍速练习双手合弹 C 大调音阶", "稳定慢速双手合弹一遍", 20, 2, "physical"),
            ("识谱与节奏", "认识高低音谱号并准确定位 5 个音符", "能快速识别 5 个基础音位", 20, 1, "deep"),
            ("识谱与节奏", "跟着节拍器打熟 3 种基础节奏型", "节拍准确拍出 3 种节奏", 20, 1, "deep"),
            ("首支小曲", "选一首入门曲目单独练熟右手旋律", "右手能流畅弹完主段", 30, 1, "physical"),
            ("首支小曲", "单独练熟左手伴奏或低音部分", "左手能稳定跟上主段", 30, 1, "physical"),
            ("首支小曲", "慢速双手合弹曲目前 8 小节", "前 8 小节双手合弹流畅", 30, 2, "physical"),
            ("首支小曲", "完整弹奏整首曲目并录下音频", "一条完整录音文件", 30, 2, "physical"),
            ("巩固录音", "回听录音改掉最明显的一处错误并选下一首", "一条改进记录与下一首曲目", 25, 1, "deep"),
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
        "zh_milestones": ["摸清范围与诊断", "首轮梳理", "刷题与错题本", "模考与查漏"],
        "zh_steps": [
            ("摸清范围与诊断", "整理考纲范围与历年真题成一份资料清单", "一份完整备考资料清单", 25, 0, "admin"),
            ("摸清范围与诊断", "列出全部考点并标注掌握程度", "一份考点掌握诊断表", 30, 1, "deep"),
            ("首轮梳理", "挑出最薄弱的 2 个考点各梳理 1 个核心概念", "能用自己的话讲清 2 个概念", 40, 2, "deep"),
            ("首轮梳理", "合上资料按主题默写核心要点框架", "每章一页默写要点提纲", 40, 2, "deep"),
            ("刷题与错题本", "限时完成 1 套真题或模拟题", "完成一份限时试卷", 60, 2, "deep"),
            ("刷题与错题本", "按错因整理本套错题与正确思路", "一页清晰的错题复盘表", 30, 1, "deep"),
            ("刷题与错题本", "针对错题本里最弱的 3 个知识点做专项补强", "3 个弱项都能独立做对", 40, 2, "deep"),
            ("刷题与错题本", "重做一次易错与混淆题目", "前后正确率对比记录", 45, 2, "deep"),
            ("模考与查漏", "严格按考试时长进行 1 次全真模考", "一次全真模考结果", 60, 2, "deep"),
            ("模考与查漏", "考前 10 分钟快速过一遍高频必背要点", "最终检查清单全部过完", 20, 1, "deep"),
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
        "zh_milestones": ["明确目标", "快速探索", "做出初版", "打磨完善", "收尾复盘"],
        "zh_steps": [
            ("明确目标", "写下目标完成时可见的具体结果", "一条可验证的成果描述", 25, 1, "deep"),
            ("明确目标", "划分出推进这个目标的 3 个关键阶段", "3 个清晰阶段节点", 30, 1, "deep"),
            ("快速探索", "找出眼下最大的一个不确定问题", "一行核心风险说明", 20, 1, "deep"),
            ("快速探索", "花 30 分钟查证并搞清这个关键问题", "一条明确调研结论", 30, 1, "research"),
            ("做出初版", "先做出最粗糙但完整的第 1 版草稿或原型", "一个可展示的初版成果", 60, 2, "deep"),
            ("做出初版", "找人快速看 5 分钟并收集 3 条具体反馈", "3 条外部真实反馈", 25, 1, "social"),
            ("打磨完善", "根据反馈完成核心修改形成第 2 版", "改进后的第 2 版成果", 60, 2, "deep"),
            ("打磨完善", "完整通检第 2 版并列出待补细节", "一份自检清单", 45, 2, "deep"),
            ("打磨完善", "核对距离目标最终达成还差哪些收尾项", "一份缺口清单", 25, 1, "deep"),
            ("收尾复盘", "整理收尾并记下 3 条可复用经验", "3 条经验总结", 25, 1, "admin"),
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


def build_steps(text: str, complexity: str = "full", lang: str = "en") -> tuple[str, list[dict]]:
    key = pick_template(text)
    raw_steps = full_steps(key, lang=lang)
    step_defs = _select_steps(raw_steps, complexity)
    return key, step_defs


def full_steps(key: str, lang: str = "en") -> list[dict]:
    """模板的完整有序步骤（不按复杂度抽减），供滚动拆解按批取。"""
    tpl = TEMPLATES.get(key, TEMPLATES["generic"])
    rows = tpl["zh_steps"] if normalize_lang(lang) == "zh" else tpl["steps"]
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
        for i, (milestone, title, outcome, est, energy, kind) in enumerate(rows, 1)
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


def milestones(template_key: str, lang: str = "en") -> list[str]:
    tpl = TEMPLATES.get(template_key, TEMPLATES["generic"])
    return list(tpl["zh_milestones"] if normalize_lang(lang) == "zh" else tpl["milestones"])
