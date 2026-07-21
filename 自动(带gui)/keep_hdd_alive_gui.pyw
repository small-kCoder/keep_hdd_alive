#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能机械硬盘防休眠脚本 - PyQt6 图形界面

功能：
  提供图形化配置与监控界面，替代命令行操作。
  启动监控后，脚本在后台线程中定期写入保活文件并检测外部磁盘活动，
  所有状态变更和日志实时显示在界面上。

依赖：
  - Python 3.8+
  - PyQt6 >= 6.5
  - psutil >= 5.9.0

用法：
  pythonw keep_hdd_alive_gui.pyw
"""

import os
import sys
import time
import logging
import traceback
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

# ---------------------------------------------------------------------------
# 导入核心工具函数（复用 keep_hdd_alive.py 中的工具函数）
# ---------------------------------------------------------------------------
from keep_hdd_alive import (
    DEFAULT_CONFIG,
    get_physical_disk_for_drive,
    validate_drive_access,
    get_all_physical_disks,
)

# ---------------------------------------------------------------------------
# PyQt6 导入
# ---------------------------------------------------------------------------
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QComboBox, QPushButton,
    QSpinBox, QStatusBar, QGridLayout, QSizePolicy, QMessageBox,
    QFileDialog, QFrame, QSystemTrayIcon, QMenu, QDialog,
    QDialogButtonBox, QCheckBox, QSlider,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QMutex, QPropertyAnimation, QEasingCurve, QSequentialAnimationGroup
from PyQt6.QtGui import QFont, QColor, QIcon, QPalette, QAction


# ============================================================
# 常量
# ============================================================

WINDOW_TITLE = "智能机械硬盘防休眠"
WINDOW_WIDTH = 700
WINDOW_HEIGHT = 400

# 状态颜色
COLOR_ACTIVE = "#27ae60"     # 绿色 - 活跃模式
COLOR_IDLE = "#f39c12"       # 橙色 - 空闲模式
COLOR_STOPPED = "#95a5a6"    # 灰色 - 已停止
COLOR_ERROR = "#e74c3c"      # 红色 - 错误

# 单位定义
TIME_UNITS = ["秒", "分钟", "小时"]
UNIT_FACTORS = {"秒": 1, "分钟": 60, "小时": 3600}
UNIT_RANGES = {
    "秒": (10, 18000),
    "分钟": (1, 300),
    "小时": (1, 5),
}


# ============================================================
# 辅助函数：格式化时长
# ============================================================

def format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的时长字符串。"""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}分{s}秒" if s > 0 else f"{m}分钟"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}小时{m}分" if m > 0 else f"{h}小时"


# ============================================================
# 自定义日志处理器：将 logging 消息转发为 Qt 信号
# ============================================================

class QtLogSignalEmitter(QThread):
    """
    将 logging 模块的日志消息通过 Qt 信号发射到 GUI 线程。
    在 QThread 中调用 logging 时，此 handler 确保线程安全。
    """
    log_emitted = pyqtSignal(str, str)  # level, message

    def __init__(self):
        super().__init__()
        self.mutex = QMutex()

    def emit_log(self, level: str, message: str):
        """线程安全地发射日志信号。"""
        self.mutex.lock()
        try:
            self.log_emitted.emit(level, message)
        finally:
            self.mutex.unlock()


class QtLogHandler(logging.Handler):
    """
    自定义 logging.Handler，将日志记录转发到 QtLogSignalEmitter。
    """

    def __init__(self, emitter: QtLogSignalEmitter):
        super().__init__()
        self.emitter = emitter
        self.setFormatter(logging.Formatter(fmt="%(message)s"))

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.emitter.emit_log(record.levelname, msg)


# ============================================================
# 监控工作线程
# ============================================================

