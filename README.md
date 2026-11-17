# Codex + 多模型协作

[![Windows 测试](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/han139242/codex-dsh-collaboration/actions/workflows/tests.yml)

**给一次目标，让 Codex 和其他模型一起找盲点。** Codex 沿用当前对话选的模型。遇到重要取舍、排查卡点或关键验收时，可请其他模型独立提议或审查，再用项目证据和本机测试核实，由 Codex 最终决定。原因明确的小修由 Codex 直接处理。

仓库提供 DeepSeek Harness（DSH）的自动化脚本。其他模型可通过用户已有的 CLI、MCP、API 或对话工具参与。用户可指定模型，也可以委托 Codex 从可用入口挑选。优先用免费入口或已有额度，新增费用按用户同意的预算。豆包、Grok、DSH 或本地模型都可以作为选择，具体用法见[模型选择与中文交接](references/model-collaboration.md)。

## 先选一种协作方式

| 你想做什么 | 推荐入口 | 返回的证据 |
| --- | --- | --- |
| 有现成方案或改动，请另一模型找漏项 | 在 Codex 里调用 `$codex-dsh-collaboration`，说明目标和验收条件 | 独立提案或审查意见，以及 Codex 实际核对的结果 |
| 让 DSH 自己观察项目并验证反例 | [独立取证模式](#让-dsh-在项目里独立取证) | 本轮工具记录、回复和进度页 |
| 先讨论，再把实测交回去复查 | [同会话协作和双向测试](#同会话协作与双向测试) | 原会话保留查证请求与回传结果，可以回查它依据了什么 |

第一次使用 DSH 时，可从合成的[姓名去重示例](examples/README.md)开始。固定版及变更见 [Releases](https://github.com/han139242/codex-dsh-collaboration/releases)。

## 安装

使用其他模型时可只安装 Skill，并沿用自己的模型入口。下面的 DSH 配置用于自动化路线。历史实跑环境为 Windows 11、Python 3.13、DeepSeek Harness Desktop 0.2.2-rc.3 和 `dsh-connect-workbuddy` 5.23.1。兼容性随上游变化，安装后先运行 `--doctor`。脚本要求 Python 3.10 或更新版本。

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
| 回复不可用或调用超时 | 命令行自动试国内 Flash、国际 Flash，再试 Hy4，详见[模型回退](references/workbuddy-routes.md)和[桌面接续](references/desktop-fallback.md) |
| 双方交流和验证证据 | [协作小结](references/presentation.md)链接中文原文和实测输出，`*.status.md` 带等待时间与日志入口 |
| 需要系统查漏时用什么 | 可选 [ISO/IEC 25010:2023 检查表](references/product-quality.md)。讨论方案、排查卡点时，围绕当前最需要回答的问题展开 |
| 怎么让 DSH 自己查证 | [独立取证与分工](references/model-collaboration.md#原生独立取证)说明如何授权只读工具，让 Codex 同时推进其他工作 |
| 怎么在一个 DSH 会话里来回交流 | [同会话协作与双向测试](#同会话协作与双向测试) |

## 同会话协作与双向测试

原生独立取证在实际项目工作区内运行。限定材料路线可以复用事先登记的协作工作区。对 Codex 说：

> 用 Codex + DSH 协作完成这个任务，放进 DSH 的「Codex + DSH 协作」工作区。同一个任务用同一条会话。让 DSH 自己挑需要实测的边界，你用本机工具验证，把实际结果回到原会话让它复查，最后你给结论。

同一任务的讨论、审查和复查都用同一个运行目录。首次命令用 `--dsh-workspace` 传入 DSH 已登记的工作区绝对路径。团队可以把多个任务放在这个组里，每个任务分别绑定自己的会话 ID 和运行目录。

### 从 PowerShell 调用

以下使用仓库自带的[姓名去重示例](examples/README.md)。在仓库或 Skill 根目录运行，按提示输入实际的工作区路径。首次讨论会建立任务与会话的绑定。

```powershell
$runner = (Resolve-Path './scripts/run_dsh.py').Path
$bridge = (Resolve-Path './scripts/bridge_tests.py').Path
$goal = (Resolve-Path './examples/goal.txt').Path
$testFile = (Resolve-Path './examples/verify.py').Path
$workspace = (Resolve-Path -LiteralPath (Read-Host '输入 DSH 已登记的工作区文件夹路径')).Path
$taskName = 'names-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$record = Join-Path $HOME "Documents/Codex/dsh-collaboration/runs/$taskName"

python -B $runner --run-dir $record --goal $goal --dsh-workspace $workspace --advice '独立分析目标和验收边界'
if ($LASTEXITCODE -ne 0) { throw '请先检查首轮的 DSH 状态和输出' }
python -B $bridge --run-dir $record --inspect-file $testFile
```

第二条命令让 DSH 通过 `codex_request_test` 登记测试输入和预期。完成后打开运行目录中的 `bridge-request.md` 和 `bridge-test-requests.json`。Codex 按原验收标准运行适合的本机测试，再制作 UTF-8 的 `bridge-codex-results.json`。数组中每个请求编号对应一条结果，例如：

```json
[
  {
    "request_id": "00000000-0000-0000-0000-000000000001",
    "status": "failed",
    "evidence": "把这里换成实际观察到的返回值、报错或未运行的原因"
  }
]
```

用原请求里的编号替换示例编号。状态可写 `passed`、`failed` 或 `not_run`。将可共享的实测结果带回原会话：

```powershell
python -B $bridge --run-dir $record --results (Join-Path $record 'bridge-codex-results.json')
```

DSH 调用 `codex_test_results` 读取证据，中文复查保存在 `bridge-results.md`，同时可以在 DSH 原会话里查看。Codex 根据实测和原验收标准决定是否改动。桥接使用任务上次可用的 WorkBuddy 路线，也可用 `--route` 显式选另一条已授权线路。

### 让 DSH 主动请 Codex 查证

在 Codex 里可以只说一次目标：

> 用 Codex + DSH 协作完成这个任务。让 DSH 独立判断，遇到影响结论的疑点就请你核对授权文件、查公开资料或诊断卡点。把实测和来源送回原会话，让它重新判断，最后由你决定。

从 PowerShell 手动调用时，沿用上一节的 `$runner`、`$record`、`$goal` 和 `$workspace`。用 `--ask-codex` 允许 DSH 主动登记查证问题。证据够用时直接生成报告，退出码 4 表示需要 Codex 查证。

```powershell
$inquiryArgs = @('--run-dir', $record, '--goal', $goal, '--dsh-workspace', $workspace,
    '--advice-label', 'verification', '--advice', '独立判断 Unicode 去重的验收边界', '--ask-codex')
python -B $runner @inquiryArgs
```

收到退出码 4 后，查看 `bridge-evidence-advice-verification.json`，同时可读同名 Markdown。按每条请求的编号，在授权范围内核对文件事实、公开一手来源或诊断结果。将每条结果写成 UTF-8 JSON，保存为 `bridge-codex-evidence.json`。

```json
[
  {
    "request_id": "00000000-0000-0000-0000-000000000001",
    "status": "verified",
    "evidence": "填写实际核对到的事实，并附文件位置、公开来源或运行结果"
  }
]
```

用实际请求编号替换示例。如果证据不足或无法查证，分别用 `inconclusive` 或 `not_checked` 并说明原因。用原参数回传：

```powershell
python -B $runner @inquiryArgs --codex-results (Join-Path $record 'bridge-codex-evidence.json')
```

脚本在原 DSH 会话交回证据，确认 DSH 读到了对应结果后才生成正式报告。Codex 依据证据和原验收标准作最终决定。审查命令带 `--check` 时，把 `--codex-results` 加在它前面。详见[同会话查证](references/model-collaboration.md#同会话查证)。

### 侧栏位置和会话占用

如果 CLI 报会话已被占用，或桌面仍将会话显示在「未分组」，先确认 DSH 当前任务已结束，再正常退出桌面。沿用原 `--run-dir` 完成 CLI 步骤后重开 DSH，在指定工作区查看原会话。运行目录里的 `dsh-conversation.json` 记录了会话 ID，可用于核对。

## 让 DSH 在项目里独立取证

显式指定获准的项目目录。原生模式默认采用只读预设，可使用本机文件、终端和网页工具；只允许读取几份文件时，改用 [限定材料取证](references/model-collaboration.md#限定材料取证)。原生模式沿用已连接的 WorkBuddy 入口。公共搜索插件可选，用 `DSH_PUBLIC_SEARCH_ENTRY` 指向本机已安装的入口。运行记录包含交接材料，存放在项目以外的私有目录。

```powershell
$runner = (Resolve-Path './scripts/run_dsh.py').Path
$goal = (Resolve-Path './examples/goal.txt').Path
$workspace = (Resolve-Path './examples').Path
$record = Join-Path ([IO.Path]::GetTempPath()) ('dsh-example-' + [guid]::NewGuid())
python -B $runner --run-dir $record --goal $goal --workspace $workspace --independent --ask-codex --advice '自己选择并验证最小反例'
```

续轮沿用原 `--run-dir`。如果退出码为 4，按[同会话查证](references/model-collaboration.md#同会话查证)核对请求并回传证据。

## 与项目调研与产品对标配合使用

本 Skill 可独立用于讨论方案、排查卡点、审查和复查。需要先广找同类项目、核实哪些做法值得借鉴时，可以配合 [项目调研与产品对标](https://github.com/han139242/benchmark-product-workflows#安装)。只想做调研时，直接用对标 Skill 即可。

> 用项目调研与产品对标、Codex + 多模型协作，优化工单列表返回后筛选丢失的问题。先核实同类做法，交叉检查方案，再改动。验收时同一账号刷新后应保留筛选，切换账号时应使用各自的状态。

先用对标 Skill 查现状和同类做法，再把同一目标、限制、验收标准和已核实摘录交给选定的模型。模型提出遗漏、反例或替代方案，Codex 核实后作决定。复查时带上本机实测结果。DSH 路线的 `--benchmark`、`--evidence` 和摘录格式见[项目对标说明](references/project-benchmark.md)。调研底稿和运行记录留在本地。

## 本地记录与验证

每次任务把相关资料和各模型的回复集中保存。DSH 记录默认在 `~/Documents/Codex/dsh-collaboration/runs`，可以用 `DSH_COLLAB_DIR` 指定。演示对话、审查日志、查证请求、回传证据和复查结果都留在本地运行目录。只共享任务允许发送的材料。DSH 路线使用已有 WorkBuddy 积分，脚本关闭 DeepSeek 官方入口。

在仓库根目录运行 `python -X utf8 -B -m unittest discover -s tests -v`。GitHub Actions 在 Windows 的 Python 3.10 和 3.13 上跑同一套测试，无需 DSH 或 WorkBuddy 账号。真实模型调用可用上面的姓名去重示例验证。

反馈问题时，请附所用入口、阶段、最小复现、期望结果和实际结果。用合成示例或脱敏摘要说明问题。新增入口的文档请写清它依赖的授权工具、费用和用户可验证的行为。

维护步骤见 [MAINTAINERS.md](MAINTAINERS.md)。

## 参考与许可

独立提案参考 [dsh-codex-peer](https://github.com/wuanthony397-hash/dsh-codex-peer#blind--independent-attempts-then-compare)，针对单条分歧做复核参考 [Review Council](https://github.com/deployhq/review-council#design-decisions)，把实测结果带回下一轮参考 [OpenAI Cookbook](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)。

本项目使用 [MIT 许可证](LICENSE)。DeepSeek Harness 和 WorkBuddy 插件按各自许可发行。
