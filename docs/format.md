# MaaTrace 事件协议 1.0

原始记录使用 UTF-8 JSONL，一行一个完整 JSON 对象，按采集器接受事件的顺序追加。事件和清单分别遵循 [event.schema.json](event.schema.json)、[manifest.schema.json](manifest.schema.json)。配置与图片作为 session 内的相对路径附件引用。

## Session、Run 与操作身份

一个记录器 session 对应一个目录，首次采集时创建。每个顶层 `Tasker.Task.Starting` 分配一个 `run_id`；同一 Tasker 的 Context 事件，包括可观察到的嵌套任务，沿用该 run。资源与控制器事件通常没有可靠任务归属，run 可以为空。

`run_id`、`span_id` 只在 session 内有效。跨文件主键必须包含 `session_id`；事件主键为 `(session_id, seq)`。框架句柄只在内存中用于关联，不写入记录。

节点定义名称不充当执行身份。同一原生操作的开始和结束用原生 ID 生成 `<run_id>:<kind>:<native_id>`：任务使用 `task_id`，识别使用 `reco_id`，动作使用 `action_id`，节点使用 `node_id`，等待使用 `wf_id`。缺少 run 或有效身份时保留为空。

支持 MaaFramework #1622 时，候选扫描使用 `<run_id>:decision:<list_id>`，嵌套任务使用 `<run_id>:task:<task_id>`。框架的 `parent.kind: node` 统一引用 `<run_id>:node:<node_id>`，包括识别专用及动作专用节点；旧宿主仍保留 `recognition_node` / `action_node` span 前缀。没有原生 ID 的候选列表，在能配对同一上下文的开始/结束时生成 `<run_id>:decision:<start_seq>`。

`context_id` 优先使用框架进程内的原生上下文 ID，`context_identity_source` 为 `native_event`；旧版使用回调句柄和任务生成的观察标识。跨进程及跨 session 不共享身份；仅凭上下文 ID 不能证明完整调用栈。

## 事件外层

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `schema_version` | string | 当前 `1.0`，独立于插件 ABI 和框架版本 |
| `session_id` | string | 本次记录器会话 |
| `run_id` | string / null | 顶层任务调用对应的运行 |
| `seq` | integer | 从 1 开始的采集序号，可因丢失出现缺口 |
| `timestamp` | string | UTC RFC 3339 时间 |
| `elapsed_us` | integer | 相对记录器初始化的单调时钟微秒数 |
| `type` | string | 归一化事件类型 |
| `source` | string | `tasker`、`context`、`resource`、`controller`、`custom`、`host`、`recorder` 等 |
| `task_id` | string / null | 原生任务 ID，不等同于 run ID |
| `span_id` | string / null | 本次操作身份 |
| `parent_span_id` | string / null | 有证据支持的所属操作；依据在 `data.flow` 中 |
| `data` | object | 原生数据、补充快照、关系或采集器诊断 |

序号表示采集器序列化顺序，不证明跨线程因果。普通事件在入队时取时间；缺口和截断标记在写入时取时间。不要把相邻两行时间差直接当作操作耗时。

## 事件映射与结果

已知前缀的 `.Starting` 映射为 `.started`；`.Succeeded`、`.Failed` 映射为 `.finished`；下表已知 `Node.*` 及 `Context.Task` 前缀的 `.Cancelled` 同样映射为 `.finished`。

| 原生前缀 | 输出 kind |
| --- | --- |
| `Tasker.Task` | `task` |
| `Context.Task` | `subtask` |
| `Node.PipelineNode` | `node` |
| `Node.RecognitionNode` | `recognition_node` |
| `Node.ActionNode` | `action_node` |
| `Node.NextList` | `decision` |
| `Node.Recognition` | `recognition` |
| `Node.Action` | `action` |
| `Node.WaitFreezes` | `wait` |
| `Controller.Action` | `controller_action` |
| `Resource.Loading` | `resource` |

`Node.Transition` 映射为 `node.transition`，它不是新的操作起止事件，`span_id` 为空，执行实例引用在 `data.flow.transition` 中。未知原生前缀或阶段保留为 `framework.event`。`MaaTraceEmit` 输出 `custom.event`；宿主 `MaaTraceRecordStopRequest` 输出 `stop.requested`。停止请求只表达意图，不合成任务终态。

普通事件 `data` 的基础内容：

```json
{
  "native_message": "Node.Recognition.Succeeded",
  "native_details": {"task_id": 100, "reco_id": 200, "name": "ClosePopup"},
  "native_ids": {"task_id": "100", "reco_id": "200"},
  "native_status": "Succeeded",
  "outcome": "matched",
  "enrichment": {}
}
```

