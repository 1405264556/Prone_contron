#pragma once

#include <QByteArray>
#include <QElapsedTimer>
#include <QHostAddress>
#include <QImage>
#include <QObject>
#include <QTimer>
#include <QVariantMap>

class QUdpSocket;

class WifiUfoClient : public QObject
{
    Q_OBJECT

public:
    struct ControlState {
        int roll = 128;
        int pitch = 128;
        int throttle = 0;
        int yaw = 128;
    };

    explicit WifiUfoClient(QObject *parent = nullptr);

    void setEndpoint(const QString &host, quint16 remotePort, quint16 localPort);
    bool isOpen() const;
    ControlState controlState() const;

public slots:
    bool open();
    void close();
    void sendHeartbeat();
    void setControlState(int roll, int pitch, int throttle, int yaw);
    void startControlStream();
    void stopControlStream();
    void sendTakeoffBurst();
    void sendLandBurst();
    void sendHardStopBurst();

signals:
    void logMessage(const QString &message);
    void statusChanged(const QString &status);
    void infoReceived(const QVariantMap &info);
    void videoFrameReady(const QImage &frame);
    void videoStatsChanged(int packets, double kilobytesPerSecond, int frames);
    void controlPacketSent(const QString &summary);

private slots:
    void readPendingDatagrams();
    void sendControlTick();
    void sendBurstTick();

private:
    QByteArray makeControlPacket(int mode) const;
    quint8 controlChecksum(const QByteArray &packet) const;
    void parseDatagram(const QByteArray &data, const QHostAddress &sender, quint16 senderPort);
    void parseWifiUfoPacket(const QByteArray &data, const QHostAddress &sender, quint16 senderPort);
    void handleVideoPacket(const QByteArray &data);
    void sendControlPacket(int mode, const QString &label);
    void ensureStatsTimer();

    QString m_host = QStringLiteral("192.168.0.1");
    quint16 m_remotePort = 40000;
    quint16 m_localPort = 40000;
    QUdpSocket *m_socket = nullptr;

    ControlState m_control;
    QTimer m_controlTimer;
    QTimer m_burstTimer;
    QElapsedTimer m_burstElapsed;
    int m_burstMode = 0;
    QString m_burstLabel;

    QByteArray m_frameBuffer;
    bool m_frameOpen = false;
    QByteArray m_frameKey;
    int m_expectedFragment = 1;
    int m_videoPackets = 0;
    int m_videoFrames = 0;
    qint64 m_videoBytes = 0;
    QElapsedTimer m_statsElapsed;
};
