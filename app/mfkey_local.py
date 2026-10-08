# -*- coding: utf-8 -*-
"""
Mifare Classic Crypto1 + mfkey32 本地密钥恢复核心（numba 加速，无全局大表）
算法移植自 proxmark3 / mfkey32v2 (GPL) 的 crypto01.c / crypto1.c / bucketsort.c

用于 NFC Mini Studio「本地破解」模式：用两轮认证采集数据离线恢复 48bit 扇区密钥。
"""
import numpy as np
from numba import njit

M32 = 0xFFFFFFFF
M24 = 0xFFFFFF

LF_POLY_ODD = 0x29CE5C
LF_POLY_EVEN = 0x870804

OUT_CAP = 1 << 21


# ---------------------------------------------------------- Crypto1 filter
@njit(cache=True, inline='always')
def filter_f(x):
    """Crypto1 非线性 filter（20bit -> 1bit），等价于 proxmark filterlut 表"""
    f = (0xf22c0 >> (x & 0xf)) & 16
    f |= (0x6c9c0 >> ((x >> 4) & 0xf)) & 8
    f |= (0x3c8b0 >> ((x >> 8) & 0xf)) & 4
    f |= (0x1e458 >> ((x >> 12) & 0xf)) & 2
    f |= (0x0d938 >> ((x >> 16) & 0xf)) & 1
    return (0xEC57E80A >> f) & 1


@njit(cache=True, inline='always')
def parity32(x):
    x &= M32
    x ^= x >> 16
    x ^= x >> 8
    x ^= x >> 4
    return (0x6996 >> (x & 0xF)) & 1


@njit(cache=True)
def swapendian(x):
    x = ((x >> 8) & 0x00FF00FF) | ((x & 0x00FF00FF) << 8)
    x &= M32
    x = ((x >> 16) | (x << 16)) & M32
    return x


@njit(cache=True)
def prng_successor(x, n):
    x = swapendian(x)
    for _ in range(n):
        x = ((x >> 1) | (((x >> 16) ^ (x >> 18) ^ (x >> 19) ^ (x >> 21)) & 1) << 31) & M32
    return swapendian(x)


# ---------------------------------------------------------------- Crypto1
@njit(cache=True)
def crypto1_init(key):
    odd = 0
    even = 0
    for i in range(47, 0, -2):
        odd = (odd << 1) | ((key >> ((i - 1) ^ 7)) & 1)
        even = (even << 1) | ((key >> (i ^ 7)) & 1)
    return odd, even


@njit(cache=True)
def crypto1_get_lfsr(odd, even):
    key = 0
    for i in range(23, -1, -1):
        key = (key << 1) | ((odd >> (i ^ 3)) & 1)
        key = (key << 1) | ((even >> (i ^ 3)) & 1)
    return key


@njit(cache=True, inline='always')
def crypto1_bit(odd, even, i, enc):
    ret = filter_f(odd & 0xFFFFF)
    feedin = ret & enc
    feedin ^= (i & 1)
    feedin ^= LF_POLY_ODD & odd
    feedin ^= LF_POLY_EVEN & even
    even = ((even << 1) | parity32(feedin)) & M24
    return even, odd, ret


@njit(cache=True)
def crypto1_word(odd, even, i, enc):
    ret = 0
    for k in range(32):
        b = (i >> (k ^ 24)) & 1
        odd, even, r = crypto1_bit(odd, even, b, enc)
        ret |= r << (24 ^ k)
    return odd, even, ret


@njit(cache=True, inline='always')
def lfsr_rollback_bit(odd, even, i, fb):
    odd &= M24
    odd, even = even, odd
    out = even & 1
    even >>= 1
    out ^= LF_POLY_EVEN & even
    out ^= LF_POLY_ODD & odd
    out ^= (i & 1)
    ret = filter_f(odd & 0xFFFFF)
    out ^= ret & fb
    even |= parity32(out) << 23
    return odd, even, ret


@njit(cache=True)
def lfsr_rollback_word(odd, even, i, fb):
    ret = 0
    for k in range(31, -1, -1):
        b = (i >> (k ^ 24)) & 1
        odd, even, r = lfsr_rollback_bit(odd, even, b, fb)
        ret |= r << (24 ^ k)
    return odd, even, ret


# ------------------------------------------------------ 状态空间扩张/收窄
@njit(cache=True, inline='always')
def update_contribution(arr, i, m1, m2):
    v = arr[i]
    p = v >> 25
    p = (p << 1) | parity32(v & m1)
    p = (p << 1) | parity32(v & m2)
    arr[i] = ((p << 24) | (v & M24)) & M32


