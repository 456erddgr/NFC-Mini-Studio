# 破解采集链路 · 逆向笔记（源自 app-service.js）

## 一、云端算法族（模块 18）
| 算法 | 接口 | 请求体 |
|---|---|---|
| mfkey32v2 | `/recovery/mfkey32v2` | uid, nt1, nr1, ar1, nt2, nr2, ar2 |
| mfkey32 | `/recovery/mfkey32` | uid, nt, nr1, ar1, nr2, ar2 |
| staticnest | `/recovery/staticnest` | uid, enc1_bytes, ks1_bytes |
| mfoc | `/recovery/mfoc` | uid, d_Nt, d_nt_last, r_nt, n_rx, n_rx_par |
| pang | `/recovery/pang` | uid, nt, ks_list_1..3, nr1..3 |
| fm11rf08s | `/recovery/fm11rf08s` | auth_uid, nt, nt_enc, nt_par_enc |
| fm11rf08s2nt | `/recovery/fm11rf08s2nt` | 上述 + nt2/nt_enc2/nt_par_enc2 |
| fm11rf08s2nt2 | `/recovery/fm11rf08s2nt2` | key_a_* / key_b_* 两组 |
| fm11rf08s2nt3 | `/recovery/fm11rf08s2nt3` | key1_* / key2_* 两组 |
| hardnested | `/hardnested` 提交 → `/get-hardnested-key` 轮询 | — |
| 卡片分析 | `/ic-card-analysis` | data |

鉴权：`NICETHEME: <登录态 token>`（登录态，会过期）；keygen 用固定 `Authorization`。

## 二、状态字节位置（dispatch 表）
```
0x06/0x07/0x08/0x09/0x0B/0x20 → 状态在 DATA[2]
0x05/0x11/0x21/0x22/0x25/0x30/0x32/0x34/0x35/0x36/0x37 → DATA[1]
```

## 三、采集解析（设备返回的字节数组，元素形如 "0x2a"）

### 0x11 寻卡
```
type   = parseInt(e[2])
uid    = e.slice(3,7).reverse()          // 4 字节 UID 需反序
sak    = e[7]
isUid  = (parseInt(e[8]) === 1)
/* 7 字节 UID 判定：e.slice(9,11).reverse() == "4400" 或 "4800"
   则 uid = e.slice(11,14).reverse() + uid */
```

### pang 全加密采集（0x20 系列，多帧）
```
if (e[1] !== 0) → 该帧是错误码，累加 2 次即失败
uid      = parseInt("0x" + e.slice(3,7)  joined)
nt       = parseInt("0x" + e.slice(7,11) joined)
nr_1     = parseInt("0x" + e.slice(11,15) joined)
ks_list_0 = parseInt(e[15])
ks_list_1 = parseInt(e[16])
...
```

### staticnest 无漏洞卡采集（0x32 / 0x32 变体）
```
opName = "staticnest_<n>_<keyType>" / "staticnest_v1_<...>"
e[1] = 状态码；!== 0 累加错误
e[2] === 9  → 采集完成
  enc1_bytes_0 / enc1_bytes_1、ks1_bytes_0 / ks1_bytes_1 从后续帧取
```

### hardnested 兼容卡采集
opName 前缀 `hardnested_collection_`；采集完成后 POST `/hardnested`，
再轮询 `/get-hardnested-key` 取密钥。

### fm11rf08s 三代卡采集
opName 前缀 `fm11rf08s_`；分 2nt / 2nt2 / 2nt3 三档，采到几组就发对应接口。

## 四、解密路由（小程序 decode 主循环）
```
mfoc 返回 201 → 切 hardnested
mfoc 返回 202 → 三代无漏洞卡 → 联网计算
pang 采集失败 → "国产全加密卡，是否尝试联网计算密码？"
网络解码 → 按 UID 查厂商密钥库
非完整KEY → 密钥缓存(批量下发) + 批量验证，或改用侦测读卡(更快)
```

## 五、待办
桌面端 `device.py` 目前**没有** nonce 采集（缺 `last_nonces`），云端链路因此拿不到输入。
要复刻官方破解，需按上面第三节逐命令实现采集，再喂给 `crack_engine`。