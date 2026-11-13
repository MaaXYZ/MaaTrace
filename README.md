# MaaTrace

MaaFramework 的可选运行记录插件。将动态库放进宿主的 `plugins` 目录，正常运行任务，即可得到可以校验、解析和导出的结构化运行记录。

项目沿用 [MaaPluginDemo](https://github.com/MaaXYZ/MaaPluginDemo) 的 plugin ABI v1 和动态库部署方式，独立构建、独立发布。发行包包含插件、记录协议、Python 命令行工具和文档；运行时使用宿主已有的 MaaFramework。

## 快速开始

1. 选择与宿主进程系统、架构一致的发行包。
2. 解压到 **MaaFramework 动态库所在目录**，保留 `plugins` 层级。
3. 重新启动宿主并运行任务。
4. 在进程当前工作目录的 `maa-traces/session-*` 中找到记录。

Windows 示例：

```text
release/bin/
├── MaaFramework.dll
├── MaaUtils.dll
├── ...已有的框架依赖...
└── plugins/
    └── MaaTrace.dll
```

Linux 使用 `plugins/libMaaTrace.so`，macOS 使用 `plugins/libMaaTrace.dylib`。选择与**宿主进程**一致的架构，例如 Windows x64 宿主使用 x64 DLL。

标准 SDK 中 MaaFramework 与 MaaUtils 在同一目录。自动扫描使用框架的 `library_dir()`，当前由 MaaUtils 提供；宿主如果将两者分开放置，应以 MaaUtils 目录下的 `plugins/` 为准，或显式加载插件。

也可以在创建 Resource、Controller、Tasker **之前**调用 `MaaGlobalLoadPlugin()`。已有对象不会自动补装插件回调。

一次进程运行生成一个 session；其中可包含多个顶层任务：

```text
maa-traces/session-<唯一标识>/
├── manifest.json          # 版本、采集能力、预算和完整度
├── events.jsonl           # 持续追加的运行事件
├── configs/               # 按 SHA256 去重的实际节点配置
└── artifacts/             # 可选的当次识别输入图
```

`configs`、`artifacts` 在有对应数据时建立。默认不保存截图。插件不自动上传记录。

## 查看、校验和导出

发行包自带 `tools/maatrace.py`，需要 Python 3.10+，无需安装第三方包：

```sh
python tools/maatrace.py inspect maa-traces/session-xxxx
python tools/maatrace.py validate maa-traces/session-xxxx
python tools/maatrace.py export maa-traces/session-xxxx -o issue-trace.zip
```

也可传入 `events.jsonl` 路径，三个命令均支持 `--json`。`inspect` 展示任务、观察到的动作顺序、候选选择和缺口；`validate` 检查事件结构、顺序、附件路径、长度和 SHA256；`export` 保存原始文件并生成含逐文件校验和的 `export.json`。

应在任务停止并刷新记录后导出。工具会拒绝导出过程中变化的文件、符号链接或越界路径。默认不覆盖已有 ZIP，可显式传 `--overwrite`。损坏或不完整的记录仍可导出用于排查，导出结果中会保留校验结论。

命令退出码：`0` 表示命令完成，仍可能报告记录不完整的警告；`1` 表示校验发现无效记录；`2` 表示命令、I/O 或不安全导出路径错误。不要用退出码 `0` 代替检查完整度。

## 记录内容与语义

- 任务、节点、候选列表、识别、动作、等待、资源和控制器事件，保留原始消息和 JSON。
- 原生操作 ID、开始/结束关联、UTC 时间、单调时钟偏移和采集序号。
- 可用的识别、动作及节点详情，以及当时查询到的有效节点配置快照。
- 同一上下文内的候选轮次、尝试顺序、识别归属和选中结果；关联中包含证据事件序号。
- 根据原生结果能够推导的节点流转。推导关系标记为 `inferred`，不能确定的关系留空。
- 可选的当次识别输入图；宿主主动补充的业务事件与停止请求。
- 队列丢失、文件截断、附件省略和记录器健康状态。

PipelineNode 的开始名称可能是来源节点，结束详情才表示选中的节点。未命中候选不等于执行节点失败；未闭合节点也不能直接判定失败。当前原生识别 `Failed` 映射为 `outcome: "unknown"`，因为它不总能区分未命中、执行错误和停止。

原生协议没有提供的因果关系不会被补造：当前不能保证完整调用树、全部回跳来源或框架原生流转原因。详情见 [记录协议](docs/format.md)、[事件 Schema](docs/event.schema.json) 和 [Manifest Schema](docs/manifest.schema.json)。

## 配置

在启动宿主前设置环境变量。配置在首次相关采集时读取：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `MAA_TRACE_ENABLED` | `1` | `0` 禁用记录输出 |
| `MAA_TRACE_DIR` | `maa-traces` | 输出根目录；相对路径基于进程工作目录 |
| `MAA_TRACE_QUEUE_CAPACITY` | `8192` | 待写事件数量上限 |
| `MAA_TRACE_QUEUE_BYTES` | `16777216` | 待写事件与附件内容的字节预算，不是进程内存硬上限 |
| `MAA_TRACE_MAX_FILE_BYTES` | `134217728` | 事件文件预算；截断标记可额外占用少量空间 |
| `MAA_TRACE_MAX_CONFIG_BYTES` | `16777216` | 本 session 配置文件总预算，按内容去重 |
| `MAA_TRACE_IMAGES` | `none` | `none`、`failed` 或 `all` |
| `MAA_TRACE_MAX_IMAGE_BYTES` | `8388608` | 单张编码图片大小上限 |
| `MAA_TRACE_MAX_ARTIFACT_BYTES` | `268435456` | 本 session 图片等附件的总预算 |

容量参数必须为正整数。`failed` 指原生识别 `Failed` 事件，**包括正常候选未命中**，不表示只保存最终失败任务的画面。

图片来自框架保留的当次识别输入缓存，需要宿主启用 MaaFramework debug 模式并保留相应缓存。插件不会修改宿主的 debug 设置，也不会在失败后重新截图。缓存不可用、超过单图上限或附件预算时，记录中会说明原因。

例如，Linux/macOS 启用全部可用识别图片：

```sh
export MAA_TRACE_DIR="$PWD/trace-output"
export MAA_TRACE_IMAGES=all
./your-host
```

PowerShell：

```powershell
$env:MAA_TRACE_DIR = "$PWD\trace-output"
$env:MAA_TRACE_IMAGES = "all"
.\your-host.exe
```

队列超限会丢弃新事件并写 `trace.gap`；事件文件超限后写 `trace.truncated` 并停止后续事件输出。配置和附件超限会产生省略说明。当前不自动轮转或删除历史目录。写入失败时标准错误流和状态接口会报告问题，不能保证错误还能写进同一个故障文件。

原始事件、有效参数及显式启用的图片不自动脱敏，可能包含文本、命令、路径和自定义业务数据。分享前应检查导出内容。

## 可选宿主接口

自动采集无需更改 Pipeline。需要补充业务信息或精确收尾时，可使用 [MaaTrace.h](include/MaaTrace/MaaTrace.h) 中的 C 接口：

```c
void MaaTraceEmit(void* tasker, const char* event_name, const char* details_json);
void MaaTraceRecordStopRequest(void* tasker, const char* reason);
int MaaTraceFlush(void);
int MaaTraceShutdown(void);
const char* MaaTraceGetStatus(void);
```

`tasker` 传原生 MaaTasker 句柄；传 `NULL` 表示 session 级事件。输入字符串在调用期间复制。`details_json` 应为 JSON 对象。

```c
MaaTraceEmit(tasker, "my_script.popup_seen", "{\"kind\":\"network\"}");
MaaTraceRecordStopRequest(tasker, "user");
/* 然后由宿主正常调用 MaaTaskerPostStop(tasker)。 */
```

停止请求只记录宿主意图，不证明任务已经停止，不会自动生成 `cancelled` 结果。

`MaaTraceFlush()` 等待此前接受的事件写入文件流。任务完成时插件也自动刷新，但不承诺 `fsync` 或断电持久化。`MaaTraceShutdown()` 排空队列、关闭文件并更新 manifest；在所有 Tasker 停止后、卸载框架前调用，可重复调用，关闭后不能重新启用。

`MaaTraceGetStatus()` 返回 UTF-8 JSON，含 `state`、`accepted`、`written`、`dropped`、`recording_errors`、`truncated`。指针有效至同线程下次调用，此接口不会初始化记录器。`Flush`/`Shutdown` 的非零返回值不等于轨迹完整，仍应检查丢失计数。

插件保留至进程结束，不支持热卸载。POSIX 注册 `atexit` 收尾；Windows 避免在 DLL loader lock 下等待线程，建议宿主显式 `MaaTraceShutdown()` 完成 manifest。未显式收尾时，已写 JSONL 可读，但 manifest 可能保持 `recording`，队列尾部可能丢失。不要从 `DllMain` 调用阻塞接口。

## 框架兼容与构建

部分 ABI v1 加载器将 `OnContextEvent` 放错 Controller 回调槽。MaaTrace 不导出该可选入口，而在任务开始时通过 `MaaTaskerAddContextSink()` 注册内部回调、任务结束后移除，兼容修复前后的加载器并避免重复采集。运行期间清除宿主的 Context sinks 会中断节点采集。

需要 CMake 3.22+、C++17 编译器及匹配平台的 MaaFramework SDK；测试与命令行工具需要 Python 3.10+。CI 固定 SDK `v5.16.0` 并校验下载 SHA256。将 SDK 解压到 `deps` 后：

```sh
cmake -S . -B build -DMAA_FRAMEWORK_ROOT="$PWD/deps" -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --parallel
ctest --test-dir build -C Release --output-on-failure
cmake --install build --config Release --prefix install
cpack --config build/CPackConfig.cmake -C Release -B dist
```

可以用 `CMAKE_PREFIX_PATH` 指向 SDK；仅构建插件可加 `-DBUILD_TESTING=OFF`。Ninja 环境可使用 `release` preset，Windows 需在相应 MSVC 开发者终端执行。

构建只需要 SDK 的 C 头文件和框架库，不需要 MaaUtils、Boost、OpenCV 开发包。宿主仍需其原有运行时依赖。nlohmann/json 3.11.3 已内置，见 [第三方说明](third_party/README.md)。Windows 使用静态 MSVC runtime；GNU/Linux 静态链接并隐藏 GNU C++ runtime 符号，隔离宿主 LLVM runtime 的异常处理。Linux/macOS 安装 RPATH 指向插件父目录。

## 验证与发布

仓库包含真实 SDK 集成、图片缓存与预算、宿主接口、流程关联、SHA256 和命令行工具测试。CI 配置为 Windows x64、Linux x64、macOS ARM64；每个目标平台的支持状态以对应提交的实际 CI 结果为准。

普通分支构建生成 Actions artifacts；推送 `v*` 标签后，工作流使用去掉 `v` 的版本号构建、打包、生成校验文件，并创建 **Draft Release**。正式发布前的复核步骤见 [发布说明](docs/RELEASE.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。发行包不重新打包 MaaFramework。

项目代码使用 [MIT License](LICENSE)。MaaFramework 和各第三方依赖保持各自许可证。
