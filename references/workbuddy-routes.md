# WorkBuddy 模型回退

- 默认 `--route auto`。首选国内 DeepSeek V4.1 Flash，再试国际版同款、国内 Hy4 Preview、国际版 Hy4 Preview。后续先用同任务最近有可用回复的路线。已明确报告 `UNKNOWN_MODEL` 的路线退出自动候选。
- `--timeout` 默认每条路线 240 秒，`--total-timeout` 默认全轮 480 秒。单条路线显式更长时，默认总预算至少给足它。也可显式设较短总预算，后备路线共享剩余时间。预算耗尽时保留原会话和已有记录。
- 模型忙、线路超时或回报无法解析时，先查已保存的会话和工具结果，再换现有 WorkBuddy 路线。本机终端沙箱初始化失败时，其他模型会遇到同一环境。可在原授权内改用只读文件工具或 Codex 补证。有已登记的查证请求就先交接，再在同一会话回传。
- 无可用 CLI 回复时才检查[桌面接续](desktop-fallback.md)。具备相同取证边界才使用桌面结果。仍有缺口时，Codex 按实测推进并标明未获得的证据。

保留当前 WorkBuddy 推理偏好。复杂推演确有收益时可显式用 `--effort max`，Flash 配合足够的时间；明确或定向复查可用 `--effort high`，Hy4 固定用 high。实际路线和档位以 DSH 本轮 `request/header` 为准。先比较有效新发现、错误和耗时再决定是否提高档位。 [DSH 模型路由](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.zh.md#选择-session-标识) · [WorkBuddy 适配器](https://github.com/dingminhua/dsh-connect-workbuddy)

讨论、审查、复查和切换同任务沿用 `--run-dir` 与原工作区。日志先记录会话 ID，后续通过 `--session-id` 继续。待回传证据使用原阶段和原材料。超时后若只剩部分发现，可用新讨论标签提出更聚焦的问题，仍在同一会话。无法确认原会话身份时停止自动重试。
