# RepoPilot

基于 LLM 与 Agentic RL 的 GitHub 仓库评估及自动修复智能体。

用户提供 GitHub 仓库地址、Issue、CI 失败记录或仓库评估任务后，系统自动完成仓库分析、问题定位、代码修改、测试验证和修复报告生成，并在满足质量门禁后创建独立分支和 Draft PR（不自动合并）。

> 项目唯一事实来源（source of truth）：[`RepoPilot.md`](RepoPilot.md)。本 README 仅为导航与现状摘要，二者冲突时以 `RepoPilot.md` 为准。

## 状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段一 | 仓库接入与评估基线 | 待开发（当前迭代） |
| 阶段二 | Agent 自动修复闭环 | 未开始 |
| 阶段三 | GitHub 自动提交与 Draft PR | 未开始 |
| 阶段四 | 异步任务管理与交互整合 | 未开始 |
| 阶段五 | 代码检索增强与评测工具链 | 未开始 |
| 阶段六 | Agentic RL 能力扩展 | 未开始 |

当前仓库仅包含设计文档，尚无源码。

## 技术栈

Python / FastAPI（后端服务）· LangGraph（Agent 编排）· vLLM（模型推理）· Dify（交互入口）· Milvus（向量检索）· Docker（代码执行沙箱）· Ruff / pytest（代码质量）· GitHub App + REST API（仓库集成）· PostgreSQL / Redis（任务与数据）· Agent Lightning（强化学习）

## 迭代推进方式

每个阶段都产出**可独立运行**的交付物，按依赖关系顺序推进，核心原则：

先完成仓库检查，再实现 Agent 修复；先跑通本地验证，再接入 GitHub 写操作；先打通基础任务管理，再引入 Dify 和 Milvus；先保证执行轨迹完整，再开发 Agentic RL。

## 目录结构（规划）

```
RepoPilot.md                     # 项目设计文档（唯一事实来源）
README.md
prompts/                         # 面向 coding agent 的分阶段实施提示词
  phase-01-repository-intake-and-evaluation-baseline.md
docs/                            # 架构说明、接口契约、开发与部署文档
tests/                           # 单元测试与端到端回归测试
```

## 当前迭代

- 迭代目标：阶段一「仓库接入与评估基线」
- 执行提示词：[`prompts/phase-01-repository-intake-and-evaluation-baseline.md`](prompts/phase-01-repository-intake-and-evaluation-baseline.md)

## 参与约定

- 默认分支 `main`，改动通过分支 + PR 提交，PR 描述需说明改动范围与验证方式。
- 提交信息使用 Conventional Commits（如 `feat:`、`fix:`、`test:`、`docs:`、`chore:`）。
- 合并前需通过 lint、格式化与测试（详见各阶段提示词中的 Definition of Done）。