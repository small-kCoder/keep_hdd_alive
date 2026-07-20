# Tasks

- [x] Task 1: 实现活动状态变更日志
  - 在 `KeepAliveWorker._check_activity()` 中记录状态切换时间点
  - 活跃→空闲时：输出活跃时长、保活写入次数、当前间隔设置
  - 空闲→活跃时：输出空闲时长
  - 通过现有 `self.logger.info()` 输出，自动显示在 GUI 日志面板

- [x] Task 2: 实现系统托盘与关闭行为
  - 创建 `QSystemTrayIcon`，设置图标和工具提示
  - 托盘图标右键菜单：显示主窗口、退出
  - 托盘图标左键单击：切换窗口显示/隐藏
  - 重写 `closeEvent`：弹出对话框（最小化到托盘 / 完全退出 / 取消）
  - 托盘菜单"退出"触发时调用 `_on_stop()` 然后 `QApplication.quit()`
  - 托盘工具提示动态更新当前模式

- [x] Task 3: 实现时间单位选择器
  - 在配置面板顶部新增一行：单位选择 QComboBox（秒 / 分钟 / 小时）
  - 默认选中"分钟"
  - 实现 `_on_unit_changed()` 方法：根据新旧单位换算 SpinBox 数值
  - 动态调整 SpinBox 的 range 和 suffix
  - 确保 `_load_config_to_ui` 和 `_collect_config_from_ui` 正确处理单位换算

# Task Dependencies
- Task 2 依赖 Task 1（托盘需要显示当前模式）
- Task 3 无依赖，可并行执行