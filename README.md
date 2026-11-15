# Codex + 多模型协作

[![Windows 测试](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml)

**给一次目标，让 Codex 和其他模型一起找盲点。** Codex 沿用当前对话选的模型。遇到重要取舍、排查卡点或关键验收时，可请其他模型独立提议或审查，再用项目证据和本机测试核实，由 Codex 最终决定。原因明确的小修由 Codex 直接处理。

仓库提供 DeepSeek Harness（DSH）的自动化脚本。其他模型可通过用户已有的 CLI、MCP、API 或对话工具参与。用户可指定模型，也可以委托 Codex 从可用入口挑选。优先用免费入口或已有额度，新增费用按用户同意的预算。豆包、Grok、DSH 或本地模型都可以作为选择，具体用法见[模型选择与中文交接](references/model-collaboration.md)。

## 安装

使用其他模型时可只安装 Skill，并沿用自己的模型入口。下面的 DSH 配置用于自动化路线。已验证环境为 Windows 11、Python 3.13、DeepSeek Harness Desktop 0.2.2-rc.3 和 `dsh-connect-workbuddy` 5.23.1。脚本要求 Python 3.10 或更新版本。

在 PowerShell 运行下面的命令。已有安装时沿用原目录。目录说明见 [Codex Skills 文档](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

```powershell
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$legacy = Join-Path $codexRoot 'skills/codex-dsh-collaboration'
$destination = Join-Path $HOME '.agents/skills/codex-dsh-collaboration'
if ((Test-Path -LiteralPath $legacy) -or (Test-Path -LiteralPath $destination)) {
    throw 'Skill already installed. Use your existing copy.'
}
git clone https://github.com/han139242/codex-dsh-collaboration.git $destination
if ($LASTEXITCODE -ne 0) { throw 'Could not clone the skill' }
```

用 Git 安装后，可在 Skill 目录运行 `git pull --ff-only` 更新。需要 DSH 时，安装 [DeepSeek Harness Desktop](https://github.com/deepseek-ai/deepseek-harness) 和 [dsh-connect-workbuddy](https://github.com/dingminhua/dsh-connect-workbuddy)，确认 WorkBuddy 国内线路的 DeepSeek V4.1 Flash 能回复。再运行 `python -B (Join-Path $destination 'scripts/run_dsh.py') --doctor` 检查文件和启动器版本。桌面位置和插件路径可分别用 `DSH_DESKTOP_DIR` 和 `DSH_WORKBUDDY_ENTRY` 覆盖，`DSH_HOME` 默认为 `~/.dsh`。

## 单独使用

打开新的 Codex 对话，给一次目标：

> 用 Codex + 多模型协作，帮我实现……，验收标准是……。优先选我已有的免费入口。

也可用 `$codex-dsh-collaboration` 明确调用，或说「用 Codex + DSH 协作」指定 DSH。各模型按同一目标、限制和验收标准工作。调用时机和 DSH 命令见 [SKILL.md](SKILL.md)。

| 想了解什么 | 从哪里看 |
| --- | --- |
| 全流程怎么跑 | [姓名去重示例](examples/README.md)提供一个有意保留缺陷的起点 |
| 回复不可用或调用超时 | 查看[模型回退](references/workbuddy-routes.md)和[桌面接续](references/desktop-fallback.md) |
| 双方交流和验证证据 | [协作小结](references/presentation.md)链接中文原文和实测输出，`*.status.md` 带等待时间与日志入口 |
| 需要系统查漏时用什么 | 可选 [ISO/IEC 25010:2023 检查表](references/product-quality.md)。讨论方案、排查卡点时，围绕当前最需要回答的问题展开 |

## 与项目调研与产品对标配合使用

本 Skill 可独立用于讨论方案、排查卡点、审查和复查。需要先广找同类项目、核实哪些做法值得借鉴时，可以配合 [项目调研与产品对标](https://github.com/han139242/benchmark-product-workflows#安装)。只想做调研时，直接用对标 Skill 即可。

> 用项目调研与产品对标、Codex + 多模型协作，优化工单列表返回后筛选丢失的问题。先核实同类做法，交叉检查方案，再改动。验收时同一账号刷新后应保留筛选，切换账号时应使用各自的状态。

先用对标 Skill 查现状和同类做法，再把同一目标、限制、验收标准和已核实摘录交给选定的模型。模型提出遗漏、反例或替代方案，Codex 核实后作决定。复查时带上本机实测结果。DSH 路线的 `--benchmark`、`--evidence` 和摘录格式见[项目对标说明](references/project-benchmark.md)。调研底稿和运行记录留在本地。

## 本地记录与验证

每次任务把相关资料和各模型的回复集中保存。DSH 记录默认在 `~/Documents/Codex/dsh-collaboration/runs`，可以用 `DSH_COLLAB_DIR` 指定。将记录留在仓库外，只共享任务允许发送的材料。DSH 路线使用已有 WorkBuddy 积分，脚本关闭 DeepSeek 官方入口。

在仓库根目录运行 `python -X utf8 -B -m unittest discover -s tests -v`。GitHub Actions 在 Windows 的 Python 3.10 和 3.13 上跑同一套测试，无需 DSH 或 WorkBuddy 账号。真实模型调用可用上面的姓名去重示例验证。

反馈问题时，请附所用入口、阶段、最小复现、期望结果和实际结果。用合成示例或脱敏摘要说明问题。新增入口的文档请写清它依赖的授权工具、费用和用户可验证的行为。

## 参考与许可

独立提案参考 [dsh-codex-peer](https://github.com/wuanthony397-hash/dsh-codex-peer#blind--independent-attempts-then-compare)，针对单条分歧做复核参考 [Review Council](https://github.com/deployhq/review-council#design-decisions)，把实测结果带回下一轮参考 [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)。

本项目使用 [MIT 许可证](LICENSE)。DeepSeek Harness 和 WorkBuddy 插件按各自许可发行。
