# RepoPilot

基于 LLM 与 Agentic RL 的 GitHub 仓库评估及自动修复智能体。

用户提供 GitHub 仓库地址、Issue、CI 失败记录或仓库评估任务后，系统自动完成仓库分析、问题定位、代码修改、测试验证和修复报告生成，并在满足质量门禁后创建独立分支和 Draft PR（不自动合并）。

> 项目唯一事实来源（source of truth）：[`RepoPilot.md`](RepoPilot.md)。本 README 仅为导航与现状摘要，二者冲突时以 `RepoPilot.md` 为准。

## 状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段一 | 仓库接入与评估基线 | **已完成（本迭代）** |
| 阶段二 | Agent 自动修复闭环 | **已完成（本迭代）** |
| 阶段三 | GitHub 自动提交与 Draft PR | **已完成（本迭代）** |
| 阶段四 | 异步任务管理与交互整合 | 未开始 |
| 阶段五 | 代码检索增强与评测工具链 | 未开始 |
| 阶段六 | Agentic RL 能力扩展 | 未开始 |

## 阶段一交付内容

输入受信任的 Python 仓库地址后，服务自动克隆仓库、固定基线 commit、扫描项目、执行确定性检查，并生成结构化评估报告。

| 能力 | 实现位置 |
|---|---|
| FastAPI 后端与任务端点 | `src/repopilot/api/routes.py`、`src/repopilot/app.py` |
| 任务模型与状态机 | `src/repopilot/models.py` |
| 克隆与基线 commit 固定 | `src/repopilot/execution/git.py` |
| 受控子进程执行（超时/输出上限/环境隔离） | `src/repopilot/execution/process.py`、`src/repopilot/execution/sandbox.py` |
| 项目扫描与 Python 识别 | `src/repopilot/scan/scanner.py` |
| 检查器（Ruff / pytest / 依赖风险 / 启发式） | `src/repopilot/checkers/` |
| 统一评估报告 | `src/repopilot/report/builder.py` |
| 评估流水线编排 | `src/repopilot/pipeline/evaluate.py` |
| 结构化 JSONL 日志（task_id + run_id） | `src/repopilot/logging_setup.py` |
| 受控 fixture 仓库 | `fixtures/`、`src/repopilot/fixtures.py` |

### 预留的抽象边界

后续阶段可替换实现而不改动调用方：

| 边界 | 协议位置 | 阶段一实现 | 后续替换方向 |
|---|---|---|---|
| `TaskStore` | `src/repopilot/storage/base.py` | `InMemoryTaskStore` | PostgreSQL（阶段四） |
| `TaskScheduler` | `src/repopilot/scheduler/base.py` | `ThreadPoolTaskScheduler` | Redis / 任务队列（阶段四） |
| `SandboxExecutor` | `src/repopilot/execution/sandbox.py` | `LocalProcessSandboxExecutor` | Docker 隔离执行 |
| `Checker` | `src/repopilot/checkers/base.py` | Ruff / pytest / pip-audit / heuristics | 新增或替换检查器 |
| `ModelClient` | `src/repopilot/llm/base.py` | `StubModelClient` / `OpenAICompatibleModelClient` | 真实 vLLM 或其它推理后端 |
| 受控工具集 | `src/repopilot/agent/tools.py` | `read_file` / `search_code` / `apply_patch` / `run_tests` / `view_diff` | 扩展只读或写入工具 |

## 阶段二交付内容

在阶段一基础上新增 `mode=repair` 的 Agent 自动修复闭环：任务理解 → 仓库探索 → 代码定位 → 生成补丁 → 执行工具 → 分析反馈 → 重试或终止 → 结果汇总。Agent 只能经下方受控工具操作工作区，没有任意 shell 能力，也不持有 GitHub 写凭据。

