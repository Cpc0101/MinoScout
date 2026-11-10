# MinoScout

执行器。接收 MinoNexus 下发的动作，操作设备，回报结果。

## 五个渠道都有的事件

| 事件 | 程序在做什么 |
|---|---|
| `launch_app` | 打开被测应用或页面。Android 用包名，iOS 和 Mac 用 bundle id，Windows 用 EXE 路径或 AUMID，Web 用地址 |
| `close_app` | 关闭被测应用或页面 |
| `tap_element` | 点一次。按该端的定位，不连点 |
| `tap_xy` | 只按坐标点一次，不查控件 |
| `input_text` | 向控件或当前焦点输入 |
| `press_key` | 按键。iOS 只接受 `home`、`volume_up`、`volume_down` |
| `swipe_direction` | 按 up / down / left / right 滑动，或按起止坐标滑 |
| `swipe_element_to_element` | 从一点拖到另一点 |
| `long_press_element` | 长按 |
| `set_clipboard` | 写剪贴板 |
| `get_foreground_app` | 读当前前台应用或当前页 |
| `screenshot` | 截图 |
| `hierarchy` | 读界面树。Mac / Windows 用辅助功能树里的文案定位控件 |
| `exec_script` | 执行该端自己的脚本命令。Android 是设备 shell，iOS 在连接设备的本机执行并带上 `MINO_IOS_UDID`，Web 在页面里执行 JavaScript，Mac 用 zsh，Windows 用 PowerShell |

## 各处单独的事件

| 渠道 | 事件 |
|---|---|
| Android | - `install`<br>- `uninstall_app`<br>- `clear_app_cache`<br>- `get_app_version`<br>- `read_device_data`<br>- `set_input_method`<br>- `wake_screen`<br>- `dismiss_keyguard`<br>- `probe_device_state` |
| iOS | - `install`<br>- `uninstall_app`<br>- `get_app_version` |
| Web | - `reload_page`<br>- `open_tab`<br>- `switch_tab`<br>- `upload_file`<br>- `wait_screen_ready`<br>- `read_web_auth`<br>- `open_web_v5`<br>- `tap_target`<br>- `input_target`<br>- `read_web_target_facts` |
| Mac | - `install` |
| Windows | - `install` |


## 工具侧事件

| 工具   | 事件 |
|------|---|
| Scout | - `wait`<br>- `sample`<br>- `on_page`<br>- `otp(account)`<br>- `llm`<br>- `vlm`<br>- `vlm_frame` |
| Nexus | - `account`<br>- `note`<br>- `prep`<br>- `do`<br>- `check`<br>- `fail` |

Android 的 `install_apk` 进来后改写成 `install`。`wake_screen`、`dismiss_keyguard`、`probe_device_state` 没有单独分支，载荷带了可执行的 `low_level` 才跑。`low_level` 的 shell 只在 Android 生效。iOS 接到 `clear_app_cache` 会拒绝。Mac 的安装包是 `.app`、`.pkg`、`.dmg`。Windows 的安装包是 `.exe`、`.msi`。Scout 和 Nexus 不是设备渠道。验证码和模型在 Scout 本机完成，密钥和视觉判断用的截图不回 Nexus。`prep`、`do`、`check` 把步骤进入和离开直接写入 Nexus 会话，`fail` 把失败原因直接写入，`account` 和 `note` 也直接交给 Nexus。