`native_details` 保留原始数字类型，跨语言无损处理 ID 应优先使用 `native_ids` 字符串。未知原生字段不删除。

原生识别 `Succeeded` 映射为 `matched`，`Failed` 映射为 `unknown`；其他对象分别映射 `succeeded` 和 `failed`。原生 `Node.*.Cancelled` 或已知终态事件明确携带 `reason: "cancelled"` 时，`outcome` 为 `cancelled`，`native_status` 仍保留原始后缀。因此旧式 `Tasker.Task.Succeeded` 配合取消原因会得到 `native_status: "Succeeded"` 和 `outcome: "cancelled"`。没有明确取消证据时不会推断用户取消，宿主停止请求本身不会改变结果。动作接口成功不证明界面已经进入目标状态。

`node.started`/`node.finished` 保留原生通知时机：Pipeline 开始名称可能为来源节点，结束 `node_details.name` 才是实际选中节点。候选识别未命中不等于候选节点执行失败。不同框架版本的停止通知可能不同，插件保留收到的通知，不补造缺失终态。

## 流程关系 `data.flow`

流程追踪器在同一 `(run_id, task_id, context_id)` 内关联事件。所有补充关系带 `provenance` 和 `evidence_seqs`：

- `observed`：直接整理原生结果或已观察事件的内容。
- `inferred`：通过同一上下文内的生命周期或结果匹配推导。
- `evidence_seqs`：用于该判断的 session 内事件序号。

### 所属关系与候选轮次

能够配对的节点、识别、动作、等待操作可以得到 `parent_span_id`。推导的所属关系使用 `parent_source: "same_context_lifecycle"`。框架未提供原生父调用信息时，不用嵌套任务的相邻事件强行建立父子关系。

新框架优先使用 `parent` / `task_parent`，标记 `parent_source: "native_event"` 和 `provenance: "observed"`。嵌套任务的父操作来自 `task_parent`；`parent_task_span_id` 指向 `parent_task_id`，`task_parent_span_id` 独立保留创建当前任务的父操作。直接 `run_recognition` / `run_action` 可能没有任务起止通知，查看器会据此显示已知调用者，并标明中间任务生命周期未被观察，不合成任务事件。显式空父操作不会被时间邻近的事件替换。原生 ID 可以在缺少开始事件时保留关系，但不证明对应开始事件已被采集。`subtask.finished` 不关闭顶层 run，也不覆盖顶层任务结果。

一对 `Node.NextList.Starting`/终态表示一次识别轮询。一个 PipelineNode 生命周期可能包含多个轮询；`round` 从 1 开始在该节点生命周期内计数，不是重新执行节点的次数。

新框架的 `list_id`、`round`、`list_type`、`selected_index`、`reco_id` 直接用于扫描身份、轮次和选中项，`selection_source` 为 `native_event`。同名候选的已选位置可以明确，未选识别的候选位置仍可能为空。旧版缺少这些字段时使用下表中的保守推断。

候选相关字段：

| 字段 | 说明 |
| --- | --- |
| `candidates` | 原生有序列表，保留对象属性与锚点标识 |
| `decision_span_id` | 本次轮询操作 |
| `candidate_index` | 能唯一识别候选身份时的 0 基索引，否则 null |
| `candidate_resolution` | `unique_native_identity`、`ambiguous` 或 `unavailable` |
| `attempts` | 实际观察到的识别顺序；含识别 span、节点名、开始/结束序号和结果 |
| `selected_recognition_span_id` | 本轮成功且仅有一个可靠观察到的命中时的识别引用 |
| `selected_candidate_index` | 能确定时的候选索引，否则 null |
| `selection_source` | 例如 `successful_round_with_single_observed_match` |

同名候选或重叠的识别可能使候选归属不明确。没有尝试记录的候选不能自动标记为失败；它可能在前面的候选命中后从未执行。

### 实际节点与流转

节点终态中的 `executed_node_name`、`selected_recognition_span_id`、`action_span_id` 优先来自原生节点详情，`result_source` 标记为 `native_node_detail`。

可以推导时，`flow.transition` 包含：

```json
{
  "from_node_name": "A",
  "from_span_id": "run-1:node:101",
  "to_node_name": "B",
  "to_span_id": "run-1:node:102",
  "provenance": "inferred",
  "reason": "selected_node_observed",
  "evidence_seqs": [10, 18, 9]
}
```

