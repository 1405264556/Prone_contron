#include "wifiufoclient.h"

#include <QDateTime>
#include <QHostAddress>
#include <QImageReader>
#include <QUdpSocket>

namespace {
constexpr int kControlIntervalMs = 50;
constexpr int kBurstDurationMs = 1000;
constexpr int kVideoFragmentHeaderOffset = 47;
constexpr int kVideoPayloadOffset = 54;
constexpr int kMaxVideoFrameBytes = 2 * 1024 * 1024;
const QByteArray kHeartbeat = QByteArray::fromHex("63630100000000");
const QByteArray kControlTemplate = QByteArray::fromHex("63630a000008006680808080000099");

int clampByte(int value)
{
    return qBound(0, value, 255);
}

QString bytesPreview(const QByteArray &data, int maxBytes = 80)
{
    return QString::fromLatin1(data.left(maxBytes).toHex(' '));
}
}

WifiUfoClient::WifiUfoClient(QObject *parent)
    : QObject(parent)
{
    m_controlTimer.setInterval(kControlIntervalMs);
    connect(&m_controlTimer, &QTimer::timeout, this, &WifiUfoClient::sendControlTick);

    m_burstTimer.setInterval(kControlIntervalMs);
    connect(&m_burstTimer, &QTimer::timeout, this, &WifiUfoClient::sendBurstTick);
}

void WifiUfoClient::setEndpoint(const QString &host, quint16 remotePort, quint16 localPort)
{
    m_host = host.trimmed();
    m_remotePort = remotePort;
    m_localPort = localPort;
}

bool WifiUfoClient::isOpen() const
{
    return m_socket && m_socket->state() == QAbstractSocket::BoundState;
}

WifiUfoClient::ControlState WifiUfoClient::controlState() const
{
    return m_control;
}

bool WifiUfoClient::open()
{
    if (isOpen()) {
        return true;
    }

    close();
    m_socket = new QUdpSocket(this);
    connect(m_socket, &QUdpSocket::readyRead, this, &WifiUfoClient::readPendingDatagrams);

    const bool ok = m_socket->bind(QHostAddress::AnyIPv4, m_localPort,
                                  QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint);
    if (!ok) {
        emit logMessage(QStringLiteral("UDP 绑定失败：0.0.0.0:%1，%2")
                            .arg(m_localPort)
                            .arg(m_socket->errorString()));
        emit statusChanged(QStringLiteral("UDP 绑定失败"));
        m_socket->deleteLater();
        m_socket = nullptr;
        return false;
    }

    m_videoPackets = 0;
    m_videoFrames = 0;
    m_videoBytes = 0;
    m_frameBuffer.clear();
    m_frameOpen = false;
    m_frameKey.clear();
    m_expectedFragment = 1;
    ensureStatsTimer();

    emit logMessage(QStringLiteral("UDP 已打开：本地 0.0.0.0:%1 -> 无人机 %2:%3")
                        .arg(m_localPort)
                        .arg(m_host)
                        .arg(m_remotePort));
    emit statusChanged(QStringLiteral("UDP 已连接"));
    return true;
}

void WifiUfoClient::close()
{
    m_controlTimer.stop();
    m_burstTimer.stop();
    if (m_socket) {
        m_socket->close();
        m_socket->deleteLater();
        m_socket = nullptr;
    }
    emit statusChanged(QStringLiteral("UDP 已断开"));
}

void WifiUfoClient::sendHeartbeat()
{
    if (!open()) {
        return;
    }
    const qint64 written = m_socket->writeDatagram(kHeartbeat, QHostAddress(m_host), m_remotePort);
    if (written == kHeartbeat.size()) {
        emit logMessage(QStringLiteral("发送 WiFi UFO 心跳：%1").arg(QString::fromLatin1(kHeartbeat.toHex())));
    } else {
        emit logMessage(QStringLiteral("心跳发送失败：%1").arg(m_socket->errorString()));
    }
}

void WifiUfoClient::setControlState(int roll, int pitch, int throttle, int yaw)
{
    m_control.roll = clampByte(roll);
    m_control.pitch = clampByte(pitch);
    m_control.throttle = clampByte(throttle);
    m_control.yaw = clampByte(yaw);
}

void WifiUfoClient::startControlStream()
{
    if (!open()) {
        return;
    }
    if (!m_controlTimer.isActive()) {
        m_controlTimer.start();
        emit logMessage(QStringLiteral("控制流已启动：%1 ms 周期").arg(kControlIntervalMs));
        emit statusChanged(QStringLiteral("控制流发送中"));
    }
}

void WifiUfoClient::stopControlStream()
{
    m_controlTimer.stop();
    emit logMessage(QStringLiteral("控制流已停止"));
    emit statusChanged(isOpen() ? QStringLiteral("UDP 已连接") : QStringLiteral("UDP 已断开"));
}

