cmake_minimum_required(VERSION 3.10)
project(arctic_sim_plugins)
find_package(gazebo REQUIRED)
include_directories(${GAZEBO_INCLUDE_DIRS})
link_directories(${GAZEBO_LIBRARY_DIRS})
list(APPEND CMAKE_CXX_FLAGS "${GAZEBO_CXX_FLAGS}")
set(CMAKE_CXX_STANDARD 17)
add_library(VesselPathPlugin SHARED VesselPathPlugin.cc)
target_link_libraries(VesselPathPlugin ${GAZEBO_LIBRARIES})

# MJPEG camera streaming. CameraPlugin comes from Gazebo's own plugin set, and
# the encoder is libjpeg, which is already in the image — no gstreamer, no
# H.264, deliberately (see the header of CameraStreamPlugin.cc).
find_package(JPEG REQUIRED)
add_library(CameraStreamPlugin SHARED CameraStreamPlugin.cc)
target_include_directories(CameraStreamPlugin PRIVATE ${JPEG_INCLUDE_DIR})
target_link_libraries(CameraStreamPlugin
    ${GAZEBO_LIBRARIES} CameraPlugin ${JPEG_LIBRARIES} pthread)