只有前一次完成节点的实际名称与当前原生来源名称一致时，才能关联 `from_span_id`。回跳或缺失事件可能导致来源执行实例无法确认，此时保留 null 并给出 `source_instance_resolution: "unavailable"`。这不是框架原生的显式 transition，也不能证明具体的 `next`/`on_error` 分支原因。

新框架的 `Node.Transition` 使用相同结构，但 `provenance` 为 `observed`，`reason` 来自框架的 `entry`、`next`、`on_error`、`jump_back`，实例直接引用 `from_node_id` / `to_node_id`。初始来源 ID 为 `0` 时 `from_span_id` 为空。回跳的目标是恢复后继列表的已有节点，不代表其动作重做。原生模式的节点结束事件不再额外生成推断边，避免重复计数。

重叠生命周期、无效事件、队列缺口和关联状态容量限制会清除相应关联，必要时给出 `association_reset`。解析器应保留未知状态，不把清除前后的实例串联起来。

## 配置与识别详情

`data.enrichment` 可以包含：

- `framework_version`：任务开始时查询的框架版本。
- `context_capture`：Context 回调的采集方式。`method: "plugin_callback"` 表示由框架加载器注册标准插件入口；旧记录的 `method: "explicit_tasker_sink"` 与 `attached` 仍可读取。
- `context_id`、`context_identity_source`：回调上下文的观察身份。
- `effective_node`：优先使用识别和动作通知中的 `pipeline`，标注 `source: "runtime_pipeline_snapshot"`；缺少快照时，开始通知按原生名称查询并标注 `source: "context_query_at_notification"`。
- `recognition`、`action`、`node`：终态时可获取的详情；含 `availability`、`source`、`value`，或不可用原因。
- `image`：图像采集策略、来源及是否成功复制缓存数据。

识别详情优先来自通知内 `reco_details`，否则查询缓存。旧缓存可能在 Pipeline `inverse` 应用前写入，插件用 `before_pipeline_inverse: true` 标记，不能仅按缓存分数重算最终命中结果。动作与节点详情类似地优先使用通知内容。WaitFreezes 直接保留通知中的 `elapsed`、`reco_ids`、`roi`，不依赖不同框架版本的缓存写入时机。

有效配置采用内容寻址：序列化 JSON 加换行后计算 SHA256，保存为 `configs/<sha256>.json`。成功时，`data.config` 和 `enrichment.effective_node.config_ref` 引用同一文件，原来的内联 `value` 移除。相同内容只保存一次。

```json
{
  "availability": "available",
  "path": "configs/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.json",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "size": 1024
}
```

超过配置预算或写入失败时，`data.config` 为 `availability: "omitted"` 并记录 `reason`，有效配置可能仍内联保留。原始 `native_details.pipeline` 不删除，配置文件预算只限制去重文件，不限制原始通知内容。`runtime_pipeline_snapshot` 表示**该操作实际使用的配置**，同一操作起止一致；查询来源仅表示**该通知时刻的查询结果**。二者都不等于完整资源归档或全部动态覆盖历史，动态 ROI / target 引用保持配置形式。

## 图片附件

`MAA_TRACE_IMAGES` 默认为 `none`，也可以为 `failed` 或 `all`。`failed` 对应原生识别 `Failed`，包括正常未命中。图片只从框架保留的当次识别输入中取得；需要宿主启用 debug，或在支持 #1622 的框架上显式启用 `RecoImageRaw` 并保留缓存。插件不会修改全局采集设置或重新截图。

`enrichment.image.native_image_status` 保留 `reco_details.image_status`（缓存插入时状态）。仅当所需原图缺失且新接口可用时查询当前状态，保存在 `current_image_status`，不轮询或重取图片；缺图原因可以为 `not_captured`、`disabled`、`budget_exceeded`、`evicted` 或 `unavailable`。旧 SDK 没有状态接口时沿用 `debug_required_or_not_retained`。

`enrichment.image.availability: "captured"` 仅表示回调已复制图片，最终落盘结果以 `data.artifacts` 为准：

```json
{
  "id": "recognition-200",
  "availability": "available",
  "path": "artifacts/artifact-42-0.png",
  "mime_type": "image/png",
  "size": 2048,
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "metadata": {
    "role": "recognition_input",
    "source": "runtime_cache",
    "reco_id": "200",
    "width": 1280,
    "height": 720,
    "coordinate_space": "recognition_image"
  }
}
```

图片无法取得时，`enrichment.image` 给出不可用原因，例如 `debug_required_or_not_retained`。总附件预算或写入失败造成的省略出现在 `data.artifacts`，`availability` 为 `omitted` 且没有 `path`。所有引用路径相对 session，SHA256 计算覆盖实际落盘文件全部字节。

