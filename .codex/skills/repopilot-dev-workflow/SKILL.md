---
name: repopilot-dev-workflow
description: RepoPilot 仓库的改动交付流程。用于在本仓库做功能开发、缺陷修复、重构或文档改动并要交付时：先建分支、同步 RepoPilot.md 与 docs/open-questions.md、自检通过后提交、以 --no-ff 合并进 main、推送并清理已合并分支。只读分析、评审或纯问答（不产生仓库改动）时不要使用。
metadata:
  short-description: 分支优先、验证后提交、合并推送并清理
---

# RepoPilot 改动交付流程

在本仓库交付改动时按以下顺序执行。目标：默认分支始终可用、设计文档与实现不脱节、每次改动都经过可重复的验证，且已完成的改动最终落到 `main`。

## 1. 先建分支

- 动手前先确认状态：`git status -sb`、`git branch --show-current`。
- 若当前在默认分支上，先创建分支再改代码：`git switch -c feat/short-topic`（同理 `fix/`、`docs/`、`chore/`、`test/`）。
- 一个任务一个分支；不要把无关改动混进同一分支或同一次提交。
- 不要 `git push --force`，不要直接提交到默认分支，不要改写已推送的历史。
- 完成后的合并、推送与分支清理是固定收尾流程，按 §5 直接执行，不需要逐次征求确认；只有用户明确说「只做本地提交、不要推送」时才停在本地。

## 2. 让 `RepoPilot.md` 保持为「活的设想」

`RepoPilot.md` 是初期设想，不是不可改的契约：

- 现实条件不满足（依赖不可得、平台/环境不支持、成本过高）或存在更优方案时，**直接调整实现**，并在同一次改动里同步差异：
  - 影响面大、需要长期跟进的偏差 → 更新 `RepoPilot.md` 对应小节；
  - 只是实现选型或设计未定义处的临时决定 → 记入 `docs/open-questions.md`。
- 不要沉默地偏离：在最终回复里明确说明「文档怎么写 / 实际怎么做 / 为什么」。
- 反向也要做：不要为了迎合文档而保留已被证明更差的实现。

## 3. 自检（提交前必做）

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

- 三条全绿才算通过。格式问题先跑 `.\.venv\Scripts\python.exe -m ruff format .` 再重跑检查。
- 改动涉及 API、报告结构或检查器时，确认 `tests/test_e2e.py` 仍覆盖新行为；端到端测试是阶段一验收的核心。
- 改动涉及 `src/repopilot/execution/` 时额外确认硬性约束仍然成立：无 `shell=True`、每个子进程都有超时与输出大小上限、宿主凭据不进入子进程、不可信来源默认不执行仓库代码。
- **自检不通过就不提交**：修好再提交；确实无法修好时，在最终回复中说明失败原因与当前状态。

## 4. 提交

提交信息用 Conventional Commits，正文说明改动范围与验证方式：

```
feat(execution): pin baseline commit for repository checkout

- 校验 detached checkout 读回值，失败即报 commit_unresolved
- 验证：pytest 49 passed；ruff check / ruff format --check 通过
```

- 只暂存本次任务相关的文件；不要用 `git add .` 把无关的未跟踪文件一起带入。
- 不提交凭据、`.env`、任务工作区、日志或报告产物。
- 用户只要求分析、评审或回答问题时，不提交。

## 5. 合并、推送与分支清理（例行操作，直接执行）

分支交付完成后按下述步骤收尾，**不要就这些步骤向用户提问**：

1. 切到 `main` 并确认干净、自检全绿：
   `git switch main` → `git status --short`（应为空）→ 跑 §3 三条命令。
   `main` 不干净或自检失败时，先修好，不要带着问题合并。
2. 用 `--no-ff` 合并，保留分支痕迹便于追溯：
   `git merge --no-ff <branch> -m "Merge branch '<branch>' into main"`
3. 推送：`git push origin main`
4. 清理已合并分支（本地 + 远端）：
   ```
   git branch -d <branch>
   git push origin --delete <branch>
   git fetch --prune
   ```
   `-d` 只在确认已合并时才成功，不要用 `-D` 绕过；删除**已合并**分支是安全的例行操作。
5. 存在多个待合并分支时，按依赖顺序依次执行 1–4。

仍需用户确认的例外（这些不是例行操作，必须显式请求后才做）：

- `git push --force`，或任何改写已推送历史的操作；
- 删除**尚未合并**的分支、丢弃未提交改动、`git reset --hard`；
- 关闭/删除远端仓库、修改可见性、修改默认分支、改动协作者权限。

## 6. 收尾：给出下一步建议

最终回复包含：

- 改动摘要与验证结果（实际运行的命令 + 关键输出）。
- 分支名与提交号，以及合并推送后 `main` 的提交号。
- 2–3 条具体的下一步建议（例如「阶段二接入容器沙箱」「把 `TaskStore` 换成 PostgreSQL」），并指出仍待确认的设计问题。