@njit(cache=False)
def extend_table_simple(arr, head, tail, b, dbg):
    tbl = head
    guard = 0
    arr[head] = (arr[head] << 1) & M32
    while tbl <= tail:
        if guard > (tail - head + 2) * 8:
            dbg[3] += 1
            break
        guard += 1
        v = arr[tbl]
        f0 = filter_f(v)
        f1 = filter_f(v | 1)
        if f0 ^ f1:
            arr[tbl] = v | (f0 ^ b)
        elif f0 == b:
            tail += 1
            tmp = arr[tbl + 1]
            tbl += 1
            arr[tail] = tmp
            arr[tbl] = arr[tbl - 1] | 1
        else:
            arr[tbl] = arr[tail]
            tail -= 1
            tbl -= 1
        tbl += 1
        arr[tbl] = (arr[tbl] << 1) & M32
    dbg[1] += 1
    return tail


@njit(cache=False)
def extend_table(arr, head, tail, b, m1, m2, in_, dbg):
    in_ = (in_ << 24) & M32
    tbl = head
    guard = 0
    arr[head] = (arr[head] << 1) & M32
    while tbl <= tail:
        if guard > (tail - head + 2) * 8:
            dbg[3] += 1
            break
        guard += 1
        v = arr[tbl]
        f0 = filter_f(v)
        f1 = filter_f(v | 1)
        if f0 ^ f1:
            arr[tbl] = v | (f0 ^ b)
            update_contribution(arr, tbl, m1, m2)
            arr[tbl] ^= in_
        elif f0 == b:
            tail += 1
            arr[tail] = arr[tbl + 1]
            arr[tbl + 1] = arr[tbl] | 1
            update_contribution(arr, tbl, m1, m2)
            arr[tbl] ^= in_
            tbl += 1
            update_contribution(arr, tbl, m1, m2)
            arr[tbl] ^= in_
        else:
            arr[tbl] = arr[tail]
            tail -= 1
            tbl -= 1
        tbl += 1
        arr[tbl] = (arr[tbl] << 1) & M32
    return tail


@njit(cache=False)
def bucket_sort_intersect(e_arr, e0, e1, o_arr, o0, o1):
    ecnt = np.zeros(256, dtype=np.int64)
    ocnt = np.zeros(256, dtype=np.int64)
    for i in range(e0, e1 + 1):
        ecnt[(e_arr[i] >> 24) & 0xFF] += 1
    for i in range(o0, o1 + 1):
        ocnt[(o_arr[i] >> 24) & 0xFF] += 1

    e_tmp = e_arr[e0:e1 + 1].copy()
    o_tmp = o_arr[o0:o1 + 1].copy()

    e_pos = np.full(256, -1, dtype=np.int64)
    o_pos = np.full(256, -1, dtype=np.int64)
    pairs = np.empty((256, 4), dtype=np.int64)

    acc_e = e0
    acc_o = o0
    npair = 0
    for j in range(256):
        if ecnt[j] > 0 and ocnt[j] > 0:
            e_pos[j] = acc_e
            o_pos[j] = acc_o
            pairs[npair, 0] = acc_e
            pairs[npair, 1] = acc_e + ecnt[j] - 1
            pairs[npair, 2] = acc_o
            pairs[npair, 3] = acc_o + ocnt[j] - 1
            npair += 1
            acc_e += ecnt[j]
            acc_o += ocnt[j]

    for i in range(e_tmp.shape[0]):
        v = e_tmp[i]
        j = (v >> 24) & 0xFF
        if e_pos[j] >= 0:
            e_arr[e_pos[j]] = v
            e_pos[j] += 1
    for i in range(o_tmp.shape[0]):
        v = o_tmp[i]
        j = (v >> 24) & 0xFF
        if o_pos[j] >= 0:
            o_arr[o_pos[j]] = v
            o_pos[j] += 1
    return pairs, npair


