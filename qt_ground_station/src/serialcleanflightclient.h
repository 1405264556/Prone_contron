#pragma once

#include <QObject>
#include <QSerialPort>
#include <QStringList>
#include <QVariantMap>

class SerialCleanflightClient : public QObject
{
    Q_OBJECT

public:
    explicit SerialCleanflightClient(QObject *parent = nullptr);

    static QStringList availablePorts();
    bool isOpen() const;

public slots:
    bool open(const QString &portName, int baudRate);
    void close();
    void sendLine(const QString &line);
    void requestVersion();
    void requestStatus();
    void requestDump();

signals:
    void logMessage(const QString &message);
    void statusChanged(const QString &status);
    void textReceived(const QString &text);
    void telemetryChanged(const QVariantMap &telemetry);

private slots:
    void readReady();

private:
    void parseText(const QString &text);

    QSerialPort m_serial;
    QByteArray m_buffer;
};
