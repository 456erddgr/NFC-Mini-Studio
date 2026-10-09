# NFC Mini 2.0 桌面读卡工具（NFC Mini Studio）v2.6.0

把原本只在小程序里使用的 NFC Mini 2.0 蓝牙读卡器能力搬到 Windows 桌面：扫描连接、寻卡、读卡、写卡、复制、密钥破解与卡夹管理。

> 本版已确认官方协议只提供 **蓝牙 BLE** 通信。此前把扩展坞误判成读卡器的 USB 直连、USB 串口入口已全部移除。

## 安装

推荐双击：

`E:\NFCMiniStudio\installer\dist\NFC Mini Studio 安装程序.exe`

安装程序支持：

- 无边框液态玻璃界面
- 自定义安装位置
- 桌面快捷方式
- 开始菜单快捷方式
- 安装完成后立即启动
- 免管理员权限安装到当前用户目录
- 静默安装：`"NFC Mini Studio 安装程序.exe" --silent [--target D:\路径] [--launch]`

绿色版：

`E:\NFCMiniStudio\dist\NFC Mini Studio\NFC Mini Studio.exe`

## 本版改动

### v2.6.0

- **修复「读卡 / 写卡失败」**：读整卡不再拿界面默认密钥覆盖破解结果。新增统一密钥解析
  `_resolve_key()`，按「会话破解 → 本卡历史库 → 密码库 → 界面输入 → 默认密钥」逐把真机认证，
  认证不过就明确报错（而不是静默读成 0/64）；读块沿用「该扇区实际认证通过」的 KEY A/B。
- **修复「破解只解 0 扇区」**：破解目标默认改为**全卡（全部 16 个扇区）**，新增
  `_crack_all_sectors()`：本地候选先扫（默认密钥卡秒级完成），仍有未解扇区才查官方 keygen；
  命中一把密钥后立刻复扫其余扇区；带总时间预算与可取消。寻卡时还会先把本卡历史库的密钥回填。
- **修复「云端 500+ 把候选密钥只剩十几把」**：`_pick_key()` 原先硬性只验证前 12 把
  （`CLOUD_VERIFY_LIMIT`），真正能开门的那把常排在后面。现改为全表逐把验证，带进度与取消。
- **修复密钥缓存错位**：`keybox_batch_verify` 曾按「块号」写 `_cracked_keys`，而该字典语义是
  「扇区号」，导致后续读卡取用错位；现统一用扇区号。
- **界面重做为 macOS 观感**：交通灯移到窗口左上角、SF + 苹方字体、Apple 系统蓝按钮、
  系统开关/分段控件；**保留液态玻璃材质并加大圆角**；新增 macOS 索诺玛 / 蒙特雷 / 石墨背景预设
  （默认索诺玛，原自定义壁纸仍保留在「外观设置」可一键切回）。
- 离线仿真自检的 `FakePeer` 改为「密钥白名单 + 认证态」，错误密钥读整卡必须 0 块，
  把这类 bug 真正锁住（旧版假设备对任何密钥都回成功，是假阳性）。
- 版本号统一为 2.6.0（`app/paths.py` 为唯一来源）。

### v2.3.0

- 删除 USB 直连与 USB 串口功能，连接方式只保留蓝牙 BLE。
- 设备页改为固定正面长方形展示，不再使用容易变形的 CSS 3D 旋转模型。
- 寻卡页移除金色芯片图形，改为长方形 NFC 卡片；卡片不透明，并跟随壁纸自动提取主色。
- 安装程序重做为无边框液态玻璃界面，加入软件预览、产品图、功能说明、使用方法和安装选项。
- 主窗口默认比例调整为 1280 × 820，最小尺寸 1080 × 700。

## 使用方法

1. 打开软件，点击「一键扫描设备」。
2. 选择 `NFC Mini 2.0` 完成蓝牙连接。
3. 将卡片贴近读卡器，进行寻卡、读卡、写卡或破解。
4. 在「外观设置」更换壁纸，卡片会自动跟随提取颜色。

连接前请先让读卡器与手机微信断开，避免蓝牙被占用。

## 重新构建

安装依赖：

```powershell
python -m pip install -r E:\NFCMiniStudio\requirements.txt
```

全量构建：

```powershell
cd E:\NFCMiniStudio
python build_all.py
```

只构建主程序：

```powershell
python build_all.py app
```

只构建安装程序（需已有 `installer\payload`）：

```powershell
python build_all.py installer
```

## 目录

- `app/`：Python 后端与原生 HTML/CSS/JS 前端
- `app/transport.py`：仅蓝牙 BLE 传输
- `app/device.py`：设备业务层
- `app/api.py`：前端调用接口
- `installer/installer.py`：无边框安装程序
- `dist/`：主程序构建产物
- `installer/dist/`：安装程序构建产物

## 协议要点

- BLE 服务：`0000FFE0-0000-1000-8000-00805F9B34FB`
- 写入特征：`0000FFE2-0000-1000-8000-00805F9B34FB`
- 通知特征：`0000FFE1-0000-1000-8000-00805F9B34FB`
- 帧格式：`FF | LEN | MSGID | DATA | SUM | FE`

详细逆向说明见：`E:\NFCMiniStudio\NFC_Mini_2.0_协议逆向报告.md`
