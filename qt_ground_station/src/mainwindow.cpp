#include "mainwindow.h"

#include <QApplication>
#include <QAbstractItemView>
#include <QBoxLayout>
#include <QCheckBox>
#include <QComboBox>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFileDialog>
#include <QGroupBox>
#include <QGridLayout>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QImage>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSlider>
#include <QSpinBox>
#include <QSplitter>
#include <QStatusBar>
#include <QTableWidget>
#include <QTableWidgetItem>
#include <QTabWidget>
#include <QTextStream>
#include <QVBoxLayout>

namespace {
QLabel *makeCardLabel(const QString &text)
{
    auto *label = new QLabel(text);
    label->setAlignment(Qt::AlignCenter);
    label->setMinimumHeight(30);
    label->setStyleSheet(QStringLiteral(
        "QLabel { background:#f5f7fb; border:1px solid #d5dce8; border-radius:4px; padding:6px; }"));
    return label;
}

QPushButton *makeButton(const QString &text)
{
    auto *button = new QPushButton(text);
    button->setMinimumHeight(30);
    return button;
}

void configureSlider(QSlider *slider, int min, int max, int value)
{
    slider->setOrientation(Qt::Horizontal);
    slider->setRange(min, max);
    slider->setValue(value);
    slider->setTickInterval(16);
    slider->setTickPosition(QSlider::TicksBelow);
}
}

MainWindow::MainWindow(QWidget *parent)
    : QMainWindow(parent)
{
    setupLogFile();
    buildUi();
    connectSignals();
    refreshSerialPorts();
    applyProfile();
    resetControls();
    appendLog(QStringLiteral("Qt6 中文上位机已启动"));
}

MainWindow::~MainWindow()
{
    m_wifi.close();
    m_serial.close();
    if (m_logFile.isOpen()) {
        m_logFile.close();
    }
}

void MainWindow::buildUi()
{
    setWindowTitle(QStringLiteral("无人机 Qt6 可视化上位机"));
    resize(1320, 820);
    setMinimumSize(1160, 720);

    setStyleSheet(QStringLiteral(
        "QMainWindow { background:#eef2f7; }"
        "QGroupBox { font-weight:600; border:1px solid #cfd8e5; border-radius:6px; margin-top:10px; background:#ffffff; }"
        "QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; }"
        "QPushButton { background:#ffffff; border:1px solid #b9c4d4; border-radius:4px; padding:5px 10px; }"
        "QPushButton:hover { background:#edf5ff; border-color:#5b8def; }"
        "QPushButton:pressed { background:#dbeafe; }"
        "QPushButton:disabled { color:#9aa4b2; background:#f4f5f7; }"
        "QLineEdit, QSpinBox, QComboBox { background:#ffffff; border:1px solid #b9c4d4; border-radius:4px; padding:4px; }"
        "QTabWidget::pane { border:1px solid #cfd8e5; background:#ffffff; }"
        "QTabBar::tab { padding:8px 14px; }"
        "QTabBar::tab:selected { background:#ffffff; border:1px solid #cfd8e5; border-bottom:none; }"));

    auto *central = new QWidget(this);
    auto *root = new QVBoxLayout(central);
    root->setContentsMargins(10, 10, 10, 10);
    root->setSpacing(8);

    buildConnectionPanel(root);

    auto *splitter = new QSplitter(Qt::Horizontal, central);
    splitter->setChildrenCollapsible(false);

    auto *left = new QWidget(splitter);
    auto *leftLayout = new QVBoxLayout(left);
    leftLayout->setContentsMargins(0, 0, 0, 0);

    auto *videoGroup = new QGroupBox(QStringLiteral("图传预览 / 状态"));
    auto *videoLayout = new QVBoxLayout(videoGroup);
    m_videoLabel = new QLabel(QStringLiteral("等待 WiFi UFO 视频流"));
    m_videoLabel->setAlignment(Qt::AlignCenter);
    m_videoLabel->setMinimumSize(560, 360);
    m_videoLabel->setStyleSheet(QStringLiteral("QLabel { background:#101820; color:#dce7f7; border-radius:4px; }"));
    m_videoStatsLabel = makeCardLabel(QStringLiteral("视频：未连接"));
    videoLayout->addWidget(m_videoLabel, 1);
    videoLayout->addWidget(m_videoStatsLabel);
    leftLayout->addWidget(videoGroup, 3);

    auto *logGroup = new QGroupBox(QStringLiteral("运行日志"));
    auto *logLayout = new QVBoxLayout(logGroup);
    m_logEdit = new QPlainTextEdit;
    m_logEdit->setReadOnly(true);
    m_logEdit->setMaximumBlockCount(1200);
    logLayout->addWidget(m_logEdit);
    leftLayout->addWidget(logGroup, 2);

    auto *tabs = new QTabWidget(splitter);
    tabs->addTab(buildMonitorTab(), QStringLiteral("监控"));
    tabs->addTab(buildControlTab(), QStringLiteral("控制"));
    tabs->addTab(buildSerialTab(), QStringLiteral("飞控 CLI"));
    tabs->addTab(buildSafetyTab(), QStringLiteral("安全/说明"));

    splitter->addWidget(left);
    splitter->addWidget(tabs);
    splitter->setStretchFactor(0, 3);
    splitter->setStretchFactor(1, 2);

    root->addWidget(splitter, 1);
    setCentralWidget(central);
    statusBar()->showMessage(QStringLiteral("就绪"));
}

