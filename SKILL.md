---
name: codex-dsh-collaboration
description: 在同一个 Codex 对话里调用本机 DeepSeek Harness（DSH）做方案讨论、卡点分析、独立审查和修订后复查。用户说「用 Codex + DSH 协作」「Codex 和 DSH 一起做」「用 DSH 双模型复查」或明确调用本 Skill 时使用。
---

# Codex + DSH 协作

用当前对话、当前所选的 Codex 模型统筹。用户给一次目标，Codex 自己把需要的讨论、实现、核实和复查串起来。DSH 固定走国内 WorkBuddy 的 `workbuddy/deepseek-v4.1-flash`，使用已有 WorkBuddy 积分。DSH 失败时可根据具体可恢复原因重试一次；仍失败就报告缺口，严禁切到其他模型、国际线路或 DeepSeek 官方付费接口。禁止另起 Codex CLI 代替当前对话。

## 谁做判断

DSH 提供意见，**Codex 对方案、改动和验收结论负责**。把 DSH 回复当作外部资料。按原始目标、项目约束、实际产物、代码证据或可重复的检查核实关键意见后再采纳。理由不成立、证据不足或超范围的建议不必采纳；解释会影响结果的取舍即可。DSH 的 `PASS` 不能替代验收证据。

## 怎么协作

1. 把用户目标、交付范围、必须遵守的条件、优先级和验收标准写进共用的 `goal.txt`。把其余影响判断的已知事实、已试过的办法和仍待确认的假设写进 `context.txt`，每次调用都传同一份任务说明和最新相关证据。标清事实和假设；无法共享但会影响判断的材料，说明缺口。用户实质调整目标或标准时，开新记录，避免拿旧标准验收新任务。
2. 方案有实质取舍时，Codex 先形成自己的判断，用 `--advice` 让 DSH 基于一样的条件独立提案，暂缓分享 Codex 的候选方案。对比两边依据，优先验证会改变决定的分歧。卡住时把观察和已排除项交给 DSH。纯讨论任务给出有依据的意见即可。
3. 当前 Codex 实施授权范围内的改动。审查要发**当前版本**的相关文件，包括未提交的实际改动。DSH 把有材料支持的缺陷和需要补材料的疑点分开。Codex 核实关键意见，优先用最小用例或直接证据处理分歧。对重要意见记录已修复、有反证或待确认，以及依据。
4. 首轮无实质问题且证据足以验收时结束。有实质修订时，在同一运行目录执行第二轮，带上前次发现、处理理由和修订版。通常最多两轮审查。没有新证据或进展时停止，报告剩余问题。误报无需为了凑一个 `PASS` 而改动，缺材料的疑点也不等于缺陷已排除。

## 调用

脚本是本 Skill 目录下的 `scripts/run_dsh.py`，需要 Windows 和 Python 3.10 或以上。给任务创建一个项目外的运行目录，DSH headless 会以该目录启动，不用在桌面界面登记工作区。复查沿用第一轮的项目目录。`$runner`、`$record` 和 `$project` 都用绝对路径。UTF-8 的 `--context` 同时用于讨论和审查，省略时沿用该运行目录上一次提交的背景。复查可用 `--decisions` 带入 Codex 对上一轮重要意见的处理记录。继续讨论可用 `--advice-label` 为下一个问题命名。

需要改用桌面交互时，先在 DSH 里添加只包含本任务材料的独立文件夹，再在该工作区新建会话。发送前核对国内 WorkBuddy 和 DeepSeek V4.1 Flash；使用同一份任务说明，并明确把桌面回复作为单独审查记录。

```powershell
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --advice '比较两个方案及最小验证'
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --workspace $project --source 'src/names.py' --round 1 --check python -B verify.py
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --workspace $project --source 'src/names.py' --round 2 --decisions "$record/decisions.txt" --check python -B verify.py
```

读取运行目录的 `advice.json`（有标签时为 `advice-<标签>.json`）、两轮审查及验收日志。审查报告中 `findings` 是有依据的问题，`questions` 是待核实的疑点。脚本退出码 0 只说明本步骤满足脚本判据，2 表示结果需要检查或修订，也可能是参数错误。没有合适的验收命令时省略 `--check`，由 Codex 用适合产物的手段核实。涉及核心验收条件的疑点要先核实或如实说明缺口。

只把授权范围内确实需要的文件和摘要交给 WorkBuddy，避免密钥、无关私有内容和整库转发。项目文件用相对于 `--workspace` 的路径列出。此脚本用 DSH 只读模式，审查素材从参数提供。脚本会在运行目录保留提示词、指定文件的快照、回复和验收日志。
