# 智能机械硬盘防休眠工具

通过定期写入保活文件 + 智能检测外部 I/O 活动，防止机械硬盘因空闲自动休眠。

## 运行原理

```
┌──────────┐  无外部活动  ┌──────────┐
│ 活跃模式  │ ──────────→ │ 空闲模式  │
│ 写入保活  │ ←────────── │ 停止写入  │
└──────────┘  有外部活动  └──────────┘
```

1. 以「活跃模式」启动，按保活间隔定期写入小文件，阻止硬盘休眠
2. 每隔检查周期评估外部磁盘 I/O 活动
3. 有外部读写 → 保持活跃；无外部活动 → 切到空闲，允许硬盘自然休眠
4. 空闲模式下持续监控，一旦检测到外部活动立即恢复保活写入

## 两个版本

| 版本 | 入口 | 方式 | 说明 |
|------|------|------|------|
| **命令行版** | `手动/keep_hdd_alive.py` | 终端运行，`Ctrl+C` 停止 | 适合服务器/后台 |
| **GUI 版** | `自动(带gui)/keep_hdd_alive_gui.pyw` | 桌面窗口，系统托盘 | 适合日常使用 |

## 快速开始

### 方式一：使用打包好的 EXE（推荐）

1. 下载 [Release](https://github.com/small-kCoder/keep_hdd_alive/releases) 中的 zip 包
2. 解压后双击 `keep_hdd_alive_gui.exe`（GUI 版）或 `keep_hdd_alive.exe`（命令行版）
3. 无需安装 Python，开箱即用

### 方式二：从源码运行

```bash
pip install -r requirements.txt
pythonw 自动(带gui)/keep_hdd_alive_gui.pyw    # GUI 版
python 手动/keep_hdd_alive.py                   # 命令行版
```

## GUI 功能说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 目标盘符 | D: | 要保活的硬盘盘符 |
| 保活间隔 | 30 分钟 | 写入保活文件的频率 |
| 检查间隔 | 30 分钟 | 检测外部 I/O 活动的频率 |
| 文件大小 | 4 KB | 保活文件大小 |

- 单位支持：秒 / 分钟 / 小时
- **关闭窗口**：运行时最小化到系统托盘，不运行时直接退出
- 配置修改自动保存，无需手动操作

## 依赖

```
psutil>=5.9.0
PyQt6>=6.5
```

## 构建

```bash
# 命令行版
pyinstaller --onedir --console --name keep_hdd_alive 手动/keep_hdd_alive.py

# GUI 版
pyinstaller --onedir --noconsole --name keep_hdd_alive_gui 自动(带gui)/keep_hdd_alive_gui.pyw
```

## 许可证

MIT