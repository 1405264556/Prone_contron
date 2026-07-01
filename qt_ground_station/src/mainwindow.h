#pragma once

#include "serialcleanflightclient.h"
#include "wifiufoclient.h"

#include <QFile>
#include <QMainWindow>
#include <QMap>
#include <QVariantMap>

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QPushButton;
class QSlider;
class QSpinBox;
class QTableWidget;
class QTabWidget;

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = nullptr);
    ~MainWindow() override;

private slots:
    void refreshSerialPorts();
    void applyProfile();
    void openUdp();
    void closeUdp();
    void sendHeartbeat();
    void startControl();
    void stopControl();
    void resetControls();
    void updateControlState();
    void requestTakeoff();
    void requestLand();
    void requestHardStop();
    void openSerial();
    void closeSerial();
    void sendCliLine();
    void updateTelemetry(const QVariantMap &values);
    void updateVideo(const QImage &frame);
    void updateVideoStats(int packets, double kbps, int frames);
    void appendLog(const QString &message);
    void appendSerialText(const QString &text);

private:
    void buildUi();
    void buildConnectionPanel(QVBoxLayout *root);
    QWidget *buildMonitorTab();
    QWidget *buildControlTab();
    QWidget *buildSerialTab();
    QWidget *buildSafetyTab();
    void connectSignals();
    void setupLogFile();
    void setMetric(const QString &name, const QString &value);
    bool ensureControlUnlocked(const QString &action);
    void setControlWidgetsEnabled(bool enabled);
    QString runtimeRoot() const;

    WifiUfoClient m_wifi;
    SerialCleanflightClient m_serial;
    QFile m_logFile;
    QMap<QString, int> m_metricRows;

    QLineEdit *m_ipEdit = nullptr;
    QSpinBox *m_remoteUdpSpin = nullptr;
    QSpinBox *m_localUdpSpin = nullptr;
    QComboBox *m_profileCombo = nullptr;
    QLabel *m_udpStatusLabel = nullptr;

    QComboBox *m_serialPortCombo = nullptr;
    QSpinBox *m_baudSpin = nullptr;
    QLabel *m_serialStatusLabel = nullptr;

    QLabel *m_videoLabel = nullptr;
    QLabel *m_videoStatsLabel = nullptr;
    QTableWidget *m_metricTable = nullptr;
    QPlainTextEdit *m_logEdit = nullptr;
    QPlainTextEdit *m_serialConsole = nullptr;
    QLineEdit *m_cliEdit = nullptr;

    QCheckBox *m_unlockCheck = nullptr;
    QLabel *m_lockStateLabel = nullptr;
    QSlider *m_rollSlider = nullptr;
    QSlider *m_pitchSlider = nullptr;
    QSlider *m_throttleSlider = nullptr;
    QSlider *m_yawSlider = nullptr;
    QLabel *m_rollValueLabel = nullptr;
    QLabel *m_pitchValueLabel = nullptr;
    QLabel *m_throttleValueLabel = nullptr;
    QLabel *m_yawValueLabel = nullptr;
    QPushButton *m_startControlButton = nullptr;
    QPushButton *m_stopControlButton = nullptr;
};

