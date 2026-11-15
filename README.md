# Codex + DSH 协作

[![Windows 测试](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml)

**给一次目标，让 Codex 和 DeepSeek Harness（DSH）一起找盲点。** Codex 沿用当前对话选的模型。遇到重要取舍、排查卡点或关键验收时，请 DSH 独立提议或审查，再用项目证据和本机测试核实，由 Codex 最终决定。原因明确的小修由 Codex 直接处理。

DSH 默认走国内 WorkBuddy 的 DeepSeek V4.1 Flash。回复不可用时可试国际版同款，再试 Hy4 Preview。优先使用命令行，必要时在桌面接续，统一保存中文原文和验收证据。

## 安装

已验证环境为 Windows 11、Python 3.13、DeepSeek Harness Desktop 0.2.2-rc.3 和 `dsh-connect-workbuddy` 5.23.1。脚本要求 Python 3.10 或更新版本。

1. 安装 [DeepSeek Harness Desktop](https://github.com/deepseek-ai/deepseek-harness)，在桌面配置中安装 [dsh-connect-workbuddy](https://github.com/dingminhua/dsh-connect-workbuddy)。用自己的 WorkBuddy 账号登录，并确认国内线路中的 DeepSeek-V4.1-Flash 能回复。
2. 在 PowerShell 运行下面的命令。安装前检查目标，避免覆盖同名 Skill。

```powershell
$codexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$destination = Join-Path $codexRoot 'skills/codex-dsh-collaboration'
if (Test-Path -LiteralPath $destination) { throw "Skill already exists at $destination" }
git clone https://github.com/han139242/codex-dsh-collaboration.git $destination
if ($LASTEXITCODE -ne 0) { throw 'Could not clone the skill' }
python -B (Join-Path $destination 'scripts/run_dsh.py') --doctor
```

用 Git 安装后，可在 Skill 目录运行 `git pull --ff-only` 更新，再运行 `--doctor` 检查本地启动器。桌面位置和插件路径可分别用 `DSH_DESKTOP_DIR` 和 `DSH_WORKBUDDY_ENTRY` 覆盖，`DSH_HOME` 默认为 `~/.dsh`。`--doctor` 只检查文件和启动器版本。

## 单独使用

打开新的 Codex 对话，给一次目标：

> 用 Codex + DSH 协作，帮我实现……，验收标准是……

也可用 `$codex-dsh-collaboration` 明确调用。两边按同一目标、限制和验收标准工作。各阶段的调用条件和命令见 [SKILL.md](SKILL.md)。

| 想了解什么 | 从哪里看 |
| --- | --- |
| 全流程怎么跑 | [姓名去重示例](examples/README.md)提供一个有意保留缺陷的起点 |
| 回复不可用或调用超时 | 查看[模型回退](references/workbuddy-routes.md)和[桌面接续](references/desktop-fallback.md) |
| 双方交流和验证证据 | [协作小结](references/presentation.md)链接中文原文和实测输出，`*.status.md` 带等待时间与日志入口 |

## 与项目调研与产品对标配合使用

本 Skill 可独立用于讨论方案、排查卡点、审查和复查。需要先广找同类项目、核实哪些做法值得借鉴时，可以配合 [项目调研与产品对标](https://github.com/han139242/benchmark-product-workflows#安装)。只想做调研时，直接用对标 Skill 即可。

> 用项目调研与产品对标、Codex + DSH 协作，优化工单列表返回后筛选丢失的问题。先核实同类做法，交叉检查方案，再改动。验收时同一账号刷新后应保留筛选，切换账号时应使用各自的状态。

先用对标 Skill 查现状和同类做法，再由协作 Skill 把 `goal.txt` 中的同一目标、限制和验收标准，以及 `--benchmark` 指定的已核实摘录交给 DSH。DSH 提出遗漏、反例或替代方案，Codex 核实后作决定。需要复查改动时，用 `--evidence` 带上与验收相关的本机实测结果。参数和摘录格式见[项目对标说明](references/project-benchmark.md)。调研底稿和运行记录留在本地。

## 本地记录与验证

运行记录包含请求和结果，默认集中在 `~/Documents/Codex/dsh-collaboration/runs`。可以用 `DSH_COLLAB_DIR` 指定位置。将记录留在仓库外，只共享任务允许发送的材料。模型调用使用已有 WorkBuddy 积分，脚本关闭 DeepSeek 官方入口。

在仓库根目录运行 `python -X utf8 -B -m unittest discover -s tests -v`。GitHub Actions 在 Windows 的 Python 3.10 和 3.13 上跑同一套测试，无需 DSH 或 WorkBuddy 账号。真实模型调用可用上面的姓名去重示例验证。

## 参考与许可

独立提案参考了 [dsh-codex-peer](https://github.com/wuanthony397-hash/dsh-codex-peer#blind--independent-attempts-then-compare)，按证据处理分歧参考了 [Review Council](https://github.com/deployhq/review-council#design-decisions)，把审查和运行验证分开参考了 [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)。这些项目均为设计参考。

本项目使用 [MIT 许可证](LICENSE)。DeepSeek Harness 和 WorkBuddy 插件按各自许可发行。