class KeepAliveWorker(QThread):
    """
    后台监控线程，负责：
      - 定期写入保活文件
      - 检测外部磁盘 I/O 活动
      - 在活跃/空闲模式间切换
      - 通过信号将状态变更通知 GUI
      - 记录状态变更详情（时长、写入次数等）
    """

    # ---- 信号定义 ----
    status_changed = pyqtSignal(bool)        # True=活跃模式, False=空闲模式
    stats_updated = pyqtSignal(int, int)     # (保活写入次数, 活动检查次数)
    drive_detected = pyqtSignal(str)         # 检测到的物理磁盘名
    init_error = pyqtSignal(str)             # 初始化错误
    worker_stopped = pyqtSignal()            # 线程已停止

    def __init__(self, config: Dict[str, Any]):
        super().__init__()
        self.config = config
        self._running = False

        # 盘符与路径
        self.drive = config["target_drive"].strip().rstrip("\\") + "\\"
        keep_alive_dir = config["keep_alive_dir"]
        if not keep_alive_dir:
            keep_alive_dir = os.path.join(self.drive, ".keep_alive")
        self.keep_alive_dir = keep_alive_dir
        self.keep_alive_path = os.path.join(self.keep_alive_dir, ".keep_alive")
        self.keep_alive_size = config["keep_alive_file_size_kb"] * 1024

        # I/O 监控
        self.physical_disk: Optional[str] = None
        self.last_io: Optional[Dict[str, int]] = None
        self.script_write_bytes: int = 0

        # 状态
        self.active_mode: bool = True
        self.last_check_time: float = 0.0
        self.total_writes: int = 0
        self.total_checks: int = 0

        # 状态变更追踪（用于日志）
        self.active_start_time: float = 0.0       # 本次活跃开始时间
        self.writes_at_active_start: int = 0       # 活跃开始时的写入次数
        self.idle_start_time: float = 0.0          # 本次空闲开始时间

        # 日志
        self.logger: Optional[logging.Logger] = None

    def run(self):
        """线程主入口。"""
        self._running = True

        # ---- 初始化日志 ----
        self.logger = logging.getLogger("HDDKeepAlive_GUI")
        self.logger.setLevel(logging.DEBUG)
        self.logger.info("后台监控线程已启动")

        # ---- 验证磁盘访问 ----
        try:
            validate_drive_access(self.drive, self.keep_alive_dir)
        except SystemExit as e:
            self.init_error.emit(f"磁盘访问验证失败: {e}")
            self._running = False
            self.worker_stopped.emit()
            return
        except Exception as e:
            self.init_error.emit(f"磁盘访问验证异常: {e}")
            self._running = False
            self.worker_stopped.emit()
            return

        # ---- 解析物理磁盘 ----
        self.physical_disk = self._resolve_disk()
        self.drive_detected.emit(self.physical_disk or "未知（监控全部磁盘）")

        # ---- 初始化 I/O 基准 ----
        counters = self._get_io_counters()
        if counters:
            self.last_io = counters
            self.logger.info(
                f"I/O 基准已建立: read={counters['read_bytes']}, "
                f"write={counters['write_bytes']}"
            )
        else:
            self.logger.warning("无法获取初始 I/O 计数器，首次检查将保守假设存在活动")

        # ---- 主循环 ----
        self.last_check_time = time.time()
        keep_alive_interval = self.config["keep_alive_interval_seconds"]
        check_interval = self.config["check_interval_seconds"]

        self.logger.info(f"保活间隔={keep_alive_interval}s, 检查间隔={check_interval}s")
        self.logger.info("监控已启动，活跃模式")

        # 记录初始活跃状态时间
        self.active_start_time = time.time()
        self.writes_at_active_start = 0
        self.status_changed.emit(True)  # 初始为活跃模式

        try:
            while self._running:
                # 步骤 1：活跃模式下写入保活文件
                if self.active_mode:
                    self._write_keep_alive()
                else:
                    self.logger.debug("空闲模式，跳过保活写入")

                # 步骤 2：活动检查
                now = time.time()
                if now - self.last_check_time >= check_interval:
                    has_activity = self._check_activity()

                    if has_activity:
                        if not self.active_mode:
                            # 空闲 → 活跃：记录空闲时长
                            idle_duration = now - self.idle_start_time
                            self.logger.info(
                                f"硬盘恢复活跃 | 空闲时长: {format_duration(idle_duration)}"
                            )
                            self.active_start_time = now
                            self.writes_at_active_start = self.total_writes
                        self.active_mode = True
                    else:
                        if self.active_mode:
                            # 活跃 → 空闲：记录活跃时长、写入次数、间隔设置
                            active_duration = now - self.active_start_time
                            writes_in_period = self.total_writes - self.writes_at_active_start
                            ka_min = self.config["keep_alive_interval_seconds"] // 60
                            ck_min = self.config["check_interval_seconds"] // 60
                            self.logger.info(
                                f"硬盘已空闲 | 活跃时长: {format_duration(active_duration)} "
                                f"| 保活写入: {writes_in_period} 次 "
                                f"| 间隔设置: 保活={ka_min}min 检查={ck_min}min"
                            )
                            self.idle_start_time = now
                        self.active_mode = False

                    self.status_changed.emit(self.active_mode)
                    self.last_check_time = now

                # 步骤 3：发射统计更新
                self.stats_updated.emit(self.total_writes, self.total_checks)

                # 步骤 4：分段睡眠（快速响应停止信号）
                sleep_remaining = keep_alive_interval
                while sleep_remaining > 0 and self._running:
                    chunk = min(3, sleep_remaining)
                    time.sleep(chunk)
                    sleep_remaining -= chunk

        except Exception as e:
            self.logger.error(f"监控线程异常: {e}\n{traceback.format_exc()}")
        finally:
            self.logger.info("监控线程已退出")
            self.worker_stopped.emit()

    def stop(self):
        """安全停止线程。"""
        self._running = False
        if self.logger:
            self.logger.info("收到停止信号，正在退出...")

    # ---- 内部方法 ----

    def _resolve_disk(self) -> Optional[str]:
        """解析盘符对应的物理磁盘。"""
        if self.config["physical_disk"]:
            self.logger.info(f"使用手动指定的物理磁盘: {self.config['physical_disk']}")
            return self.config["physical_disk"]
        disk = get_physical_disk_for_drive(self.config["target_drive"])
        if disk:
            self.logger.info(f"自动检测到物理磁盘: {disk}")
            return disk
        self.logger.warning("无法自动检测物理磁盘，降级为监控全部磁盘")
        return None

    def _check_activity(self) -> bool:
        """检查是否有外部磁盘 I/O 活动。"""
        self.total_checks += 1
        current = self._get_io_counters()

        if current is None:
            self.logger.warning("无法获取 I/O 计数器，保守假设存在外部活动")
            return True

        if self.last_io is None:
            self.last_io = current
            self.script_write_bytes = 0
            return True

        read_delta = current["read_bytes"] - self.last_io["read_bytes"]
        write_delta = current["write_bytes"] - self.last_io["write_bytes"]
        external_write = max(0, write_delta - self.script_write_bytes)

        self.last_io = current
        self.script_write_bytes = 0

        has_activity = (read_delta > 0) or (external_write > 0)
        self.logger.debug(
            f"检查 #{self.total_checks}: read_delta={read_delta}, "
            f"write_delta={write_delta}, external_write={external_write}, "
            f"active={has_activity}"
        )
        return has_activity

    def _write_keep_alive(self) -> bool:
        """写入保活文件。"""
        try:
            timestamp = f"{time.time()}\n".encode("utf-8")
            random_data = os.urandom(max(0, self.keep_alive_size - len(timestamp)))
            data = (timestamp + random_data)[:self.keep_alive_size]

            with open(self.keep_alive_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            self.script_write_bytes += len(data)
            self.total_writes += 1
            self.logger.debug(
                f"保活文件已写入 ({len(data)} bytes, 总计 #{self.total_writes})"
            )
            return True

        except PermissionError:
            self.logger.error(f"无权限写入保活文件: {self.keep_alive_path}")
            return False
        except OSError as e:
            self.logger.error(f"写入保活文件失败 (OSError): {e}")
            return False
        except Exception as e:
            self.logger.error(f"写入保活文件异常: {e}")
            return False

    def _get_io_counters(self) -> Optional[Dict[str, int]]:
        """获取磁盘 I/O 计数器。"""
        try:
            import psutil
        except ImportError:
            self.logger.error("psutil 未安装，请执行: pip install psutil")
            return None

        try:
            all_counters = psutil.disk_io_counters(perdisk=True)
            if all_counters is None:
                return None
        except Exception as e:
            self.logger.error(f"获取 I/O 计数器失败: {e}")
            return None

        if self.physical_disk and self.physical_disk in all_counters:
            c = all_counters[self.physical_disk]
            return {"read_bytes": c.read_bytes, "write_bytes": c.write_bytes}
        else:
            total_read = sum(c.read_bytes for c in all_counters.values())
            total_write = sum(c.write_bytes for c in all_counters.values())
            return {"read_bytes": total_read, "write_bytes": total_write}


# ============================================================
# 磁盘加载线程
# ============================================================

class DiskLoadWorker(QThread):
    """后台线程：调用 PowerShell 获取物理磁盘列表，避免阻塞 GUI 启动。"""
    disks_loaded = pyqtSignal(list)  # 发送磁盘列表

    def run(self):
        disks = get_all_physical_disks()
        self.disks_loaded.emit(disks)


# ============================================================
# 主窗口
# ============================================================

class MainWindow(QMainWindow):
    """主窗口：配置面板 + 状态面板 + 日志面板 + 控制按钮。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setMinimumSize(600, 500)

        # 内部状态
        self.config: Dict[str, Any] = {}
        self.worker: Optional[KeepAliveWorker] = None
        self.log_emitter: Optional[QtLogSignalEmitter] = None
        self.is_monitoring: bool = False
        self.current_mode: str = "stopped"  # active / idle / stopped
        self.current_unit: str = "分钟"       # 当前时间单位
        self.all_disks: list = []             # 延迟加载
        self.external_disks: list = []        # 延迟加载
        self.disk_load_worker: Optional[DiskLoadWorker] = None

        # 构建 UI（不加载磁盘列表，先显示窗口）
        self._init_ui()
        self._init_tray()
        self._update_control_states()

        # 状态栏初始显示
        self.status_bar.showMessage("就绪 - 点击「启动监控」开始")

    # ============================================================
    # UI 构建
    # ============================================================

    def _init_ui(self):
        """初始化所有 UI 组件。"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ---- 配置面板 ----
        main_layout.addWidget(self._create_config_panel())

        # ---- 状态面板 ----
        main_layout.addWidget(self._create_status_panel())

        # ---- 控制按钮 ----
        main_layout.addLayout(self._create_control_buttons())

        # 底部弹簧，吸收多余空间
        main_layout.addStretch()

        # ---- 状态栏 ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _create_config_panel(self) -> QGroupBox:
        """创建配置面板。"""
        group = QGroupBox("配置")
        grid = QGridLayout(group)
        grid.setSpacing(8)

        row = 0

        # 时间单位选择
        grid.addWidget(QLabel("时间单位:"), row, 0)
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(TIME_UNITS)
        self.cmb_unit.setCurrentText("分钟")
        self.cmb_unit.setToolTip("选择保活写入间隔和活动检查间隔的显示单位")
        self.cmb_unit.currentTextChanged.connect(self._on_unit_changed)
        grid.addWidget(self.cmb_unit, row, 1)
        row += 1

        # 目标硬盘（物理磁盘）
        grid.addWidget(QLabel("目标硬盘:"), row, 0)
        self.cmb_disk = QComboBox()
        self.cmb_disk.addItem("正在检测硬盘...")
        self.cmb_disk.setEnabled(False)
        self.cmb_disk.setMinimumWidth(200)
        grid.addWidget(self.cmb_disk, row, 1, 1, 4)
        row += 1

        # 磁盘检测进度条（滑块动画）
        self.pbar_disk = QSlider(Qt.Orientation.Horizontal)
        self.pbar_disk.setRange(0, 100)
        self.pbar_disk.setValue(0)
        self.pbar_disk.setEnabled(False)  # 禁止用户拖动
        self.pbar_disk.setMaximumHeight(18)
        self.pbar_disk.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #e8e8e8;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: transparent;
            }
            QSlider::handle:horizontal {
                background: #3498db;
                width: 50px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
        """)
        self.pbar_disk.hide()
        grid.addWidget(self.pbar_disk, row, 0, 1, 5)
        row += 1

        # 保活写入间隔
        grid.addWidget(QLabel("保活写入间隔:"), row, 3)
        self.spin_keep_alive = QSpinBox()
        self._apply_unit_range(self.spin_keep_alive, "分钟")
        self.spin_keep_alive.setToolTip("每隔多久写入一次保活文件")
        grid.addWidget(self.spin_keep_alive, row, 4)
        row += 1

        # 活动检查间隔
        grid.addWidget(QLabel("活动检查间隔:"), row, 0)
        self.spin_check = QSpinBox()
        self._apply_unit_range(self.spin_check, "分钟")
        self.spin_check.setToolTip("每隔多久检查一次外部磁盘活动")
        grid.addWidget(self.spin_check, row, 1)

        # 保活文件大小
        grid.addWidget(QLabel("保活文件大小:"), row, 3)
        self.spin_size = QSpinBox()
        self.spin_size.setRange(1, 1024)
        self.spin_size.setSuffix(" KB")
        self.spin_size.setToolTip("每次写入的保活文件大小")
        grid.addWidget(self.spin_size, row, 4)
        row += 1

        # 保活目录
        grid.addWidget(QLabel("保活目录:"), row, 0)
        self.edit_dir = QLineEdit()
        self.edit_dir.setPlaceholderText("留空 = 自动使用 <盘符>\\.keep_alive")
        self.edit_dir.setToolTip("保活文件存放目录，留空则自动创建")
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(self.edit_dir)
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._on_browse_dir)
        dir_layout.addWidget(btn_browse)
        grid.addLayout(dir_layout, row, 1, 1, 4)
        row += 1

        # 允许内置硬盘
        self.chk_allow_internal = QCheckBox("允许用于内置硬盘（默认仅外接硬盘）")
        self.chk_allow_internal.setToolTip("勾选后可以对 C: 盘等内置硬盘执行保活操作")
        self.chk_allow_internal.toggled.connect(self._on_allow_internal_toggled)
        grid.addWidget(self.chk_allow_internal, row, 0, 1, 5)
        row += 1

        return group

    def _create_status_panel(self) -> QGroupBox:
        """创建状态面板。"""
        group = QGroupBox("状态")
        layout = QHBoxLayout(group)
        layout.setSpacing(20)

        # 模式指示器
        self.lbl_mode_indicator = QLabel("●")
        font = QFont()
        font.setPointSize(20)
        self.lbl_mode_indicator.setFont(font)
        self.lbl_mode_indicator.setFixedWidth(30)
        layout.addWidget(self.lbl_mode_indicator)

        self.lbl_mode_text = QLabel("未启动")
        mode_font = QFont()
        mode_font.setPointSize(12)
        mode_font.setBold(True)
        self.lbl_mode_text.setFont(mode_font)
        layout.addWidget(self.lbl_mode_text)

        # 分隔线
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        layout.addWidget(sep1)

        # 磁盘信息
        self.lbl_disk_info = QLabel("物理磁盘: --")
        layout.addWidget(self.lbl_disk_info)

        # 分隔线
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        layout.addWidget(sep2)

        # 统计信息
        self.lbl_stats = QLabel("保活写入: 0 次  |  活动检查: 0 次")
        layout.addWidget(self.lbl_stats)

        layout.addStretch()
        return group

    def _create_control_buttons(self) -> QHBoxLayout:
        """创建控制按钮区。"""
        layout = QHBoxLayout()

        self.btn_start = QPushButton("▶  启动监控")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ACTIVE}; color: white; "
            f"font-weight: bold; font-size: 13px; border-radius: 4px; padding: 6px 20px; }}"
            f"QPushButton:hover {{ background-color: #219a52; }}"
            f"QPushButton:disabled {{ background-color: #bdc3c7; }}"
        )
        self.btn_start.clicked.connect(self._on_start)

        self.btn_stop = QPushButton("■  停止监控")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setStyleSheet(
            f"QPushButton {{ background-color: {COLOR_ERROR}; color: white; "
            f"font-weight: bold; font-size: 13px; border-radius: 4px; padding: 6px 20px; }}"
            f"QPushButton:hover {{ background-color: #c0392b; }}"
            f"QPushButton:disabled {{ background-color: #bdc3c7; }}"
        )
        self.btn_stop.clicked.connect(self._on_stop)

        layout.addStretch()
        layout.addWidget(self.btn_start)
        layout.addWidget(self.btn_stop)
        layout.addStretch()
        return layout

    # ============================================================
    # 系统托盘
    # ============================================================

    def _init_tray(self):
        """初始化系统托盘图标和菜单。"""
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip(f"{WINDOW_TITLE} - 未启动")

        # 使用内置图标（无外部图标文件依赖）
        self.tray_icon.setIcon(self.style().standardIcon(
            self.style().StandardPixmap.SP_DriveHDIcon
        ))

        # 托盘右键菜单
        tray_menu = QMenu()
        self.action_show = QAction("显示主窗口")
        self.action_show.triggered.connect(self._tray_show_window)
        tray_menu.addAction(self.action_show)

        tray_menu.addSeparator()

        self.action_quit = QAction("退出")
        self.action_quit.triggered.connect(self._tray_quit)
        tray_menu.addAction(self.action_quit)

        self.tray_icon.setContextMenu(tray_menu)

        # 左键单击：切换窗口显示/隐藏
        self.tray_icon.activated.connect(self._on_tray_activated)

        self.tray_icon.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """托盘图标激活事件。"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # 左键单击：切换显示/隐藏
            if self.isVisible():
                self.hide()
            else:
                self._tray_show_window()

    def _tray_show_window(self):
        """从托盘恢复主窗口。"""
        self.show()
        self.raise_()
        self.activateWindow()

    def _force_cleanup(self):
        """强制清理所有后台线程和子进程。"""
        # 1. 停止 worker 线程
        if self.worker:
            self.worker.stop()
            if not self.worker.wait(2000):
                self.worker.terminate()
                self.worker.wait(1000)

        # 2. 停止日志发射器线程
        if self.log_emitter:
            self.log_emitter.quit()
            if not self.log_emitter.wait(2000):
                self.log_emitter.terminate()
                self.log_emitter.wait(1000)

        # 3. 停止磁盘加载线程
        if self.disk_load_worker and self.disk_load_worker.isRunning():
            self.disk_load_worker.terminate()
            self.disk_load_worker.wait(1000)

    def _tray_quit(self):
        """从托盘菜单完全退出程序。"""
        self._force_cleanup()
        self.tray_icon.hide()
        QApplication.quit()
        # 确保进程彻底退出
        QTimer.singleShot(500, lambda: os._exit(0))

    def _update_tray_tooltip(self):
        """更新托盘图标的工具提示。"""
        mode_text = {"active": "活跃模式", "idle": "空闲模式", "stopped": "未启动"}
        self.tray_icon.setToolTip(
            f"{WINDOW_TITLE} - {mode_text.get(self.current_mode, '未知')}"
        )

    # ============================================================
    # 配置读写
    # ============================================================

    def _init_defaults(self):
        """用默认配置填充 UI 控件。"""
        self.config = dict(DEFAULT_CONFIG)

        # 默认选中第一个外接磁盘，若没有则选第一个磁盘
        if self.external_disks:
            self.cmb_disk.setCurrentIndex(0)
        elif self.all_disks:
            self.cmb_disk.setCurrentIndex(0)

        factor = UNIT_FACTORS[self.current_unit]
        self.spin_keep_alive.setValue(self.config["keep_alive_interval_seconds"] // factor)
        self.spin_check.setValue(self.config["check_interval_seconds"] // factor)
        self.spin_size.setValue(self.config["keep_alive_file_size_kb"])
        self.edit_dir.setText(self.config["keep_alive_dir"] or "")
        self.chk_allow_internal.setChecked(self.config["allow_internal_drive"])

    def _start_disk_loading(self):
        """启动后台线程加载物理磁盘列表。"""
        self.status_bar.showMessage("正在检测硬盘信息...")
        self.pbar_disk.setValue(0)
        self.pbar_disk.show()

        # 单向动画：滑块 0→100，到最右后立即从最左重新开始，无缝循环
        anim1 = QPropertyAnimation(self.pbar_disk, b"value")
        anim1.setDuration(1600)
        anim1.setStartValue(0)
        anim1.setEndValue(100)
        anim1.setEasingCurve(QEasingCurve.Type.InOutCubic)

        anim2 = QPropertyAnimation(self.pbar_disk, b"value")
        anim2.setDuration(1600)
        anim2.setStartValue(0)
        anim2.setEndValue(100)
        anim2.setEasingCurve(QEasingCurve.Type.InOutCubic)

        self._disk_anim = QSequentialAnimationGroup()
        self._disk_anim.addAnimation(anim1)
        self._disk_anim.addAnimation(anim2)
        self._disk_anim.setLoopCount(-1)
        self._disk_anim.start()

        self.disk_load_worker = DiskLoadWorker()
        self.disk_load_worker.disks_loaded.connect(self._on_disks_loaded)
        self.disk_load_worker.start()

    def _on_disks_loaded(self, disks: list):
        """磁盘列表加载完成，填充下拉框。"""
        if hasattr(self, '_disk_anim'):
            self._disk_anim.stop()
        self.pbar_disk.hide()

        self.all_disks = disks
        self.external_disks = [d for d in disks if d["is_external"]]

        # 填充下拉框
        self.cmb_disk.clear()
        self.cmb_disk.setEnabled(True)
        if self.external_disks:
            for d in self.external_disks:
                self.cmb_disk.addItem(d["display_text"], d)
        elif self.all_disks:
            for d in self.all_disks:
                self.cmb_disk.addItem(d["display_text"], d)
        else:
            self.cmb_disk.addItem("未检测到硬盘")
            self.cmb_disk.setEnabled(False)

        # 初始化默认配置
        self._init_defaults()
        self.status_bar.showMessage("就绪 - 点击「启动监控」开始", 3000)

    def _on_allow_internal_toggled(self, checked: bool):
        """切换物理磁盘下拉列表：勾选时显示全部，取消时仅外接。"""
        if not self.all_disks:
            return
        current_data = self.cmb_disk.currentData()
        self.cmb_disk.clear()
        disks = self.all_disks if checked else self.external_disks
        for d in disks:
            self.cmb_disk.addItem(d["display_text"], d)
        # 恢复之前选中的磁盘
        if current_data:
            for i in range(self.cmb_disk.count()):
                if self.cmb_disk.itemData(i)["number"] == current_data["number"]:
                    self.cmb_disk.setCurrentIndex(i)
                    break

    def _on_browse_dir(self):
        """浏览选择保活目录。"""
        disk_data = self.cmb_disk.currentData()
        start_dir = "C:\\"
        if disk_data and disk_data["drive_letters"]:
            start_dir = disk_data["drive_letters"][0] + "\\"
        folder = QFileDialog.getExistingDirectory(self, "选择保活目录", start_dir)
        if folder:
            self.edit_dir.setText(folder)

    # ============================================================
    # 单位选择
    # ============================================================

    def _on_unit_changed(self, new_unit: str):
        """时间单位切换时，换算 SpinBox 数值并调整范围。"""
        if new_unit == self.current_unit:
            return

        old_factor = UNIT_FACTORS[self.current_unit]
        new_factor = UNIT_FACTORS[new_unit]

        # 换算数值：保持实际时长（秒）不变
        old_keep_alive = self.spin_keep_alive.value()
        old_check = self.spin_check.value()
        actual_keep_alive_sec = old_keep_alive * old_factor
        actual_check_sec = old_check * old_factor

        # 更新单位
        self.current_unit = new_unit

        # 更新 range 和 suffix
        self._apply_unit_range(self.spin_keep_alive, new_unit)
        self._apply_unit_range(self.spin_check, new_unit)

        # 设置新值
        self.spin_keep_alive.setValue(actual_keep_alive_sec // new_factor)
        self.spin_check.setValue(actual_check_sec // new_factor)

    def _apply_unit_range(self, spin: QSpinBox, unit: str):
        """根据单位设置 SpinBox 的范围和后缀。"""
        rng = UNIT_RANGES[unit]
        spin.setRange(rng[0], rng[1])
        spin.setSuffix(f" {unit}")

    # ============================================================
    # 监控控制
    # ============================================================

    def _on_start(self):
        """启动监控。"""
        disk_data = self.cmb_disk.currentData()
        if not disk_data:
            QMessageBox.warning(self, "错误", "未选择目标硬盘")
            return

        target_drive = disk_data["drive_letters"][0] if disk_data["drive_letters"] else None
        if not target_drive:
            QMessageBox.warning(
                self, "错误",
                f"物理磁盘 {disk_data['name']} ({disk_data['friendly_name']}) 没有可用的盘符分区"
            )
            return

        # 外接硬盘检查
        if not self.chk_allow_internal.isChecked() and not disk_data["is_external"]:
            reply = QMessageBox.warning(
                self, "非外接硬盘",
                f"磁盘 {disk_data['name']} ({disk_data['friendly_name']}) 检测为内置硬盘。\n\n"
                "默认仅允许用于外接硬盘以保护系统盘。\n\n"
                "如需继续，请勾选「允许用于内置硬盘」后重试。",
                QMessageBox.StandardButton.Ok,
            )
            return

        factor = UNIT_FACTORS[self.current_unit]
        self.config = {
            "target_drive": target_drive,
            "keep_alive_dir": self.edit_dir.text().strip() or None,
            "keep_alive_interval_seconds": self.spin_keep_alive.value() * factor,
            "check_interval_seconds": self.spin_check.value() * factor,
            "keep_alive_file_size_kb": self.spin_size.value(),
            "physical_disk": disk_data["name"],
            "log_file": None,
            "log_level": "INFO",
            "max_log_file_size_mb": 10,
            "log_backup_count": 3,
            "allow_internal_drive": self.chk_allow_internal.isChecked(),
        }

        # 参数校验
        check_sec = self.spin_check.value() * UNIT_FACTORS[self.current_unit]
        keep_sec = self.spin_keep_alive.value() * UNIT_FACTORS[self.current_unit]
        if check_sec < keep_sec:
            reply = QMessageBox.warning(
                self, "参数警告",
                "活动检查间隔应 >= 保活写入间隔。\n是否自动调整检查间隔为写入间隔的 2 倍？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.spin_check.setValue(self.spin_keep_alive.value() * 2)
                self.config["check_interval_seconds"] = (
                    self.spin_check.value() * UNIT_FACTORS[self.current_unit]
                )
            else:
                return

        # 创建日志发射器
        self.log_emitter = QtLogSignalEmitter()
        self.log_emitter.log_emitted.connect(self._append_log)
        self.log_emitter.start()

        # 创建并配置工作线程
        self.worker = KeepAliveWorker(self.config)
        handler = QtLogHandler(self.log_emitter)
        self.worker.logger = logging.getLogger("HDDKeepAlive_GUI")
        self.worker.logger.handlers.clear()
        self.worker.logger.addHandler(handler)
        self.worker.logger.setLevel(logging.DEBUG)

        # 连接信号
        self.worker.status_changed.connect(self._on_status_changed)
        self.worker.stats_updated.connect(self._on_stats_updated)
        self.worker.drive_detected.connect(self._on_drive_detected)
        self.worker.init_error.connect(self._on_init_error)
        self.worker.worker_stopped.connect(self._on_worker_stopped)

        # 启动
        self.worker.start()
        self.is_monitoring = True
        self._update_control_states()
        self._set_mode_display("stopped")  # 等待 worker 确认
        self.status_bar.showMessage("监控正在启动...")

    def _on_stop(self):
        """停止监控。"""
        if self.worker:
            self.worker.stop()
            self.status_bar.showMessage("正在停止监控...")
            self.btn_stop.setEnabled(False)
            self.btn_start.setEnabled(False)

    def _on_worker_stopped(self):
        """工作线程停止后的清理。"""
        self.is_monitoring = False
        self._update_control_states()
        self._set_mode_display("stopped")
        self.status_bar.showMessage("监控已停止")

        # 清理日志发射器
        if self.log_emitter:
            self.log_emitter.quit()
            self.log_emitter.wait(2000)
            self.log_emitter = None

        # 清理 worker 引用
        if self.worker:
            self.worker.wait(3000)
            self.worker = None

    # ============================================================
    # 状态更新（GUI 线程）
    # ============================================================

    def _on_status_changed(self, active: bool):
        """处理模式切换信号。"""
        self.current_mode = "active" if active else "idle"
        self._set_mode_display(self.current_mode)
        self._update_tray_tooltip()
        if active:
            self.status_bar.showMessage("活跃模式 - 正在写入保活文件", 5000)
        else:
            self.status_bar.showMessage("空闲模式 - 已停止保活写入，允许硬盘休眠", 5000)

    def _on_stats_updated(self, writes: int, checks: int):
        """更新统计信息。"""
        self.lbl_stats.setText(f"保活写入: {writes} 次  |  活动检查: {checks} 次")

    def _on_drive_detected(self, disk_name: str):
        """更新检测到的物理磁盘信息。"""
        self.lbl_disk_info.setText(f"物理磁盘: {disk_name}")

    def _on_init_error(self, message: str):
        """处理初始化错误。"""
        self._append_log("ERROR", message)
        QMessageBox.critical(self, "初始化失败", message)
        self._on_worker_stopped()

    def _set_mode_display(self, mode: str):
        """设置模式指示器颜色和文本。"""
        self.current_mode = mode
        if mode == "active":
            self.lbl_mode_indicator.setStyleSheet(f"color: {COLOR_ACTIVE};")
            self.lbl_mode_text.setText("活跃模式")
            self.lbl_mode_text.setStyleSheet(f"color: {COLOR_ACTIVE}; font-weight: bold;")
        elif mode == "idle":
            self.lbl_mode_indicator.setStyleSheet(f"color: {COLOR_IDLE};")
            self.lbl_mode_text.setText("空闲模式")
            self.lbl_mode_text.setStyleSheet(f"color: {COLOR_IDLE}; font-weight: bold;")
        else:
            self.lbl_mode_indicator.setStyleSheet(f"color: {COLOR_STOPPED};")
            self.lbl_mode_text.setText("未启动")
            self.lbl_mode_text.setStyleSheet(f"color: {COLOR_STOPPED}; font-weight: bold;")
            self.lbl_disk_info.setText("物理磁盘: --")
            self.lbl_stats.setText("保活写入: 0 次  |  活动检查: 0 次")

        self._update_tray_tooltip()

    def _update_control_states(self):
        """更新按钮和控件的启用状态。"""
        self.btn_start.setEnabled(not self.is_monitoring)
        self.btn_stop.setEnabled(self.is_monitoring)
        self.cmb_disk.setEnabled(not self.is_monitoring)
        self.spin_keep_alive.setEnabled(not self.is_monitoring)
        self.spin_check.setEnabled(not self.is_monitoring)
        self.spin_size.setEnabled(not self.is_monitoring)
        self.edit_dir.setEnabled(not self.is_monitoring)

    # ============================================================
    # 日志显示
    # ============================================================

    def _append_log(self, level: str, message: str):
        """向状态栏显示消息（线程安全）。"""
        if level == "DEBUG":
            return
        if level in ("ERROR", "CRITICAL"):
            self.status_bar.showMessage(f"✗ {message}", 10000)
        elif level == "WARNING":
            self.status_bar.showMessage(f"⚠ {message}", 8000)
        else:
            self.status_bar.showMessage(message, 5000)

    # ============================================================
    # 窗口关闭
    # ============================================================

    def closeEvent(self, event):
        """窗口关闭事件：弹出选项对话框。"""
        if not self.is_monitoring:
            # 未在监控中，直接退出
            self.tray_icon.hide()
            event.accept()
            return

        # 弹出三选项对话框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("关闭选项")
        msg_box.setText("请选择关闭方式：")
        msg_box.setIcon(QMessageBox.Icon.Question)

        btn_tray = msg_box.addButton("最小化到系统托盘", QMessageBox.ButtonRole.AcceptRole)
        btn_quit = msg_box.addButton("完全退出", QMessageBox.ButtonRole.DestructiveRole)
        btn_cancel = msg_box.addButton("取消", QMessageBox.ButtonRole.RejectRole)

        msg_box.setDefaultButton(btn_tray)
        msg_box.exec()

        clicked = msg_box.clickedButton()

        if clicked == btn_tray:
            # 最小化到托盘
            self.hide()
            self._append_log("INFO", "程序已最小化到系统托盘，监控继续运行中")
            event.ignore()
        elif clicked == btn_quit:
            # 完全退出
            self._force_cleanup()
            self.tray_icon.hide()
            event.accept()
            QTimer.singleShot(500, lambda: os._exit(0))
        else:
            # 取消
            event.ignore()


# ============================================================
# 入口
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # 关闭窗口不退出程序（托盘运行）

    # 全局字体
    app.setFont(QFont("Microsoft YaHei", 9))

    window = MainWindow()
    window.show()
    # 窗口显示后再后台加载磁盘列表（避免启动卡顿）
    QTimer.singleShot(50, window._start_disk_loading)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()