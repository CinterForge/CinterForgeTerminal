cmake_minimum_required(VERSION 3.16)
project(CinterForgePythonHost VERSION 0.3.0 LANGUAGES CXX)

# Both libraries have identical APIs. The shared variant exposes a stable C
# interface for Swift, Windows programs and other C-compatible consumers.
add_library(CinterForgePythonHost STATIC src/CinterPythonHost.cpp src/CinterPythonHostC.cpp)
add_library(CinterForgePythonHostShared SHARED src/CinterPythonHost.cpp src/CinterPythonHostC.cpp)
set_target_properties(CinterForgePythonHostShared PROPERTIES OUTPUT_NAME CinterForgePythonHost)
foreach(target CinterForgePythonHost CinterForgePythonHostShared)
    target_compile_features(${target} PUBLIC cxx_std_17)
    target_include_directories(${target} PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include")
endforeach()
target_compile_definitions(CinterForgePythonHostShared PRIVATE CPH_BUILD_SHARED)
target_compile_definitions(CinterForgePythonHost PUBLIC CPH_BUILD_STATIC)
if(WIN32)
    set_target_properties(CinterForgePythonHost PROPERTIES OUTPUT_NAME CinterForgePythonHostStatic)
endif()

# Network discovery is optional. No Bonjour SDK or Avahi dependency is needed:
# this module uses the standardized IPv4 mDNS/DNS-SD wire protocol directly.
option(CPH_ENABLE_ZEROCONF "Build optional mDNS/DNS-SD discovery API" ON)
if(CPH_ENABLE_ZEROCONF)
    foreach(target CinterForgePythonHost CinterForgePythonHostShared)
        target_sources(${target} PRIVATE src/CinterZeroconf.cpp)
        target_compile_definitions(${target} PUBLIC CPH_HAS_ZEROCONF=1)
        if(WIN32)
            target_link_libraries(${target} PUBLIC ws2_32)
        endif()
    endforeach()
    add_executable(cinter-zeroconf-smoke src/zeroconf_smoke.cpp)
    target_link_libraries(cinter-zeroconf-smoke PRIVATE CinterForgePythonHost)
    # Same implementation, a production CLI for advertisement and discovery.
    add_executable(cinter-zeroconf src/zeroconf_smoke.cpp)
    target_link_libraries(cinter-zeroconf PRIVATE CinterForgePythonHost)
endif()

# The full Application Shell is a single shared C++ source implementation on
# every target. The small webview wrapper uses each OS browser engine; source
# and SDK dependencies are pinned/provisioned under sibling Builds, not here.
option(CPH_BUILD_APPLICATION_SHELL "Build CinterForgeTerminal with webview/webview" OFF)
if(CPH_BUILD_APPLICATION_SHELL)
    if(NOT CPH_WEBVIEW_SOURCE_DIR OR NOT EXISTS "${CPH_WEBVIEW_SOURCE_DIR}/CMakeLists.txt")
        message(FATAL_ERROR "CinterForgeTerminal requires pinned webview source under Builds/Toolchains; set CPH_WEBVIEW_SOURCE_DIR")
    endif()
    get_filename_component(_cph_webview_source "${CPH_WEBVIEW_SOURCE_DIR}" REALPATH)
    if(NOT CPH_BUILDS_ROOT)
        set(CPH_BUILDS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../Builds")
    endif()
    get_filename_component(_cph_builds "${CPH_BUILDS_ROOT}" REALPATH)
    string(FIND "${_cph_webview_source}/" "${_cph_builds}/" _cph_source_inside)
    if(NOT _cph_source_inside EQUAL 0)
        message(FATAL_ERROR "webview source must be provisioned into sibling Builds, not source checkouts")
    endif()
    # Use the exact upstream CMake consumer contract everywhere: one library,
    # one shared C++ shell and bridge, native browser engine per platform.
    # The SDK source is staged during Setup; builds must not fetch dependencies.
    if(WIN32)
        if(NOT MSWebView2_ROOT OR NOT EXISTS "${MSWebView2_ROOT}/build/native/include/WebView2.h")
            message(FATAL_ERROR "WebView2 headers must be staged in Builds during Setup; set MSWebView2_ROOT")
        endif()
    endif()
    # WebView embeds with this option OFF by default. MinGW needs its own
    # EventToken.h compatibility header when compiling Microsoft WebView2.h
    # on a case-sensitive Linux filesystem. Reuse upstream's header unchanged.
    if(WIN32 AND (MINGW OR CMAKE_CXX_COMPILER_ID STREQUAL "GNU"))
        if(NOT EXISTS "${_cph_webview_source}/compatibility/mingw/include/EventToken.h")
            message(FATAL_ERROR "Pinned WebView MinGW EventToken.h compatibility header is missing")
        endif()
        set(WEBVIEW_USE_COMPAT_MINGW ON CACHE BOOL "Use upstream WebView MinGW compatibility headers" FORCE)
    endif()
    set(WEBVIEW_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(WEBVIEW_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(WEBVIEW_BUILD_DOCS OFF CACHE BOOL "" FORCE)
    add_subdirectory("${_cph_webview_source}" "${CMAKE_CURRENT_BINARY_DIR}/webview" EXCLUDE_FROM_ALL)
    # Apple's real File menu lives in the existing shell source. Compile that
    # unit as Objective-C++ on macOS instead of introducing a separate .mm file.
    if(APPLE)
        enable_language(OBJCXX)
        set_source_files_properties(src/CinterApplicationShell.cpp PROPERTIES LANGUAGE OBJCXX)
    endif()
    add_library(CinterForgeApplicationShell STATIC src/CinterApplicationShell.cpp)
    if(APPLE)
        target_link_libraries(CinterForgeApplicationShell PUBLIC "-framework Cocoa")
    endif()
    target_include_directories(CinterForgeApplicationShell PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/include")
    target_compile_features(CinterForgeApplicationShell PUBLIC cxx_std_17)
    target_link_libraries(CinterForgeApplicationShell PUBLIC CinterForgePythonHost webview::core)
    # One upstream WebView example, built unchanged for Mac, Linux and Windows.
    add_executable(cinter-bootstrap-sample build/terminal_bootstrap/main.cpp)
    set_target_properties(cinter-bootstrap-sample PROPERTIES OUTPUT_NAME CinterBootstrapSample)
    target_compile_features(cinter-bootstrap-sample PRIVATE cxx_std_17)
    target_link_libraries(cinter-bootstrap-sample PRIVATE webview::core)
    add_executable(cinter-terminal src/cinterforge_terminal_main.cpp)
    # Preserve the CMake target for existing build scripts, but ship the renamed product executable.
    set_target_properties(cinter-terminal PROPERTIES OUTPUT_NAME CinterForgeTerminal)
    if(WIN32)
        # Native GUI executable: Windows must not spawn a separate black console
        # alongside the CinterForge WebView. Child Host Python is already started
        # with CREATE_NO_WINDOW and communicates over its own pipes.
        set_target_properties(cinter-terminal PROPERTIES WIN32_EXECUTABLE TRUE)
    endif()
    target_link_libraries(cinter-terminal PRIVATE CinterForgeApplicationShell)
    # One user-authored icon source for every platform. The PNG must be in
    # Terminal itself; never build by reaching into a sibling source checkout.
    set(_cinter_icon "${CMAKE_CURRENT_SOURCE_DIR}/build/resources/AppIcon.png")
    if(NOT EXISTS "${_cinter_icon}")
        message(FATAL_ERROR "Missing build/resources/AppIcon.png: restore the authored icon under Terminal/build/resources before building")
    endif()
    configure_file("${_cinter_icon}" "${CMAKE_CURRENT_BINARY_DIR}/AppIcon.png" COPYONLY)
    if(WIN32)
        enable_language(RC)
        find_package(Python3 COMPONENTS Interpreter REQUIRED)
        set(_cinter_ico "${CMAKE_CURRENT_BINARY_DIR}/AppIcon.ico")
        add_custom_command(OUTPUT "${_cinter_ico}"
            COMMAND "${Python3_EXECUTABLE}" -B -m host.mac_app make-ico
                    --icon-source "${_cinter_icon}" --icon-output "${_cinter_ico}"
            DEPENDS "${_cinter_icon}" "${CMAKE_CURRENT_SOURCE_DIR}/host/mac_app.py"
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
            VERBATIM)
        add_custom_target(cinter-terminal-icon DEPENDS "${_cinter_ico}")
        add_dependencies(cinter-terminal cinter-terminal-icon)
        set(_cinter_rc "${CMAKE_CURRENT_BINARY_DIR}/AppIcon.rc")
        file(WRITE "${_cinter_rc}" "1 ICON \"${_cinter_ico}\"\n")
        set_source_files_properties("${_cinter_rc}" PROPERTIES OBJECT_DEPENDS "${_cinter_ico}")
        target_sources(cinter-terminal PRIVATE "${_cinter_rc}")
    elseif(UNIX AND NOT APPLE)
        # A real Linux desktop launcher and taskbar icon, staged alongside the
        # built executable. Runtime GTK uses this same colocated PNG.
        file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/CinterForge Terminal.desktop"
            CONTENT "[Desktop Entry]\nType=Application\nName=CinterForge Terminal\nExec=$<TARGET_FILE:cinter-terminal>\nIcon=${CMAKE_CURRENT_BINARY_DIR}/AppIcon.png\nStartupWMClass=CinterForgeTerminal\nTerminal=false\nCategories=Development;System;\n")
    endif()
endif()

add_executable(cinter-python-host-smoke src/host_smoke.cpp)
target_link_libraries(cinter-python-host-smoke PRIVATE CinterForgePythonHost)
add_executable(cinter-python-host src/cph_cli.cpp)
target_link_libraries(cinter-python-host PRIVATE CinterForgePythonHost)
add_executable(cinter-host-utilities-smoke src/utilities_smoke.cpp)
target_link_libraries(cinter-host-utilities-smoke PRIVATE CinterForgePythonHost)
enable_testing()
add_test(NAME message_and_dispatch COMMAND cinter-host-utilities-smoke)
if(CPH_ENABLE_ZEROCONF)
    add_test(NAME zeroconf_local_lan COMMAND cinter-zeroconf-smoke)
endif()