void WifiUfoClient::sendTakeoffBurst()
{
    m_burstMode = 1;
    m_burstLabel = QStringLiteral("起飞");
    m_burstElapsed.restart();
    m_burstTimer.start();
    emit logMessage(QStringLiteral("开始发送起飞指令脉冲 1 秒"));
}

void WifiUfoClient::sendLandBurst()
{
    m_burstMode = 2;
    m_burstLabel = QStringLiteral("降落");
    m_burstElapsed.restart();
    m_burstTimer.start();
    emit logMessage(QStringLiteral("开始发送降落指令脉冲 1 秒"));
}

void WifiUfoClient::sendHardStopBurst()
{
    m_burstMode = 4;
    m_burstLabel = QStringLiteral("急停");
    m_burstElapsed.restart();
    m_burstTimer.start();
    emit logMessage(QStringLiteral("开始发送急停指令脉冲 1 秒"));
}

void WifiUfoClient::readPendingDatagrams()
{
    if (!m_socket) {
        return;
    }

    while (m_socket->hasPendingDatagrams()) {
        QHostAddress sender;
        quint16 senderPort = 0;
        QByteArray data;
        data.resize(int(m_socket->pendingDatagramSize()));
        m_socket->readDatagram(data.data(), data.size(), &sender, &senderPort);
        parseDatagram(data, sender, senderPort);
    }
}

void WifiUfoClient::sendControlTick()
{
    sendControlPacket(0, QStringLiteral("手动控制"));
}

void WifiUfoClient::sendBurstTick()
{
    if (m_burstElapsed.elapsed() > kBurstDurationMs) {
        m_burstTimer.stop();
        emit logMessage(QStringLiteral("%1 指令脉冲结束").arg(m_burstLabel));
        return;
    }
    sendControlPacket(m_burstMode, m_burstLabel);
}

QByteArray WifiUfoClient::makeControlPacket(int mode) const
{
    QByteArray packet = kControlTemplate;
    packet[8] = char(m_control.roll);
    packet[9] = char(m_control.pitch);
    packet[10] = char(m_control.throttle);
    packet[11] = char(m_control.yaw);
    packet[12] = char(clampByte(mode));
    packet[13] = char(controlChecksum(packet));
    return packet;
}

quint8 WifiUfoClient::controlChecksum(const QByteArray &packet) const
{
    quint8 checksum = 0;
    for (int i = 8; i <= 12 && i < packet.size(); ++i) {
        checksum ^= quint8(packet.at(i));
    }
    return checksum;
}

void WifiUfoClient::parseDatagram(const QByteArray &data, const QHostAddress &sender, quint16 senderPort)
{
    if (data.startsWith("cc")) {
        parseWifiUfoPacket(data, sender, senderPort);
        return;
    }

    QVariantMap info;
    info.insert(QStringLiteral("格式"), QStringLiteral("未知 UDP"));
    info.insert(QStringLiteral("来源"), QStringLiteral("%1:%2").arg(sender.toString()).arg(senderPort));
    info.insert(QStringLiteral("长度"), data.size());
    info.insert(QStringLiteral("预览"), bytesPreview(data));
    emit infoReceived(info);
    emit logMessage(QStringLiteral("收到未知 UDP：%1:%2 len=%3 %4")
                        .arg(sender.toString())
                        .arg(senderPort)
                        .arg(data.size())
                        .arg(bytesPreview(data, 24)));
}

void WifiUfoClient::parseWifiUfoPacket(const QByteArray &data, const QHostAddress &sender, quint16 senderPort)
{
    const int type = data.size() > 2 ? quint8(data.at(2)) : -1;
    if (type == 1) {
        const QByteArray ssidBytes = data.mid(7, 64).split('\0').value(0);
        QVariantMap info;
        info.insert(QStringLiteral("格式"), QStringLiteral("WiFi UFO 信息包"));
        info.insert(QStringLiteral("来源"), QStringLiteral("%1:%2").arg(sender.toString()).arg(senderPort));
        info.insert(QStringLiteral("SSID"), QString::fromLatin1(ssidBytes));
        info.insert(QStringLiteral("包类型"), type);
        info.insert(QStringLiteral("长度"), data.size());
        info.insert(QStringLiteral("预览"), bytesPreview(data));
        emit infoReceived(info);
        emit logMessage(QStringLiteral("收到 WiFi UFO 信息包：SSID=%1 len=%2")
                            .arg(QString::fromLatin1(ssidBytes))
                            .arg(data.size()));
        return;
    }

    if (type == 3) {
        handleVideoPacket(data);
        return;
    }

    QVariantMap info;
    info.insert(QStringLiteral("格式"), QStringLiteral("WiFi UFO 数据包"));
    info.insert(QStringLiteral("来源"), QStringLiteral("%1:%2").arg(sender.toString()).arg(senderPort));
    info.insert(QStringLiteral("包类型"), type);
    info.insert(QStringLiteral("长度"), data.size());
    info.insert(QStringLiteral("预览"), bytesPreview(data));
    emit infoReceived(info);
}

