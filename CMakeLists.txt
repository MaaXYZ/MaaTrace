cmake_minimum_required(VERSION 3.22)

project(MaaTrace VERSION 0.2.1 LANGUAGES CXX)

set(MAA_FRAMEWORK_ROOT "" CACHE PATH "Root of an extracted MaaFramework SDK")
set(MAA_TRACE_PACKAGE_VERSION "${PROJECT_VERSION}" CACHE STRING "Version embedded in the plugin and package")

# Only the public C ABI is used. Importing the entire SDK's CMake package also
# requires MaaUtils/OpenCV/Boost development packages, which this plugin does not
# use. The SDK itself remains an external runtime dependency.
find_path(MAA_FRAMEWORK_INCLUDE_DIR MaaFramework/MaaAPI.h
    HINTS "${MAA_FRAMEWORK_ROOT}" PATH_SUFFIXES include REQUIRED)
find_library(MAA_FRAMEWORK_LIBRARY NAMES MaaFramework
    HINTS "${MAA_FRAMEWORK_ROOT}" PATH_SUFFIXES lib bin REQUIRED)
add_library(maa_framework_sdk SHARED IMPORTED)
set_target_properties(maa_framework_sdk PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "${MAA_FRAMEWORK_INCLUDE_DIR}")
if(WIN32)
    find_file(MAA_FRAMEWORK_RUNTIME NAMES MaaFramework.dll
        HINTS "${MAA_FRAMEWORK_ROOT}" PATH_SUFFIXES bin REQUIRED)
    set_target_properties(maa_framework_sdk PROPERTIES
        IMPORTED_IMPLIB "${MAA_FRAMEWORK_LIBRARY}"
        IMPORTED_LOCATION "${MAA_FRAMEWORK_RUNTIME}")
else()
    set_target_properties(maa_framework_sdk PROPERTIES
        IMPORTED_LOCATION "${MAA_FRAMEWORK_LIBRARY}")
endif()

find_package(Threads REQUIRED)
add_library(MaaTrace SHARED src/plugin.cpp src/recorder.cpp src/flow.cpp)
target_compile_features(MaaTrace PRIVATE cxx_std_17)
target_include_directories(MaaTrace
    PUBLIC "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
    PRIVATE src third_party)
target_compile_definitions(MaaTrace PRIVATE
    MAA_TRACE_BUILD MAA_TRACE_VERSION="${MAA_TRACE_PACKAGE_VERSION}")