| 能力 | 实现位置 |
|---|---|
| 模型抽象（OpenAI 兼容，真实指向 vLLM） | `src/repopilot/llm/base.py`、`src/repopilot/llm/openai_client.py` |
| 确定性 stub（离线跑通全流程） | `src/repopilot/llm/stub.py` |
| provider 切换与连通性探测 | `src/repopilot/llm/registry.py`、`src/repopilot/llm/health.py` |
| LangGraph 修复工作流 | `src/repopilot/agent/graph.py` |
| 受控工具集（Agent 唯一入口） | `src/repopilot/agent/tools.py` |
| 有限循环预算控制 | `src/repopilot/agent/limits.py` |
| 质量门禁（设计文档 §3.6） | `src/repopilot/agent/quality_gate.py` |
| 修复流水线与状态持久化 | `src/repopilot/pipeline/repair.py` |
| 共享 pytest 运行与 JUnit 解析 | `src/repopilot/execution/test_run.py` |

### 受控工具集

Agent 只能调用这五个工具，全部在宿主侧校验路径与预算，无 `shell=True`：

| 工具 | 作用 |
|---|---|
| `read_file` | 读取工作区内文本文件（带行号窗口）或列出目录 |
| `search_code` | 工作区内正则搜索，可限制 glob 与结果上限 |
| `apply_patch` | 用 `git apply` 应用统一 diff（先 `--check`） |
| `run_tests` | 经阶段一 `SandboxExecutor` 运行 pytest 并解析 JUnit XML |
| `view_diff` | 查看当前工作区改动 |

### 有限循环控制

命中任一上限即终止，并把原因写入 `termination_reason`：最大修复轮数、总执行时间、模型调用次数、模型 token 预算、工具调用次数、单次工具输出上限、补丁行数与文件数。重复应用同一补丁会触发 `no_progress`。

### 质量门禁（设计文档 §3.6）

七项检查全部通过才算修复成功：`patch_applied`、`tests_executed`、`baseline_attributable`、`target_tests_pass`、`no_regression`、`change_scope`、`no_cheating`。删除或跳过测试、弱化断言、把断言改成恒真等作弊行为会被拒绝并标记 `cheating_detected`；无法确认时落到 `needs_review` / `failed`，绝不报成功。

### 状态持久化与续跑

每轮都把状态与工具输出写入工作区 `artifacts/repair-state.json`；在请求里带 `resume_task_id` 可基于上次状态继续追加轮次。每轮执行都写入统一 `task_id` + `run_id` 的 JSONL 日志，为阶段六轨迹采集保留数据。

## 阶段三交付内容

在阶段二基础上新增「门禁通过后自动提交修复分支并创建 Draft PR」：从固定基线 commit 建分支 → 应用已验证补丁 → 提交 → 调用 GitHub REST API 创建 **Draft PR**，把 PR 链接与远端状态写回任务记录。**仅创建 Draft PR，绝不自动合并、绝不直接推送默认分支。**

| 能力 | 实现位置 |
|---|---|
| 凭据集中管理（脱敏、懒读取） | `src/repopilot/credentials.py` |
| GitHub 客户端边界（无 merge / 无默认分支推送操作） | `src/repopilot/github/base.py` |
| REST 实现（错误码映射、响应大小上限） | `src/repopilot/github/rest.py` |
| 离线 fake（测试不触网，记录读写调用） | `src/repopilot/github/fake.py` |
| 门禁前置的幂等发布器（唯一远端写入口） | `src/repopilot/pipeline/publish.py` |
| 容器隔离沙箱 | `src/repopilot/execution/container.py`、`src/repopilot/execution/sandbox.py` |

### 发布流程与幂等

`PullRequestPublisher` 是**唯一**执行远端写操作的地方；Agent 既不持有也无法接触 GitHub 写凭据。