void MainWindow::buildConnectionPanel(QVBoxLayout *root)
{
    auto *group = new QGroupBox(QStringLiteral("连接配置"));
    auto *layout = new QGridLayout(group);
    layout->setColumnStretch(11, 1);

    m_profileCombo = new QComboBox;
    m_profileCombo->addItems({QStringLiteral("WiFi UFO UDP"), QStringLiteral("Tello 兼容 UDP"), QStringLiteral("仅串口 Cleanflight")});

    m_ipEdit = new QLineEdit(QStringLiteral("192.168.0.1"));
    m_remoteUdpSpin = new QSpinBox;
    m_remoteUdpSpin->setRange(1, 65535);
    m_remoteUdpSpin->setValue(40000);
    m_localUdpSpin = new QSpinBox;
    m_localUdpSpin->setRange(1, 65535);
    m_localUdpSpin->setValue(40000);

    auto *openUdpButton = makeButton(QStringLiteral("打开 UDP"));
    auto *closeUdpButton = makeButton(QStringLiteral("关闭 UDP"));
    auto *heartbeatButton = makeButton(QStringLiteral("UFO 心跳"));
    m_udpStatusLabel = makeCardLabel(QStringLiteral("UDP 未连接"));

    m_serialPortCombo = new QComboBox;
    m_serialPortCombo->setMinimumWidth(190);
    m_baudSpin = new QSpinBox;
    m_baudSpin->setRange(1200, 2000000);
    m_baudSpin->setValue(115200);
    auto *refreshPortsButton = makeButton(QStringLiteral("刷新串口"));
    auto *openSerialButton = makeButton(QStringLiteral("连接串口"));
    auto *closeSerialButton = makeButton(QStringLiteral("关闭串口"));
    m_serialStatusLabel = makeCardLabel(QStringLiteral("串口未连接"));

    layout->addWidget(new QLabel(QStringLiteral("协议")), 0, 0);
    layout->addWidget(m_profileCombo, 0, 1);
    layout->addWidget(new QLabel(QStringLiteral("无人机 IP")), 0, 2);
    layout->addWidget(m_ipEdit, 0, 3);
    layout->addWidget(new QLabel(QStringLiteral("远端 UDP")), 0, 4);
    layout->addWidget(m_remoteUdpSpin, 0, 5);
    layout->addWidget(new QLabel(QStringLiteral("本地 UDP")), 0, 6);
    layout->addWidget(m_localUdpSpin, 0, 7);
    layout->addWidget(openUdpButton, 0, 8);
    layout->addWidget(closeUdpButton, 0, 9);
    layout->addWidget(heartbeatButton, 0, 10);
    layout->addWidget(m_udpStatusLabel, 0, 11);

    layout->addWidget(new QLabel(QStringLiteral("飞控串口")), 1, 0);
    layout->addWidget(m_serialPortCombo, 1, 1, 1, 3);
    layout->addWidget(new QLabel(QStringLiteral("波特率")), 1, 4);
    layout->addWidget(m_baudSpin, 1, 5);
    layout->addWidget(refreshPortsButton, 1, 6);
    layout->addWidget(openSerialButton, 1, 7);
    layout->addWidget(closeSerialButton, 1, 8);
    layout->addWidget(m_serialStatusLabel, 1, 9, 1, 3);

    connect(openUdpButton, &QPushButton::clicked, this, &MainWindow::openUdp);
    connect(closeUdpButton, &QPushButton::clicked, this, &MainWindow::closeUdp);
    connect(heartbeatButton, &QPushButton::clicked, this, &MainWindow::sendHeartbeat);
    connect(refreshPortsButton, &QPushButton::clicked, this, &MainWindow::refreshSerialPorts);
    connect(openSerialButton, &QPushButton::clicked, this, &MainWindow::openSerial);
    connect(closeSerialButton, &QPushButton::clicked, this, &MainWindow::closeSerial);

    root->addWidget(group);
}