## 采集器诊断

### `trace.gap`

队列事件数或字节预算超限时，丢弃新事件并写入：

```json
{"reason": "queue_limit", "first_seq": 42, "last_seq": 48, "count": 7}
```

标记的外层 `seq` 等于 `first_seq`，后续事件从 `last_seq + 1` 继续。记录器清空活跃 run 和流程关联，防止漏掉的任务边界导致错误归属。直到重新观察到任务开始，相关 run/span 可能为 null。

### `trace.truncated`

事件文件达到预算时，用当前事件序号写入：

```json
{"reason": "file_size_limit", "max_file_bytes": 134217728}
```

随后停止事件输出。标记自身可使文件略超过预算。没有自动轮转或历史删除。

### `trace.invalid_event`

`details_json` 不能解析为 JSON 对象时，保存 `native_message`、`raw_details`、`enrichment`，并设置 `reason: "details_must_be_json_object"`。损坏内容不会冒充正常事件。

## Manifest 与完整度

初始 manifest 包含版本、session、开始时间、`events: "events.jsonl"`、预算、采集策略和 `state: "recording"`。`capabilities` **不保证每条事件都具备对应信息**。`native_parent_links`、`explicit_transitions`、`runtime_config_snapshots`、`stop_reason` 初始为 false，在实际收到相应框架字段后置为 true，并在正常收尾时写回清单；宿主业务事件和停止意图不会开启这些标记。`complete_call_tree` 仍为 false，不承诺框架未公开的内部调用。宿主可以通过专用接口显式补充停止请求。

正常关闭后增加或更新：

- `state`：`closed`、`error` 或 `truncated`。
- `finished_at`：记录器收尾时间，不是业务任务完成时间。
- `open_runs`：仍未闭合的运行数量。
- `recording_complete`：没有已报告的错误、丢失或截断，且没有未闭合 run。它不证明原生语义完整，不代表所有业务任务成功，也不证明所有 span 已闭合。
- `accepted`、`written`、`dropped`、`recording_errors`：采集器计数；`written` 包含缺口标记，`recording_errors` 包含桥接、采集、详情查询及写入报告的错误。
- `artifact_bytes`、`config_bytes`：成功保存的附件与去重配置总大小。

manifest 使用临时文件替换更新。异常退出时可能仍为 `recording`；应先读取可用事件，不要据此宣布业务任务失败。

支持 #1622 时，宿主可以在所有操作结束后调用 `MaaGlobalFlushPlugins()`，并在销毁全部框架对象后调用 `MaaGlobalShutdownPlugins()`；标准 hook 转发到 MaaTrace 的同一套刷新和关闭接口。旧宿主继续使用专用 `MaaTraceFlush()` / `MaaTraceShutdown()`。这些接口应由宿主线程显式调用，不能从事件回调或 `DllMain` 发起。

## 查看摘要

`inspect --json` 的每个 run 增加 `hierarchy`（按 span 合并的有界操作样本）、`hierarchy_start_total` 和顶层终止 `reason`；嵌套任务结果独立保留。文本层级展示依据父操作引用，缺失父事件和环不会被补成完整调用树。

session 摘要的 `termination_reasons` 汇总已知终态原因；`resource_load_total`、`resource_failed_total`、`resource_error_total` 分别统计资源加载、失败和诊断数量。`resource_loads` 只采样失败或携带诊断的加载，不要求任务身份，保留已知的错误码、文件、节点、字段、JSON Pointer 和目标。样本数量有上限，完整原始事件仍在 JSONL 中。

## 导出格式与读取规则

离线导出 ZIP 保留整个 session 的原始文件，并增加 `export.json`。该文件列出每个原始文件的相对路径、大小、SHA256，以及校验结论；不把自身列入递归哈希清单。导出并不修复记录，损坏事件仍按原始字节保留。

读取时：

1. 逐行解析；末尾未写完的一行可忽略并报告，中间损坏则报告错误。
2. 未闭合操作保留未知，不自动补失败或取消。
3. 所有关联先检查原生身份、证据序号及 `provenance`，不要把推导当原生保证。
4. 忽略未知可选字段，保留未知事件；不要依赖对象字段顺序。
5. 序号连续不证明语义完整，框架未发出的事件或被宿主移除的 sink 无法单靠序号检测。
6. 只将规定位置的附件引用解析为 session 文件，不把原生配置中任意 `path` 当附件。
7. 当前每行后刷新文件流但不 `fsync`；进程崩溃、强制退出和断电的持久化保证不同。

更改现有字段含义或必填条件应升级主版本；新增可选信息保持旧读取器可用。
