# SystemOne 协议规格（Intern-Decision /v1/decisions）

> 本文档是 systemone-bridge 南向客户端的实现依据。
> 分级标注：【已确认】= 直接来自官方源码/模型卡原文，可逐字溯源；【推测】= 基于代码结构的合理推断，待真实端点联测验证。
> 溯源基准：官方仓库 https://github.com/internlm/Intern-Decision （浅克隆于编写当日）、
> 模型卡 https://huggingface.co/internlm/Intern-Decision-4B 、技术报告 arXiv:2610.22131。
> 关键源文件（官方仓库内）：`src/service/app.py`（HTTP 层）、`src/inference/pipeline.py`（校验与响应装配）、
> `src/inputs/schema.py`（schema 编译）、`src/inference/engine.py`（打分与答案装配）、
> `src/inference/temperature.py`（校准）、`src/service/uploads.py`（图片上传）。

## 0. 总览

SystemOne（即 Intern-Decision 官方 serving）是**结构化候选打分服务**：一次前向传播对 schema 化的
一组问题（每题 ≤62 个候选项）给出**校准概率分布**，不做自由文本生成（不调用 `generate()`）。
官方 HTTP 服务是 FastAPI 应用（`src.service.app:app`，uvicorn 启动），主端点为 `POST /v1/decisions`
（别名 `POST /v1/jev`）。【已确认】

响应包络称 "Jev envelope"：`model` / `answers` / `usage`，外加 `request_id`、`timing`、`thinking` 与
扩展字段 `backend`、`calibration`。【已确认】

## 1. 端点清单【已确认】

| 方法与路径 | 用途 |
|---|---|
| `POST /v1/decisions`（别名 `/v1/jev`） | 主推理端点，JSON 进 JSON 出，无流式 |
| `POST /v1/decisions/{request_id}/thinking` | 对低置信字段发起思考模型复核，SSE 流式 |
| `GET /health` | `{"status": "ok"\|"unavailable", "model_loaded": bool}` |
| `GET /demo-meta` | 思考模型可用性、上传限制等演示元数据 |
| `GET /examples` | 合成示例请求 |
| `GET /`、`GET /demo.js` | 浏览器演示页静态资源 |

## 2. 鉴权

- 【已确认】官方开源 serving 代码**没有任何鉴权**：`app.py` 无 API Key 校验、无 auth 中间件，
  请求体里也没有可放凭证的标准字段。
- 【推测】若未来部署在 OpenXLab 等托管平台，预期会在网关层加 Bearer Token（`Authorization: Bearer <key>`），
  与 OpenAI 生态惯例一致。桥实现按「有 api_key 配置就发 `Authorization` 头，为空则不发」处理，
  两种情况都兼容。

## 3. 请求体（POST /v1/decisions）

顶层为单个 JSON 对象。HTTP 层用 FastAPI 裸 `dict` 接收（**未知顶层键被静默忽略**），语义校验在
pipeline 层完成。【已确认】

```json
{
  "state": "…任意 JSON 值（字符串或对象均可）…",
  "questions": {
    "<field名>": { "type": "choice", "instructions": "…", "criteria": {"选项值": "描述", …} },
    "<field名>": { "type": "score",  "instructions": "…", "criteria": ["档位描述", …] },
    "<field名>": { "type": "noul",   "instructions": "…", "criteria": {"yes": "…", "no": "…"} }
  },
  "images": [{"data": "<base64 或 data URL>", "type": "image/png"}, …],
  "thinking": {"enabled": true, "confidence_threshold": 0.6}
}
```

### 3.1 顶层字段约束【已确认】

| 字段 | 类型 | 约束 | 违反后果 |
|---|---|---|---|
| `state` | 任意 JSON | 必填（仅检查存在性）；渲染为 `json.dumps(indent=2)` 进 prompt；**不得包含保留标记 `<decision>`** | 422 |
| `questions` | object | 必填，1–16 个条目；键为非空字符串；每值须为 object 且 `type ∈ {choice, score, noul}` | 422 |
| `images` | list | 可省；经 HTTP 上传接口时 1–8 张，单张 ≤12MB、合计 ≤32MB、合计像素 ≤16M，格式 JPEG/PNG/WebP/静态 GIF；元素必须是 `{"data": base64}`，**不接受本地路径或远程 URL** | 422 |
| `thinking` | object | 可省；仅允许 `enabled`(bool) 与 `confidence_threshold`(有限数, [0,1]) | 422 |
| 其他键 | — | 被忽略（含模型卡提到的 `model` 字段——**不切换权重**） | — |

### 3.2 各题型 criteria 语义【已确认】

- **choice**：`criteria` 必须是有序 object：`{选项值: 描述}`。选项值回填进答案。
- **score**：`criteria` 为 list（选项键自动取 `"0","1",…`，描述为元素）**或** object（键必须是可解析为
  有限数字的字符串）。注意：期望分 `answers.<f>.score` 是**按选项键的加权平均**——想得到真实分值
  （如 DC 10/15/20/25），必须用 `{"10": "简单", …}` 形式，用 list 会得到 0..N-1 的均值。
- **noul**：固定二元选项，**顺序固定 no → yes**。`criteria` 可省；给出时键 `yes/true/1`、`no/false/0`
  分别描述两支。答案 `answers.<f>.noul` = P(yes)。
- 选项数：每题 1–62 个（候选符号池 `A..Z a..z 0..9` 共 62 个单 token 符号，按序映射，题目与选项
  **顺序必须保留**）。【已确认】