1. 门禁前置：仅当 `quality_gate.passed` 且 `result == fixed` 时才尝试发布，否则直接 `skipped`，**零远端调用**。
2. 容器前置：默认 `REPOPILOT_PUBLISH_REQUIRES_CONTAINER=true`，非容器隔离的任务发布失败（`code_execution_not_isolated`），不触碰网络。
3. 分支名由 `task_id` 推导（`repopilot/fix-<task_id>`），同一任务重复触发不会产生第二个分支。
4. 已存在同任务 PR 时直接复用（`reused`）；分支名已存在但无 PR 视为冲突（`github_conflict`）。
5. PR 创建失败会删除刚建的分支回滚，绝不留下半成品分支。
6. PR 描述由**同一份**改动集生成（问题背景、修改说明、影响文件、测试命令、基线/修复后测试结果），不会与推送内容不一致。

### 错误码与任务状态

| 场景 | `error_code` | 任务状态 | 远端写 |
|---|---|---|---|
| 门禁未通过 / needs_review / publish 关闭 | （`skipped`） | 保持原状态 | 0 |
| 非容器隔离 | `code_execution_not_isolated` | `failed` | 0 |
| 权限不足 / token 无效 | `github_permission_denied` | `needs_review` | 0 |
| 远端冲突（分支已存在、PR 已存在等） | `github_conflict` | `needs_review` | 0（不覆盖） |
| API 失败 / 限流 / 不可达 | `github_unavailable` / `github_rate_limited` | `needs_review` | 已回滚 |

### 凭据不泄漏（验证方式）

- `Secret` / `GitHubCredentials` 的 `repr`、`str` 一律返回 `***REDACTED***`，只有 `get_secret_value()` 可取原文。
- token 仅在构造请求头时读取，不进入任务记录、报告、结构化日志或沙箱环境。
- `tests/test_phase3_e2e.py::test_credentials_never_reach_logs_report_or_sandbox` 断言 token 不出现在日志、报告、`repair-state.json`，且不在 `build_sandbox_env()` 生成的环境里。

### 容器隔离（对外开放写的前置）

`REPOPILOT_SANDBOX_BACKEND=container` 时使用 `ContainerSandboxExecutor`：`docker run` 参数含 `--network=none`、`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`、非 root `--user` 与 `--memory` / `--cpus` / `--pids-limit` 上限；仓库以 `:ro` 只读挂载，仅 artifacts 与隔离 HOME 可写；容器环境来自白名单，绝不透传宿主凭据。

### GitHub 连通性与本地联调

- fake 全流程：不接任何网络即可跑通「建分支 → 提交 → Draft PR」（`tests/test_phase3_e2e.py`）。
- 真实联调：开启发布并注入凭据后创建 `repair` 任务（本地镜像仓库需用 `REPOPILOT_GITHUB_REPO_SLUG` 指定远端 `owner/name`）：

```powershell
$env:REPOPILOT_ENABLE_GITHUB_PUBLISH = "true"
$env:REPOPILOT_GITHUB_TOKEN = "<token>"           # 仅写入进程环境，绝不入库/日志
$env:REPOPILOT_GITHUB_REPO_SLUG = "acme/widget"   # 本地镜像仓库时指定远端 owner/name
```

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 1) 物化受控 fixture 仓库（生成真实 git 仓库，含两个 commit）
.\.venv\Scripts\python.exe scripts\make_fixtures.py --destination .fixtures-repos

# 2) 声明受信任来源，使 fixture 上允许执行仓库代码
$env:REPOPILOT_TRUSTED_SOURCE_ROOTS = (Resolve-Path .fixtures-repos).Path

