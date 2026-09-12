/*
 * ESP32-S3 + INMP441 按键录音，经 BLE 实时推流。
 *
 * ESP32-S3 只有 BLE（无经典蓝牙 SPP/A2DP），因此用 GATT Notify 传输 PCM。
 * 芯片上只保留极小的 DMA / 发送缓冲，不保存完整录音。
 * 空闲时关闭 I2S/麦克风时钟、放宽 BLE 连接间隔并降低发射功率。
 *
 * Arduino IDE 开发板设置（ESP32-S3-N16R8）:
 *   开发板: ESP32S3 Dev Module
 *   USB CDC On Boot: Enabled
 *   Flash Size: 16MB (128Mb)
 *   PSRAM: OPI PSRAM
 *   需要 Arduino-ESP32 3.x（使用 ESP_I2S）
 *
 * 接线见 连接.md:
 *   INMP441  SCK=IO14  SD=IO15  WS=IO16  L/R=GND
 *   按钮1    IO42 按住录音
 *   按钮2    IO41 短按退格 / 长按按住退格
 *   按钮3    IO40 单击回车
 *   按钮4    IO39
 *   LED      IO02 -> R1 -> LED1 -> GND
 */

#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <ESP_I2S.h>
#include <WiFi.h>
#include <driver/i2s_common.h>
#include <esp_gap_ble_api.h>
#include <string.h>

static const char *DEVICE_NAME = "ESP32-MIC";
static const char *SERVICE_UUID = "e9ea0001-7dca-4e3d-9a9a-1c4f6b8e0001";
static const char *STATUS_CHAR_UUID = "e9ea0002-7dca-4e3d-9a9a-1c4f6b8e0001";
static const char *AUDIO_CHAR_UUID = "e9ea0003-7dca-4e3d-9a9a-1c4f6b8e0001";

static const int I2S_SCK_PIN = 14;
static const int I2S_SD_PIN = 15;
static const int I2S_WS_PIN = 16;
static const int BUTTON_PIN = 42;
static const int BUTTON2_PIN = 41;
static const int BUTTON3_PIN = 40;
static const int BUTTON4_PIN = 39;
static const int LED_PIN = 2;

static const uint32_t SAMPLE_RATE = 16000;
static const uint8_t SAMPLE_BITS = 16;
static const uint8_t CHANNELS = 1;
static const int PCM_GAIN = 4;
static const uint32_t DEBOUNCE_MS = 30;
static const uint32_t LONG_PRESS_MS = 400;
static const size_t I2S_READ_BYTES = 512;

static const uint8_t EVENT_STOP = 0;
static const uint8_t EVENT_START = 1;
static const uint8_t EVENT_BUTTON = 2;
static const uint8_t BUTTON_ACTION_CLICK = 1;
static const uint8_t BUTTON_ACTION_HOLD_START = 2;
static const uint8_t BUTTON_ACTION_HOLD_END = 3;

// BLE 连接间隔单位 1.25 ms；广播间隔单位 0.625 ms
static const uint16_t CONN_FAST_MIN = 6;    // 7.5 ms
static const uint16_t CONN_FAST_MAX = 12;   // 15 ms
static const uint16_t CONN_IDLE_MIN = 40;   // 50 ms
static const uint16_t CONN_IDLE_MAX = 80;   // 100 ms
static const uint16_t CONN_IDLE_LATENCY = 2;
static const uint16_t CONN_TIMEOUT = 400;   // 4 s
static const uint16_t ADV_FAST_MIN = 0x20;  // 20 ms
static const uint16_t ADV_FAST_MAX = 0x40;  // 40 ms
static const uint16_t ADV_SLOW_MIN = 0x00A0;  // 100 ms
static const uint16_t ADV_SLOW_MAX = 0x0140;  // 200 ms
static const uint32_t ADV_FAST_MS = 30000;

I2SClass i2s;

BLEServer *pServer = nullptr;
BLECharacteristic *pStatusChar = nullptr;
BLECharacteristic *pAudioChar = nullptr;