QWidget *MainWindow::buildMonitorTab()
{
    auto *page = new QWidget;
    auto *layout = new QVBoxLayout(page);

    auto *quick = new QGridLayout;
    quick->addWidget(makeCardLabel(QStringLiteral("WiFi：WiFiUFO-3BE7F2")), 0, 0);
    quick->addWidget(makeCardLabel(QStringLiteral("飞控：Cleanflight SPRACINGF3")), 0, 1);
    quick->addWidget(makeCardLabel(QStringLiteral("控制：默认锁定")), 0, 2);
    layout->addLayout(quick);

    auto *metricGroup = new QGroupBox(QStringLiteral("遥测 / 解析字段"));
    auto *metricLayout = new QVBoxLayout(metricGroup);
    m_metricTable = new QTableWidget(0, 2);
    m_metricTable->setHorizontalHeaderLabels({QStringLiteral("字段"), QStringLiteral("值")});
    m_metricTable->horizontalHeader()->setStretchLastSection(true);
    m_metricTable->verticalHeader()->setVisible(false);
    m_metricTable->setEditTriggers(QAbstractItemView::NoEditTriggers);
    m_metricTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    metricLayout->addWidget(m_metricTable);
    layout->addWidget(metricGroup, 1);

    setMetric(QStringLiteral("无人机 IP"), QStringLiteral("192.168.0.1"));
    setMetric(QStringLiteral("WiFi 协议"), QStringLiteral("WiFi UFO UDP 40000"));
    setMetric(QStringLiteral("串口飞控"), QStringLiteral("Cleanflight/SPRACINGF3 1.13.0"));
    setMetric(QStringLiteral("安全锁"), QStringLiteral("未解锁"));

    return page;
}

