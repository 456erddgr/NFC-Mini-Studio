# 手机读卡器 APK 说明（Android 端）

> 适用版本：**APK 1.1.0**（对应桌面端 v3.1.0）
> 包名：`net.nfcreader.minireader`　最低系统：Android 5.0（API 21）　目标：API 34

这个 APK 的作用只有一句话：**把一台带 NFC 的安卓手机，变成电脑能用的 NFC 读卡器。**

电脑通过 Wi-Fi 连上手机，之后所有「寻卡 / 读块 / 写块 / 写 UID / 整卡导出 / 密钥校验」都在电脑上操作，
手机只负责最后那一米——把射频命令发给卡片。

---

## 1. 相对 1.0.0 改了什么

1.0.0 能用，但坑不少。1.1.0 是一次比较大的重修。

### 1.1 修掉的 bug

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | 读块时**忽略传入的密钥**，硬编码 `FF FF FF FF FF FF` | 只要卡片不是出厂密钥，读块**必然失败** | 认证结果按 `(扇区, 密钥类型)` 缓存，读块前先查缓存、再用传入密钥认证 |
| 2 | 每次操作都 `MifareClassic.connect()/close()` | 认证态随连接一起丢掉，极慢，写块经常失败 | 连接与认证态跨命令保持，只在卡片变化 / RESET 时关闭 |
| 3 | `statusPos()` 缺省值写反（default 返回 2） | `0x17 / 0x23 / 0x24 / 0x26 / 0x27 / 0x38` 的**错误码被电脑读成「成功」** | 与桌面端 `protocol.STATUS_INDEX` 完全对齐：只有 `0x06/0x07/0x08/0x09/0x0A/0x0B/0x20` 在 index 2 |
| 4 | 卡片放 8.5 秒后被强行判为「已离开」 | **整卡导出中途失败**（16 扇区读不完 8.5 秒） | 取消定时清空，改为「每次成功操作刷新 30 秒空闲期」+ 失败即失效 |
| 5 | 服务放在 Activity 里 | 一切后台 / 锁屏就掉线 | 改由**前台 Service** 托管，通知栏常驻 |
| 6 | 信标广播的 IP 只算一次 | 手机换 Wi-Fi 后电脑搜到的还是旧 IP | 每轮广播重新取 IP |
| 7 | 设备 SN 用 `Build.SERIAL` | targetSdk 31+ 恒为 `unknown`，每次启动 SN 都变 | 首次生成后写入 SharedPreferences 持久化 |
| 8 | `getSak()` 调错 API | 编译期就挂（`MifareClassic` 没有这个方法） | 改从 `NfcA.getSak()` 取 |
| 9 | 0x0C 写卡号用了未初始化变量 | 写卡号偶尔写进全 0 块 0 | 统一走 `buildBlockZero(uid4, sak)`，自带 BCC 计算 |

### 1.2 新增的功能

- **NFC 引导与权限**：自动检测「本机不支持 NFC / NFC 未开启 / 已开启」，未开启时直接给「去开启 NFC」按钮，逐级降级尝试 NFC 设置 → 无线设置 → 应用详情。Android 13+ 会请求通知权限（前台服务必需）。
- **液态玻璃界面**：壁纸 + GPU 实时高斯模糊（`RenderEffect`，Android 12+）+ 半透明渐变卡片 + 高光描边 + 涟漪。
- **背景更换**：7 套内置渐变（午夜蓝 / 极光 / 落日 / 深海 / 森林 / 石墨 / 雾玫），也可以**从相册选自己的图**（走 `ACTION_OPEN_DOCUMENT`，不需要存储权限）。模糊强度 0~40 可调，可一键关闭玻璃效果。
- **本机读卡工具**（不连电脑也能用）：
  - 「读取卡片信息」——UID、SAK、ATQA、卡类型、扇区数/块数/容量
  - 「读扇区」——输入扇区号 + 密钥，直接看 4 个块的十六进制数据（尾块会标注）
  - 「复制 UID」
- **浅色 / 深色主题**切换。
- **TCP 端口可改**（1024~65535），避免端口冲突。
- **常亮 / 震动 / 自动启动**开关，全部持久化。
- **运行日志**面板（等宽字体、可选中、可复制、可清空）。
- **正式签名**：1.0.0 用的是 debug 签名，1.1.0 起用专门生成的 release 证书，升级安装不会因为签名不一致而失败。

---

## 2. 权限到底要哪些

很多人以为 NFC 需要「运行时申请」，其实不是。这里把话说清楚：

