cmake_minimum_required(VERSION 3.15)
project(duxbot_bridge CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Path to a checkout/installation of the Booster Robotics SDK built for aarch64.
# The SDK is NOT included in this repository; obtain it from Booster Robotics.
set(SDK_ROOT "/opt/duxbot/sdk" CACHE PATH "Booster Robotics SDK root")

include_directories(BEFORE ${SDK_ROOT}/include)

link_directories(
    ${SDK_ROOT}/lib/aarch64
    ${SDK_ROOT}/third_party/lib/aarch64
)

add_executable(duxbot_bridge duxbot_bridge.cpp)

target_link_libraries(duxbot_bridge
    ${SDK_ROOT}/lib/aarch64/libbooster_robotics_sdk.a
    fastrtps
    fastcdr
    ${SDK_ROOT}/third_party/lib/aarch64/libfoonathan_memory-0.7.3.a
    pthread
)
