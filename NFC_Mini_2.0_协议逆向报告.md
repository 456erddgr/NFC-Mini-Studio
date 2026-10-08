---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: f6356acaacd12c7e639ee70366cc8602_b27c16ccc08111f1884b525400cd780f
    ReservedCode1: mRjc6lTUvo7aJoWk1xGmlIdiw1d4mx1+iRwWMiNO1aPN8bNamfAtaTjNaMLankNW76UKL3ZazmHg4FOKJx/kTgbGi/MMQF3XYfqgEOEKREstI8vl+t3y+tYTSQMfPKiqs8E15fyUUKbOwwhof5jr0flrl2Gm2kjHVTPg/Xe6p/Ce5Exd67OPJYK/oFA=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: f6356acaacd12c7e639ee70366cc8602_b27c16ccc08111f1884b525400cd780f
    ReservedCode2: mRjc6lTUvo7aJoWk1xGmlIdiw1d4mx1+iRwWMiNO1aPN8bNamfAtaTjNaMLankNW76UKL3ZazmHg4FOKJx/kTgbGi/MMQF3XYfqgEOEKREstI8vl+t3y+tYTSQMfPKiqs8E15fyUUKbOwwhof5jr0flrl2Gm2kjHVTPg/Xe6p/Ce5Exd67OPJYK/oFA=
---

# NFC Mini 2.0 蓝牙读卡器 · BLE 私有协议逆向报告

> 目标设备：NFC Mini 2.0（宜新 NFC Reader Tool）  
> 逆向来源：微信小程序 `wxc3dd33d9d4ae0fd6` 解密包 `app-service.js` + 实机通信验证  
> 验证状态：**已实测通过**（设备信息读取、寻卡指令、校验和算法全部与代码一致）  
> 日期：2026-10-05

---

## 一、设备与 BLE 连接参数

| 项目 | 值 |
|---|---|
| 设备名称 | NFC Mini 2.0 |
| MAC 地址 | `D0:0C:5E:94:0B:A8` |
| 服务 UUID | `0000FFE0-0000-1000-8000-00805F9B34FB` |
| 写特征 | `0000FFE2-0000-1000-8000-00805F9B34FB` |
| 通知特征 | `0000FFE1-0000-1000-8000-00805F9B34FB` |
| 通信方式 | BLE 串口透传（NUS 风格） |
| 固件版本 | 0.1.2（实测读出） |
| 序列号 | `A80B945E0CD0483AB3F2DC82`（前 6 字节为 MAC 反序） |

**连接要点**

- 小程序侧不硬编码 UUID，而是：遍历服务找到 uuid 含 `FFE0` 的服务 → 遍历其特征，`properties.write` 者为写特征、`notify/indicate` 者为通知特征。因此不同批次硬件即使 UUID 变化也能自适应。
- 写操作分片：每片长度 = `MTU - 3`，起始按 20 字节，若返回 `errCode 10008` 则折半分片重试。
- 设备**不会主动上报数据**，必须先由主机发送命令；连接后即处于待命状态。

---

## 二、帧格式（核心）

主机与设备使用**同一种帧结构**，双向对称：

```
┌────┬─────┬───────┬─────────────┬─────┬────┐
│ FF │ LEN │ MSGID │    DATA     │ SUM │ FE │
└────┴─────┴───────┴─────────────┴─────┴────┘
  1B   1B     1B      LEN 字节       1B   1B
```

| 字段 | 长度 | 说明 |
|---|---|---|
| `FF` | 1 | 帧头，固定 0xFF |
| `LEN` | 1 | **DATA 区字节数**（不含头尾与校验） |
| `MSGID` | 1 | 消息 ID，主机分配，取值 1~253 循环递增；设备响应时**原样回显** |
| `DATA` | LEN | `DATA[0]` = 命令码，其后为参数/数据 |
| `SUM` | 1 | **校验和 = DATA 全部字节累加后取低 8 位**（`sum(DATA) & 0xFF`） |
| `FE` | 1 | 帧尾，固定 0xFE |

- 帧总长度 = `LEN + 5`
- 长数据（如写卡升级）另有 `0xFD` 开头的 6 字节头变体：`FD | LEN_H | LEN_L | CMD | DATA | SUM | FE`，用于分包传输。

**校验和计算示例（实测）**