| 权限 | 申请方式 | 用来干什么 | 不给会怎样 |
|---|---|---|---|
| `android.permission.NFC` | **安装时自动授予**，没有运行时弹窗 | 调用 NFC 硬件 | 装不了（已声明 `uses-feature required=true`） |
| `VIBRATE` | 安装时自动授予 | 读卡震动反馈 | 只是不震 |
| `INTERNET` / `ACCESS_NETWORK_STATE` / `ACCESS_WIFI_STATE` | 安装时自动授予 | TCP 服务 + 取本机 IP | 服务起不来 |
| `CHANGE_WIFI_MULTICAST_STATE` | 安装时自动授予 | UDP 广播信标（让电脑能自动搜到手机） | 只能手动填 IP |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_CONNECTED_DEVICE` | 安装时自动授予 | 让服务切后台不掉线 | 一切后台就断 |
| `POST_NOTIFICATIONS` | **Android 13+ 运行时弹窗** | 前台服务的常驻通知 | 服务仍能跑，但看不到通知 |
| `WAKE_LOCK` | 安装时自动授予 | 保持读卡过程中不休眠 | 长时间操作可能中断 |

**除了系统 NFC 开关，你唯一会看到的权限弹窗是 Android 13+ 的通知权限。**
NFC 本身只需要你**手动打开系统开关**（设置 → 更多连接 / 连接与共享 → NFC），App 会给你一个直达按钮。

> 为什么不能「在 App 里自动打开 NFC」？
> 从 Android 10 起，任何应用都**不允许**用代码静默开关 NFC，只能跳设置页让用户自己点。这是系统限制，不是偷懒。

> 为什么必须让界面保持在前台？
> Android 的 NFC 轮询（reader mode）是绑定在 Activity 上的，Activity 一旦暂停，读卡就停。所以手机端读卡期间请**别锁屏、别切走**。
> 前台服务能保证「TCP 连接不断」，但**读卡本身**需要界面在前台。App 默认会开屏幕常亮。

---

## 3. 安装

### 3.1 从 Release 下载（推荐）

去 [Releases](https://github.com/456erddgr/NFC-Mini-Studio/releases/latest) 下 `NFCMiniReader.apk`，
传到手机（微信文件传输 / 数据线 / `adb install`）后点击安装。

首次安装需要在系统里允许「安装未知应用」。

用 adb 更省事：

```bash
adb install -r NFCMiniReader.apk
```

### 3.2 自己编译

```bash
python build_android.py            # 输出到 android/NFCMiniReader.apk
python build_android.py --debug    # debug 版（包名带 .debug 后缀，可与正式版共存）
python build_android.py --install  # 编译完顺手 adb install
```

前提：JDK 17+、Android SDK（platform 34 + build-tools 34.0.0）、Gradle 8.7。

脚本会自动把 `android/` 镜像到 `C:\nfcmreader-build`（纯 ASCII 路径）再构建，
因为 AGP 8.x 在 Windows 上遇到中文路径会直接报
`Your project path contains non-ASCII characters` —— 而本项目路径里有「我的项目」四个字。

---

## 4. 签名说明（重要）

`android/keystore/nfcmini-release.jks` 是本项目**公开的演示签名证书**，
密码就写在 `android/keystore.properties` 里：

```properties
storeFile=keystore/nfcmini-release.jks
storePassword=nfcmini2026
keyAlias=nfcmini
keyPassword=nfcmini2026
```

证书信息：`CN=NFC Mini Studio, OU=RC2 Unlock Project, O=RC2 Unlock Project, C=CN`，RSA 2048，有效期 30 年。

**为什么把私钥公开？**

1. 这是个免费开源的小工具，不存在「保护品牌」的需求；
2. 公开后任何人都能克隆源码，**构建出与 Release 上字节级一致的签名包**，方便增量覆盖安装；
3. 免得用户遇到「签名不一致，请先卸载旧版本」这种破事。

**如果你想以自己名义发布**，替换掉这两个文件即可：

```bash
keytool -genkeypair -v -keystore my-release.jks -alias mykey \
  -keyalg RSA -keysize 2048 -validity 10950 \
  -dname "CN=Your Name, O=Your Org, C=CN"
