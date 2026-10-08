# 官方固件提取记录

来源：微信小程序「宜新 NFC Reader Tool」→ `GET /nice-json/mini-program/v1/get-devices`
（请求头 `NICETHEME: <登录态 token>`），token 由 `app/wx_token.py` 从微信进程内存自动抓取。

本机读卡器固件版本 **0.1.4**，对应清单里的 **NFC Max 2.0**。

| 项 | 值 |
|---|---|
| 文件 | `NFC_Max_2.0_v0.1.4.bin` |
| 大小 | 210,140 字节（205.2 KB） |
| MD5 | `653595dc7e353eaa8c485e24d69df7c7` |
| 头部 | `6f 50 10 1e 00 00 00 00 00 00 00 00 a0 00 00 20 …` |

初步判断：纯二进制机器码固件（可打印字符串仅 58 段，无明文字符串表），
头部 `6f 50 10 1e` 非标准容器格式，`a0 00 00 20` 疑似 Cortex-M 的
栈顶地址（0x200000A0）风格，倾向 ARM Cortex-M 裸机镜像。
要深入需要按对应 MCU 做反汇编（需先确认型号）。

## 16 款官方设备清单（同一接口返回）

NFC Reader Tool / Tool Pro / Tool Max / Tool Plus / Tool Mini / Tool Ultra（0.5.9 / 4.7.6 系列，均带 sniff 或 key_cache 标志）
NFC Max Rev / Plus Rev / Pro Rev / Mini Rev / Mini LK（0.2.x）
NFC Max 2.0(0.1.4) / Plus 2.0(0.1.3) / LK Mini(0.1.3)

其中带 `sniff: true` 的型号支持侦测读卡；带 `key_cache: true` 的支持密钥缓存。
本机 NFC Max 2.0 为 `sniff: true` + `key_cache: false`。