QWidget *MainWindow::buildControlTab()
{
    auto *page = new QWidget;
    auto *layout = new QVBoxLayout(page);

    auto *safetyGroup = new QGroupBox(QStringLiteral("安全锁"));
    auto *safetyLayout = new QHBoxLayout(safetyGroup);
    m_unlockCheck = new QCheckBox(QStringLiteral("我已拆桨/固定机体，并确认允许发送飞行动作"));
    m_lockStateLabel = makeCardLabel(QStringLiteral("控制锁定"));
    safetyLayout->addWidget(m_unlockCheck, 1);
    safetyLayout->addWidget(m_lockStateLabel);
    layout->addWidget(safetyGroup);

    auto *manualGroup = new QGroupBox(QStringLiteral("手动控制量"));
    auto *manualLayout = new QGridLayout(manualGroup);

    m_rollSlider = new QSlider;
    m_pitchSlider = new QSlider;
    m_throttleSlider = new QSlider;
    m_yawSlider = new QSlider;
    configureSlider(m_rollSlider, 0, 255, 128);
    configureSlider(m_pitchSlider, 0, 255, 128);
    configureSlider(m_throttleSlider, 0, 255, 0);
    configureSlider(m_yawSlider, 0, 255, 128);

    m_rollValueLabel = makeCardLabel(QStringLiteral("128"));
    m_pitchValueLabel = makeCardLabel(QStringLiteral("128"));
    m_throttleValueLabel = makeCardLabel(QStringLiteral("0"));
    m_yawValueLabel = makeCardLabel(QStringLiteral("128"));

    manualLayout->addWidget(new QLabel(QStringLiteral("横滚 Roll")), 0, 0);
    manualLayout->addWidget(m_rollSlider, 0, 1);
    manualLayout->addWidget(m_rollValueLabel, 0, 2);
    manualLayout->addWidget(new QLabel(QStringLiteral("俯仰 Pitch")), 1, 0);
    manualLayout->addWidget(m_pitchSlider, 1, 1);
    manualLayout->addWidget(m_pitchValueLabel, 1, 2);
    manualLayout->addWidget(new QLabel(QStringLiteral("油门 Throttle")), 2, 0);
    manualLayout->addWidget(m_throttleSlider, 2, 1);
    manualLayout->addWidget(m_throttleValueLabel, 2, 2);
    manualLayout->addWidget(new QLabel(QStringLiteral("偏航 Yaw")), 3, 0);
    manualLayout->addWidget(m_yawSlider, 3, 1);
    manualLayout->addWidget(m_yawValueLabel, 3, 2);
    manualLayout->setColumnStretch(1, 1);
    layout->addWidget(manualGroup);

    auto *buttons = new QGridLayout;
    m_startControlButton = makeButton(QStringLiteral("启动控制流"));
    m_stopControlButton = makeButton(QStringLiteral("停止控制流"));
    auto *neutralButton = makeButton(QStringLiteral("回中/油门归零"));
    auto *takeoffButton = makeButton(QStringLiteral("起飞"));
    auto *landButton = makeButton(QStringLiteral("降落"));
    auto *hardStopButton = makeButton(QStringLiteral("急停"));
    hardStopButton->setStyleSheet(QStringLiteral("QPushButton { background:#fff1f2; border-color:#fb7185; color:#9f1239; }"));

    buttons->addWidget(m_startControlButton, 0, 0);
    buttons->addWidget(m_stopControlButton, 0, 1);
    buttons->addWidget(neutralButton, 0, 2);
    buttons->addWidget(takeoffButton, 1, 0);
    buttons->addWidget(landButton, 1, 1);
    buttons->addWidget(hardStopButton, 1, 2);
    layout->addLayout(buttons);
    layout->addStretch(1);

    connect(m_startControlButton, &QPushButton::clicked, this, &MainWindow::startControl);
    connect(m_stopControlButton, &QPushButton::clicked, this, &MainWindow::stopControl);
    connect(neutralButton, &QPushButton::clicked, this, &MainWindow::resetControls);
    connect(takeoffButton, &QPushButton::clicked, this, &MainWindow::requestTakeoff);
    connect(landButton, &QPushButton::clicked, this, &MainWindow::requestLand);
    connect(hardStopButton, &QPushButton::clicked, this, &MainWindow::requestHardStop);

    const QList<QSlider *> sliders = {m_rollSlider, m_pitchSlider, m_throttleSlider, m_yawSlider};
    for (QSlider *slider : sliders) {
        connect(slider, &QSlider::valueChanged, this, &MainWindow::updateControlState);
    }
    connect(m_unlockCheck, &QCheckBox::toggled, this, [this](bool checked) {
        m_lockStateLabel->setText(checked ? QStringLiteral("控制已解锁") : QStringLiteral("控制锁定"));
        setMetric(QStringLiteral("安全锁"), checked ? QStringLiteral("已解锁") : QStringLiteral("未解锁"));
        appendLog(checked ? QStringLiteral("安全锁已手动解锁") : QStringLiteral("安全锁已锁定"));
    });

    return page;
}