```
发送: FF 01 01 11 11 FE
      ↑  ↑  ↑  ↑  ↑  ↑
      │  │  │  │  └─┴─ SUM = 0x11+0x07? 否 —— 发送侧 SUM = sum(0x11) = 0x11
      │  │  │  └─ DATA = [0x11]
      │  │  └─ MSGID = 1
      │  └─ LEN = 1
      └─ 0xFF

接收: FF 02 01 11 07 18 FE
      LEN=2, MSGID=1, DATA=[0x11, 0x07], SUM=0x11+0x07=0x18 ✓
      → 0x07 = "IC寻卡失败"（当前未放卡）
```

---

## 三、命令总表

`DATA[0]` 为命令码，下表为请求方向：

| CMD | 功能 | 请求 DATA 布局 | 响应要点 |
|---|---|---|---|
| `0x01` | 获取设备信息 | `[01]` | `DATA[1..3]`=版本号，`DATA[4..15]`=序列号 |
| `0x02` | 获取设备状态 | `[02]` | 状态字节 |
| `0x06` | 密钥校验（KEY A） | `[06, block, key0..key5]` (8B) | `DATA[2]==0` 为成功 |
| `0x07` | 读块 | `[07, block, keyType]` | `[07, block, status, data0..15]` |
| `0x08` | 写块（带密钥） | `[08, block, keyType, key0..5, data0..15]` (22B) | `DATA[2]==0` 成功 |
| `0x09` | 密钥校验（KEY B） | `[09, block, key0..key5]` | 同 0x06 |
| `0x0A` | 密钥认证 | `[0A, block, keyType, key0..5]` (9B) | 认证结果 |
| `0x0B` | 全卡扫描/解码 | `[0B]` | 响应 `0x11` 头 + 多帧块数据流 |
| `0x0C` | 写卡 | `[0C, d0..d4]` (6B) | |
| `0x11` | 寻卡（读卡号） | `[11]` | `[11, status, type, uid0..3, sak, isUid]` |
| `0x16` | 写块数据 | `[16, block, data0..15]` (18B) | |
| `0x17` | 封卡 | `[17]` | |
| `0x18` | 写块（含密钥类型） | `[18, block, keyType, data0..15]` (19B) | |
| `0x20` | hardnested 密钥采集 | `[20, t, s[n], a, s[r]]` (5B) | 用于 M1 卡密钥破解 |
| `0x22` | full_decode 全卡解码 | `[22, block, keyType]` (3B) | |
| `0x24` | 保留指令 | `[24]` | |
| `0x25` | is_bug 漏洞卡检测 | `[25, t, keyType, a]` (5B) | |
| `0x26` | 保留指令 | `[26]` | |
| `0x27` | 写 UID 卡 | `[27, 00, uid 5B 大端]` (8B) | |
| `0x30` | half_decode 半解 | `[30, t, n, keyTypeA, keyTypeB]` (5B) | |
| `0x32` | staticnest 静态嵌套 | `[32, t, n, keyB, keyA]` (5B) | |
| `0x34` | 保留指令 | `[34]` | |
| `0x35` | 卡片信息上报 | `[35, uid..., SAK, ATQA]` | |
| `0x36` | 保留指令 | `[36]` | |
| `0x37` | FM11RF08S 专用 | `[37, t, keyType]` (3B) | |
| `0x38` | staticnest v1 | `[38, t, n, keyB, keyA]` (5B) | |
| `0x41` | 批量寻卡 | `[41, keyOffH, keyOffL, cntH, cntL, keyType, r]` (7B) | |
| `0x91` | 扩展数据操作 | `[91, 6B]` (7B) | |

> `keyType`：`0 = KEY A`，`1 = KEY B`

**响应命令码与状态字节位置表**（小程序内部用于快速判定"无卡"错误）：

```
{ 0x05:1, 0x11:1, 0x06:2, 0x07:2, 0x08:2, 0x09:2, 0x0B:2,
  0x20:2, 0x21:1, 0x22:1, 0x25:1, 0x30:1, 0x32:1, 0x34:1,
  0x35:1, 0x36:1, 0x37:1 }
```

---

## 四、状态码 / 错误码表

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 错误 Code:1 |
| 2 | 指令长度错误 |
| 3 | 指令 CRC 校验错误 |
| 4 | 固件升级地址错误 |
| 5 | 固件升级扇区擦除失败 |
| 6 | 固件升级写入失败 |
| 7 | **IC 寻卡失败**（未检测到卡） |
| 8 | 密钥认证失败 |
| 9 | IC 读卡失败 |
| 10 | IC 写卡失败 |
| 11 | 写 UID 卡失败 |
| 12 | 写 GTU 卡失败 |
| 13 | 写 IC 卡控制段错误 |
| 14 | 写 IC 卡厂商数据段错误 |
| 15 | 参数超出范围 |
| 16 | 采集随机数失败 |
| 17 | **检测不到卡片，请重新摆放后再试**（即 0x11） |