volatile bool deviceConnected = false;
uint16_t connId = 0;
esp_bd_addr_t peerBda = {};
bool havePeerBda = false;
bool i2sReady = false;
bool i2sRunning = false;
bool advIsFast = false;
uint32_t advFastUntil = 0;

bool buttonPressed = false;
bool lastRawButton = false;
uint32_t lastDebounceMs = 0;
bool streaming = false;

bool button2Raw = false;
bool button2Pressed = false;
bool button2Holding = false;
uint32_t button2DebounceMs = 0;
uint32_t button2PressAt = 0;
bool button3Raw = false;
bool button3Pressed = false;
uint32_t button3DebounceMs = 0;

uint16_t audioSeq = 0;
uint32_t pcmBytesSent = 0;
bool use16BitTransform = true;

int32_t i2sRaw32[I2S_READ_BYTES / 4];
int16_t i2sPcm16[I2S_READ_BYTES / 2];
uint8_t notifyBuf[512];

void notifyStatus(uint8_t event, uint32_t pcmBytes) {
  if (pStatusChar == nullptr) {
    return;
  }

  uint8_t pkt[11];
  pkt[0] = event;
  memcpy(pkt + 1, &SAMPLE_RATE, sizeof(SAMPLE_RATE));
  pkt[5] = SAMPLE_BITS;
  pkt[6] = CHANNELS;
  memcpy(pkt + 7, &pcmBytes, sizeof(pcmBytes));

  pStatusChar->setValue(pkt, sizeof(pkt));
  if (deviceConnected) {
    pStatusChar->notify();
  }
}

void notifyButton(uint8_t id, uint8_t action) {
  if (pStatusChar == nullptr) {
    return;
  }
  uint8_t pkt[3] = {EVENT_BUTTON, id, action};
  pStatusChar->setValue(pkt, sizeof(pkt));
  if (deviceConnected) {
    pStatusChar->notify();
  }
}

size_t audioPayloadBytes() {
  uint16_t mtu = 23;
  if (pServer != nullptr && deviceConnected) {
    uint16_t peer = pServer->getPeerMTU(connId);
    if (peer >= 23) {
      mtu = peer;
    }
  }

  size_t payload = mtu > 3 ? (mtu - 3) : 20;
  if (payload > 2) {
    payload -= 2;  // 序列号
  }
  payload &= ~static_cast<size_t>(1);  // 16-bit 样本对齐
  if (payload < 2) {
    payload = 2;
  }
  if (payload > 480) {
    payload = 480;
  }
  return payload;
}

void applyGain(int16_t *samples, int count) {
  for (int i = 0; i < count; ++i) {
    int32_t v = static_cast<int32_t>(samples[i]) * PCM_GAIN;
    if (v > 32767) {
      v = 32767;
    } else if (v < -32768) {
      v = -32768;
    }
    samples[i] = static_cast<int16_t>(v);
  }
}

void sendPcm(const uint8_t *data, size_t len) {
  if (!deviceConnected || pAudioChar == nullptr || len == 0) {
    return;
  }

  const size_t chunk = audioPayloadBytes();
  size_t offset = 0;
  while (offset < len) {
    size_t n = len - offset;
    if (n > chunk) {
      n = chunk;
    }

    notifyBuf[0] = static_cast<uint8_t>(audioSeq & 0xff);
    notifyBuf[1] = static_cast<uint8_t>((audioSeq >> 8) & 0xff);
    memcpy(notifyBuf + 2, data + offset, n);

    pAudioChar->setValue(notifyBuf, n + 2);
    pAudioChar->notify();

    audioSeq++;
    pcmBytesSent += n;
    offset += n;
  }
}

void flushI2S() {
  if (!i2sRunning) {
    return;
  }
  uint8_t dump[512];
  i2s.readBytes(reinterpret_cast<char *>(dump), sizeof(dump));
}