QWidget *MainWindow::buildSerialTab()
{
    auto *page = new QWidget;
    auto *layout = new QVBoxLayout(page);

    auto *buttons = new QHBoxLayout;
    auto *versionButton = makeButton(QStringLiteral("version"));
    auto *statusButton = makeButton(QStringLiteral("status"));
    auto *dumpButton = makeButton(QStringLiteral("dump"));
    m_cliEdit = new QLineEdit;
    m_cliEdit->setPlaceholderText(QStringLiteral("输入 Cleanflight CLI 命令，例如 help / version / status"));
    auto *sendButton = makeButton(QStringLiteral("发送 CLI"));
    buttons->addWidget(versionButton);
    buttons->addWidget(statusButton);
    buttons->addWidget(dumpButton);
    buttons->addWidget(m_cliEdit, 1);
    buttons->addWidget(sendButton);
    layout->addLayout(buttons);

    m_serialConsole = new QPlainTextEdit;
    m_serialConsole->setReadOnly(true);
    m_serialConsole->setMaximumBlockCount(3000);
    layout->addWidget(m_serialConsole, 1);

    connect(versionButton, &QPushButton::clicked, &m_serial, &SerialCleanflightClient::requestVersion);
    connect(statusButton, &QPushButton::clicked, &m_serial, &SerialCleanflightClient::requestStatus);
    connect(dumpButton, &QPushButton::clicked, &m_serial, &SerialCleanflightClient::requestDump);
    connect(sendButton, &QPushButton::clicked, this, &MainWindow::sendCliLine);
    connect(m_cliEdit, &QLineEdit::returnPressed, this, &MainWindow::sendCliLine);

    return page;
}

QWidget *MainWindow::buildSafetyTab()
{
    auto *page = new QWidget;
    auto *layout = new QVBoxLayout(page);
    auto *text = new QLabel(QStringLiteral(
        "使用步骤：\n"
        "1. 先拆桨或固定机体，再连接 WiFiUFO-3BE7F2。\n"
        "2. WiFi 监控选择 WiFi UFO UDP，IP=192.168.0.1，端口=40000。\n"
        "3. 点击“UFO 心跳”后应收到信息包和视频分片。\n"
        "4. 串口监控选择 COM9/115200，可读取 Cleanflight CLI 状态。\n"
        "5. 飞行动作默认锁定；只有确认安全并勾选安全锁后才会发送控制流。\n\n"
        "已知硬件线索：WiFi UFO 图传/控制模块 + Cleanflight SPRACINGF3 1.13.0 飞控。"
    ));
    text->setWordWrap(true);
    text->setAlignment(Qt::AlignTop | Qt::AlignLeft);
    text->setStyleSheet(QStringLiteral("QLabel { background:#ffffff; padding:12px; border:1px solid #cfd8e5; border-radius:6px; }"));
    layout->addWidget(text);
    layout->addStretch(1);
    return page;
}

void MainWindow::connectSignals()
{
    connect(m_profileCombo, &QComboBox::currentTextChanged, this, &MainWindow::applyProfile);

    connect(&m_wifi, &WifiUfoClient::logMessage, this, &MainWindow::appendLog);
    connect(&m_wifi, &WifiUfoClient::statusChanged, this, [this](const QString &status) {
        m_udpStatusLabel->setText(status);
        statusBar()->showMessage(status, 3000);
    });
    connect(&m_wifi, &WifiUfoClient::infoReceived, this, &MainWindow::updateTelemetry);
    connect(&m_wifi, &WifiUfoClient::videoFrameReady, this, &MainWindow::updateVideo);
    connect(&m_wifi, &WifiUfoClient::videoStatsChanged, this, &MainWindow::updateVideoStats);
    connect(&m_wifi, &WifiUfoClient::controlPacketSent, this, [this](const QString &summary) {
        static int counter = 0;
        if (++counter % 20 == 0) {
            appendLog(QStringLiteral("控制包：%1").arg(summary));
        }
    });

    connect(&m_serial, &SerialCleanflightClient::logMessage, this, &MainWindow::appendLog);
    connect(&m_serial, &SerialCleanflightClient::statusChanged, this, [this](const QString &status) {
        m_serialStatusLabel->setText(status);
    });
    connect(&m_serial, &SerialCleanflightClient::textReceived, this, &MainWindow::appendSerialText);
    connect(&m_serial, &SerialCleanflightClient::telemetryChanged, this, &MainWindow::updateTelemetry);
}

