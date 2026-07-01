cmake_minimum_required(VERSION 3.21)

project(QtDroneStation VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Network SerialPort)

qt_add_executable(QtDroneStation
    src/main.cpp
    src/mainwindow.cpp
    src/mainwindow.h
    src/serialcleanflightclient.cpp
    src/serialcleanflightclient.h
    src/wifiufoclient.cpp
    src/wifiufoclient.h
)

target_link_libraries(QtDroneStation PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Widgets
    Qt6::Network
    Qt6::SerialPort
)

if (WIN32)
    set_target_properties(QtDroneStation PROPERTIES WIN32_EXECUTABLE TRUE)
endif()

