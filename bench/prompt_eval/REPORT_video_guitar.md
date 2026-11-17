# 滚动续批 prompt 泛化性评测：S5 短视频（项目型）× S6 吉他（技能型）

- 日期：2026-10-28
- 模型：deepseek-flash（api.deepseek.com，`.env` 配置），temperature=0.0，`response_format=json_object`
- 变体 A：线上版本 `UPCOMING_SYSTEM`（从 `goalcal/domain/refine.py` 导入），payload 与生产一致（goal_title / outline / done_steps / remaining_steps / miss_reasons，无天数）
- 变体 B：候选 v2（system prompt 原样见 `run_eval_s5_s6.py` 中 `B_SYSTEM`），payload 额外带 `days_left:int` 与 `phases:[{name, done_steps, todo_steps}]`（按 milestone 统计）
- 协议：初始 done=2 步、todo 空；每轮构造 payload → 调 LLM（失败重试 1 次）→ 校验 milestone ∈ outline（违规记录后按生产 `_next_phase` 逻辑归一）→ 新步骤追加 todo → 模拟全部完成（actual_min=est_min）→ 下一轮，共 6 轮；miss_reasons 恒为空
- 原始记录：`s5_video30_A/B.jsonl`、`s6_guitar45_A/B.jsonl`（每行一轮，含完整 payload 与原始 response）；机器指标：`metrics_s5_s6.json`；复现：`python bench/prompt_eval/run_eval_s5_s6.py && python bench/prompt_eval/analyze_s5_s6.py`

## 总体结果

| 场景×变体 | JSON 合法 | LLM 错误/重试 | milestone 违规 | 近似重复对（ratio>0.75） | 语言违规 |
|---|---|---|---|---|---|
| S5 短视频 × A | 6/6 | 0 | 0 | 0 | 0 |
| S5 短视频 × B | 6/6 | 0 | 0 | 0 | 0 |
| S6 吉他 × A | 6/6 | 0 | 0 | 0 | 0 |
| S6 吉他 × B | 6/6 | 0 | 0 | 0 | 0 |

24 次调用全部成功（无需重试），24 个响应全部解析为合法 JSON，milestone 全部合法，步骤标题零近似重复，英文目标全部产出英文步骤。est_min 均在 15–45 分钟区间，符合 5–60 分钟约束。

---

## S5 短视频 30 天（项目型，goal_title="Publish my first short video"）

outline：① Lock the concept（初始阶段，含 2 个 done）② Script and storyboard ③ Film and edit ④ Set up channel and publish ⑤ Review and plan video two。days_left（B）：[24, 21, 18, 15, 12, 9]

### S5 × A（现状）

每轮步骤：

| 轮 | milestone | 标题 | est_min |
|---|---|---|---|
| 1 | Lock the concept | Write down who it's for and the one thing they should take away | 15 |
| 1 | Script and storyboard | Draft the script as 6-8 bullet beats with a hook in the first line | 30 |
| 1 | Script and storyboard | Sketch a rough shot list, one line per beat | 20 |
| 2 | Script and storyboard | Add timing and camera notes to each shot in your list | 20 |
| 2 | Script and storyboard | Read the script aloud once and trim any lines that feel slow | 15 |
| 2 | Film and edit | Set up your filming spot and record one 10-second test clip | 25 |
| 3 | Film and edit | Record just the hook line until you have one clean take | 20 |
| 3 | Film and edit | Film the remaining shots on your list in one sitting | 40 |
| 3 | Film and edit | Assemble a rough cut: lay the clips in order, no polish yet | 35 |
| 4 | Film and edit | Watch the rough cut once and write down 2-3 fixes (cuts, pacing, audio level) | 15 |
| 4 | Film and edit | Make those fixes and add captions or a title card | 30 |
| 4 | Film and edit | Export as vertical 1080x1920 and watch it once on your phone | 20 |
| 5 | Set up channel and publish | Create the channel (or update name, handle and a 3-line bio) | 20 |
| 5 | Set up channel and publish | Write the title, a 3-sentence description and 3-5 tags for this video | 20 |
| 5 | Set up channel and publish | Upload the exported video, pick a thumbnail frame and hit publish | 30 |
| 6 | Review and plan video two | Open your video's analytics and write down just two numbers: views and average view duration | 10 |
| 6 | Review and plan video two | Jot 2 things that worked and 1 thing you'd change next time, one line each | 15 |
| 6 | Review and plan video two | Pick your video-two idea and write its one-sentence promise | 20 |