void MainWindow::refreshSerialPorts()
{
    const QString current = m_serialPortCombo ? m_serialPortCombo->currentText() : QString();
    m_serialPortCombo->clear();
    const QStringList ports = SerialCleanflightClient::availablePorts();
    m_serialPortCombo->addItems(ports);
    const int idx = m_serialPortCombo->findText(current);
    if (idx >= 0) {
        m_serialPortCombo->setCurrentIndex(idx);
    } else {
        for (int i = 0; i < m_serialPortCombo->count(); ++i) {
            if (m_serialPortCombo->itemText(i).startsWith(QStringLiteral("COM9"))) {
                m_serialPortCombo->setCurrentIndex(i);
                break;
            }
        }
    }
}

void MainWindow::applyProfile()
{
    const QString profile = m_profileCombo->currentText();
    if (profile.contains(QStringLiteral("WiFi UFO"))) {
        m_remoteUdpSpin->setValue(40000);
        m_localUdpSpin->setValue(40000);
    } else if (profile.contains(QStringLiteral("Tello"))) {
        m_remoteUdpSpin->setValue(8889);
        m_localUdpSpin->setValue(8890);
    }
    appendLog(QStringLiteral("协议配置：%1").arg(profile));
}

void MainWindow::openUdp()
{
    m_wifi.setEndpoint(m_ipEdit->text(), quint16(m_remoteUdpSpin->value()), quint16(m_localUdpSpin->value()));
    m_wifi.open();
}

void MainWindow::closeUdp()
{
    m_wifi.close();
}

void MainWindow::sendHeartbeat()
{
    m_profileCombo->setCurrentText(QStringLiteral("WiFi UFO UDP"));
    openUdp();
    m_wifi.sendHeartbeat();
}

void MainWindow::startControl()
{
    if (!ensureControlUnlocked(QStringLiteral("启动控制流"))) {
        return;
    }
    openUdp();
    updateControlState();
    m_wifi.startControlStream();
}

void MainWindow::stopControl()
{
    m_wifi.stopControlStream();
}

void MainWindow::resetControls()
{
    m_rollSlider->setValue(128);
    m_pitchSlider->setValue(128);
    m_throttleSlider->setValue(0);
    m_yawSlider->setValue(128);
    updateControlState();
}

void MainWindow::updateControlState()
{
    m_rollValueLabel->setText(QString::number(m_rollSlider->value()));
    m_pitchValueLabel->setText(QString::number(m_pitchSlider->value()));
    m_throttleValueLabel->setText(QString::number(m_throttleSlider->value()));
    m_yawValueLabel->setText(QString::number(m_yawSlider->value()));
    m_wifi.setControlState(m_rollSlider->value(), m_pitchSlider->value(), m_throttleSlider->value(), m_yawSlider->value());
}

void MainWindow::requestTakeoff()
{
    if (!ensureControlUnlocked(QStringLiteral("起飞"))) {
        return;
    }
    const int ret = QMessageBox::question(this, QStringLiteral("确认起飞"),
                                          QStringLiteral("即将连续发送 1 秒起飞指令。请确认桨叶安全、机体固定或处于安全飞行区。"));
    if (ret == QMessageBox::Yes) {
        openUdp();
        m_wifi.sendTakeoffBurst();
    }
}

void MainWindow::requestLand()
{
    if (!ensureControlUnlocked(QStringLiteral("降落"))) {
        return;
    }
    const int ret = QMessageBox::question(this, QStringLiteral("确认降落"),
                                          QStringLiteral("即将连续发送 1 秒降落指令。"));
    if (ret == QMessageBox::Yes) {
        openUdp();
        m_wifi.sendLandBurst();
    }
}

void MainWindow::requestHardStop()
{
    if (!ensureControlUnlocked(QStringLiteral("急停"))) {
        return;
    }
    const int ret = QMessageBox::warning(this, QStringLiteral("确认急停"),
                                         QStringLiteral("急停可能导致飞行器直接掉落。只有在失控或危险时使用。是否发送？"),
                                         QMessageBox::Yes | QMessageBox::No,
                                         QMessageBox::No);
    if (ret == QMessageBox::Yes) {
        openUdp();
        m_wifi.sendHardStopBurst();
    }
}

void MainWindow::openSerial()
{
    if (m_serialPortCombo->currentText().isEmpty()) {
        QMessageBox::information(this, QStringLiteral("没有串口"), QStringLiteral("未发现可用串口。"));
        return;
    }
    m_serial.open(m_serialPortCombo->currentText(), m_baudSpin->value());
}