```

然后改 `keystore.properties`。没有 `keystore.properties` 时，`app/build.gradle`
会自动退回 debug 签名，保证总能装。

验证签名：

```bash
keytool -printcert -jarfile NFCMiniReader.apk
```

---

## 5. 界面怎么用

主界面从上到下五块：

### 5.1 状态胶囊

`NFC：已开启 / 未开启 / 本机不支持`　`网络：192.168.x.x`　`卡片：A1B2C3D4`

### 5.2 读卡服务

- 大按钮「启动服务 / 停止服务」
- 大字号显示 `IP:端口`，电脑就是照这个填
- 「N 个电脑连接」
- 点「端口 8765」小胶囊可跳到端口输入框
- NFC 没开时这里会出现「去开启 NFC」
- 「重新检测」重走一遍硬件检测

### 5.3 本机读卡

贴卡后自动填 UID / 卡类型 / 技术 / SAK+ATQA / 扇区数·块数·容量。
三个按钮：

- **读取卡片信息**：重新读一遍
- **读扇区**：弹窗输入扇区号（0~39）和密钥（12 位十六进制，留空 = `FFFFFFFFFFFF`），
  返回 4 个块的十六进制值，尾块会标「（尾块·密钥/控制位）」
- **复制 UID**

### 5.4 背景与外观

- 一排圆形色板：7 套内置渐变 + 「相册」入口
- 「背景模糊 0~40」滑杆
- 「液态玻璃效果」开关（关掉 = 纯色不透明卡片，省电）
- 「浅色主题」开关

> 模糊实现：Android 12+ 走 `RenderEffect.createBlurEffect()`，是 GPU 实时模糊，滑杆拖动即时生效；
> Android 11 及以下没有这个 API，退化成「先缩小再放大」的位图模糊，效果差一些但也能看。

### 5.5 设置 & 日志

- 保持屏幕常亮 / 读卡震动 / 打开应用时自动启动服务
- TCP 端口输入框 + 「应用」（运行中改端口会自动重启服务）
- 日志面板：复制 / 清空

---

## 6. 电脑端怎么连

1. 手机和电脑连**同一个 Wi-Fi**（手机热点也行，电脑连手机热点）
2. 手机上点「启动服务」，记下显示的 `IP:端口`
3. 电脑打开 NFC Mini Studio → 设备页 → 切到「手机读卡器」
4. 点「搜索手机读卡器」（走 UDP 8766 广播自动发现）
5. 列表里出现 `Pixel-8 · 192.168.1.5:8765` 就点它
6. 搜不到就点「手动输入地址」，手填 `192.168.1.5:8765`

连上后和原厂读卡器用法完全一样，只是**手机侧的卡要贴着手机背面**。

---

## 7. 能做什么 / 不能做什么

| 功能 | 手机读卡器 | 说明 |
|---|---|---|
| 寻卡 / 卡片识别 | ✅ | UID、SAK、卡类型 |
| 读块 / 读扇区 / 整卡导出 | ✅ | 支持 KEY A / KEY B |
| 写块（三种模式） | ✅ | 极速 / 普通 / 带密钥 |
| 写 UID / 写卡号 | ✅ | 需要块 0 可写（UID 卡） |
| 密钥校验 | ✅ | 逐密钥试 |
| 设备端破解（0x20/0x22/0x30/0x32/0x41） | ❌ | 需要原厂固件生成随机数并采集，手机 NFC 芯片做不到 |
| 读 125 kHz ID 卡（0x26） | ❌ | 手机 NFC 只有 13.56 MHz |
| 封卡 / GTU（0x17/0x23） | ❌ | 依赖固件层写锁，厂商私有实现 |
| 固件升级（0x90/0x91） | ❌ | 没有原厂硬件 |

电脑端会根据通道自动禁用这些按钮，不会让你点了才报错。

---

## 8. 协议（给想自己写客户端的人）

手机端是**服务端**，协议与原厂蓝牙读卡器完全一致。

### 8.1 传输层

1. 手机监听 TCP `8765`，同时每 1500 ms 向 `255.255.255.255:8766` 广播一行 UTF-8 文本：
   ```
   NFCMINI/1 <设备名> <本机IP> <端口>
   ```
   例：`NFCMINI/1 Pixel-8 192.168.1.5 8765`

2. 电脑连上后，手机**先发一行同样的握手文本 + `\n`**，然后进入二进制帧循环。
   （电脑端 `NetTransport` 会把第一个 `\n` 之前的内容当握手丢掉。）

### 8.2 帧格式

短帧（`DATA ≤ 255`）：

```
FF | LEN | MSGID | DATA… | SUM | FE
```

长帧（`DATA > 255`）：

```
FD | LEN_H | LEN_L | MSGID | DATA… | SUM | FE
```

- `LEN` = DATA 长度
- `SUM` = `sum(DATA) & 0xFF`（**不包含** MSGID）
- `MSGID` ∈ 1..253，应答必须**原样带回**请求的 MSGID
- 接收方要能容忍噪声字节、校验失败的帧（直接丢弃重同步）

### 8.3 支持的命令

`0x01` INFO、`0x02` STATE、`0x06` KEY A 校验、`0x07` 读块、`0x08` 写块、`0x09` KEY B 校验、
`0x0A` AUTH（空命令，恒回 `0A 00 00`）、`0x0B` 扫描、`0x0C` 写卡号、`0x11` 寻卡、
`0x16` 极速写块、`0x18` 普通写块、`0x24` 卡号校验、`0x27` 写 UID、`0x7F` 复位。
其余命令统一回「参数超出范围（15）」。

### 8.4 状态码位置（最容易踩的坑）

不同命令的**状态码在 DATA 里的下标不一样**：

| 命令 | 状态码下标 |
|---|---|
| `0x06 / 0x07 / 0x08 / 0x09 / 0x0A / 0x0B / 0x20` | **2** |
| 其它所有命令 | **1** |

搞错的话错误码会被读成「成功」，1.0.0 就栽在这里。

### 8.5 寻卡应答（UID 是反的！）

4 字节 UID 卡：

```
11 | 00 | typeId | uid[3] uid[2] uid[1] uid[0] | SAK | isUID
```

注意 UID **故意反序**发送——因为桌面端 `uid_reverse()` 会再翻一次。

7 字节 UID 卡（Ultralight / NTAG）：

```
11 | 00 | 04 | uid[0..6] | SAK | isUID
```

7 字节布局**保持正序**，且总长 12 字节。

---

## 9. 代码结构

```
android/
├── build.gradle                     AGP 8.6.1
├── keystore.properties              签名配置（公开演示证书）
├── keystore/nfcmini-release.jks     签名证书
├── app/build.gradle                 compileSdk 34 / minSdk 21 / versionName 1.1.0
└── app/src/main/
    ├── AndroidManifest.xml
    ├── java/net/nfcreader/minireader/
    │   ├── Protocol.java            帧编解码 + 常量 + 状态码位置表
    │   ├── NfcEngine.java           私有命令 → Android NFC API（核心）
    │   ├── ReaderServer.java        TCP 服务 + UDP 信标
    │   ├── ReaderCore.java          单例：持有 engine/server/settings
    │   ├── ReaderService.java       前台 Service（后台存活）
    │   ├── MainActivity.java        界面 + 液态玻璃 + 换背景 + 本机读卡
    │   ├── DeviceInfo.java          设备名/SN/IP/电量
    │   ├── AppSettings.java         SharedPreferences 封装
    │   └── WallpaperFactory.java    渐变壁纸生成 + 模糊 + 自定义图导入
    └── res/                         布局 / 字符串 / 图标（PNG 由 make_icons.py 生成）