- 近似重复（ratio>0.75）：0 对。
- 阶段首次开启：② round 1，③ round 2，④ round 5，⑤ round 6（① 为初始）。
- 判断：6 轮走完全部 5 个阶段，步骤逐轮推进、无重复无停滞；第 3 轮整轮停在 ③ 属正常（拍摄+粗剪本身就大），未出现阶段扩张吞掉剩余时间的问题。

### S5 × B（候选 v2）

| 轮 | milestone | 标题 | est_min |
|---|---|---|---|
| 1 | Lock the concept | Write a concept card: working title, one target viewer, and the one takeaway they should remember | 15 |
| 1 | Script and storyboard | Write three different opening hooks (first 5 seconds) and pick the strongest one | 20 |
| 1 | Script and storyboard | Draft the full 45-60 second script, one line per beat (hook, setup, payoff, call to action) | 35 |
| 2 | Script and storyboard | Turn the finished script into an 8-10 shot list: for each shot note framing (wide/close), what happens, and its seconds on screen | 30 |
| 2 | Script and storyboard | Read the script aloud against the shot list with a stopwatch and trim any line so the whole thing lands under 60 seconds | 15 |
| 2 | Film and edit | Do a 60-second camera test: record one shot in your actual filming spot and check light, sound, and framing on playback | 20 |
| 3 | Film and edit | Film every shot on your list in one session: two takes per shot, and re-shoot any take where the framing or sound fails your camera-test standard | 45 |
| 3 | Film and edit | Assemble a rough cut: lay all clips on the timeline in script order and trim each to its planned seconds until the cut runs under 60 | 45 |
| 3 | Film and edit | Polish pass on the rough cut: add captions, level the audio, fix one thing that bugs you, and export the final file | 40 |
| 4 | Set up channel and publish | Create the channel on your chosen platform: name, handle, profile image, and a one-line bio that matches your concept card's promise | 25 |
| 4 | Set up channel and publish | Write the upload package: a title under 60 characters, a 2-sentence description with your takeaway as the first line, 3 tags, and the exact cover/thumbnail text | 25 |
| 4 | Set up channel and publish | Upload the final file as a private/unlisted draft with the full upload package, then watch it start to finish on your phone and write down every fix needed before it goes public | 30 |
| 5 | Set up channel and publish | Work down the fix list from your private-draft watch-through: fix the top three items, re-export, and flip the video to public — then note the exact publish time and paste the link somewhere you'll see it | 25 |
| 5 | Review and plan video two | Pull the first numbers on the published video (views, average view duration, likes/comments) and mark on the retention graph the exact second where most viewers drop off — one screenshot, one sentence on why | 20 |
| 5 | Review and plan video two | Turn that drop-off second plus the best-performing moment into a one-page brief for video two: the lesson you're applying, a working title, the target viewer, and the single takeaway — no shot list yet | 30 |
| 6 | Review and plan video two | Turn your one-page video-two brief into a written promise, a target viewer, and three candidate hooks — then pick the hook that directly fixes the retention-drop second | 25 |
| 6 | Script and storyboard | Draft video two's 45–60 second script in one sitting, opening with the chosen retention-fix hook and reusing the beat structure that worked in video one | 40 |
| 6 | Script and storyboard | Convert video two's script into an 8-shot list with seconds per shot, and label the first shot as the retention-fix shot — stop before filming | 30 |

- 近似重复（ratio>0.75）：0 对。
- 阶段首次开启：② round 1，③ round 2，④ round 4，⑤ round 5（① 为初始）。
- 判断：**无莫名其妙的重复，也无停滞**。days_left 预算生效：④比 A 早一轮开启（round 4，days_left=15），round 5 就完成公开发布，round 6 进入复盘并自然展开「video two」的规划——⑤阶段名就叫 "Review and plan video two"，第 6 轮回到 ② 给 video two 写脚本属于该阶段的本意，不是倒退。相对 A，B 的交付物链更闭环（private draft → 修复清单 → 公开发布；retention 掉点 → video two brief → 针对性 hook），每一步都引用前一步的产物（concept card、camera-test standard、fix list、drop-off second），项目型目标上没有任何为凑数而生的重复。

---

## S6 吉他 45 天（技能型，goal_title="learn guitar"）

outline：① Setup & first chords（初始阶段，含 2 个 done）② Chord transitions & strumming ③ Song repertoire build ④ Full-song polish & dynamics ⑤ Demo performance prep。days_left（B）：[40, 35, 30, 25, 20, 15]

### S6 × A（现状）