void MainWindow::closeSerial()
{
    m_serial.close();
}

void MainWindow::sendCliLine()
{
    const QString line = m_cliEdit->text().trimmed();
    if (line.isEmpty()) {
        return;
    }
    m_serial.sendLine(line);
    m_cliEdit->clear();
}

void MainWindow::updateTelemetry(const QVariantMap &values)
{
    for (auto it = values.constBegin(); it != values.constEnd(); ++it) {
        setMetric(it.key(), it.value().toString());
    }
}

void MainWindow::updateVideo(const QImage &frame)
{
    if (frame.isNull()) {
        return;
    }
    const QPixmap pixmap = QPixmap::fromImage(frame).scaled(m_videoLabel->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation);
    m_videoLabel->setPixmap(pixmap);
}

void MainWindow::updateVideoStats(int packets, double kbps, int frames)
{
    m_videoStatsLabel->setText(QStringLiteral("视频包：%1    速率：%2 KB/s    解码帧：%3")
                                   .arg(packets)
                                   .arg(kbps, 0, 'f', 1)
                                   .arg(frames));
    setMetric(QStringLiteral("WiFi 视频包"), QString::number(packets));
    setMetric(QStringLiteral("WiFi 视频速率"), QStringLiteral("%1 KB/s").arg(kbps, 0, 'f', 1));
    setMetric(QStringLiteral("WiFi 解码帧"), QString::number(frames));
}

void MainWindow::appendLog(const QString &message)
{
    const QString line = QStringLiteral("[%1] %2")
                             .arg(QDateTime::currentDateTime().toString(QStringLiteral("HH:mm:ss")))
                             .arg(message);
    if (m_logEdit) {
        m_logEdit->appendPlainText(line);
    }
    if (m_logFile.isOpen()) {
        QTextStream stream(&m_logFile);
        stream << line << '\n';
        stream.flush();
    }
}

void MainWindow::appendSerialText(const QString &text)
{
    if (m_serialConsole) {
        m_serialConsole->appendPlainText(text.trimmed());
    }
}

void MainWindow::setupLogFile()
{
    const QString dirPath = runtimeRoot() + QStringLiteral("/qt_logs");
    QDir().mkpath(dirPath);
    const QString filePath = dirPath + QStringLiteral("/qt_ground_station_%1.log")
                                       .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd_HHmmss")));
    m_logFile.setFileName(filePath);
    m_logFile.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text);
}

void MainWindow::setMetric(const QString &name, const QString &value)
{
    if (!m_metricTable) {
        return;
    }
    int row = m_metricRows.value(name, -1);
    if (row < 0) {
        row = m_metricTable->rowCount();
        m_metricTable->insertRow(row);
        m_metricTable->setItem(row, 0, new QTableWidgetItem(name));
        m_metricTable->setItem(row, 1, new QTableWidgetItem(value));
        m_metricRows.insert(name, row);
    } else {
        m_metricTable->item(row, 1)->setText(value);
    }
}

bool MainWindow::ensureControlUnlocked(const QString &action)
{
    if (m_unlockCheck && m_unlockCheck->isChecked()) {
        return true;
    }
    QMessageBox::warning(this, QStringLiteral("控制已锁定"),
                         QStringLiteral("“%1”需要先勾选安全锁。请确认拆桨或固定机体后再解锁。").arg(action));
    appendLog(QStringLiteral("已阻止动作：%1，原因：安全锁未解锁").arg(action));
    return false;
}

void MainWindow::setControlWidgetsEnabled(bool enabled)
{
    const QList<QWidget *> widgets = {m_startControlButton, m_stopControlButton};
    for (QWidget *widget : widgets) {
        if (widget) {
            widget->setEnabled(enabled);
        }
    }
}

QString MainWindow::runtimeRoot() const
{
    QDir dir(QCoreApplication::applicationDirPath());
    if (dir.dirName().compare(QStringLiteral("bin"), Qt::CaseInsensitive) == 0) {
        dir.cdUp();
    }
    if (dir.dirName().compare(QStringLiteral("qt_ground_station"), Qt::CaseInsensitive) == 0) {
        dir.cdUp();
    }
    return dir.filePath(QStringLiteral("runtime"));
}
