# ESP32 麦克风

ESP32-S3 + INMP441 通过 BLE 把 16 kHz / 16-bit 单声道 PCM 推到电脑。本机可做语音识别并粘贴到当前焦点，也可把板子注册成系统默认麦克风。

硬件按 [连接.md](连接.md) 接线。板子是 **ESP32-S3-N16R8**（Arduino 核心 3.0.7）。

## 功能

| 控件 | 引脚 | 作用 |
| --- | --- | --- |
| 按钮1 | IO42 | 按住录音，松开后识别、润色并粘贴 |
| 按钮2 | IO41 | 短按退格；长按按住退格 |
| 按钮3 | IO40 | 单击回车 |
| 按钮4 | IO39 | 切换电脑麦克风（持续推流，不识别） |
| LED1 | IO02 | 电脑麦克风模式常亮；否则仅按钮1按下时亮 |
| LED2 | IO38 | BLE 已连接常亮；未连接闪烁 |

INMP441：SCK=IO14，SD=IO15，WS=IO16，L/R=GND，VDD=3V3。各按钮另一端接 GND。

## 固件

```bash
pip install -r requirements.txt
```

烧录（编译并写入板子）：

```bash
python firmware.py all --port /dev/ttyACM0
```

分步：

```bash
python firmware.py compile
python firmware.py upload --port /dev/ttyACM0
python firmware.py monitor --port /dev/ttyACM0
```

| 命令 | 作用 |
| --- | --- |
| `python firmware.py compile` | 只编译 |
| `python firmware.py upload --port /dev/ttyACM0` | 烧录已编译固件 |
| `python firmware.py all --port /dev/ttyACM0` | 编译并烧录 |
| `python firmware.py monitor --port /dev/ttyACM0` | 串口监视（115200） |

不写 `--port` 时会尝试自动找串口，常见是 `/dev/ttyACM0` 或 `/dev/ttyUSB0`。首次运行会安装 `arduino-cli` 和 ESP32 核心。

也可用 Arduino IDE：开发板选 **ESP32S3 Dev Module**，USB CDC On Boot 开启，Flash 16MB，PSRAM 选 OPI，分区 `app3M_fat9M_16MB`。

设备 BLE 名：`ESP32-MIC`。

## 电脑端

需要 Linux（Wayland / PipeWire）。粘贴依赖 `wl-copy` 和 `ydotool`（`ydotoold` 要在跑）。虚拟麦克风依赖 `pactl`。

```bash
python receiver.py
```

按住按钮1 说话，松开后识别。润色结果默认粘贴到当前键盘焦点。终端窗口用 Ctrl+Shift+V，普通窗口用 Ctrl+V。

常用参数：

```bash
python receiver.py --address AA:BB:CC:DD:EE:FF
python receiver.py --keep-models          # 识别后不卸 GGUF，连续说话更快
python receiver.py --no-paste             # 只打印，不粘贴
python receiver.py --no-polish            # 只出 ASR，不润色
python receiver.py --no-asr               # 只收流，不识别
python receiver.py --asr-cpu              # LLM 走 CPU
```

上次连上的地址会记在 `.ble_last_address`。连错板可删这个文件。

### 电脑麦克风

`receiver.py` 运行中单击按钮4：本机出现 **ESP32麦克风** 并设为默认输入，板子持续推流。再按一次退出并恢复原来的默认输入。此模式不走识别。

### 语音识别模型

默认松手后再加载 GGUF，用完卸掉。缺模型时仍可收音，只是不识别。

放到 `models/`（已 gitignore）：

- `qwen3_asr_encoder_frontend.int4.onnx`
- `qwen3_asr_encoder_backend.int4.onnx`
- `qwen3_asr_llm.q4_k.gguf`
- `Qwen3-0.6B-Q8_0.gguf`（润色）

克隆 [Qwen3-ASR-GGUF](https://github.com/QwenLM/Qwen3-ASR-GGUF) 到 `third_party/Qwen3-ASR-GGUF`，并准备 Vulkan 版 llama.cpp 动态库：

`libllama.so`、`libggml.so`、`libggml-base.so`、`libggml-vulkan.so`

放到 `third_party/Qwen3-ASR-GGUF/qwen_asr_gguf/inference/bin/`。本仓库 `patches/llama.py` 用于对齐当前 llama.cpp ABI。

## 仓库结构

| 文件 | 作用 |
| --- | --- |
| `esp32_microphone.ino` | 固件 |
| `firmware.py` | 编译 / 烧录 / 串口监视 |
| `receiver.py` | BLE 接收、识别、粘贴、虚拟麦克风 |
| `asr_engine.py` | Qwen3-ASR + 润色 |
| `paste_input.py` | 剪贴板 + ydotool |
| `virtual_mic.py` | PipeWire 虚拟输入 |
| `连接.md` | 接线 |