int readMicPcm16(int16_t *out, size_t maxSamples) {
  if (!i2sRunning) {
    return 0;
  }
  if (use16BitTransform) {
    size_t want = (maxSamples * sizeof(int16_t)) & ~static_cast<size_t>(1);
    if (want == 0) {
      return 0;
    }
    int got = i2s.readBytes(reinterpret_cast<char *>(out), want);
    if (got <= 0) {
      return 0;
    }
    return got / 2;
  }

  size_t want32 = maxSamples * sizeof(int32_t);
  if (want32 > sizeof(i2sRaw32)) {
    want32 = sizeof(i2sRaw32);
  }
  want32 &= ~static_cast<size_t>(3);
  if (want32 == 0) {
    return 0;
  }

  int got = i2s.readBytes(reinterpret_cast<char *>(i2sRaw32), want32);
  if (got <= 0) {
    return 0;
  }
  int samples = got / 4;
  for (int i = 0; i < samples; ++i) {
    out[i] = static_cast<int16_t>(i2sRaw32[i] >> 16);
  }
  return samples;
}

void startAdvertising(bool fast) {
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->stop();
  if (fast) {
    advertising->setMinInterval(ADV_FAST_MIN);
    advertising->setMaxInterval(ADV_FAST_MAX);
    advFastUntil = millis() + ADV_FAST_MS;
  } else {
    advertising->setMinInterval(ADV_SLOW_MIN);
    advertising->setMaxInterval(ADV_SLOW_MAX);
  }
  advertising->start();
  advIsFast = fast;
}

void maybeSlowAdvertising() {
  if (deviceConnected || !advIsFast) {
    return;
  }
  if ((int32_t)(millis() - advFastUntil) < 0) {
    return;
  }
  startAdvertising(false);
  Serial.println("广播已改为节能间隔");
}

void requestConnParams(uint16_t minInt, uint16_t maxInt, uint16_t latency) {
  if (!deviceConnected || !havePeerBda) {
    return;
  }

  esp_ble_conn_update_params_t connParams = {};
  memcpy(connParams.bda, peerBda, sizeof(peerBda));
  connParams.min_int = minInt;
  connParams.max_int = maxInt;
  connParams.latency = latency;
  connParams.timeout = CONN_TIMEOUT;
  esp_ble_gap_update_conn_params(&connParams);
}

static bool i2sChannelOk(esp_err_t err) {
  return err == ESP_OK || err == ESP_ERR_INVALID_STATE;
}

bool initMic() {
  if (i2sReady) {
    return true;
  }

  i2s.setPins(I2S_SCK_PIN, I2S_WS_PIN, -1, I2S_SD_PIN);
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO,
                 I2S_STD_SLOT_LEFT)) {
    Serial.println("I2S 初始化失败");
    return false;
  }

  if (!i2s.configureRX(SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO,
                       I2S_RX_TRANSFORM_32_TO_16)) {
    Serial.println("I2S 16-bit 转换不可用，改用软件右移");
    use16BitTransform = false;
  } else {
    use16BitTransform = true;
    Serial.println("I2S 就绪（32-bit -> 16-bit）");
  }
  i2s.setTimeout(40);
  i2sReady = true;
  i2sRunning = true;
  return true;
}

bool resumeMic() {
  if (!i2sReady && !initMic()) {
    return false;
  }
  if (i2sRunning) {
    return true;
  }
  i2s_chan_handle_t rx = i2s.rxChan();
  if (rx == nullptr) {
    Serial.println("I2S RX 通道无效");
    return false;
  }
  if (!i2sChannelOk(i2s_channel_enable(rx))) {
    Serial.println("I2S 启动失败");
    return false;
  }
  i2sRunning = true;
  delay(8);
  return true;
}

void pauseMic() {
  if (!i2sReady || !i2sRunning) {
    return;
  }
  i2s_chan_handle_t rx = i2s.rxChan();
  if (rx != nullptr) {
    i2sChannelOk(i2s_channel_disable(rx));
  }
  i2sRunning = false;
}

