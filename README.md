# Codex + DSH 协作

把同一个目标交给当前 Codex 对话。遇到方案取舍时，Codex 和 DeepSeek Harness（DSH）先各自思考，再比较依据。Codex 实施并核实 DSH 的意见，按需要修复和复查。

**Codex 沿用你在当前对话选的模型。** DSH 固定使用国内 WorkBuddy 的 `DeepSeek-V4.1-Flash`。本项目是一个 Codex Skill 和本地 Python 脚本，通过 DSH headless 调用模型，无需 MCP 服务。

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

如果 DSH 装在自定义位置，可在本次 PowerShell 会话设置 `DSH_DESKTOP_DIR` 指向包含 `DeepSeek Harness.exe` 的目录。`DSH_HOME` 默认是 `~/.dsh`。需要时可用 `DSH_WORKBUDDY_ENTRY` 指向已安装插件的 `lib/index.js`。这些覆盖也要在运行 Codex 的环境中可见。`--doctor` 只检查文件和启动器版本，无模型调用。

## 使用

打开新的 Codex 对话，给一次目标：

> 用 Codex + DSH 协作，帮我实现……，验收标准是……

也可用 `$codex-dsh-collaboration` 明确调用。辅助思考时，DSH 获得和 Codex 一致的任务目标、约束和验收标准。需要独立提案时先不发 Codex 的候选方案。审查转交当前文件快照，复查带上上一轮发现和 Codex 的处理依据。Codex 会核实意见，区分实际问题和待确认疑点。详见 [SKILL.md](SKILL.md)。

用于实际项目时，先确定哪些材料可发给 WorkBuddy。模型调用消耗已有 WorkBuddy 积分。运行记录包括提交给模型的内容和结果，建议放在项目外。脚本通过临时配置锁定国内 WorkBuddy 的指定模型，关闭 DeepSeek 官方模型入口。

## 验证

在仓库根目录运行 `python -B -m unittest discover -s tests -v`，无需 WorkBuddy 账号。测试覆盖了讨论与审查使用同一任务说明、标准发生变化时拒绝沿用旧记录，以及审查后源文件变动时拒绝交付旧结论等行为。

如果你已配置 WorkBuddy，可用 [姓名去重示例](examples) 验证真实模型调用。

## 参考与许可

独立提案参考了 [dsh-codex-peer](https://github.com/wuanthony397-hash/dsh-codex-peer#blind--independent-attempts-then-compare)，按证据处理分歧参考了 [Review Council](https://github.com/deployhq/review-council#design-decisions)，把审查和运行验证分开参考了 [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)。这些项目均为设计参考。

本项目使用 [MIT 许可证](LICENSE)。DeepSeek Harness 和 WorkBuddy 插件按各自许可发行。