| 轮 | milestone | 标题 | est_min |
|---|---|---|---|
| 1 | Setup & first chords | Learn the open G and C chords and check each string rings cleanly | 30 |
| 1 | Setup & first chords | Practice slow switches between Em, Am, C and G for 10 minutes | 20 |
| 1 | Chord transitions & strumming | Strum steady downstrokes on one chord while counting 1-2-3-4 out loud | 15 |
| 2 | Chord transitions & strumming | Practice a down-up strumming pattern (D-DU-UDU) on a muted strum for 10 minutes | 15 |
| 2 | Chord transitions & strumming | Do a 10-minute metronome drill switching between Em, Am, C and G at a slow tempo | 20 |
| 2 | Song repertoire build | Pick one easy 2-3 chord song and play its chord changes with steady downstrokes | 25 |
| 3 | Chord transitions & strumming | Play your chosen song's chord progression with the D-DU-UDU pattern at a slow tempo for 15 minutes | 20 |
| 3 | Song repertoire build | Learn the chord shapes and changes for a second easy 2-3 chord song | 25 |
| 3 | Song repertoire build | Play both songs back-to-back once each without stopping to build stamina | 20 |
| 4 | Song repertoire build | Play your first song start to finish once and note the shakiest chord change | 15 |
| 4 | Song repertoire build | Loop that shakiest chord change slowly for 10 minutes until it feels smooth | 15 |
| 4 | Full-song polish & dynamics | Play the same song again with a soft verse and a louder chorus to practice dynamics | 20 |
| 5 | Full-song polish & dynamics | Record yourself playing your first song once on your phone, then listen back and write down two things to fix | 20 |
| 5 | Full-song polish & dynamics | Spend 15 minutes fixing those two spots slowly with a metronome, then play the song once more | 20 |
| 5 | Demo performance prep | Do a full run-through of the song standing up, playing it start to finish without stopping, as a dress rehearsal | 20 |
| 6 | Demo performance prep | Set your phone up to record and capture one full standing take of your song from start to finish | 20 |
| 6 | Demo performance prep | Listen back to the recording and loop the one spot that slips for 10 minutes with the metronome | 15 |
| 6 | Demo performance prep | Play the song live once for a friend or family member, or record and share a second full take | 20 |

- 近似重复（ratio>0.75）：0 对。
- 阶段首次开启：② round 1，③ round 2，④ round 4，⑤ round 5（① 为初始）。
- 判断：整体有推进（和弦扩展、加扫弦模式、两首歌、录音复盘），第 5–6 轮出现**语义级回旋**：r5 "full run-through standing up as dress rehearsal" → r6 "record one full standing take" → r6 又 "play live... or record and share a second full take"，「站立完整弹一遍/录一遍」的同类动作三度出现，标题文字不同所以 difflib 未超阈值，但动作本身几乎无递进（r6 相比 r5 只多了"录像"）。这正是生产上「重复步骤」的一类温和形态。

### S6 × B（候选 v2）

| 轮 | milestone | 标题 | est_min |
|---|---|---|---|
| 1 | Setup & first chords | Learn the D chord and check each string rings cleanly | 25 |
| 1 | Setup & first chords | Learn the G chord and do 5 minutes of slow Em–Am–D–G changes with a metronome at 40 bpm | 30 |
| 1 | Chord transitions & strumming | First strumming rep: steady all-downstroke 4/4 pattern over Em–Am–D–G, 5 clean laps in a row | 30 |
| 2 | Chord transitions & strumming | Learn the C chord, check each string rings, then play slow C–G–C changes for 5 minutes at 40 bpm | 30 |
| 2 | Chord transitions & strumming | Switch the all-downstroke pattern to down-up eighth notes (1-and-2-and) over Em–Am at 50 bpm, 4 clean laps in a row | 35 |
| 2 | Chord transitions & strumming | One-minute change drill on the hardest pair (likely C–G): count clean changes, then beat that count on a second run | 25 |
| 3 | Chord transitions & strumming | Learn the D-DU-UDU strumming pattern and loop it over G–C at 55 bpm until you get 4 clean laps without stopping | 30 |
| 3 | Song repertoire build | Pick your first song (e.g. Knockin' on Heaven's Door: G–D–Am–C) and play just its chord progression on downstrokes at 60 bpm, 3 passes | 30 |
| 3 | Song repertoire build | Play that song's verse section end to end with the strumming pattern, 3 run-throughs without stopping, and write down the one spot that breaks | 30 |
| 4 | Song repertoire build | Isolate the spot that broke last time: loop just that bar/change 10 times slowly, then play the verse 3 times clean and log whether the break moves | 25 |
| 4 | Song repertoire build | Learn the chorus of song 1 and play verse straight into chorus without stopping, 3 passes at 60 bpm | 30 |
| 4 | Song repertoire build | Add a second song with an easy progression (e.g. Three Little Birds: A–D–E) and play 3 passes of its progression with the D-DU-UDU pattern at 60 bpm | 35 |
| 5 | Song repertoire build | Close out song 2: learn its chorus, then play verse straight into chorus 3 times without stopping, and finish by running both songs back to back once as a mini set | 35 |
| 5 | Full-song polish & dynamics | Record one full song start to finish on your phone, listen back once, and write down the 3 spots that sound weakest | 25 |
| 5 | Full-song polish & dynamics | Dynamic contrast pass: verse with light downstrokes, chorus with full D-DU-UDU at 65 bpm, 3 passes per song so the chorus is audibly louder | 30 |
| 6 | Full-song polish & dynamics | Record song 1 again with the verse-light/chorus-loud plan at 65 bpm, A/B it against your earlier take, and write down whether the chorus is now clearly louder | 30 |
| 6 | Demo performance prep | Stand up and play both songs back to back as one mini set with no stops, recorded in a single take; note the total time and the two spots where you flinched | 35 |
| 6 | Demo performance prep | Pick the single song for your demo and play it 3 times in a row with a 30-second pause between takes, holding the same tempo and dynamics each time | 35 |