void startStream() {
  if (streaming) {
    return;
  }

  requestConnParams(CONN_FAST_MIN, CONN_FAST_MAX, 0);
  delay(30);

  if (!resumeMic()) {
    requestConnParams(CONN_IDLE_MIN, CONN_IDLE_MAX, CONN_IDLE_LATENCY);
    return;
  }

  audioSeq = 0;
  pcmBytesSent = 0;
  flushI2S();
  streaming = true;

  uint16_t mtu = pServer != nullptr ? pServer->getPeerMTU(connId) : 23;
  Serial.printf("开始录音  MTU=%u  chunk=%u\n", mtu, static_cast<unsigned>(audioPayloadBytes()));
  notifyStatus(EVENT_START, 0);
  delay(15);
}

void stopStream() {
  if (!streaming) {
    pauseMic();
    requestConnParams(CONN_IDLE_MIN, CONN_IDLE_MAX, CONN_IDLE_LATENCY);
    return;
  }

  int samples = readMicPcm16(i2sPcm16, sizeof(i2sPcm16) / sizeof(i2sPcm16[0]));
  if (samples > 0) {
    applyGain(i2sPcm16, samples);
    sendPcm(reinterpret_cast<uint8_t *>(i2sPcm16), static_cast<size_t>(samples) * 2);
  }

  delay(8);
  notifyStatus(EVENT_STOP, pcmBytesSent);
  streaming = false;
  pauseMic();
  requestConnParams(CONN_IDLE_MIN, CONN_IDLE_MAX, CONN_IDLE_LATENCY);
  Serial.printf("停止录音  已发送 %u 字节\n", pcmBytesSent);
}

void handlePress() {
  digitalWrite(LED_PIN, HIGH);
  Serial.println("按钮按下");
  if (deviceConnected) {
    startStream();
  } else {
    Serial.println("未连接 BLE，无法传输录音");
  }
}

void handleRelease() {
  digitalWrite(LED_PIN, LOW);
  Serial.println("按钮松开");
  stopStream();
}

void pollRecordButton() {
  bool raw = digitalRead(BUTTON_PIN) == LOW;
  uint32_t now = millis();

  if (raw != lastRawButton) {
    lastRawButton = raw;
    lastDebounceMs = now;
  }

  if ((now - lastDebounceMs) > DEBOUNCE_MS && buttonPressed != lastRawButton) {
    buttonPressed = lastRawButton;
    if (buttonPressed) {
      handlePress();
    } else {
      handleRelease();
    }
  }
}

void pollBackspaceButton() {
  bool raw = digitalRead(BUTTON2_PIN) == LOW;
  uint32_t now = millis();

  if (raw != button2Raw) {
    button2Raw = raw;
    button2DebounceMs = now;
  }

  if ((now - button2DebounceMs) <= DEBOUNCE_MS) {
    return;
  }

  if (button2Pressed != button2Raw) {
    button2Pressed = button2Raw;
    if (button2Pressed) {
      button2Holding = false;
      button2PressAt = now;
    } else if (button2Holding) {
      button2Holding = false;
      notifyButton(2, BUTTON_ACTION_HOLD_END);
      Serial.println("按钮2 松开长按");
    } else {
      notifyButton(2, BUTTON_ACTION_CLICK);
      Serial.println("按钮2 单击退格");
    }
    return;
  }

  if (button2Pressed && !button2Holding && (now - button2PressAt) >= LONG_PRESS_MS) {
    button2Holding = true;
    notifyButton(2, BUTTON_ACTION_HOLD_START);
    Serial.println("按钮2 长按退格");
  }
}

void pollEnterButton() {
  bool raw = digitalRead(BUTTON3_PIN) == LOW;
  uint32_t now = millis();

  if (raw != button3Raw) {
    button3Raw = raw;
    button3DebounceMs = now;
  }

  if ((now - button3DebounceMs) <= DEBOUNCE_MS) {
    return;
  }

  if (button3Pressed != button3Raw) {
    button3Pressed = button3Raw;
    if (!button3Pressed) {
      notifyButton(3, BUTTON_ACTION_CLICK);
      Serial.println("按钮3 单击回车");
    }
  }
}

