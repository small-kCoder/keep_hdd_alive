#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能机械硬盘防休眠脚本 (Smart HDD Keep-Alive)

================================================================================
功能说明：
  通过定期在目标硬盘上写入小文件来防止机械硬盘自动休眠，同时智能检测外部
  读写活动。如果硬盘在检查周期内无任何外部读写操作（脚本自身写入不计入），
  则自动暂停保活写入，让硬盘自然进入休眠状态以降低磨损和功耗。

运行逻辑：
  1. 以「活跃模式」启动，按 keep_alive_interval 频率写入保活文件
  2. 每 check_interval 秒评估一次外部磁盘 I/O 活动
  3. 有外部活动 → 保持/恢复活跃模式（继续写入保活文件）
  4. 无外部活动 → 切换至空闲模式（停止写入，让硬盘休眠）
  5. 空闲模式下持续监控，一旦检测到外部活动立即恢复活跃模式

依赖：
  - Python 3.8+
  - psutil >= 5.9.0  (pip install psutil)

用法：
  python keep_hdd_alive.py
================================================================================
"""

import json
import os
import sys
import time
import signal
import logging
import logging.handlers
import subprocess
import traceback
from pathlib import Path
from typing import Optional, Dict, Any

# ============================================================
# 常量定义
# ============================================================

# 默认配置（当配置文件不存在或字段缺失时使用）
DEFAULT_CONFIG: Dict[str, Any] = {
    "target_drive": "D:",                       # 目标硬盘盘符
    "keep_alive_dir": None,                      # 保活文件目录（None = 自动创建）
    "keep_alive_interval_seconds": 60,           # 保活文件写入间隔（秒），默认 1 分钟
    "check_interval_seconds": 1800,              # 外部活动检查间隔（秒），默认 30 分钟
    "keep_alive_file_size_kb": 4,                # 保活文件大小（KB）
    "log_file": None,                            # 日志文件路径（None = 输出到 stdout）
    "log_level": "INFO",                         # 日志级别：DEBUG / INFO / WARNING / ERROR
    "physical_disk": None,                       # 手动指定物理磁盘名（如 PhysicalDrive1）
    "max_log_file_size_mb": 10,                  # 日志文件最大大小（MB）
    "log_backup_count": 3,                       # 日志文件备份数量
}

# 保活文件固定名称
KEEP_ALIVE_FILENAME = ".keep_alive"

# 全局运行标志（用于优雅退出）
_running = True


# ============================================================
# 工具函数
# ============================================================

def setup_signal_handlers():
    """注册信号处理器，实现优雅退出（Ctrl+C 终止）。"""
    def _shutdown(signum, frame):
        global _running
        _running = False
        # 不在这里调用 sys.exit，让主循环自然退出
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


def get_physical_disk_for_drive(drive_letter: str) -> Optional[str]:
    """
    使用 PowerShell 将 Windows 盘符映射到物理磁盘名称。

    原理：
      Get-Partition -DriveLetter X → 获取分区对象
      Get-Disk → 获取该分区所属的物理磁盘
      Select-Object Number → 提取磁盘编号，拼接为 PhysicalDriveN

    Args:
        drive_letter: 盘符，如 "D:" 或 "D"

    Returns:
        物理磁盘名称（如 "PhysicalDrive1"），失败返回 None
    """
    drive = drive_letter.strip().rstrip(":\\").upper()
    if len(drive) != 1:
        return None

    try:
        cmd = (
            f'powershell -NoProfile -NonInteractive -Command '
            f'"Get-Partition -DriveLetter {drive} | Get-Disk | '
            f'Select-Object -ExpandProperty Number"'
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=True,
            timeout=15,  # PowerShell 启动可能较慢，设置 15 秒超时
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0 and result.stdout.strip():
            disk_num = result.stdout.strip()
            return f"PhysicalDrive{disk_num}"
    except subprocess.TimeoutExpired:
        pass  # PowerShell 超时，降级处理
    except FileNotFoundError:
        pass  # 系统无 PowerShell（极端情况）
    except Exception:
        pass  # 其他未知错误，降级处理

    return None


def get_all_physical_disks() -> list:
    """
    获取所有物理磁盘及其盘符信息。

    分两步查询避免 PowerShell $变量被工具 shell 吃掉：
    1. 获取所有物理磁盘的 Number, BusType, FriendlyName, Size
    2. 在 Python 中循环，为每个磁盘单独查询盘符（每条命令不含 $变量）

    Returns:
        [
            {
                "number": 0,
                "name": "PhysicalDrive0",
                "bus_type": "NVMe",
                "friendly_name": "THNSN5256GPUK NVMe TOSHIBA 256GB",
                "size_gb": 238,
                "drive_letters": ["C:"],
                "is_external": False,
                "display_text": "PhysicalDrive0 - NVMe THNSN5256GPUK [C:]"
            },
            ...
        ]
        失败时返回空列表 []
    """
    try:
        # ---- 步骤1：获取所有物理磁盘基本信息 ----
        ps_cmd = (
            'Get-Disk | '
            'Select-Object Number, BusType, FriendlyName, Size | '
            'ConvertTo-Json'
        )
        result = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        raw_disks = json.loads(result.stdout.strip())
        if isinstance(raw_disks, dict):
            raw_disks = [raw_disks]

        # ---- 步骤2：为每个磁盘查询盘符 ----
        disks = []
        for raw in raw_disks:
            number = raw.get("Number")
            if number is None:
                continue

            # 查询该磁盘的盘符列表（不涉及 $变量）
            drive_letters = []
            try:
                ps_cmd2 = (
                    f'Get-Partition -DiskNumber {number} | '
                    f'Where-Object DriveLetter | '
                    f'Select-Object -ExpandProperty DriveLetter'
                )
                result2 = subprocess.run(
                    ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd2],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result2.returncode == 0 and result2.stdout.strip():
                    drive_letters = [
                        f"{letter.strip()}:" for letter in result2.stdout.strip().split('\n') if letter.strip()
                    ]
            except Exception:
                pass  # 查询盘符失败，drive_letters 保持为空

            bus_type = raw.get("BusType", "Unknown")
            friendly_name = raw.get("FriendlyName", "Unknown Disk")
            size_bytes = raw.get("Size", 0)
            size_gb = round(size_bytes / (1024 ** 3), 1) if size_bytes else 0

            is_external = (bus_type.upper() == "USB")

            # 构建显示文本
            drive_str = f"[{', '.join(drive_letters)}]" if drive_letters else "[无盘符]"
            display_text = (
                f"PhysicalDrive{number} - {bus_type} {friendly_name} {drive_str}"
            )

            disks.append({
                "number": number,
                "name": f"PhysicalDrive{number}",
                "bus_type": bus_type,
                "friendly_name": friendly_name,
                "size_gb": size_gb,
                "drive_letters": drive_letters,
                "is_external": is_external,
                "display_text": display_text,
            })

        return disks

    except json.JSONDecodeError:
        return []
    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def validate_drive_access(drive: str, keep_alive_dir: str) -> None:
    """
    验证目标盘符和保活目录的可访问性。

    检查项：
      1. 盘符是否存在
      2. 保活目录是否可创建/可写入

    Args:
        drive: 目标盘符（如 "D:"）
        keep_alive_dir: 保活文件目录路径

    Raises:
        SystemExit: 验证失败时退出
    """
    # 检查盘符是否存在
    if not os.path.exists(drive + os.sep):
        print(f"[错误] 目标盘符 '{drive}' 不存在或无法访问", file=sys.stderr)
        sys.exit(1)

    # 检查/创建保活目录
    try:
        os.makedirs(keep_alive_dir, exist_ok=True)
    except PermissionError:
        print(f"[错误] 无权限创建保活目录 '{keep_alive_dir}'", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"[错误] 创建保活目录失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 写入测试
    test_file = os.path.join(keep_alive_dir, ".write_test")
    try:
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
    except Exception as e:
        print(f"[错误] 保活目录写入测试失败: {e}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# 核心类：HDDKeepAlive
# ============================================================

class HDDKeepAlive:
    """
    智能硬盘保活管理器。

    状态机：
      ┌──────────┐    无外部活动    ┌──────────┐
      │ 活跃模式  │ ───────────────→ │ 空闲模式  │
      │ (写入保活) │ ←─────────────── │ (停止写入) │
      └──────────┘    有外部活动    └──────────┘

    两种模式都会持续监控磁盘 I/O，区别仅在于是否写入保活文件。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化保活管理器。

        Args:
            config: 合并后的配置字典
        """
        self.config = config
        self.logger = self._setup_logging()

        # 解析盘符与保活目录
        self.drive = config["target_drive"].strip().rstrip("\\") + "\\"
        if config["keep_alive_dir"]:
            self.keep_alive_dir = config["keep_alive_dir"]
        else:
            # 默认在目标盘根目录下创建隐藏目录
            self.keep_alive_dir = os.path.join(self.drive, ".keep_alive")

        self.keep_alive_path = os.path.join(self.keep_alive_dir, KEEP_ALIVE_FILENAME)
        self.keep_alive_size_bytes = config["keep_alive_file_size_kb"] * 1024

        # 活动检测相关
        self.physical_disk = self._resolve_physical_disk()
        self.last_io_counters: Optional[Dict[str, int]] = None
        self.script_write_bytes: int = 0  # 累计脚本自身写入字节数（用于排除）

        # 状态
        self.active_mode: bool = True  # 初始为活跃模式
        self.last_check_time: float = 0.0

        # 统计
        self.total_keep_alive_writes: int = 0
        self.total_checks: int = 0

        # 验证目标磁盘
        validate_drive_access(self.drive, self.keep_alive_dir)

        self.logger.info("=" * 60)
        self.logger.info("智能硬盘防休眠脚本已启动")
        self.logger.info(f"  目标盘符:      {self.drive}")
        self.logger.info(f"  物理磁盘:      {self.physical_disk or '自动检测失败，监控全部磁盘'}")
        self.logger.info(f"  保活目录:      {self.keep_alive_dir}")
        self.logger.info(f"  保活写入间隔:  {config['keep_alive_interval_seconds']} 秒")
        self.logger.info(f"  活动检查间隔:  {config['check_interval_seconds']} 秒")
        self.logger.info(f"  保活文件大小:  {config['keep_alive_file_size_kb']} KB")
        self.logger.info("=" * 60)

    # ----------------------------------------------------------
    # 初始化辅助方法
    # ----------------------------------------------------------

    def _setup_logging(self) -> logging.Logger:
        """
        配置日志系统。

        支持两种输出模式：
          - log_file 为 None → 输出到 stdout（适合前台运行）
          - log_file 指定路径 → 输出到文件（带自动轮转，适合后台运行）

        Returns:
            配置好的 Logger 实例
        """
        logger = logging.getLogger("HDDKeepAlive")
        logger.setLevel(logging.DEBUG)  # Logger 自身设为最低级别，由 Handler 控制

        # 日志格式
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-7s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if self.config["log_file"]:
            # 文件日志（带轮转）
            handler = logging.handlers.RotatingFileHandler(
                self.config["log_file"],
                maxBytes=self.config["max_log_file_size_mb"] * 1024 * 1024,
                backupCount=self.config["log_backup_count"],
                encoding="utf-8",
            )
        else:
            # 控制台日志
            handler = logging.StreamHandler(sys.stdout)

        handler.setFormatter(formatter)
        handler.setLevel(getattr(logging, self.config["log_level"].upper(), logging.INFO))
        logger.addHandler(handler)

        return logger

    def _resolve_physical_disk(self) -> Optional[str]:
        """
        解析目标盘符对应的物理磁盘名称。

        优先级：
          1. 配置文件中手动指定的 physical_disk
          2. 通过 PowerShell 自动检测
          3. 返回 None（降级为监控全部磁盘）

        Returns:
            物理磁盘名称（如 "PhysicalDrive1"），或 None
        """
        # 优先级 1：手动指定
        if self.config["physical_disk"]:
            self.logger.info(f"使用手动指定的物理磁盘: {self.config['physical_disk']}")
            return self.config["physical_disk"]

        # 优先级 2：PowerShell 自动检测
        drive_letter = self.config["target_drive"]
        disk = get_physical_disk_for_drive(drive_letter)
        if disk:
            self.logger.info(f"自动检测到物理磁盘: {disk} (盘符 {drive_letter})")
            return disk

        # 优先级 3：降级
        self.logger.warning(
            f"无法自动检测盘符 '{drive_letter}' 对应的物理磁盘，"
            f"将监控全部磁盘的 I/O 活动（准确性可能降低）"
        )
        return None

    # ----------------------------------------------------------
    # 磁盘 I/O 监控
    # ----------------------------------------------------------

    def _get_disk_io_counters(self) -> Optional[Dict[str, int]]:
        """
        获取目标磁盘的累计读写字节数。

        使用 psutil.disk_io_counters(perdisk=True) 获取各物理磁盘的
        累计读写统计。如果指定了 physical_disk，只取对应磁盘的数据；
        否则汇总全部磁盘数据。

        Returns:
            {"read_bytes": int, "write_bytes": int} 或 None（获取失败时）
        """
        try:
            import psutil
        except ImportError:
            self.logger.error(
                "未安装 psutil 库，请执行: pip install psutil\n"
                "脚本将在 5 秒后退出。"
            )
            time.sleep(5)
            sys.exit(1)

        try:
            all_counters = psutil.disk_io_counters(perdisk=True)
            if all_counters is None:
                self.logger.warning("disk_io_counters() 返回 None")
                return None
        except Exception as e:
            self.logger.error(f"获取磁盘 I/O 计数器失败: {e}")
            return None

        if self.physical_disk and self.physical_disk in all_counters:
            # 精确监控指定物理磁盘
            c = all_counters[self.physical_disk]
            return {"read_bytes": c.read_bytes, "write_bytes": c.write_bytes}
        else:
            # 降级：汇总全部磁盘
            total_read = sum(c.read_bytes for c in all_counters.values())
            total_write = sum(c.write_bytes for c in all_counters.values())
            return {"read_bytes": total_read, "write_bytes": total_write}

    def check_external_activity(self) -> bool:
        """
        检查自上次检查以来是否有外部磁盘 I/O 活动。

        算法：
          1. 获取当前累计 I/O 计数器
          2. 计算与上次记录的差值（delta）
          3. 从写字节差值中减去脚本自身写入的字节数
          4. 如果读字节差值 + 外部写字节差值 > 0，则存在外部活动

        特殊处理：
          - 首次检查：记录基准并返回 True（保守假设有活动）
          - 计数器获取失败：返回 True（保守假设有活动，避免误判导致数据丢失）

        Returns:
            True 表示有外部活动，False 表示完全无外部活动
        """
        self.total_checks += 1
        current = self._get_disk_io_counters()

        if current is None:
            self.logger.warning("无法获取磁盘 I/O 计数器，保守假设存在外部活动")
            return True

        if self.last_io_counters is None:
            # 首次检查，建立基准线
            self.last_io_counters = current
            self.script_write_bytes = 0
            self.logger.debug(
                f"首次 I/O 基准记录: "
                f"read={current['read_bytes']}, write={current['write_bytes']}"
            )
            return True

        # 计算差值
        read_delta = current["read_bytes"] - self.last_io_counters["read_bytes"]
        write_delta = current["write_bytes"] - self.last_io_counters["write_bytes"]

        # 排除脚本自身写入
        external_write = max(0, write_delta - self.script_write_bytes)

        # 更新基准
        self.last_io_counters = current
        self.script_write_bytes = 0

        has_activity = (read_delta > 0) or (external_write > 0)

        self.logger.debug(
            f"活动检查 #{self.total_checks}: "
            f"read_delta={read_delta}, write_delta={write_delta}, "
            f"external_write={external_write}, "
            f"has_activity={has_activity}"
        )

        return has_activity

    # ----------------------------------------------------------
    # 保活文件写入
    # ----------------------------------------------------------

    def write_keep_alive_file(self) -> bool:
        """
        在目标磁盘上写入保活文件。

        写入内容包含时间戳和随机数据，确保每次写入内容不同，
        避免文件系统缓存优化导致写入被跳过。

        Returns:
            True 表示写入成功，False 表示写入失败
        """
        try:
            # 生成带时间戳的随机数据，确保每次写入内容不同
            timestamp = f"{time.time()}\n".encode("utf-8")
            random_data = os.urandom(max(0, self.keep_alive_size_bytes - len(timestamp)))
            data = timestamp + random_data
            data = data[: self.keep_alive_size_bytes]  # 精确控制大小

            # 写入文件（覆盖模式，避免目录膨胀）
            with open(self.keep_alive_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())  # 强制刷盘，确保物理写入

            self.script_write_bytes += len(data)
            self.total_keep_alive_writes += 1

            self.logger.debug(
                f"保活文件已写入: {self.keep_alive_path} "
                f"({len(data)} bytes, 总计 #{self.total_keep_alive_writes})"
            )
            return True

        except PermissionError:
            self.logger.error(f"无权限写入保活文件: {self.keep_alive_path}")
            return False
        except OSError as e:
            self.logger.error(f"写入保活文件失败 (OSError): {e}")
            return False
        except Exception as e:
            self.logger.error(f"写入保活文件时发生未知错误: {e}\n{traceback.format_exc()}")
            return False

    # ----------------------------------------------------------
    # 主循环
    # ----------------------------------------------------------

    def run(self):
        """
        主事件循环。

        循环逻辑：
          1. 如果处于活跃模式 → 写入保活文件
          2. 检查是否到达活动检查周期
          3. 如果到达 → 评估外部活动并切换模式
          4. 休眠 keep_alive_interval_seconds 秒
          5. 重复直到收到退出信号
        """
        global _running
        _running = True

        keep_alive_interval = self.config["keep_alive_interval_seconds"]
        check_interval = self.config["check_interval_seconds"]

        self.last_check_time = time.time()

        self.logger.info("主循环已启动，按 Ctrl+C 停止")

        try:
            while _running:
                # ---- 步骤 1：如果活跃模式，写入保活文件 ----
                if self.active_mode:
                    self.write_keep_alive_file()
                else:
                    self.logger.debug("空闲模式，跳过保活写入")

                # ---- 步骤 2：检查是否到达活动评估周期 ----
                now = time.time()
                if now - self.last_check_time >= check_interval:
                    has_activity = self.check_external_activity()

                    if has_activity:
                        if not self.active_mode:
                            self.logger.info(
                                "检测到外部磁盘活动，恢复活跃模式（开始写入保活文件）"
                            )
                        self.active_mode = True
                    else:
                        if self.active_mode:
                            self.logger.info(
                                "无外部磁盘活动，切换至空闲模式"
                                "（停止保活写入，允许硬盘休眠）"
                            )
                        self.active_mode = False

                    self.last_check_time = now

                # ---- 步骤 3：等待下一个周期 ----
                # 使用分段睡眠，以便更快响应退出信号
                sleep_remaining = keep_alive_interval
                while sleep_remaining > 0 and _running:
                    sleep_chunk = min(5, sleep_remaining)  # 每次最多睡 5 秒
                    time.sleep(sleep_chunk)
                    sleep_remaining -= sleep_chunk

        except KeyboardInterrupt:
            # 这个异常已在 signal handler 中通过 _running 标志处理，
            # 但作为双重保险保留此捕获
            pass
        finally:
            self._shutdown()

    def _shutdown(self):
        """优雅退出：清理资源并输出统计信息。"""
        self.logger.info("=" * 60)
        self.logger.info("脚本正在退出...")
        self.logger.info(f"  总计保活写入次数:  {self.total_keep_alive_writes}")
        self.logger.info(f"  总计活动检查次数:  {self.total_checks}")
        self.logger.info(f"  最终模式:          {'活跃' if self.active_mode else '空闲'}")
        self.logger.info("=" * 60)
        self.logger.info("脚本已安全退出")


# ============================================================
# 入口
# ============================================================

def main():
    """程序入口。"""
    setup_signal_handlers()
    config = dict(DEFAULT_CONFIG)

    # ---- 命令行参数解析 ----
    if "--list-disks" in sys.argv:
        print("正在查询物理磁盘列表...")
        print()
        disks = get_all_physical_disks()
        if not disks:
            print("  未检测到任何物理磁盘，或 PowerShell 不可用。")
        else:
            for d in disks:
                ext_tag = " [外接]" if d["is_external"] else " [内置]"
                size_str = f"{d['size_gb']} GB"
                letters_str = ", ".join(d["drive_letters"]) if d["drive_letters"] else "无盘符"
                print(f"  {d['name']}{ext_tag}")
                print(f"    型号: {d['friendly_name']}")
                print(f"    容量: {size_str}")
                print(f"    盘符: {letters_str}")
                print()
        return

    if "--disk" in sys.argv:
        try:
            idx = sys.argv.index("--disk")
            disk_num = int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            print("[错误] 用法: python keep_hdd_alive.py --disk <磁盘编号>", file=sys.stderr)
            print("       使用 --list-disks 查看所有可用磁盘", file=sys.stderr)
            sys.exit(1)

        disks = get_all_physical_disks()
        found = None
        for d in disks:
            if d["number"] == disk_num:
                found = d
                break

        if not found:
            print(f"[错误] 未找到 PhysicalDrive{disk_num}", file=sys.stderr)
            print("       使用 --list-disks 查看所有可用磁盘", file=sys.stderr)
            sys.exit(1)

        if not found["drive_letters"]:
            print(f"[错误] PhysicalDrive{disk_num} 没有可用的盘符分区", file=sys.stderr)
            sys.exit(1)

        config["target_drive"] = found["drive_letters"][0]
        config["physical_disk"] = found["name"]

    # 创建管理器并运行
    try:
        manager = HDDKeepAlive(config)
    except SystemExit:
        return
    except Exception as e:
        print(f"[致命错误] 初始化失败: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    manager.run()


if __name__ == "__main__":
    main()