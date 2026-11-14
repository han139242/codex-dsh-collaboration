# 姓名去重示例

在 Codex 中打开此仓库。先读 `goal.txt`，用下面这句话启动完整流程：

> 用 Codex + DSH 协作完成 examples/goal.txt。动手前先独立比较 Unicode 去重策略。让 DSH 审查当前改动，修复有依据的问题，然后复查。

初版的 `names.py` 只折叠大小写，故意留了一个规范等价问题。用 `python -B verify.py` 从本目录运行验收，修复前应有 1 项失败，修复后应全部通过。工作记录建议放在仓库之外。