```

数据流：

```
电脑 NfcDevice.request()
  → 私有帧  →  NetTransport.send()  →  TCP
  →  手机 ReaderServer.serveClient()
  →  Protocol.Parser.feed() 拆出 (msgid, data)
  →  NfcEngine.dispatch(data)  →  Android NfcA / MifareClassic / MifareUltralight
  →  Protocol.buildFrame(reply, msgid)  →  TCP 回写
  →  电脑 FrameParser.feed()  →  业务层
```

---

## 10. 常见问题

**Q：手机贴卡没反应？**
A：① 确认系统 NFC 已开；② 卡贴在天线位置（多数机型在摄像头附近，可拿身份证试）；③ 手机壳太厚（>3 mm）会挡；④ App 必须在前台。

**Q：电脑搜不到手机？**
A：① 两边必须同一网段；② 路由器开了「AP 隔离」要关掉；③ 有些公司 Wi-Fi 禁止广播 → 用手动填 IP；④ 手机是「客户端隔离」热点也要注意。

**Q：能连上但一读卡就报错？**
A：看手机上的日志面板。常见是「卡离开感应区」——手机 NFC 感应距离只有 1~3 cm，且要求卡稳定不动。读整卡时最好把卡平放在手机背面别动。

**Q：读块一直认证失败？**
A：说明这张卡的扇区密钥不是 `FFFFFFFFFFFF`。先用电脑端「破解」功能（需要原厂读卡器）或已知密钥试。手机端本身不能采集随机数破解。

**Q：为什么不能后台读卡？**
A：Android 的 reader mode 绑在 Activity 上，Activity 暂停就停。前台服务只保住 TCP 连接，保不住 NFC 轮询。这是系统限制。

**Q：升级安装提示签名不一致？**
A：1.0.0 是 debug 签名，1.1.0 换成 release 证书了。**先卸载旧版再装**，之后就不会再变。

**Q：APK 会收集我的数据吗？**
A：不会。APK 不联网（除了局域网 TCP 服务），没有任何统计 SDK，不需要任何账号。

---

## 11. 许可

MIT。和主项目一致，见仓库根目录 `LICENSE`。
