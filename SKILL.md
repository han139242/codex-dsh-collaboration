---
name: codex-dsh-collaboration
description: 在同一个 Codex 对话里调用本机 DeepSeek Harness（DSH）做方案讨论、卡点分析、独立审查和修订后复查。用户说「用 Codex + DSH 协作」「Codex 和 DSH 一起做」「用 DSH 双模型复查」或明确调用本 Skill 时使用。
---

# Codex + DSH 协作

用当前对话和用户所选的 Codex 模型统筹。用户给一次目标，Codex 串起讨论、实现、核实和复查。DSH 默认走国内 WorkBuddy 的 DeepSeek V4.1 Flash，回复不可用时按[模型回退](references/workbuddy-routes.md)试 WorkBuddy 国际版同款，再用 Hy4。可使用已有 WorkBuddy 积分，禁止使用 DeepSeek 官方接口。优先用命令行，超时先尝试取回本地已完成的回复；Computer Use 作为[桌面接续](references/desktop-fallback.md)保底。

## 什么时候调用 DSH

用户明确要求的阶段照做。其余由 Codex 判断：**DSH 的独立视角可能改变方案、下一步排查或验收结论时才调用**。原因明确、影响局部且能直接验收的小修，Codex 自己改和验证。

| 时机 | 交给 DSH 的问题 |
| --- | --- |
| 动手前有重要取舍 | Codex 先形成自己的方案，随后用 `--advice` 发送共同目标、约束、证据和待决问题，暂留自己的选择。请 DSH 找遗漏的方案、适用边界或能区分方案的例子。 |
| 排查遇到卡点 | 现有证据分不清原因，或下一步尝试代价较大时，用 `--advice` 发已观察到的现象、已排除项和剩余假设。请 DSH 补一个不同的解释和最省成本的验证。 |
| 改动影响较大，或核心验收有漏项风险 | 用当前产物请 DSH 独立审查，重点找反例、未覆盖的验收点，以及有证据的改法。 |
| 重要问题修订后 | Codex 先按原标准验收，再请 DSH 核对修订、测试证据和是否引入回归。 |

按任务只选需要的阶段。已有证据能回答当前问题时就推进；发现新证据或有实质修订时再复查。

## 谁做判断

**Codex 对方案、改动和验收结论负责。** 两边按同一目标和证据独立判断。对会改变决定的意见，Codex 检查代码、原始资料或可复现的用例，再决定采纳、驳回或待证。双方一致时也按原验收标准核查。复查时把实测结果和处置理由交给 DSH 检查有没有遗漏。最后由 Codex 说明重要取舍和依据。

## 怎么协作

1. 把用户目标、交付范围、必须遵守的条件、优先级和验收标准写进共用的 `goal.txt`。用 `context.txt` 补已知事实和待证假设。只给一次目标就推进，能低成本验证的细节按合理假设推进并告知用户。用户实质调整目标或标准时开新记录，避免拿旧标准验收新任务。
2. 做新项目、有重要设计取舍或优化主要流程时，先查现状，再做[项目对标](references/project-benchmark.md)。用 `--benchmark` 传已查证的摘录，证据少时说明覆盖范围，用本地基线补足。只调研时按用户授权交付。
3. Codex 实施授权范围内的改动。要审查时发送**当前版本**的相关文件，包括未提交的实际改动。DSH 把有材料支持的缺陷和需要补材料的疑点分开。对重要意见记录已修复、有反证或待确认，以及依据。
4. 首轮无实质问题且证据足以验收时结束。有实质修订时，先按原标准验证修订版。复查沿用同一运行目录，给 DSH 修订后的材料、相关验收脚本（`--source`）、实测命令与结果（`--evidence`）和每条重要意见的处理理由（`--decisions`）。通常最多两轮审查。用实际证据处理剩余疑点。

有方案讨论、多轮审查或用户要查看过程时，按[展示与协作记录](references/presentation.md)给一页中文小结。让用户能看到这一步问谁、拿到什么、如何验证以及最后怎么决定。简单任务可直接在回复里给同样的信息。

默认用简体中文写任务说明、背景、讨论问题和复查依据。DSH 用中文写核查报告，即使通过也要说明核对了哪条标准、依据哪段材料，并用有区分度的例子或证据解释判断。给用户展示具体分歧、Codex 的取舍和可重复的检验。代码、命令和错误原文保留原样。

## 调用

脚本是本 Skill 目录下的 `scripts/run_dsh.py`，需要 Windows 和 Python 3.10 或以上。默认把任务记录集中在 `~/Documents/Codex/dsh-collaboration/runs` 下，按任务保留记录以免覆盖。有 `DSH_COLLAB_DIR` 时改用该目录下的 `runs`。DSH 桌面只登记一个「Codex + DSH 协作」工作区，各轮接续在里面打开各自的会话。复查沿用同一个任务记录和第一轮的项目目录。`$runner`、`$record` 和 `$project` 都用绝对路径。UTF-8 的 `--context` 同时用于讨论和审查，省略时沿用最近保存的背景。复查用 `--decisions` 带入 Codex 的处理依据。继续讨论可用 `--advice-label` 为下一个问题命名。

```powershell
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --advice '比较两个方案及最小验证'
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --workspace $project --source 'src/names.py' --round 1 --check python -B verify.py
python -B $runner --run-dir $record --goal "$record/goal.txt" --context "$record/context.txt" --workspace $project --source 'src/names.py' 'verify.py' --round 2 --decisions "$record/decisions.txt" --evidence "$record/evidence.txt" --check python -B verify.py
```

组合使用时，在讨论或审查命令中加 `--benchmark "$record/benchmark.md"`。同任务后续调用省略此参数会沿用已保存的摘录，更新 `--context` 时也会保留。显式传入新版可替换摘录，用 `--clear-benchmark` 可撤回。原始发送记录仍可查看。只把相关公开机制和必须共享的内容写进摘录。

运行目录里的 `advice.md`（有标签时为 `advice-<标签>.md`）和 `review-1.md`、`review-2.md` 可以直接阅读。需要看 Codex 发了什么时，打开相应的 `*.prompt.txt`，桌面接续则看 `*.desktop-request.txt`。脚本另存 JSON 供程序使用。退出码 0 只说明本步骤满足脚本判据，2 表示结果需要检查或修订，也可能是参数错误，3 表示模型超时且已准备桌面请求。报告格式有误时，读保存的原文核实其中意见。没有合适的验收命令时省略 `--check`，由 Codex 用适合产物的手段核实。

只把授权范围内确实需要的文件和摘要交给 WorkBuddy，避免密钥、无关私有内容和整库转发。项目文件用相对于 `--workspace` 的路径列出。此脚本用 DSH 只读模式，审查素材从参数提供。脚本会在运行目录保留提示词、指定文件的快照、回复和验收日志。