# 注意：_recover 是自递归函数，numba 缓存回载时其自引用符号无法链接
# （LLVM ERROR: Symbol not found: .numba.unresolved$..._recover），故禁用缓存，改为每次进程内即时编译。
@njit(cache=False)
def _recover(o_arr, o0, o1, oks, e_arr, e0, e1, eks, rem, in_, out, cnt, dbg):
    dbg[0] += 1
    if dbg[0] > 200000:
        dbg[4] = 1
        return
    if rem == -1:
        for ei in range(e0, e1 + 1):
            ev = (e_arr[ei] << 1) & M32
            ev ^= parity32(e_arr[ei] & LF_POLY_EVEN)
            if in_ & 4:
                ev ^= 1
            e_arr[ei] = ev
            for oi in range(o0, o1 + 1):
                ov = o_arr[oi]
                n = cnt[0]
                if n < OUT_CAP:
                    out[n, 0] = ev ^ parity32(ov & LF_POLY_ODD)
                    out[n, 1] = ov
                    cnt[0] = n + 1
        return

    i = 0
    while i < 4:
        if rem == 0:
            rem = -1
            break
        rem -= 1
        oks >>= 1
        eks >>= 1
        in_ >>= 2
        o1 = extend_table(o_arr, o0, o1, oks & 1, (LF_POLY_EVEN << 1) | 1, LF_POLY_ODD << 1, 0, dbg)
        if o0 > o1:
            return
        e1 = extend_table(e_arr, e0, e1, eks & 1, LF_POLY_ODD, (LF_POLY_EVEN << 1) | 1, in_ & 3, dbg)
        if e0 > e1:
            return
        i += 1

    pairs, npair = bucket_sort_intersect(e_arr, e0, e1, o_arr, o0, o1)
    for k in range(npair - 1, -1, -1):
        _recover(o_arr, pairs[k, 2], pairs[k, 3], oks,
                 e_arr, pairs[k, 0], pairs[k, 1], eks,
                 rem, in_, out, cnt, dbg)


@njit(cache=False)
def lfsr_recovery32(ks2, in_, out, cnt, bits, dbg):
    oks = 0
    eks = 0
    for i in range(31, -1, -2):
        oks = (oks << 1) | ((ks2 >> (i ^ 24)) & 1)
    for i in range(30, -1, -2):
        eks = (eks << 1) | ((ks2 >> (i ^ 24)) & 1)

    odd = np.empty(OUT_CAP, dtype=np.int64)
    even = np.empty(OUT_CAP, dtype=np.int64)
    o_tail = -1
    e_tail = -1
    o0 = oks & 1
    e0 = eks & 1
    for i in range(1 << bits, -1, -1):
        fi = filter_f(i & 0xFFFFF)
        if fi == o0:
            o_tail += 1
            odd[o_tail] = i
        if fi == e0:
            e_tail += 1
            even[e_tail] = i

    for _ in range(4):
        oks >>= 1
        eks >>= 1
        o_tail = extend_table_simple(odd, 0, o_tail, oks & 1, dbg)
        e_tail = extend_table_simple(even, 0, e_tail, eks & 1, dbg)
    dbg[5] = o_tail
    dbg[6] = e_tail

    in_ &= M32
    in_ = ((in_ >> 16) & 0xFF) | ((in_ << 16) & M32) | (in_ & 0xFF00)
    in_ = (in_ << 1) & M32
    _recover(odd, 0, o_tail, oks, even, 0, e_tail, eks, 11, in_, out, cnt, dbg)


# ------------------------------------------------------------------ 主流程
@njit(cache=False)
def recover_key(uid, nt0, nr0_enc, ar0_enc, nt1, nr1_enc, ar1_enc, out, cnt, bits, dbg):
    """两轮认证数据 -> (key, 候选数)；key = -1 表示未找到"""
    p64 = prng_successor(nt0, 64)
    p64b = prng_successor(nt1, 64)
    ks2 = ar0_enc ^ p64

    cnt[0] = 0
    lfsr_recovery32(ks2, 0, out, cnt, bits, dbg)
    n = cnt[0]

    for k in range(n):
        odd = np.int64(out[k, 0])
        even = np.int64(out[k, 1])
        odd, even, _ = lfsr_rollback_word(odd, even, 0, 0)
        odd, even, _ = lfsr_rollback_word(odd, even, nr0_enc, 1)
        odd, even, _ = lfsr_rollback_word(odd, even, uid ^ nt0, 0)
        key = crypto1_get_lfsr(odd, even)

        o2, e2 = crypto1_init(key)
        o2, e2, _ = crypto1_word(o2, e2, uid ^ nt1, 0)
        o2, e2, _ = crypto1_word(o2, e2, nr1_enc, 1)
        o2, e2, ks = crypto1_word(o2, e2, 0, 0)
        if ar1_enc == (ks ^ p64b):
            return key, n
    return -1, n