# 3) 启动服务
.\.venv\Scripts\python.exe -m repopilot
```

```powershell
# 4) 创建评估任务并轮询
$body = @{ repo_url = (Resolve-Path .fixtures-repos\py_calc_buggy).Path } | ConvertTo-Json
$task = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/tasks -ContentType application/json -Body $body
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$($task.task_id)"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/tasks/$($task.task_id)/report"
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/tasks` | 创建评估任务，**立即返回** `task_id`，后台异步执行（202） |
| `GET` | `/api/tasks/{task_id}` | 查询任务状态、固定 commit、失败原因与问题摘要 |
| `GET` | `/api/tasks/{task_id}/report` | 获取统一评估报告；未到达终态时返回 409 |
| `GET` | `/api/tasks/{task_id}/logs` | 获取结构化 JSONL 日志（支持 `?tail=N`） |
| `POST` | `/api/tasks/{task_id}/cancel` | 请求协作式取消（202）；任务已是终态时返回 409 |
| `POST` | `/api/tasks/{task_id}/pull-request` | 为门禁通过的修复创建/复用 Draft PR；任务未到终态返回 409 |
| `GET` | `/api/health` | 健康检查（非任务端点） |

请求体：

```json
{ "repo_url": "https://github.com/org/repo.git", "base_branch": "main", "base_commit": null }
```
`mode` 可取 `assess`（默认，阶段一评估）或 `repair`（阶段二自动修复）。`repair` 任务必须提供 `problem`；可选 `target_tests`（缺省则以基线失败的测试为目标）、`allowed_paths`（额外允许改动的路径）与 `resume_task_id`（基于已保存状态续跑）。

`base_commit` 优先；未提供时解析 `base_branch` 的 HEAD，未提供 `base_branch` 时使用远端默认分支 HEAD。
解析结果（`resolved_commit`）会写回任务记录，并在报告中出现，保证结果可复现。

重复提交相同 `repo_url` + `base_branch` + `base_commit` 的活跃任务时返回既有 `task_id`（`deduplicated: true`）。

## 检查器与选型

| 检查器 | 工具 | 产出问题类型 | 验证级别 |
|---|---|---|---|
| 静态检查 | Ruff（`ruff check --output-format json`） | `lint` | 工具已确认 |
| 测试执行 | pytest（JUnit XML 解析） | `test_failure` / `test_error` | 工具已确认 |
| 依赖风险 | pip-audit（JSON） | `dependency_vulnerability` | 工具已确认 |
| 启发式提示 | 内置静态文本扫描 | `dependency_pinning` / `weak_verification` / `error_swallowing` / `verification_gap` | **未验证的潜在问题** |

**依赖扫描选型理由**：优先 `pip-audit`——由 PyPA 维护，直接查询公开的 OSV / PyPI 漏洞库而**无需账号或令牌**（因此不会有凭据进入沙箱），输出机器可读 JSON，并且可以直接审计 requirements 文件而不改动环境。对比方案：`safety` 完整结果需要付费/认证的漏洞库 API；`osv-scanner` 需要额外安装 Go 二进制；`pip list --outdated` 根本不是漏洞库。

未受信任来源的依赖审计会追加 `--no-deps`，避免为解析传递依赖而下载并构建第三方 sdist。

## 任务状态与判定

| 状态 | 含义 |
|---|---|
| `pending` / `running` | 排队中 / 执行中 |
| `succeeded` | 全部必需检查器均已运行，报告可信 |
| `needs_review` | 存在必需检查器未能运行（例如非受信来源禁用代码执行），结论不完整，**不等同成功** |
| `failed` | 克隆、解析 commit 或检查器本身失败，`failure_reason` 给出明确原因 |
| `cancelled` | 收到取消请求 |

判定规则（`src/repopilot/pipeline/evaluate.py`）：任一步骤抛错 → `failed`；任一检查器状态为 `error` → `failed`；存在「必需但未运行」的检查器 → `needs_review`；否则 `succeeded`。取消请求优先于上述所有判定。

报告同时给出两类问题列表，二者不混淆：

- `confirmed_issues`：工具直接确认的问题（origin 通常为 `tool`）。
- `unverified_issues`：未验证的潜在问题，必须人工确认，不计入确认问题汇总。

## 模型接入与 vLLM

- provider 由 `REPOPILOT_MODEL_PROVIDER` 选择：`stub`（默认，离线确定性）或 `openai` / `vllm`（OpenAI 兼容端点）。
- 接入 vLLM 只需改配置，无需改代码：

```powershell
$env:REPOPILOT_MODEL_PROVIDER = "openai"
$env:REPOPILOT_MODEL_BASE_URL = "http://127.0.0.1:8000/v1"
$env:REPOPILOT_MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
$env:REPOPILOT_MODEL_API_KEY = "EMPTY"   # vLLM 默认不校验
```

- 连通性验证：

```powershell
.\.venv\Scripts\python.exe -m repopilot.llm.health
# 退出码：0 可达、2 不可达、3 provider 配置错误；只打印端点元数据，绝不打印 API key
```

- 模型超时、请求失败、输出截断、服务不可用都会显式失败（`model_unavailable` / `model_output_invalid`），不会降级为「修复成功」；token 用量、请求延迟与任务级推理成本记入任务 `model_usage` 与报告 `repair.usage`。

## 安全约束（硬性）

- **禁止 shell**：所有子进程使用参数列表调用，代码中不存在 `shell=True`。
- **超时与输出上限**：每个子进程都有超时（含硬上限 `REPOPILOT_MAX_COMMAND_TIMEOUT_SECONDS`）与输出字节上限，超时通过 `taskkill /T`（Windows）或进程组 `SIGKILL`（POSIX）终止整个进程树。
- **环境变量白名单**：子进程只继承 `REPOPILOT_ENV_ALLOWLIST` 中的变量并注入确定性变量；`GITHUB_TOKEN`、`AWS_SECRET_ACCESS_KEY` 等凭据不会传入。`GIT_TERMINAL_PROMPT=0` 与空 gitconfig 避免交互式提示与宿主凭据助手。
- **凭据绝不落盘**：日志写入前递归脱敏（敏感键名 + token 形态正则）；报告不包含环境变量。
- **不可信代码默认不执行**：只有 `repo_url` 位于 `REPOPILOT_TRUSTED_SOURCE_ROOTS` 之下（或显式开启 `REPOPILOT_ALLOW_UNTRUSTED_CODE_EXECUTION`）才允许执行仓库代码。第三方仓库的 pytest 会被拒绝执行，任务落到 `needs_review`。
- 执行仓库代码时，`HOME`/`TEMP` 被重定向到任务工作区内部。

## 配置

全部通过 `REPOPILOT_*` 环境变量覆盖（默认值见 `src/repopilot/config.py`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPOPILOT_WORKSPACE_ROOT` | 系统临时目录下的 `repopilot/workspaces` | 任务工作区根目录（刻意放在控制仓库之外） |
| `REPOPILOT_LOG_DIR` | 系统临时目录下的 `repopilot/logs` | JSONL 日志与报告副本 |
| `REPOPILOT_TRUSTED_SOURCE_ROOTS` | 空 | 受控 fixture 目录列表（`os.pathsep` 分隔） |
| `REPOPILOT_ALLOW_UNTRUSTED_CODE_EXECUTION` | `false` | 是否允许对非受信来源执行仓库代码 |
| `REPOPILOT_COMMAND_TIMEOUT_SECONDS` | `300` | 单条命令超时 |
| `REPOPILOT_MAX_COMMAND_TIMEOUT_SECONDS` | `900` | 单条命令超时硬上限 |
| `REPOPILOT_CLONE_TIMEOUT_SECONDS` | `180` | 克隆超时 |
| `REPOPILOT_DEPENDENCY_AUDIT_TIMEOUT_SECONDS` | `180` | 依赖审计超时 |
| `REPOPILOT_MAX_OUTPUT_BYTES` | `2000000` | 单流输出上限 |
| `REPOPILOT_ENABLE_RUFF` / `_PYTEST` / `_DEPENDENCY_AUDIT` / `_HEURISTICS` | `true` | 检查器开关 |
| `REPOPILOT_REQUIRE_PYTEST_FOR_DETECTED_TESTS` | `true` | 检测到测试时，pytest 未运行即视为未验证 |
| `REPOPILOT_REQUIRE_DEPENDENCY_AUDIT` | `false` | 依赖审计不可用时是否降级为 `needs_review` |
| `REPOPILOT_KEEP_WORKSPACE` | `true` | 任务结束后是否保留工作区 |
| `REPOPILOT_SCHEDULER_MAX_WORKERS` | `4` | 后台线程数 |
| `REPOPILOT_MODEL_PROVIDER` | `stub` | 模型 provider：`stub` / `openai` / `vllm` |
| `REPOPILOT_MODEL_BASE_URL` | `http://127.0.0.1:8000/v1` | OpenAI 兼容端点 |
| `REPOPILOT_MODEL_NAME` | `local-model` | 模型名 |
| `REPOPILOT_MODEL_API_KEY_ENV` | `REPOPILOT_MODEL_API_KEY` | 存放 API key 的环境变量名（值不落盘） |
| `REPOPILOT_MODEL_TIMEOUT_SECONDS` | `60` | 单次模型请求超时 |
| `REPOPILOT_MODEL_MAX_TOKENS` | `1024` | 单次生成上限 |
| `REPOPILOT_MODEL_MAX_RESPONSE_CHARS` | `20000` | 响应长度上限，超出视为截断 |
| `REPOPILOT_AGENT_MAX_ROUNDS` | `6` | 最大修复轮数 |
| `REPOPILOT_AGENT_MAX_WALL_SECONDS` | `900` | 修复总时长上限 |
| `REPOPILOT_AGENT_MAX_MODEL_CALLS` | `12` | 模型调用次数上限 |
| `REPOPILOT_AGENT_MAX_MODEL_TOKENS` | `60000` | 模型 token 预算 |
| `REPOPILOT_AGENT_MAX_TOOL_CALLS` | `40` | 工具调用次数上限 |
| `REPOPILOT_AGENT_MAX_TOOL_OUTPUT_BYTES` | `200000` | 单次工具输出上限 |
| `REPOPILOT_AGENT_MAX_DIFF_LINES` | `400` | 补丁行数上限 |
| `REPOPILOT_AGENT_MAX_CHANGED_FILES` | `10` | 补丁文件数上限 |
| `REPOPILOT_ENABLE_GITHUB_PUBLISH` | `false` | 是否允许创建修复分支与 Draft PR（默认关闭） |
| `REPOPILOT_GITHUB_TOKEN_ENV` | `REPOPILOT_GITHUB_TOKEN` | 存放 GitHub token 的环境变量名（值不落盘） |
| `REPOPILOT_GITHUB_API_BASE_URL` | `https://api.github.com` | GitHub API 基址（可指向 GHES） |
| `REPOPILOT_GITHUB_API_TIMEOUT_SECONDS` | `30` | 单次 GitHub API 超时 |
| `REPOPILOT_GITHUB_MAX_RESPONSE_BYTES` | `1000000` | GitHub 响应大小上限 |
| `REPOPILOT_GITHUB_REPO_SLUG` | 空 | 本地镜像仓库时指定远端 `owner/name` |
| `REPOPILOT_GITHUB_BASE_BRANCH` | `main` | 创建 PR 的目标基分支（优先采用仓库默认分支） |
| `REPOPILOT_GITHUB_BRANCH_PREFIX` | `repopilot/fix-` | 修复分支名前缀 |
| `REPOPILOT_GITHUB_MAX_CHANGED_FILES` | `50` | 单次发布改动文件数上限 |
| `REPOPILOT_GITHUB_MAX_COMMIT_BYTES` | `2000000` | 单次发布改动总字节上限 |
| `REPOPILOT_SANDBOX_BACKEND` | `local` | 沙箱后端：`local` / `container` |
| `REPOPILOT_PUBLISH_REQUIRES_CONTAINER` | `true` | 发布前是否必须容器隔离 |
| `REPOPILOT_DOCKER_IMAGE` | `python:3.11-slim` | 容器镜像 |
| `REPOPILOT_CONTAINER_MEMORY` / `_CPUS` / `_PIDS_LIMIT` | `1g` / `1.0` / `256` | 容器资源上限 |
| `REPOPILOT_CONTAINER_TIMEOUT_SECONDS` | `600` | 容器单条命令超时 |

