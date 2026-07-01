#include "mainwindow.h"

#include <QApplication>
#include <QDir>
#include <QFont>
#include <QLocale>
#include <QTranslator>

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    QApplication::setOrganizationName("AIProject");
    QApplication::setApplicationName("Qt Drone Ground Station");
    QApplication::setApplicationVersion("0.1");

    QLocale::setDefault(QLocale(QLocale::Chinese, QLocale::China));

    QFont font = QApplication::font();
    font.setFamily("Microsoft YaHei UI");
    font.setPointSize(9);
    QApplication::setFont(font);

    MainWindow window;
    window.show();

    return app.exec();
}