void WifiUfoClient::handleVideoPacket(const QByteArray &data)
{
    ++m_videoPackets;
    m_videoBytes += data.size();

    const int soi = data.indexOf(QByteArray::fromHex("ffd8"));
    const bool hasFragmentHeader = data.size() >= kVideoPayloadOffset
        && quint8(data.at(kVideoFragmentHeaderOffset)) == 1;

    if (hasFragmentHeader) {
        const int fragmentIndex = quint8(data.at(kVideoFragmentHeaderOffset + 1));
        const int totalFragments = quint8(data.at(kVideoFragmentHeaderOffset + 3));
        int payloadLength = quint8(data.at(kVideoFragmentHeaderOffset + 5))
            | (quint8(data.at(kVideoFragmentHeaderOffset + 6)) << 8);
        if (payloadLength <= 0 || payloadLength > data.size() - kVideoPayloadOffset) {
            payloadLength = data.size() - kVideoPayloadOffset;
        }

        if (fragmentIndex >= 1 && totalFragments >= fragmentIndex) {
            const QByteArray frameKey = data.mid(8, 6);
            const QByteArray payload = data.mid(kVideoPayloadOffset, payloadLength);
            if (fragmentIndex <= 1 || soi >= 0) {
                const int start = soi >= 0 ? soi : kVideoPayloadOffset;
                const int length = qMax(0, kVideoPayloadOffset + payloadLength - start);
                m_frameBuffer = data.mid(start, length);
                m_frameOpen = true;
                m_frameKey = frameKey;
                m_expectedFragment = fragmentIndex + 1;
            } else if (m_frameOpen && frameKey == m_frameKey && fragmentIndex == m_expectedFragment) {
                m_frameBuffer.append(payload);
                ++m_expectedFragment;
            } else if (m_frameOpen) {
                m_frameBuffer.clear();
                m_frameOpen = false;
                m_frameKey.clear();
                m_expectedFragment = 1;
            }
        }
    } else if (soi >= 0) {
        m_frameBuffer = data.mid(soi);
        m_frameOpen = true;
        m_frameKey.clear();
        m_expectedFragment = 1;
    } else if (m_frameOpen) {
        const int payloadOffset = qMin(kVideoPayloadOffset, data.size());
        m_frameBuffer.append(data.mid(payloadOffset));
    }

    if (m_frameOpen) {
        const int eoi = m_frameBuffer.indexOf(QByteArray::fromHex("ffd9"));
        if (eoi >= 0) {
            const QByteArray jpg = m_frameBuffer.left(eoi + 2);
            QImage frame;
            frame.loadFromData(jpg, "JPG");
            if (!frame.isNull()) {
                ++m_videoFrames;
                emit videoFrameReady(frame);
            }
            m_frameBuffer.clear();
            m_frameOpen = false;
            m_frameKey.clear();
            m_expectedFragment = 1;
        } else if (m_frameBuffer.size() > kMaxVideoFrameBytes) {
            m_frameBuffer.clear();
            m_frameOpen = false;
            m_frameKey.clear();
            m_expectedFragment = 1;
            emit logMessage(QStringLiteral("视频帧缓存过大，已丢弃当前帧"));
        }
    }

    if (m_videoPackets == 1 || m_videoPackets % 30 == 0) {
        ensureStatsTimer();
        const double seconds = qMax(0.001, m_statsElapsed.elapsed() / 1000.0);
        const double kbps = (m_videoBytes / 1024.0) / seconds;
        emit videoStatsChanged(m_videoPackets, kbps, m_videoFrames);
    }
}

void WifiUfoClient::sendControlPacket(int mode, const QString &label)
{
    if (!open()) {
        return;
    }

    const QByteArray packet = makeControlPacket(mode);
    const qint64 written = m_socket->writeDatagram(packet, QHostAddress(m_host), m_remotePort);
    if (written == packet.size()) {
        emit controlPacketSent(QStringLiteral("%1 r=%2 p=%3 t=%4 y=%5 m=%6")
                                   .arg(label)
                                   .arg(m_control.roll)
                                   .arg(m_control.pitch)
                                   .arg(m_control.throttle)
                                   .arg(m_control.yaw)
                                   .arg(mode));
    } else {
        emit logMessage(QStringLiteral("控制包发送失败：%1").arg(m_socket->errorString()));
    }
}

void WifiUfoClient::ensureStatsTimer()
{
    if (!m_statsElapsed.isValid()) {
        m_statsElapsed.start();
    }
}