## Fixtures 与测试

`fixtures/` 下提供三个受控仓库模板，`scripts/make_fixtures.py` 会将其物化为真实 git 仓库（含基线 commit 与后续 commit，用于验证 commit 固定）：

| Fixture | 已知可检出问题 |
|---|---|
| `py_calc_buggy` | Ruff `F401`/`F821`/`F841`/`E711`/`B006`；1 个失败测试；1 个未固定版本依赖 |
| `py_lib_clean` | 无 Ruff 问题、测试全绿、含 1 个被跳过的测试（启发式未验证提示） |
| `py_stats_repair` | `mean` 存在 off-by-one 真实缺陷，`tests/test_stats.py::test_mean_of_values` 基线失败；用于阶段二修复闭环 |

```powershell
.\.venv\Scripts\python.exe -m pytest              # 全部单元测试 + 端到端测试
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

端到端测试覆盖：`POST /api/tasks` → 轮询至 `succeeded` → `/report` 返回合规问题列表；不可达远端优雅失败（`failed` + `failure_reason`）；非受信来源落到 `needs_review`；取消运行中的任务落到 `cancelled`。

阶段二在 `tests/test_repair_e2e.py` 增加确定性端到端测试（用 stub 模型）：给定 fixture 仓库与明确问题，完成定位 → 补丁 → 测试 → 判定，产出补丁、质量门禁结论与完整执行记录；并覆盖超限终止、作弊补丁拦截、模型不可用/输出不可解析、未受信来源不执行、以及基于已保存状态的续跑。

阶段三在 `tests/test_publish.py`、`tests/test_container_sandbox.py`、`tests/test_phase3_e2e.py` 增加：伪造 GitHub API 的端到端测试（门禁通过 → 建分支 → 提交 → Draft PR → 任务记录含 PR 链接）；门禁拦截时断言零远端写；权限不足 / 冲突 / API 失败 / 重复提交的状态与错误码；容器 argv 加固断言；以及凭据不进入日志、报告、状态文件与沙箱环境。

## 本迭代明确不做

阶段二明确不做：任何 GitHub 写操作（分支 / 提交 / Draft PR）、Redis / PostgreSQL / Dify、Milvus / 向量检索、Agentic RL，以及「自主判断并修复任意仓库的任意问题」。代码执行路径仍默认关闭，仅在受控 fixture 上启用。

阶段三明确不做：自动合并、推送到默认分支、Redis / PostgreSQL / Dify、Milvus / 向量检索、Agentic RL。首版仅 Draft PR，远端写操作只能由后端 PR 模块执行，且默认要求容器隔离。

## 目录结构

```
RepoPilot.md                  # 项目设计文档（唯一事实来源）
README.md
pyproject.toml                # 依赖、ruff 与 pytest 配置
docs/open-questions.md        # 设计文档未定义处的决策记录
fixtures/                     # 受控 fixture 仓库模板
scripts/make_fixtures.py      # 物化 fixture 为真实 git 仓库
src/repopilot/agent/          # LangGraph 修复工作流、受控工具集、质量门禁
src/repopilot/llm/            # 模型抽象（stub / OpenAI 兼容 vLLM 客户端）
src/repopilot/                # 后端服务与评估流水线
tests/                        # 单元测试与端到端回归测试
```

## 参与约定

- 默认分支 `main`，改动通过分支 + PR 提交，PR 描述需说明改动范围与验证方式。
- 提交信息使用 Conventional Commits（如 `feat:`、`fix:`、`test:`、`docs:`、`chore:`）。
- 合并前需通过 lint、格式化与测试（详见设计文档 §5 工程验收标准）。