- 近似重复（ratio>0.75）：0 对。
- 阶段首次开启：② round 1，③ round 3，④ round 5，⑤ round 6（① 为初始）。
- 判断：**和弦/练习重复全部带明确递进，无原地重复**。证据链：
  - 速度与指标阶梯：扫弦 40bpm（r1，5 clean laps）→ 50bpm 八分音（r2，4 laps）→ 55bpm 引入 D-DU-UDU（r3）→ 60bpm 进歌（r3–4）→ 65bpm 力度对比（r5–6），bpm 数值明确写进标题。
  - 换弦递进：r1 Em–Am–D–G 慢速转换 → r2 C–G 转换 + "one-minute change drill，数干净次数再打破纪录" → r3 整段扫弦不停顿 4 圈 → r4 "Isolate the spot that broke last time…log whether the break moves"（显式引用上次失败点并验证是否修复）。
  - 曲目递进：r3 第一首只弹和弦进行 → r3 副歌前进到 verse 段落 → r4 学 chorus、verse 直连 chorus → r4 加第二首（新和弦集 A–D–E）→ r5 两首连奏 mini set。曲目、段落、数量三层都在涨。
  - 录音/表现递进：r5 录一遍找 3 个最弱点 → r6 "A/B it against your earlier take"，用对比早先录音验证 chorus 是否更响——可观测的质量门槛，而不是再录一遍完事。

---

## 关键判断汇总

1. **吉他 B 的重复是递进式重复**：每次重复都声明了本次变化（更高 bpm、更多 clean laps、更难的和弦对、更大的曲目单元、A/B 对比验证），完全符合候选 v2 Rule 5 的设计意图。
2. **短视频 B 无重复、无停滞**：项目型目标上 B 表现出 days_left 预算能力（阶段 ④比 A 早一轮开启，第 5 轮即完成发布），第 6 轮回到 ② 是阶段 ⑤"plan video two"的本意展开，并非回退或原地打转。
3. **本协议下生产问题未复现**：A 在两个场景都没有出现「大量重复步骤、阶段停滞」。吉他 A 存在语义级回旋（r5–r6「站立完整演奏/录制」三度出现，标题级 difflib 未超 0.75），是最接近问题形态的迹象。生产问题更可能由 miss_reasons 非空、actual 超时等历史噪声触发，本协议（全部按时完成、miss 恒空）未覆盖该路径——这是本评测的主要盲区。
4. **B 的阶段节奏偏保守但合理**：吉他 B 把 ③④⑤ 分别推到 r3/r5/r6，比 A 晚一轮，与 45 天预算（40/35/30 天时仍在打基础）一致；每轮内容仍有明确递进，不构成停滞。
5. **工程质量项**：24/24 JSON 合法、milestone 零违规、语言一致（英文目标全英文输出）、est_min 全部落在 5–60 分钟规则区间内。

## 局限

- 单模型（deepseek-flash）、temperature=0 各跑一次；未做多次采样检验稳定性。
- 近似重复仅用标题 difflib ratio>0.75，语义级重复（如 S6 A 的 r5–r6 回旋）需人工复核，已在上文标注。
- 模拟用户永远按时完成（actual=est、miss_reasons 空），Rule 3 的降级校准路径未触发；变体 B 在该路径上的行为需另测。
