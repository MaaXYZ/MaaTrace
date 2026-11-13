# 构建与发布

本项目使用独立仓库和 `v<version>` 标签。发行 ZIP 只包含插件、公共头文件、命令行工具、协议及许可证，不附带 MaaFramework SDK。安装时把 ZIP 解压至框架库所在目录，保留 `plugins/`。

## 发布前验证

1. 更新 `CMakeLists.txt` 项目版本、`CHANGELOG.md` 和必要的协议文档。改变现有字段语义时升级 schema 主版本。
2. 若升级 SDK，同时更新工作流的固定版本和三个平台的 SHA256，禁止只更改版本号。
3. 在匹配平台的 SDK 上配置、构建、测试；检查安装目录和 ZIP 内的路径。

```sh
cmake -S . -B build -DMAA_FRAMEWORK_ROOT="$PWD/deps" -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --parallel
ctest --test-dir build -C Release --output-on-failure
cmake --install build --config Release --prefix install
cpack --config build/CPackConfig.cmake -C Release -B dist
```

测试覆盖原生框架通知、候选未命中、嵌套任务、停止、异常 JSON、并发、输出故障、队列与文件预算、图片缓存、宿主事件、SHA256 和离线工具。测试数量会随版本变化，以 CTest 的实际结果为准。

额外做一次实际安装验收：将插件放入一个完整 SDK/宿主的 `plugins` 目录，重新启动且不显式调用加载 API，运行一个包含识别和动作的任务。确认创建了 session，执行 `inspect`、`validate`、`export`，检查导出的图片与配置 SHA256。若启用图片，使用宿主显式 debug 模式，不要将事后截图当作识别输入。

## 提交、标签与 Actions

以下命令在有目标仓库写入权限的发布环境执行：

```sh
git push origin main
gh run list --workflow build.yml --branch main
# 使用列表返回的运行 ID，等待目标提交的全部平台成功。
gh run watch RUN_ID --exit-status

git tag -a v0.1.0 -m "MaaTrace 0.1.0"
git push origin v0.1.0
gh run list --workflow build.yml
gh run watch TAG_RUN_ID --exit-status
```

工作流当前配置 Windows x64、Linux x64、macOS ARM64，固定 MaaFramework `v5.16.1`，宿主最低版本同为 `v5.16.1`。标签构建的版本取自标签去掉前缀 `v` 的值。普通分支也提供 Actions artifacts，方便在创建正式版本前验收。

不得把某一平台本地成功等同于其他平台已通过。目标提交和标签必须以各自的 Actions 结果为准。

## 草稿 Release 验收

标签工作流成功后自动创建 Draft Release。下载其附件进行复核：

```sh
gh release view v0.1.0
gh release download v0.1.0 --dir release-check
```

对每份 ZIP 验证 `.zip.sha256` 中的 SHA256，并确认：

- 文件名的版本、平台与架构正确。
- 包内 `plugins/` 只有目标插件，没有重复的 MaaFramework 或额外开发依赖。
- 公共头文件、README、协议 Schema、工具和许可证均包含在包中。
- 干净宿主可自动加载；记录、配置和可用的图片能够被校验与导出。
- Release notes 明确兼容 SDK、采集内容和已知边界，没有把推导关系说成原生因果。

确认草稿内容正确后发布：

```sh
gh release edit v0.1.0 --draft=false
```

发布后若有问题，优先追加修复版本并保留原版本记录，不覆盖已经分发的相同版本二进制。源码本身是标准 CMake 项目，可由用户从标签复建。

## 故障定位

- **无法建仓或推送：** 检查组织权限和当前 GitHub 连接对目标仓库的授权。代码准备完成不能代表远端创建已经成功。
- **框架没有发现插件：** 确认进程架构、库文件依赖、自动扫描目录和加载时机。标准 SDK 的框架与 MaaUtils 应在同一目录；分离布局需按实际 `library_dir()` 放置或显式加载。
- **有事件但没有图片：** 检查图片策略、宿主 debug、缓存是否留存以及单图/总附件预算；以事件中的省略原因判断。
- **manifest 仍是 `recording`：** 可能是异常退出或没有显式关闭。先读取可用 JSONL，不要据此断言任务失败。
- **报告缺口、截断或附件省略：** 检查预算、I/O 和记录器状态，保留这些诊断标记。增大预算只能影响后续运行，不能修复已丢失的事件。