- 总输入长度：超过推理引擎 `max_length`（默认 8192 token）整单拒绝，**不截断**。【已确认（模型卡）】
- prompt 编译（供理解语义，桥不需复刻）：system 固定为"careful decision assistant"模板；user 为
  `## State` + `## Decision schema`（每题一行说明 + 每选项 `A = 值: 描述`）；assistant 预填含每字段一个
  `<decision>` 占位符的 JSON 骨架；在占位符前一位读 logits 做候选内 softmax。【已确认】

### 3.3 thinking 请求【已确认】

`thinking.enabled=true` 且某字段最高概率 < `confidence_threshold` 时，该字段进入响应
`thinking.tasks`。对每个 task 可再发 `POST /v1/decisions/{request_id}/thinking`，体为
`{"field": <字段名>, "token": <task.token>}`（字段集合必须恰为这两个键），返回 SSE 流：
事件依次为 `start` →（思考模型的中间事件）→ `final`（`answer` 为最终答案，`source:"thinking"`）
或 `error`（`retained_initial: true`，保留初始答案）。会话有 TTL 与容量上限（满时 422）。
**桥 v1 不使用 thinking 扩展**（透传概率分布已够用）。

## 4. 响应体（200）

```json
{
  "model": "intern-decision",
  "request_id": "32 位 hex",
  "answers": {
    "<field>": {
      "type": "choice",
      "probabilities": {"选项值": 0.87, …},
      "confidence": 0.87,
      "decision": "选项值",
      "choice": "选项值",
      "source": "local"
    },
    "<score字段>": { "type": "score", "probabilities": {…}, "confidence": …, "decision": "15",
                     "score": 14.2, "legend": {"10": "简单", …}, "source": "local" },
    "<noul字段>":  { "type": "noul", "probabilities": {"no": 0.3, "yes": 0.7}, "confidence": 0.7,
                     "decision": "yes", "noul": 0.7, "source": "local" }
  },
  "usage": {"input_tokens": 1234, "output_tokens": 3, "decision_count": 3},
  "timing": {"queue_ms": 0.5, "server_ms": 45.1, "inference_ms": 44.0},
  "thinking": {"enabled": false, "confidence_threshold": 0.5, "tasks": []},
  "backend": "hf",
  "calibration": {"method": "temperature-scaling", "temperature": 1.99241824}
}
```

【已确认】要点：
- `decision` = `argmax(probabilities)`，平局取**字典序最小**选项（`min(probs, key=lambda l: (-p[l], l))`）。
- `probabilities` 已过校准（4B 默认 T=1.99241824，`softmax(log(p)/T)`），保持 argmax 不变；
  和恒为 1（±1e-3 容差）。
- `score` 为期望值，可落在两档之间；其 `confidence` 属于最可能档位。
- `usage.output_tokens` / `decision_count` 计的是**打分字段数**，不是生成 token。
- `model` 字段固定写 `"intern-decision"`（不区分规格型号）；请求里的 `model` 不影响权重选择。
- `backend`/`calibration` 是扩展字段：本地 HF 后端为 `"hf"`；校准关闭时可能缺省。【推测（缺省形态）】

## 5. 错误语义【已确认】

| 状态码 | 触发 | 体 |
|---|---|---|
| 422 | 校验失败（缺 state、questions 越界、题型/criteria 非法、保留标记、图片超限、thinking 参数非法、输入超 max_length、thinking 凭证未配置、会话容量满） | `{"detail": "<英文原因>"}` |
| 503 | 模型未就绪（启动中） | `{"detail": "Inference is not ready."}` |
| 404 | thinking 会话/字段/token 不存在或过期 | `{"detail": …}` |
| 409 | 同一 thinking 任务已在运行 | `{"detail": …}` |

主端点**无流式**；网络层错误（连接拒绝/超时）由客户端自行分类。桥的错误分类据此设：
连接失败 / 非粘超时 / 5xx（含 503）/ 4xx（请求被拒——视为翻译产物不合法，也要回退）。

## 6. 部署形态【已确认 + 推测】

- 官方唯一后端是 `backend="hf"`（本地 HF 前向，需 PyTorch/CUDA；0.8B/2B/4B 实测 P95 33–45ms @ RTX 4090）。
  【已确认（模型卡）】
- 无 Docker、无 vLLM 集成（仓库内未见）。启动方式为 uvicorn 拉起 `src.service.app:app`。【已确认（代码结构）】
- 【推测】未来第三方托管/本地 CPU 量化部署都仍会暴露同一 `/v1/decisions` 契约——桥只依赖 HTTP 契约，
  与部署形态解耦。
- 【环境事实】截至本 mission 当日，公开托管端点不存在（OpenXLab 未托管 Decision 系列）——
  桥 v1 以透传代理形态先行部署是设计预期。

## 7. 桥的翻译映射约定（本项目设计决策，非官方协议）

北向 OpenAI `/v1/chat/completions` → 南向 `/v1/decisions` 的映射由各任务解析器产出（见
`src/systemone_bridge/parsers/`），共同约束：

1. 解析器只做**确定性模板识别**，识别失败 → 请求原样直通 fallback 上游（GLM），并计日志。
2. state 放对话/情境证据；候选项进 choice 题的 criteria（选项值必须可回填北向响应格式）。
3. 候选 >62 / 题目 >16 等 SystemOne 硬限制无法承载时，同样直通回退（绝不截断语义）。
4. 南向任何失败（连接/超时/5xx/422）→ 直通回退；回退响应必须与"现状"（直接打 GLM）等价。