target_link_libraries(MaaTrace PRIVATE maa_framework_sdk Threads::Threads ${CMAKE_DL_LIBS})
set_target_properties(MaaTrace PROPERTIES
    CXX_EXTENSIONS OFF
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
if(MSVC)
    target_compile_options(MaaTrace PRIVATE /W4 /utf-8 /permissive-)
else()
    target_compile_options(MaaTrace PRIVATE -Wall -Wextra -Wpedantic)
endif()
if(APPLE)
    set_target_properties(MaaTrace PROPERTIES INSTALL_RPATH "@loader_path/..")
elseif(UNIX)
    set_target_properties(MaaTrace PROPERTIES INSTALL_RPATH "$ORIGIN/..")
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        # The official SDK uses libc++/libunwind. Keep GNU exception machinery
        # private to this C-only plugin so host load order cannot interpose it.
        target_link_options(MaaTrace PRIVATE -static-libstdc++ -static-libgcc
            "LINKER:--exclude-libs,ALL")
    endif()
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
    add_test(NAME plugin_integration
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_plugin.py"
            --plugin "$<TARGET_FILE:MaaTrace>"
            --framework "$<TARGET_FILE:maa_framework_sdk>")
    add_test(NAME evidence_integration
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_evidence.py"
            --plugin "$<TARGET_FILE:MaaTrace>" --framework "$<TARGET_FILE:maa_framework_sdk>")
    add_test(NAME host_api
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_host_api.py"
            --plugin "$<TARGET_FILE:MaaTrace>" --framework "$<TARGET_FILE:maa_framework_sdk>")
    add_test(NAME native_runtime
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_native_runtime.py"
            --plugin "$<TARGET_FILE:MaaTrace>" --framework "$<TARGET_FILE:maa_framework_sdk>")
    set_tests_properties(native_runtime PROPERTIES SKIP_REGULAR_EXPRESSION "SKIP native runtime")
    add_test(NAME drop_in_autoload
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_autoload.py"
            --plugin "$<TARGET_FILE:MaaTrace>" --framework "$<TARGET_FILE:maa_framework_sdk>")
    add_test(NAME trace_tools COMMAND "${Python3_EXECUTABLE}" -m unittest discover
        -s "${CMAKE_CURRENT_SOURCE_DIR}/tests" -p test_tools.py)
    add_test(NAME upstream_ci_tools COMMAND "${Python3_EXECUTABLE}" -m unittest discover
        -s "${CMAKE_CURRENT_SOURCE_DIR}/tests" -p test_upstream_ci.py)
    add_test(NAME pipeline_trace_validator COMMAND "${Python3_EXECUTABLE}" -m unittest discover
        -s "${CMAKE_CURRENT_SOURCE_DIR}/tests" -p test_pipeline_trace.py)
    add_executable(test_flow tests/test_flow.cpp src/flow.cpp)
    target_compile_features(test_flow PRIVATE cxx_std_17)
    target_include_directories(test_flow PRIVATE src third_party)
    add_test(NAME flow_associations COMMAND test_flow)
    add_executable(test_sha256 tests/test_sha256.cpp)
    target_compile_features(test_sha256 PRIVATE cxx_std_17)
    target_include_directories(test_sha256 PRIVATE src)
    add_test(NAME sha256_vectors COMMAND test_sha256)
    set_tests_properties(plugin_integration evidence_integration host_api drop_in_autoload PROPERTIES
        TIMEOUT 120 ENVIRONMENT "MAA_TRACE_TEST_LOG_ROOT=${CMAKE_BINARY_DIR}")
    if(WIN32)
        set_tests_properties(plugin_integration evidence_integration host_api PROPERTIES
            ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:$<TARGET_FILE_DIR:maa_framework_sdk>")
    elseif(APPLE)
        set_tests_properties(plugin_integration evidence_integration host_api PROPERTIES
            ENVIRONMENT_MODIFICATION "DYLD_LIBRARY_PATH=path_list_prepend:$<TARGET_FILE_DIR:maa_framework_sdk>")
    else()
        set_tests_properties(plugin_integration evidence_integration host_api PROPERTIES
            ENVIRONMENT_MODIFICATION "LD_LIBRARY_PATH=path_list_prepend:$<TARGET_FILE_DIR:maa_framework_sdk>")
    endif()
endif()

# Extract the archive into the directory containing MaaFramework.dll/.so/.dylib.
install(TARGETS MaaTrace RUNTIME DESTINATION plugins LIBRARY DESTINATION plugins
    ARCHIVE DESTINATION lib)
install(DIRECTORY include/ DESTINATION include)
install(FILES README.md LICENSE CHANGELOG.md DESTINATION .)
install(DIRECTORY docs/ DESTINATION docs OPTIONAL)
install(PROGRAMS tools/maatrace.py DESTINATION tools)
install(FILES third_party/nlohmann/LICENSE.MIT
    DESTINATION licenses RENAME nlohmann-json-LICENSE.MIT)
install(FILES third_party/README.md DESTINATION third_party)
install(FILES third_party/nlohmann/LICENSE.MIT DESTINATION third_party/nlohmann)

if(WIN32)
    set(_trace_os win)
elseif(APPLE)
    set(_trace_os macos)
else()
    string(TOLOWER "${CMAKE_SYSTEM_NAME}" _trace_os)
endif()
string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" _trace_arch)
if(_trace_arch MATCHES "^(amd64|x86_64|x64)$")
    set(_trace_arch x86_64)
elseif(_trace_arch MATCHES "^(arm64|aarch64)$")
    set(_trace_arch aarch64)
endif()
set(CPACK_GENERATOR ZIP)
set(CPACK_PACKAGE_NAME MaaTrace)
set(CPACK_PACKAGE_VERSION "${MAA_TRACE_PACKAGE_VERSION}")
set(CPACK_PACKAGE_FILE_NAME "MaaTrace-${MAA_TRACE_PACKAGE_VERSION}-${_trace_os}-${_trace_arch}")
set(CPACK_INCLUDE_TOPLEVEL_DIRECTORY OFF)
include(CPack)
