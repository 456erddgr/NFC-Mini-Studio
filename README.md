---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: f6356acaacd12c7e639ee70366cc8602_71879688c08811f1884b525400cd780f
    ReservedCode1: UTxdVyhkCRf0+OzSdDblvRsL+SWCIe9zfm4gY4fF5m0EaE3Q56ciFu40IpDf1ymyB16766Nmh4QLIBtEKb59gRLpD1qT2utkbdHgQbftoioUVQQaQXW4m7qsZBcriAqOuhLoAWq6ZO3xuI8+pchieQOpoijW36LCIXCruLUU05MiVi/9mmLZ6ECbSeo=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: f6356acaacd12c7e639ee70366cc8602_71879688c08811f1884b525400cd780f
    ReservedCode2: UTxdVyhkCRf0+OzSdDblvRsL+SWCIe9zfm4gY4fF5m0EaE3Q56ciFu40IpDf1ymyB16766Nmh4QLIBtEKb59gRLpD1qT2utkbdHgQbftoioUVQQaQXW4m7qsZBcriAqOuhLoAWq6ZO3xuI8+pchieQOpoijW36LCIXCruLUU05MiVi/9mmLZ6ECbSeo=
---

# NFC Mini 2.0 · 桌面读卡神器（NFC Mini Studio） v2.2.0

把原本只能在微信小程序里用的 **NFC Mini 2.0 蓝牙读卡器**，完整搬到 Windows 桌面上：
一键扫描连接、寻卡、读卡、写卡、复制、粘贴、密码破解、卡夹管理，界面是苹果味的液态玻璃，背景可随时更换。

**v2.2.0 新增**：钥匙破解升级为**双模式**——默认走**云端解算**（在线 mfkey32 云端服务，省本地算力、出结果快），
在「设置」里可一键切到**本地离线 mfkey32**（完全离线，不依赖网络与云端额度）；
云端未配置 / 调用失败 / 超时 / 返回异常时**自动降级**到本地算法继续破解，两条路都拿不到结果再交回固件级联兜底。

**v2.1.0 新增**：读卡器 **USB 有线直连**（libusb 厂商通道，与蓝牙 / USB 串口并列的第三种连接方式），
含设备发现、握手探测、试发帧与原始收发日志。

> 项目位置：本版本起整体位于 `E:\NFCMiniStudio`（含源码、打包产物与安装程序）。

---

## 一、安装（二选一）

### 方式 A：一键安装（推荐）
双击 `installer\dist\NFC Mini Studio 安装程序.exe` → 点「一键安装」→ 完成。
- 安装位置：`%LOCALAPPDATA%\Programs\NFCMiniStudio`（可在界面里点「更改」换盘）
- 自动创建桌面 + 开始菜单快捷方式
- 免管理员权限，可在「设置 → 应用 → 已安装的应用」里卸载
- 也支持静默安装：`"NFC Mini Studio 安装程序.exe" --silent [--target D:\路径] [--launch]`

### 方式 B：绿色免安装
直接运行 `dist\NFC Mini Studio\NFC Mini Studio.exe`，整个文件夹拷到哪都能用。

> 环境要求：Windows 10/11 + WebView2 运行库（Win11 自带，Win10 若缺失会由系统自动补装）。

---

## 二、功能一览

| 模块 | 能力 |
|------|------|
| 连接方式 | ① 蓝牙 BLE ② USB 串口（自动排除蓝牙虚拟串口）③ **USB 直连**（libusb 厂商通道，v2.1 新增） |
| 设备发现（直连） | 枚举可直连的 USB 设备（已知 VID/PID + 厂商自定义类带 bulk 端点），显示接口/端点/序列号 |
| 握手探测（直连） | 打开厂商接口并发送 INFO / STATE 帧，如实回报「应答 / 已发出无应答」 |
| 试发帧 + 原始日志 | 手动发送任意十六进制帧并收集回包，界面展示 TX/RX 原始字节流水 |
| 设备 | 一键扫描附近蓝牙设备 → 选中自动连接 → 显示版本、SN、信号强度 |
| 寻卡 | 实时寻卡，自动识别 IC 卡 / ID 卡 / UID 卡 / 7 字节 UID 卡，显示卡类型 / UID / SAK |
| IC 卡读写 | 16 扇区可视化网格，逐块读 / 写，密钥 A/B 认证，写后回读校验 |
| 复制 / 粘贴 | 整卡 dump 到卡夹，一键把卡夹里的数据写回新卡（克隆） |
| 钥匙破解 | **双模式（v2.2）**：默认**云端解算**，设置里可切**本地离线 mfkey32**，云端失效**自动降级**；两条路都无果时再自动级联 staticnested → staticnested2 → half_decode → hardnested → full_decode → FM11RF08S，逐个自动切换 |
| 破解设置（v2.2） | 云端地址 / Token / 超时 可配置（支持环境变量覆盖），本地算法一键自检（内置官方 mfkey32 测试向量） |
| 卡夹 | 卡片数据本地留档，随时读取、重命名、再次写入 |
| 进度可视化 | 读卡 / 写卡 / 复制 / 破解 / 卡夹写回 全程进度条（已完成块数 / 总块数 / 耗时） |
| 设备页 | 读卡器 3D 展示（正面实拍贴图 + CSS 3D 建模），绕 Y 轴 360° 旋转，指示灯随状态联动 |
| 外观设置 | macOS 液态玻璃（通透度 / 模糊 / 折射高光）、背景壁纸切换（自带 + 自定义图片） |
| 运行日志 | 全量协议收发日志，出问题一眼定位 |