void pollButtons() {
  pollRecordButton();
  pollBackspaceButton();
  pollEnterButton();
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    deviceConnected = true;
  }

  void onConnect(BLEServer *server, esp_ble_gatts_cb_param_t *param) override {
    deviceConnected = true;
    connId = param->connect.conn_id;
    memcpy(peerBda, param->connect.remote_bda, sizeof(peerBda));
    havePeerBda = true;

    requestConnParams(CONN_IDLE_MIN, CONN_IDLE_MAX, CONN_IDLE_LATENCY);

    Serial.printf("BLE 已连接  conn=%u\n", connId);
    if (buttonPressed) {
      startStream();
    }
  }

  void onDisconnect(BLEServer *server) override {
    deviceConnected = false;
    connId = 0;
    havePeerBda = false;
    if (streaming) {
      streaming = false;
      pauseMic();
      Serial.println("连接断开，停止传输");
    }
    Serial.println("BLE 断开，快速广播 30 秒");
    delay(80);
    startAdvertising(true);
  }
};

void setupBLE() {
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(517);
  BLEDevice::setPower(ESP_PWR_LVL_P3, ESP_BLE_PWR_TYPE_ADV);
  BLEDevice::setPower(ESP_PWR_LVL_P3, ESP_BLE_PWR_TYPE_DEFAULT);

  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService *service = pServer->createService(SERVICE_UUID);

  pStatusChar = service->createCharacteristic(
      STATUS_CHAR_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  pStatusChar->addDescriptor(new BLE2902());
  notifyStatus(EVENT_STOP, 0);

  pAudioChar =
      service->createCharacteristic(AUDIO_CHAR_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pAudioChar->addDescriptor(new BLE2902());

  service->start();

  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->setMinPreferred(CONN_IDLE_MIN);
  advertising->setMaxPreferred(CONN_IDLE_MAX);
  startAdvertising(true);

  Serial.printf("BLE 快速广播中，设备名: %s\n", DEVICE_NAME);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nESP32-S3 INMP441 BLE 录音");

  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(BUTTON2_PIN, INPUT_PULLUP);
  pinMode(BUTTON3_PIN, INPUT_PULLUP);
  pinMode(BUTTON4_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  lastRawButton = digitalRead(BUTTON_PIN) == LOW;
  buttonPressed = false;
  lastDebounceMs = millis();
  button2Raw = digitalRead(BUTTON2_PIN) == LOW;
  button2Pressed = false;
  button2Holding = false;
  button2DebounceMs = millis();
  button3Raw = digitalRead(BUTTON3_PIN) == LOW;
  button3Pressed = false;
  button3DebounceMs = millis();

  if (!initMic()) {
    while (true) {
      delay(1000);
    }
  }
  pauseMic();
  setupBLE();
  Serial.println("按钮1 按住录音；按钮2 退格；按钮3 回车");
}

void loop() {
  pollButtons();

  if (streaming && deviceConnected) {
    int samples = readMicPcm16(i2sPcm16, sizeof(i2sPcm16) / sizeof(i2sPcm16[0]));
    if (samples > 0) {
      applyGain(i2sPcm16, samples);

      static uint32_t lastAmpMs = 0;
      static int32_t peak = 0;
      for (int i = 0; i < samples; ++i) {
        int32_t mag = abs(i2sPcm16[i]);
        if (mag > peak) {
          peak = mag;
        }
      }
      if (millis() - lastAmpMs >= 1000) {
        Serial.printf("录音中  峰值=%d  已发送=%u\n", peak, pcmBytesSent);
        peak = 0;
        lastAmpMs = millis();
      }

      sendPcm(reinterpret_cast<uint8_t *>(i2sPcm16), static_cast<size_t>(samples) * 2);
    }
  } else {
    maybeSlowAdvertising();
    delay(10);
  }
}
