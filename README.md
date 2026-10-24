# systemone-bridge

## English

**systemone-bridge** is a local **pure protocol converter**: it translates
OpenAI-compatible `/v1/chat/completions` requests (northbound) into
Intern-Decision "SystemOne" `/v1/decisions` structured decision calls
(southbound). It does **no fallback** — when a request cannot be converted
(no template anchor, parse failure, upstream failure), the bridge returns an
explicit OpenAI-style error code and the **caller owns the fallback policy**.

- **No model-name routing** — every request is matched against the enabled
  task parsers regardless of `model`; an unknown template is answered with
  `422 unknown_template`.
- **Built-in task parsers (v1)** — MaiBot expression selection (`expression_selector`)
  and DiceFrame check planning (`df_check_planner`), producing probability-backed
  answers or OpenAI `tool_calls`.
- **WebUI console** — served at the bridge root: config editing with hot reload,
  a real minimal decisions probe, and live decision stats. Local-only by design.
- **License**: AGPL-3.0-or-later.

Quick start (Python 3.12, managed with [uv](https://docs.astral.sh/uv/)):

```powershell
.\start.ps1    # auto uv sync on first run; config.toml auto-generated from the example template
```

```bash
# equivalent on Linux/macOS
uv sync
uv run systemone-bridge --config config.toml
```

Full documentation is in Chinese below; the field-level protocol spec (mined from the
official Intern-Decision sources) lives in
[specs/systemone-protocol.md](specs/systemone-protocol.md).

---

本地**纯协议转换器**：把 **OpenAI 兼容的 `/v1/chat/completions`**（北向）翻译成
**Intern-Decision「SystemOne」`/v1/decisions` 结构化决策协议**（南向）。
不做任何回退——任何一环失败都直接返回明确的 OpenAI 风格错误码，
**回退策略由调用方自行负责**。

协议规格的字段级定义见 [specs/systemone-protocol.md](specs/systemone-protocol.md)
（挖自 Intern-Decision 官方仓库源码 / HF 模型卡，已确认 vs 推测分级标注）。

## 它是什么

```
MaiBot 决策任务 ─┐
                 ├─ 北向 OpenAI /v1/chat/completions ──► systemone-bridge :18500
DiceFrame 检定 ──┘                                          │
                              模板锚点可识别且可解析？
                                   │ 是                          │ 否（unknown_template /
                                   ▼                             │    bad_template / 502 / 500）
                    翻译为 /v1/decisions（state+questions）        ▼
                                   │                        返回 OpenAI 风格错误体
                                   ▼                        （回退由调用方自行处理）
                    概率分布 → 合成 OpenAI 回包 / tool_calls
```

- **纯转换，无回退**：模型名不参与路由（任意模型名一律尝试解析器）；解析失败、
  SystemOne 失败等都返回明确错误码，绝不代理给其他 LLM。
- **usage 透传**：上游 SystemOne 的 `usage` 原样映射为 OpenAI `usage`
  （`input_tokens→prompt_tokens`、`output_tokens→completion_tokens`）。注意上游
  `output_tokens` 计的是打分字段数而非生成 token（见 `specs/systemone-protocol.md` §4）；
  上游未回报 usage 时响应同样不带 usage 字段。
- **任务解析器**（v1 五个）：
  - `expression_selector`：MaiBot 表达方式选择。识别 system 消息中的「候选表达方式：」清单
    （`id: 情景=… | 风格=…`），候选 → 单题 choice（实测候选 5–50 条，≤62 选项硬限制），
    聊天上下文 → state；SystemOne 概率分布取 top-K（默认 ≤5、p ≥ 0.05）回填
    `{"selected_ids":[…]}`，全低则空数组——与原模板语义一致。
  - `df_check_planner`：DiceFrame 回合检定规划。识别 `dice_checks` 工具 + 检定规划器模板，
    user 消息（planner 上下文 JSON）→ 逐玩家 noul/choice/score 题（d20 每人 4 题、d100 每人
    3 题，≤16 题预算内）；答案合成合法的 `dice_checks` tool_calls
    （d20 带 target=期望 DC 并夹到 max_check_dc，d100 技能优先映射 skill/attribute）。
  - `expression_check`：MaiBot 表达方式入库前审核（expression.check）。识别
    「请评估以下表达方式或语言风格」模板，situation/style 字段行 → 结构化 state +
    单题 noul；P(适合) ≥ 0.5 回填 `{"suitable":bool,"reason":"…"}`。
  - `jargon_compare`：MaiBot 黑话推断比对（jargon.inference 第三相）。识别
    「请比较这两个推断结果」模板，两段推断原文（可解析时转对象）→ state + 单题 noul；
    P(相似) ≥ 0.5 回填 `{"is_similar":bool,"reason":"…"}`。
  - `emoji_replace`：MaiBot 表情包替换决策。识别「需要决定是否取消注册一个旧表情包」模板，
    候选条目块（编号/描述/使用次数/添加时间）+ keep → 单题 choice（≤21 选项）；按概率
    argmax 合成调用方期望的自由文本 `取消注册编号X` / `不取消注册`（emoji_manager 正则直接可解析）。

## 错误码表

所有错误体统一 OpenAI 风格：`{"error": {"message", "type", "code"}}`。

| HTTP | code | 触发条件 | 说明 |
|---|---|---|---|
| 400 | `invalid_request` | 请求体非 JSON / 非 JSON 对象 / messages 缺失 | 北向请求形状不合法 |
| 400 | `streaming_not_supported` | `stream=true` | 桥只做非流式转换 |
| 422 | `unknown_template` | 无解析器锚点（模型名不参与路由） | 调用方应换用常规 LLM 上游 |
| 422 | `bad_template` | 锚点命中但解析失败（候选超限、上下文非 JSON 等） | 调用方应换用常规 LLM 上游 |
| 500 | `build_error` | 决策结果合成响应失败 | 桥内部错误 |
| 502 | `upstream_error` | SystemOne 不可达/超时/4xx/5xx/响应不合法 | 原四分类（unreachable/server_error/rejected/bad_response）并入 `message` |

**回退由调用方负责**：调用方收到 422/502 后自行决定走哪个常规 LLM 上游。
MaiBot 侧 expression_selector 的调用链路已内置 `except Exception` 优雅兜底，
无需桥再做代理。

## 部署

```powershell
cd systemone-bridge                     # 仓库根目录
.\start.ps1                            # 首次自动 uv sync；config.toml 缺失时自动从模板生成
# 手动等价：uv sync ; uv run systemone-bridge --config config.toml
```

- Python 3.12（`.python-version` 已钉），依赖 aiohttp + pydantic，uv 管理。
- 首次启动无 `config.toml` 时自动从 `config.toml.example` 生成（保留注释），可直接开箱，
  密钥推荐到 WebUI 填写；也可手动 `copy config.toml.example config.toml`。
- 默认监听 `127.0.0.1:18500`（8000/8001/7999/18000/18660 已被占用，18500 空闲）。
- 日志：`data/logs/bridge.log`（滚动 8MB×5），每次请求记录路由决策与错误码。
- `GET /health`：只报桥自身（version / uptime / 已启用解析器清单），**不探测上游**；
  `GET /v1/models`：intern-decision-4b / 0.8b（仅展示，不参与路由）。

## 配置项（config.toml）

| 节 | 键 | 说明 |
|---|---|---|
| `[server]` | `host` / `port` | 监听地址，默认 127.0.0.1:18500 |
| `[upstream_systemone]` | `base_url` | SystemOne 服务根（桥在其下拼 `decisions_path`） |
| | `decisions_path` | 决策端点路径：官方开源 serving `/v1/decisions`（默认），OpenXLab 托管网关 `/v1/systemone` |
| | `api_key` | 留空不发 Authorization；官方开源 serving 无鉴权，托管网关填 Bearer key |
| | `model` | 注入请求体顶层 model 字段（OpenXLab 网关校验必需，如 `intern-decision-4b`）；留空不注入保持官方兼容 |
| | `timeout_seconds` | 单次决策超时，默认 10 |
| `[task_parsers]` | `expression_selector` / `df_check_planner` / `expression_check` / `jargon_compare` / `emoji_replace` | 解析器开关，关了对应任务返回 422 unknown_template |
| `[translation]` | `expression_min_probability` / `expression_max_selected` | 概率→selected_ids 的入选阈值与条数上限 |
| `[logging]` | `level` / `dir` | 日志级别与目录 |

真实 `config.toml` 不入库（.gitignore），只入库 `config.toml.example`。

## WebUI 控制台

浏览器访问 **`http://127.0.0.1:18500/`**（即桥自身地址），单页原生 JS 实现，无外部依赖。

| 卡片 | 字段 / 内容 |
|---|---|
| SystemOne 上游 | `base_url` / `api_key`（留空不发 Authorization）/ `decisions_path` / `model` / 决策超时；「测试决策」按钮发一次 2 选项 choice 的最小真实 decisions 探测（消耗可忽略），显示「决策往返 Xms，选择=xx」或具体错误 |
| 解析器 | exprsel / DF 检定 / 表达审核 / 黑话比对 / 表情包替换开关（关了返回 422 unknown_template）；exprsel 最低入选概率与最多入选条数 |
| 服务与日志 | host / port / 日志级别 / 日志目录（改 host、port、日志目录需重启；其余保存即生效） |
| 运行状态 | 总请求数、决策成功（按解析器分）、决策失败（按错误码分）、最近错误（每 5 秒自动刷新） |

**保存与热生效**：「保存配置」先 pydantic 校验（失败不落盘并返回瘦身错误），再原子写盘
（临时文件 + replace），最后热应用到运行中的桥——上游地址 / 密钥 / 模型 / 超时、解析器开关、
翻译参数、日志级别对**后续请求立即生效，无需重启**。

**密钥安全**：
- `GET /api/config` 与保存回包中的 `api_key` 一律脱敏为「前 4 字符 + `***`」；
- 提交仍为掩码占位（`***` 结尾）时后端自动保留旧密钥，不会把掩码写进配置；
- 校验失败不落盘，错误详情不回显密钥。

**安全提示**：WebUI 无登录鉴权，且能看到配置、改写密钥指向——**只在本机使用，勿把 18500
端口暴露到公网 / 局域网**（保持默认 `host = 127.0.0.1`；确需远程改配置请走 SSH 隧道）。
浏览器直接访问即可，配合 `host = 0.0.0.0` 会同时向所有人开放北向接口与控制台，默认禁止。

## MaiBot（MB）接入指引

1. WebUI → 模型配置，把「表达方式选择」所用的小模型绑到桥：
   - API Base：`http://127.0.0.1:18500/v1`
   - API Key：任意非空占位（桥不校验北向 key）
   - 模型名：`intern-decision-4b`（模型名不参与路由，保持原名即可）
2. 断句使用 `mode=rule`（规则断句）的请求**不经过桥**，只有表达方式选择
   （expression_selector）快照模板走桥翻译。
3. 桥收到无法识别的请求时返回 422/502 错误码；MaiBot 调用方（expression_selector 链路）
   已有优雅兜底，回退由其自行完成——桥不做任何代理。
4. 想让某类请求明确不走翻译路径：关掉对应 `task_parsers` 开关（该类请求即返回 422）。

### 五个可接管任务与混合槽位配置

桥已接管 5 类 MaiBot 决策型负载；每类的锚点、判定形状与回填格式：

| 解析器 | 锚点（模板唯一句） | 形状 | 回填输出 |
|---|---|---|---|
| `expression_selector` | 候选表达方式： | choice ≤62 | `{"selected_ids":[…]}` |
| `expression_check` | 请评估以下表达方式或语言风格 | noul | `{"suitable":bool,"reason":"…"}` |
| `jargon_compare` | 请比较这两个推断结果 | noul | `{"is_similar":bool,"reason":"…"}` |
| `emoji_replace` | 需要决定是否取消注册一个旧表情包 | choice ≤21 | 自由文本 `取消注册编号X` / `不取消注册` |
| `df_check_planner` | 回合检定规划器（+dice_checks 工具） | noul/choice/score | `dice_checks` tool_calls |

**判定型任务与生成型任务共用槽位时，用混合 model_list 依赖 422 切换语义**：桥对无锚点
请求返回 422 unknown_template，MaiBot（utils_model.py）把它当不可重试错误 → 自动切到
model_list 的下一个模型。因此推荐：

```toml
# model_config.toml —— learner 槽（1 判定 + 3 生成混合）：
# expression.check 命中桥解析器走决策；expression.learner / jargon.learner /
# jargon.inference 前两相（生成型）422 后自动落 GLM。
[llm.task_config.learner]
model_list = ["systemone-exprsel", "GLM-5.3-Flash"]

# emoji 槽（替换决策 + send_emoji 带图选择子代理混合）：
# 替换决策命中桥；带图选择子代理（25 选 1）无锚点 422 后落 GLM（visual=true 兜底）。
# 不可只配 [systemone-exprsel] 单模型——选择子代理会硬失败。
[llm.task_config.emoji]
model_list = ["systemone-exprsel", "GLM-5.3-Flash"]
```

其中 `systemone-exprsel` 是 MaiBot 侧指向桥的 Provider 条目名（桥识别模板锚点，
不看模型名）。回滚 = 把 `systemone-exprsel` 从 model_list 删掉即可，回退路径与现状等价。

## DiceFrame（DF）接入指引

1. 服务商/Provider 增加一条自定义 OpenAI 兼容条目：
   - Base URL：`http://127.0.0.1:18500/v1`
   - API Key：占位
   - 模型：`intern-decision-4b`
2. 在需要系统决策的会话把检定规划模型切到该条目。桥识别 `dice_checks` 工具调用请求并
   走结构化决策；识别失败返回 422，DF 侧安全网（safety net）继续生效。
3. 检定输出为 OpenAI `tool_calls`（`dice_checks`）。

## 已知限制（v1）

- SystemOne 单请求 ≤16 题：exprsel 候选 >62、DF 同轮玩家 >4（d20）/ >5（d100）时返回
  422 bad_template（不截断语义）；exprsel 多选是"top-K 概率阈值"近似，非逐项独立判定。
- DF 的 advantage/modifier v1 固定 normal/0；economy_actions / overreach /
  world_requirements / world_time_advance 不由桥生成（DF 侧独立容错解析，缺失不影响
  checks；安全网与 GM 裁量仍在）。
- DF 的 AI 队友（companions）委托检定不翻译，返回 422 unknown_template。
- 不支持图片翻译（北向图片消息不进 exprsel/DF 模板，无需支持）。
- 不支持流式（`stream=true` → 400 streaming_not_supported）。

## 测试

```powershell
uv run pytest tests/ -q     # 83 用例：真实快照解析 / DF 合成 / 三决策解析器（expression.check /
                            # jargon compare / emoji replace：命中 + 相邻模板不误识别）/ 
                            # 错误码（422×2/502/500/400）/ mock 端到端 / OpenXLab 网关形状
                            # （/v1/systemone + model 注入 + noul 最小答案归一化）/ WebUI
```

`tests/fixtures/` 含 4 份真实 MaiBot exprsel 快照、3 份由真实模板渲染的三决策任务命中
fixture 与 DF 检定规划模板副本；
`tests/mock_systemone.py` 按 P1 规格实现 mock（含官方校验语义与 422）。

## 协议适配说明

官方开源 serving 的决策端点是 `POST /v1/decisions`（默认），OpenXLab 托管网关则为
`POST /v1/systemone` 且要求请求体顶层 `model` 字段、choice 题必带 `instructions`、
noul 题必带键为 `true`/`false` 的 `criteria`（官方规格允许省略），且 noul 答案只回
`{"type":"noul","noul":P(yes)}`（南向客户端已归一化为官方完整包络）——
通过 `[upstream_systemone]` 的 `decisions_path` / `model` 配置项切换，字段级差异见
[specs/systemone-protocol.md](specs/systemone-protocol.md)。

## 许可证 / License

以 [AGPL-3.0-or-later](LICENSE) 发布。Copyright (C) 2026 systemone-bridge contributors。
This program is free software: you can redistribute it and/or modify it under the terms
of the GNU Affero General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.