---

## 三、目录结构

```
NFCMiniStudio/
├─ run.py                    开发运行入口
├─ build_all.py              一键构建脚本（主程序 + 安装程序）
├─ requirements.txt          依赖清单
├─ assets/                   应用图标（png / ico）
├─ app/                      主程序
│   ├─ main.py               窗口与启动
│   ├─ api.py                前端可调用的全部接口（含 wired_devices / probe_wired / connect_wired / crack_engine_status / crack_vectors / crack_start）
│   ├─ crack_engine.py       破解引擎：云端解算 + 本地离线 mfkey32 双模式与自动降级（v2.2）
│   ├─ mfkey_local.py        本地离线 mfkey32 实现（numba 加速，内置官方测试向量自检，v2.2）
│   ├─ device.py             设备层（连接 / 寻卡 / 识卡 / 读写 / 破解），对通道无感
│   ├─ transport.py          传输层：BleTransport / SerialTransport / WiredUsbTransport（v2.1）
│   ├─ protocol.py           私有协议帧编解码与命令表
│   ├─ store.py              设置持久化 + 卡夹管理
│   ├─ paths.py              路径解析 / 版本号
│   ├─ selftest.py           后端自检（接口联调）
│   └─ web/                  前端（HTML / CSS / JS，液态玻璃 UI）
│       ├─ assets/           设备正面实拍图等素材
│       └─ wallpapers/       壁纸目录
├─ installer/
│   ├─ installer.py          图形化一键安装程序
│   ├─ payload/              待安装的主程序数据
│   └─ dist/                 生成的安装程序 exe
├─ tools/                    自检工具（check_align.py / verify_ui.py 等）
└─ dist/NFC Mini Studio/     生成的主程序（绿色版）
```

> 注意：`tools/check_align.py`（静态对齐）与 `tools/verify_ui.py`（实机点按）必须在改完前端后各跑一遍再打包。

---

## 四、二次开发

```bat
:: 安装依赖
pip install -r requirements.txt

:: 直接跑源码
python run.py

:: 重新打包（主程序 + 安装程序）
python build_all.py

:: 只打包其中一步
python build_all.py app
python build_all.py installer
```

---

## 五、协议速查（逆向所得）

- 帧格式：`FF | LEN | MSGID | DATA | SUM | FE`，`SUM = DATA 逐字节累加 & 0xFF`
- BLE：服务 `FFE0` / 写 `FFE2` / 通知 `FFE1`
- 常用命令码：
  `0x01` 设备信息 · `0x11` 寻卡 · `0x07` 读块 · `0x08` 写块 · `0x0A` 密钥认证 · `0x0B` 全卡扫描 · `0x20/0x30/0x32/0x37/0x38` 破解系列
- 错误码：`0` 成功 · `7` 寻卡失败 · `8` 密钥认证失败 · `9` 读卡失败 · `10` 写卡失败 · `17` 检测不到卡片

### USB 直连（厂商通道，v2.1 实测）

本机实测读卡器的 USB 描述为 `VID_0483 & PID_66AA`（产品串「佳点V1PRO」）：

| 项 | 值 |
|----|----|
| 接口 0 | 厂商自定义类（`bInterfaceClass = 0xFF`），可用于私有帧收发 |
| Bulk OUT | `0x01` |
| Bulk IN | `0x81` |
| 单包长度 | 64 字节 |
| 接口 1 | HID 键盘接口（不参与读卡通信） |
| 串口 | 本机无 USB 串口，仅有蓝牙虚拟串口（已被串口枚举自动排除） |

> 实测：可正常打开接口并发出 6 字节私有帧 `FF 06 01 00 00 00 00 FE`；设备**暂未按本协议应答**
> （固件可能使用私有帧格式或需要先做端点复位）。软件对此**如实提示「已发出、无应答」**，不做任何编造；
> 硬件具备该通道、协议层握手完成，后续固件放出对应帧格式即可直接复用该页面完成收发。

详见 `NFC_Mini_2.0_协议逆向报告.md`。

---

## 六、版本历史

| 版本 | 要点 |
|------|------|
| 1.0.0 | 首个桌面版：蓝牙连接、寻卡、读写、复制粘贴、破解、卡夹 |
| 2.0.0 | 修复「读卡成功却写不了卡」（写前密钥认证 A/B 自动切换、写后回读校验、写帧格式自适应）；寻卡自动识卡（IC/ID/UID/7 字节 UID）；破解自动级联并实时显示方法与耗时；读/写/复制/破解全程进度条；macOS 液态玻璃 UI；设备页 3D 展示；USB 串口连接；项目迁移至 `E:\NFCMiniStudio` |
| 2.1.0 | 新增 **USB 有线直连**通道（libusb 厂商接口 + Bulk 端点）：设备发现 / 握手探测 / 试发任意帧 / 原始收发日志；`app.js` 全量同步重写并与后端接口一一对齐；README 与打包产物同步更新 |
| 2.2.0 | 钥匙破解升级为**双模式**：默认**云端解算**，设置里可切**本地离线 mfkey32**，云端失效**自动降级**；新增云端地址 / Token / 超时配置与本地算法一键自检（官方 mfkey32 测试向量 5/5 命中）；安装程序界面功能说明、README、前端版本号同步至 v2.2.0 |

---

作者：只因君 · 版本 2.2.0
*（内容由AI生成，仅供参考）*
