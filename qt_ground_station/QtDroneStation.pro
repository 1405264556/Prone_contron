QT += core gui widgets network serialport

CONFIG += c++17
CONFIG -= app_bundle

TEMPLATE = app
TARGET = QtDroneStation

SOURCES += \
    src/main.cpp \
    src/mainwindow.cpp \
    src/serialcleanflightclient.cpp \
    src/wifiufoclient.cpp

HEADERS += \
    src/mainwindow.h \
    src/serialcleanflightclient.h \
    src/wifiufoclient.h

DESTDIR = $$PWD/bin
OBJECTS_DIR = $$PWD/build/obj
MOC_DIR = $$PWD/build/moc
RCC_DIR = $$PWD/build/rcc
UI_DIR = $$PWD/build/ui

