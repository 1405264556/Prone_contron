#include "serialcleanflightclient.h"

#include <QIODevice>
#include <QRegularExpression>
#include <QSerialPortInfo>

SerialCleanflightClient::SerialCleanflightClient(QObject *parent)
    : QObject(parent)
{
    connect(&m_serial, &QSerialPort::readyRead, this, &SerialCleanflightClient::readReady);
}

QStringList SerialCleanflightClient::availablePorts()
{
    QStringList ports;
    const auto infos = QSerialPortInfo::availablePorts();
    for (const QSerialPortInfo &info : infos) {
        QString label = info.portName();
        if (!info.description().isEmpty()) {
            label += QStringLiteral(" - ") + info.description();
        }
        ports << label;
    }
    return ports;
}

bool SerialCleanflightClient::isOpen() const
{
    return m_serial.isOpen();
}

bool SerialCleanflightClient::open(const QString &portLabel, int baudRate)
{
    close();
    const QString portName = portLabel.section(QStringLiteral(" - "), 0, 0).trimmed();
    m_serial.setPortName(portName);
    m_serial.setBaudRate(baudRate);
    m_serial.setDataBits(QSerialPort::Data8);
    m_serial.setParity(QSerialPort::NoParity);
    m_serial.setStopBits(QSerialPort::OneStop);
    m_serial.setFlowControl(QSerialPort::NoFlowControl);

    if (!m_serial.open(QIODevice::ReadWrite)) {
        emit logMessage(QStringLiteral("串口打开失败：%1，%2").arg(portName, m_serial.errorString()));
        emit statusChanged(QStringLiteral("串口失败"));
        return false;
    }

    emit logMessage(QStringLiteral("串口已打开：%1 @ %2 8N1").arg(portName).arg(baudRate));
    emit statusChanged(QStringLiteral("串口已连接"));
    return true;
}

void SerialCleanflightClient::close()
{
    if (m_serial.isOpen()) {
        m_serial.close();
        emit logMessage(QStringLiteral("串口已关闭"));
    }
    emit statusChanged(QStringLiteral("串口未连接"));
}

void SerialCleanflightClient::sendLine(const QString &line)
{
    if (!m_serial.isOpen()) {
        emit logMessage(QStringLiteral("串口未连接，无法发送：%1").arg(line));
        return;
    }
    const QByteArray payload = line.trimmed().toUtf8() + "\r\n";
    m_serial.write(payload);
    emit logMessage(QStringLiteral("CLI TX：%1").arg(line.trimmed()));
}

void SerialCleanflightClient::requestVersion()
{
    sendLine(QStringLiteral("version"));
}

void SerialCleanflightClient::requestStatus()
{
    sendLine(QStringLiteral("status"));
}

void SerialCleanflightClient::requestDump()
{
    sendLine(QStringLiteral("dump"));
}

void SerialCleanflightClient::readReady()
{
    m_buffer.append(m_serial.readAll());
    const QString text = QString::fromUtf8(m_buffer);
    if (text.contains('\n') || m_buffer.size() > 4096) {
        m_buffer.clear();
        emit textReceived(text);
        parseText(text);
    }
}

void SerialCleanflightClient::parseText(const QString &text)
{
    QVariantMap telemetry;

    static const QRegularExpression versionRe(QStringLiteral("(Cleanflight/[^\\r\\n]+)"));
    const QRegularExpressionMatch versionMatch = versionRe.match(text);
    if (versionMatch.hasMatch()) {
        telemetry.insert(QStringLiteral("飞控固件"), versionMatch.captured(1).trimmed());
    }

    static const QRegularExpression voltageRe(QStringLiteral("Voltage:\\s*(\\d+)\\s*\\*\\s*0\\.1V"));
    const QRegularExpressionMatch voltageMatch = voltageRe.match(text);
    if (voltageMatch.hasMatch()) {
        const double voltage = voltageMatch.captured(1).toDouble() * 0.1;
        telemetry.insert(QStringLiteral("电压"), QStringLiteral("%1 V").arg(voltage, 0, 'f', 1));
    }

    static const QRegularExpression cpuRe(QStringLiteral("CPU Clock=(\\d+MHz),\\s*GYRO=([^,\\r\\n]+),\\s*ACC=([^\\r\\n]+)"));
    const QRegularExpressionMatch cpuMatch = cpuRe.match(text);
    if (cpuMatch.hasMatch()) {
        telemetry.insert(QStringLiteral("CPU"), cpuMatch.captured(1));
        telemetry.insert(QStringLiteral("陀螺仪"), cpuMatch.captured(2).trimmed());
        telemetry.insert(QStringLiteral("加速度计"), cpuMatch.captured(3).trimmed());
    }

    static const QRegularExpression uptimeRe(QStringLiteral("System Uptime:\\s*(\\d+)\\s*seconds"));
    const QRegularExpressionMatch uptimeMatch = uptimeRe.match(text);
    if (uptimeMatch.hasMatch()) {
        telemetry.insert(QStringLiteral("飞控运行时间"), QStringLiteral("%1 s").arg(uptimeMatch.captured(1)));
    }

    static const QRegularExpression i2cRe(QStringLiteral("I2C Errors:\\s*(\\d+)"));
    const QRegularExpressionMatch i2cMatch = i2cRe.match(text);
    if (i2cMatch.hasMatch()) {
        telemetry.insert(QStringLiteral("I2C 错误"), i2cMatch.captured(1));
    }

    if (!telemetry.isEmpty()) {
        emit telemetryChanged(telemetry);
    }
}