**辅助码表**

- ID 卡频率：`{1:"125kHz", 2:"250kHz", 3:"375kHz", 4:"500kHz"}`
- HID 卡位宽：`{1:"HID 26bit" ... 12:"HID 37bit"}`

---

## 五、实测通信记录

### 1. 获取设备信息

```
-> FF 01 01 01 01 FE
<- FF 10 01 01 00 01 02 A8 0B 94 5E 0C D0 48 3A B3 F2 DC 82 0A FE

LEN=0x10(16), MSGID=0x01 ✓
DATA = 01 00 01 02 A8 0B 94 5E 0C D0 48 3A B3 F2 DC 82
       │  └──┬──┘ └──────────────┬──────────────┘
       │     └ 版本 = 0.1.2      └ 序列号(前6字节为MAC反序)
       └ 命令码 0x01
SUM  = 0x0A ✓（校验通过）
```

### 2. 寻卡（未放卡）

```
-> FF 01 01 11 11 FE
<- FF 02 01 11 07 18 FE
      LEN=2, MSGID=1, DATA=[11, 07]  → 状态 0x07 = IC寻卡失败（未放卡）
```

### 3. 寻卡成功时的响应解析

```
DATA = [11, status, type, uid0, uid1, uid2, uid3, sak, isUid]
uid  = bytes(uid0..uid3)[::-1].hex()      # 4 字节 UID 需反序
```
额外逻辑：若 `DATA[9..10]` 反序后为 `4400` 或 `4800`，则判定为 **7 字节 UID 卡**，再取 `DATA[11..13]` 反序拼接为前 3 字节。

---

## 六、卡片数据结构（小程序内部模型）

```
{
  H: "",                    // 卡号（hex）
  type: "",                 // 卡类型
  IC: { H, type, source[] },// IC 卡
  ID: { H, D, type, source[] },  // ID 卡（含频率 kHz）
  HID: { id, cardType, card_code, facility_code }  // HID 卡（含位宽）
}
```

支持卡种：M1 / UID / CUID / FUID / UFUID / GDM-GTU / 5577 / 5200 / 8265 等，
13.56MHz IC 卡与 125/250/375/500kHz ID 卡，并内置 M1 卡密钥破解
（staticnest、hardnested、half_decode、FM11RF08S 等攻击指令集）。

---

## 七、Windows 端复刻实现要点

1. **无需微信小程序**：Windows 只需 BLE 中心设备能力，实测 `bleak` 即可直连。
2. **收发核心**：
   - 发送：`build_frame(DATA, msgid)` → 写入 FFE2
   - 接收：订阅 FFE1，按 `LEN+5` 累积分片，校验 `SUM`，按 `MSGID` 匹配请求
3. **MSGID 必须自增管理**，并发请求靠它区分；设备原样回显。
4. **注意分片**：一帧可能被拆成多个 BLE 通知，必须累积到完整再解析。
5. **读写卡流程**：
   - 寻卡：`0x11` → 得到 UID
   - 认证：`0x0A`（或 `0x06`/`0x09`）
   - 读块：`0x07`；写块：`0x08`
   - 整卡遍历时逐块读，尾块（`block % 4 == 3`）为密钥/控制块，慎写

---

## 八、配套程序

本报告同时交付可直接运行的 Python 上位机：**`nfcmini.py`**

```bash
pip install bleak

python nfcmini.py info                 # 固件版本 + 序列号
python nfcmini.py find                 # 寻卡读卡号
python nfcmini.py read -b 4            # KEY A 读第 4 块
python nfcmini.py read -b 4 -B         # KEY B 读第 4 块
python nfcmini.py dump -k FFFFFFFFFFFF # 遍历读整卡
python nfcmini.py write -b 4 -k FFFFFFFFFFFF -d 00112233445566778899AABBCCDDEEFF
python nfcmini.py scan                 # 扫描周边 BLE 设备
```

已实测通过：`info`（版本 0.1.2 / SN 读取成功）、`find`（正确返回状态码 7）。
*（内容由AI生成，仅供参考）*
