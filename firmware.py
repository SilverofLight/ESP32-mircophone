#!/usr/bin/env python3
"""在 micromamba/venv 环境中编译并烧录 ESP32-S3 固件。

不依赖系统里是否已有 arduino-cli：首次运行会把 CLI 装到当前 Python 环境的 bin 目录。

用法:
  python firmware.py compile
  python firmware.py upload
  python firmware.py upload --port /dev/ttyACM0
  python firmware.py monitor
  python firmware.py all
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKETCH = ROOT
BUILD_DIR = ROOT / ".build"

FQBN = (
    "esp32:esp32:esp32s3:"
    "CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB"
)
ESP32_CORE = "esp32:esp32@3.0.7"
ESP32_INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
CLI_VERSION = "1.4.1"


def env_bin_dir() -> Path:
    return Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")


def cli_path() -> Path | None:
    local = env_bin_dir() / ("arduino-cli.exe" if os.name == "nt" else "arduino-cli")
    if local.exists():
        return local
    return None


def download(url: str, dest: Path) -> None:
    print(f"下载 {url}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def install_arduino_cli() -> Path:
    dest_dir = env_bin_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "arduino-cli"

    system = os.uname().sysname.lower()
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("arm"):
        arch = "armv7"
    else:
        raise RuntimeError(f"不支持的 CPU 架构: {machine}")

    if "linux" in system:
        os_name = "Linux"
        ext = "tar.gz"
    elif "darwin" in system:
        os_name = "macOS"
        ext = "tar.gz"
    else:
        raise RuntimeError(f"不支持的系统: {system}")

    url = (
        "https://github.com/arduino/arduino-cli/releases/download/"
        f"v{CLI_VERSION}/arduino-cli_{CLI_VERSION}_{os_name}_{arch}.{ext}"
    )

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"arduino-cli.{ext}"
        download(url, archive)
        if ext == "tar.gz":
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter="data")
        else:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)
        extracted = Path(tmp) / "arduino-cli"
        if not extracted.exists():
            matches = list(Path(tmp).rglob("arduino-cli"))
            if not matches:
                raise RuntimeError("压缩包里没有找到 arduino-cli")
            extracted = matches[0]
        shutil.copy2(extracted, dest)

    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"已安装 arduino-cli -> {dest}")
    return dest


def ensure_cli() -> Path:
    existing = cli_path()
    if existing is not None:
        return existing

    dest_dir = env_bin_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "arduino-cli"
    system_cli = shutil.which("arduino-cli")
    if system_cli:
        os.symlink(system_cli, dest)
        print(f"已把系统 arduino-cli 链接到当前环境: {dest}")
        return dest

    print("当前环境没有 arduino-cli，开始安装到虚拟环境 ...")
    return install_arduino_cli()


def run_cli(
    cli: Path,
    args: list[str],
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        [str(env_bin_dir()), str(cli.parent), env.get("PATH", "")]
    )
    print("+", cli.name, *args, flush=True)
    result = subprocess.run(
        [str(cli), *args],
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
        capture_output=capture,
    )
    if capture and result.stdout:
        sys.stdout.write(result.stdout)
    if capture and result.stderr:
        sys.stderr.write(result.stderr)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def ensure_core(cli: Path) -> None:
    result = run_cli(cli, ["core", "list"], check=False, capture=True)
    listing = f"{result.stdout or ''}{result.stderr or ''}"
    if result.returncode == 0 and "esp32:esp32" in listing:
        return

    run_cli(
        cli,
        [
            "config",
            "add",
            "board_manager.additional_urls",
            ESP32_INDEX,
        ],
        check=False,
    )
    run_cli(cli, ["core", "update-index"])
    print(f"安装 Arduino-ESP32 核心 {ESP32_CORE} ...")
    run_cli(cli, ["core", "install", ESP32_CORE])


def compile_firmware(cli: Path) -> None:
    ensure_core(cli)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    run_cli(
        cli,
        [
            "compile",
            "--fqbn",
            FQBN,
            "--build-path",
            str(BUILD_DIR),
            str(SKETCH),
        ],
    )
    print("编译完成")


def detect_port() -> str | None:
    try:
        from serial.tools import list_ports
    except ImportError:
        return None

    ports = list(list_ports.comports())
    preferred = []
    others = []
    for port in ports:
        text = f"{port.device} {port.description} {port.hwid}".lower()
        if any(key in text for key in ("usb", "acm", "uart", "cp210", "ch340", "esp32", "silicon")):
            preferred.append(port.device)
        else:
            others.append(port.device)
    if preferred:
        return preferred[0]
    if others:
        return others[0]
    return None


def upload_firmware(cli: Path, port: str | None) -> None:
    if not BUILD_DIR.exists():
        compile_firmware(cli)
    selected = port or detect_port()
    if not selected:
        raise SystemExit("未找到串口。请用 --port 指定，例如 --port /dev/ttyACM0")
    print(f"使用串口 {selected}")
    run_cli(
        cli,
        [
            "upload",
            "--fqbn",
            FQBN,
            "--port",
            selected,
            "--input-dir",
            str(BUILD_DIR),
        ],
    )
    print("烧录完成")


def monitor_serial(cli: Path, port: str | None) -> None:
    selected = port or detect_port()
    if not selected:
        raise SystemExit("未找到串口。请用 --port 指定，例如 --port /dev/ttyACM0")
    run_cli(
        cli,
        [
            "monitor",
            "--port",
            selected,
            "--config",
            "baudrate=115200",
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="编译 / 烧录 ESP32-S3 麦克风固件")
    parser.add_argument(
        "command",
        choices=("compile", "upload", "monitor", "all"),
        help="compile=只编译, upload=烧录, monitor=串口监视, all=编译+烧录",
    )
    parser.add_argument("--port", help="串口设备，例如 /dev/ttyACM0 或 /dev/ttyUSB0")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cli = ensure_cli()
    print(f"arduino-cli: {cli}")

    if args.command == "compile":
        compile_firmware(cli)
    elif args.command == "upload":
        upload_firmware(cli, args.port)
    elif args.command == "monitor":
        monitor_serial(cli, args.port)
    elif args.command == "all":
        compile_firmware(cli)
        upload_firmware(cli, args.port)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        raise SystemExit(130)
