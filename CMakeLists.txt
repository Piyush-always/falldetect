#
# falldetect-gkl - wearable fall detection firmware.
#
# This is the product firmware and lives at the repository root: CMakeLists.txt,
# prj.conf and src/ sit here, not under apps/.
#
# apps/ holds bench instruments only (blink, mic_record). They are separate
# Zephyr applications with their own CMakeLists.txt and prj.conf, and are not
# built as part of this one.
#
cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(falldetect_gkl)

target_sources(app PRIVATE
  src/main.c
  src/imu/imu_ff.c
)

# Lets sources include "imu/imu_ff.h" rather than relative paths.
target_include_directories(app PRIVATE src)
