# systemone-bridge

本地协议桥：把 **OpenAI 兼容的 `/v1/chat/completions`**（北向）翻译成
**Intern-Decision「SystemOne」`/v1/decisions` 结构化决策协议**（南向），
并在任何一环失败时**原样直通回退**到常规 LLM 上游（默认 GLM）——
任何情况下不比"不走桥"更差。

协议规格的字段级定义见 [specs/systemone-protocol.md](specs/systemone-protocol.md)
（挖自 Intern-Decision 官方仓库源码 / HF 模型卡，已确认 vs 推测分级标注）。

## 它是什么

```
MaiBot 表达选择 ─┐
                 ├─ 北向 OpenAI /v1/chat/completions ──► systemone-bridge :18500
DiceFrame 检定 ──┘                                          │
                              模型名 intern-decision-* 且模板可解析？
                                   │ 是                        │ 否 / 解析失败 / SystemOne 不可达
                                   ▼                           ▼
                    翻译为 /v1/decisions（state+questions）      原样直通 fallback 上游
                                   │                           （GLM，等价于现状）
                                   ▼
                    概率分布 → 合成 OpenAI 回包 / tool_calls
```

- **模型名路由**：`model` 以 `intern-decision` 开头 → 走解析翻译路径；其余一律直通。
- **任务解析器**（v1 两个）：
  - `expression_selector`：MaiBot 表达方式选择。识别 system 消息中的「候选表达方式：」清单
    （`id: 情景=… | 风格=…`），候选 → 单题 choice（实测候选 5–50 条，≤62 选项硬限制），
    聊天上下文 → state；SystemOne 概率分布取 top-K（默认 ≤5、p ≥ 0.05）回填
    `{"selected_ids":[…]}`，全低则空数组——与原模板语义一致。
  - `df_check_planner`：DiceFrame 回合检定规划。识别 `dice_checks` 工具 + 检定规划器模板，
    user 消息（planner 上下文 JSON）→ 逐玩家 noul/choice/score 题（d20 每人 4 题、d100 每人
    3 题，≤16 题预算内，超限回退）；答案合成合法的 `dice_checks` tool_calls
    （d20 带 target=期望 DC 并夹到 max_check_dc，d100 技能优先映射 skill/attribute）。
- **回退铁律**：解析失败 / SystemOne 不可达 / 超时 / 4xx/5xx → 北向请求**原样**直通
  fallback 上游（`model` 指向 intern-decision-* 时改写为 fallback.model），流式请求按 SSE
  逐块透传。fallback 自身失败才向客户端返回 502。

## 部署

```powershell
cd D:\项目\systemone-bridge
copy config.toml.example config.toml   # 填 fallback.api_key；SystemOne 端点可先不配
.\start.ps1                            # 首次自动 uv sync；等价于 uv run systemone-bridge
# 手动等价：uv sync ; uv run systemone-bridge --config config.toml
```

- Python 3.12（`.python-version` 已钉），依赖 aiohttp + pydantic，uv 管理。
- 默认监听 `127.0.0.1:18500`（8000/8001/7999/18000/18660 已被占用，18500 空闲）。
- 日志：`data/logs/bridge.log`（滚动 8MB×5），每次请求记录路由决策与回退原因。
- `GET /health`：桥自身状态 + SystemOne 上游探测；`GET /v1/models`：intern-decision-4b / 0.8b。

## 配置项（config.toml）

| 节 | 键 | 说明 |
|---|---|---|
| `[server]` | `host` / `port` | 监听地址，默认 127.0.0.1:18500 |
| `[upstream_systemone]` | `base_url` | SystemOne 服务根（桥拼 `/v1/decisions`）；当前端点不存在也能跑 |
| | `api_key` | 留空不发 Authorization；官方开源 serving 无鉴权 |
| | `timeout_seconds` | 单次决策超时，默认 10 |
| `[fallback]` | `base_url` / `api_key` / `model` / `timeout_seconds` | 回退上游（OpenAI 兼容），填 GLM 的 |
| `[task_parsers]` | `expression_selector` / `df_check_planner` | 解析器开关，关了就直通 |
| `[translation]` | `expression_min_probability` / `expression_max_selected` | 概率→selected_ids 的入选阈值与条数上限 |
| `[logging]` | `level` / `dir` | 日志级别与目录 |

真实 `config.toml` 不入库（.gitignore），只入库 `config.toml.example`。

## MaiBot（MB）接入指引

1. WebUI → 模型配置，把「表达方式选择」所用的小模型绑到桥：
   - API Base：`http://127.0.0.1:18500/v1`
   - API Key：任意非空占位（桥不校验北向 key）
   - 模型名：`intern-decision-4b`（路由进翻译路径）
2. 现阶段 SystemOne 端点未就绪：请求会被解析 → SystemOne 失败 → **自动直通回退 GLM**，
   行为与不接桥等价，可放心提前绑定。
3. 想让某类请求明确不走翻译路径：换回原模型名（非 intern-decision-*）或关掉对应
   `task_parsers` 开关。

## DiceFrame（DF）接入指引

1. 服务商/Provider 增加一条自定义 OpenAI 兼容条目：
   - Base URL：`http://127.0.0.1:18500/v1`
   - API Key：占位
   - 模型：`intern-decision-4b`
2. 在需要系统决策的会话把检定规划模型切到该条目。桥识别 `dice_checks` 工具调用请求并
   走结构化决策；识别失败原样直通，不影响现有 GLM 流程。
3. 检定输出为 OpenAI `tool_calls`（`dice_checks`），DF 侧安全网（safety net）继续生效。

## 回退行为与已知限制

**回退行为**（计日志，`via=passthrough.*` 前缀可 grep）：
- 无解析器识别 / 解析失败 / 响应合成失败 → 直通（模型名指向 intern-decision-* 时改写）。
- SystemOne 连接失败 / 超时 / 5xx / 4xx → 直通。
- 流式回退按 SSE 逐块透传；翻译路径的 stream=true 请求合成两块 SSE（delta 语义）+ `[DONE]`。

**已知限制（v1）**：
- SystemOne 单请求 ≤16 题：exprsel 候选 >62、DF 同轮玩家 >4（d20）/ >5（d100）时直接回退
  （不截断语义）；exprsel 多选是"top-K 概率阈值"近似，非逐项独立判定。
- DF 的 advantage/modifier v1 固定 normal/0；economy_actions / overreach /
  world_requirements / world_time_advance 不由桥生成（DF 侧独立容错解析，缺失不影响
  checks；安全网与 GM 裁量仍在）。
- DF 的 AI 队友（companions）委托检定不翻译，走回退。
- 不支持图片翻译（北向图片消息不进 exprsel/DF 模板，无需支持）。

## 测试

```powershell
uv run pytest tests/ -q     # 39 用例：真实快照解析 / DF 合成 / 各类回退 / mock 端到端
```

`tests/fixtures/` 含 4 份真实 MaiBot exprsel 快照与 DF 检定规划模板副本；
`tests/mock_systemone.py` 按 P1 规格实现 mock（含官方校验语义与 422）。
