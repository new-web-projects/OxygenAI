cmake_minimum_required(VERSION 3.18)

# Oxygen AI native performance layer — Passage 1 §5.
#
# Two distinct concerns, kept separate exactly as §5.1 requires:
#   * C++  — a native CPU layer, always built.
#   * CUDA — GPU acceleration, OFF by default and only enabled when a
#            workload has actually been promoted on profiling evidence
#            (§5.3). Nothing here is promoted yet, correctly so.

project(oxygen_native LANGUAGES CXX VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(OXYGEN_BUILD_PYTHON_MODULE "Build the pybind11 Python extension" ON)
option(OXYGEN_BUILD_TESTS "Build the native C++ test suite" ON)
option(OXYGEN_ENABLE_CUDA "Enable CUDA kernels (only after a §5.3 promotion)" OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# ---------------------------------------------------------------------
# Core library — no Python, no pybind11. Testable on its own (§5.5).
# ---------------------------------------------------------------------
add_library(oxygen_core STATIC src/indicators.cpp)
target_include_directories(oxygen_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(OXYGEN_ENABLE_CUDA)
  enable_language(CUDA)
  target_compile_definitions(oxygen_core PUBLIC OXYGEN_CUDA_ENABLED)
  message(STATUS "Oxygen native: CUDA enabled")
else()
  message(STATUS "Oxygen native: CPU only (no workload promoted to GPU — Passage 1 §5.3)")
endif()

# ---------------------------------------------------------------------
# Python extension module (pybind11 — §5.4's recommended mechanism)
# ---------------------------------------------------------------------
if(OXYGEN_BUILD_PYTHON_MODULE)
  find_package(Python3 COMPONENTS Interpreter Development.Module REQUIRED)

  # Locate pybind11 through the active interpreter, so the build follows
  # whichever venv is in use rather than a system-wide install.
  execute_process(
    COMMAND ${Python3_EXECUTABLE} -m pybind11 --cmakedir
    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE PYBIND11_LOOKUP_RESULT
  )

  if(PYBIND11_LOOKUP_RESULT EQUAL 0)
    list(APPEND CMAKE_PREFIX_PATH "${PYBIND11_CMAKE_DIR}")
    find_package(pybind11 CONFIG REQUIRED)
    pybind11_add_module(oxygen_native src/bindings.cpp)
    target_link_libraries(oxygen_native PRIVATE oxygen_core)
    target_include_directories(oxygen_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    message(STATUS "Oxygen native: building Python extension via pybind11")
  else()
    message(WARNING
      "pybind11 not found — skipping the Python extension. "
      "The API still runs: every native function has a pure-Python "
      "fallback (Passage 1 §5.5). Install it with: pip install pybind11")
  endif()
endif()

# ---------------------------------------------------------------------
# Native test suite — §5.5 requires it to run independently of Python.
# Uses a dependency-free harness rather than fetching Catch2/GoogleTest
# over the network, so `ctest` works in an offline build environment.
# ---------------------------------------------------------------------
if(OXYGEN_BUILD_TESTS)
  enable_testing()
  add_executable(oxygen_native_tests tests/test_indicators.cpp)
  target_link_libraries(oxygen_native_tests PRIVATE oxygen_core)
  target_include_directories(oxygen_native_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
  add_test(NAME oxygen_native_tests COMMAND oxygen_native_tests)
endif()