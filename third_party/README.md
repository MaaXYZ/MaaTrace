# Third-party components

## nlohmann/json

- Upstream: <https://github.com/nlohmann/json>
- Version: **3.11.3** (`v3.11.3`)
- License: [MIT](nlohmann/LICENSE.MIT)
- Vendored file: `nlohmann/json.hpp`, copied unmodified from the upstream single-header distribution.
- Download: <https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp>
- Header SHA256: `9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6`
- License SHA256: `86b998c792894ccb911a1cb7994f7a9652894e7a094c0b5e45be2f553f45cf14`

No dependency download is required to compile this header. Release archives include its license in `licenses/nlohmann-json-LICENSE.MIT`.

## MaaFramework and MaaPluginDemo

[MaaFramework](https://github.com/MaaXYZ/MaaFramework) is an external SDK and runtime dependency. Its binaries and source are not vendored here. Users retain the framework and its runtime dependencies already shipped with their host application; MaaTrace release archives do not redistribute them.

[MaaPluginDemo](https://github.com/MaaXYZ/MaaPluginDemo) informed the plugin ABI v1 exports, dynamic-library integration, and per-platform packaging approach. MaaTrace provides its own recorder and minimal build configuration without a MaaUtils submodule. Please consult the respective upstream repositories for their licenses.

## Compiler runtimes

GNU/Linux builds statically link the GNU C++ and GCC support runtimes with hidden symbols to isolate exception handling from the host's LLVM runtimes. These compiler components are covered by their upstream licenses and the [GCC Runtime Library Exception](https://www.gnu.org/licenses/gcc-exception-3.1.html). Windows MSVC builds use the static Microsoft C/C++ runtime under its applicable redistribution terms. These settings do not change the licenses of MaaFramework or its dependencies.
