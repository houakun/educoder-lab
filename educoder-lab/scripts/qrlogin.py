#!/usr/bin/env python3
"""educoder 微信扫码登录：终端出二维码，扫完自动换 session。

纯标准库，无第三方依赖。流程：
  1. 请求微信开放平台 qrconnect，拿到 uuid
  2. 用内置 QR 生成器把确认链接渲染成终端二维码
  3. 轮询 long.open.weixin.qq.com 直到用户扫码确认
  4. 拿授权 code 调 educoder 换 _educoder_session cookie
  5. 探测账号(zzud)并写入 ~/.educoder/session.json

用法:
    python3 qrlogin.py
"""
from __future__ import annotations

import re
import sys
import time
import json
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import educoder  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

APPID = "wx6b119e2d829c13fa"
REDIRECT_URI = "https://www.educoder.net/otherloginstart"


# ---------------------------------------------------------------------------
# QR 码生成器（byte 模式，纠错级别 L，version 1-10，自动选版本）
# ---------------------------------------------------------------------------
# 对齐图案中心坐标（version 1 无对齐图案）
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
# byte 模式数据容量（L 纠错级别，字节数）
_CAP = {1: 17, 2: 32, 3: 53, 4: 78, 5: 106, 6: 134, 7: 154, 8: 192, 9: 230, 10: 271}
# RS 分块表：version -> (每块纠错码字数, [(块数, 每块数据码字数), ...])
_RS = {
    1: (7,  [(1, 19)]),
    2: (10, [(1, 34)]),
    3: (15, [(1, 55)]),
    4: (20, [(1, 80)]),
    5: (26, [(1, 108)]),
    6: (18, [(2, 68)]),
    7: (20, [(2, 78)]),
    8: (24, [(2, 97)]),
    9: (30, [(2, 116)]),
    10: (18, [(2, 68), (2, 69)]),
}


def _gf_mul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1D  # x^8 + x^4 + x^3 + x^2 + 1
        b >>= 1
    return p


def _rs_generator(nsym: int) -> list[int]:
    g = [1]
    for i in range(nsym):
        # g *= (x + α^i)，根为 α^0..α^(nsym-1)
        alpha_i = _gf_pow(i)
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, alpha_i)
        g = ng
    return g


def _gf_pow(n: int) -> int:
    """返回 α^n（α=2）在 GF(256) 中的值。"""
    r = 1
    for _ in range(n):
        r = _gf_mul(r, 2)
    return r


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    gen = _rs_generator(nsym)
    res = data + [0] * nsym
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(nsym):
                res[i + j + 1] ^= _gf_mul(gen[j + 1], coef)
    return res[-nsym:]


def _bits_append(bits: list[int], value: int, n: int) -> None:
    for i in range(n - 1, -1, -1):
        bits.append((value >> i) & 1)


def _data_codeword_count(version: int) -> int:
    return sum(n * d for n, d in _RS[version][1])


def _encode_data(data: bytes, version: int) -> list[int]:
    """byte 模式编码，返回数据码字列表（已含 terminator 与 pad）。"""
    cap_bits = _data_codeword_count(version) * 8
    bits: list[int] = []
    _bits_append(bits, 0b0100, 4)  # mode indicator: byte
    cc_bits = 8 if version <= 9 else 16
    _bits_append(bits, len(data), cc_bits)
    for b in data:
        _bits_append(bits, b, 8)

    if len(bits) > cap_bits:
        raise ValueError(f"data too long for version {version}")
    # terminator：最多 4 个 0
    bits += [0] * min(4, cap_bits - len(bits))
    # 对齐到字节
    while len(bits) % 8:
        bits.append(0)
    # pad bytes 0xEC 0x11 交替
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < cap_bits:
        _bits_append(bits, pad[i % 2], 8)
        i += 1

    out = []
    for k in range(0, len(bits), 8):
        v = 0
        for b in bits[k:k + 8]:
            v = (v << 1) | b
        out.append(v)
    return out


def _interleave(data: list[int], version: int) -> list[int]:
    ec_per, blocks = _RS[version]
    # 建立每块的数据槽
    block_data = []
    for nblocks, data_len in blocks:
        for _ in range(nblocks):
            block_data.append([0] * data_len)
    # 按列分配数据码字到各块
    dindex = 0
    maxd = max(d for _, d in blocks)
    for col in range(maxd):
        for bi, (nblocks, data_len) in enumerate(blocks):
            for bi2 in range(nblocks):
                if col < data_len:
                    block_idx = sum(n for n, _ in blocks[:bi]) + bi2
                    block_data[block_idx][col] = data[dindex]
                    dindex += 1
    # 逐块算纠错
    block_ec = [_rs_encode(bd, ec_per) for bd in block_data]
    # 交织输出：先数据列，再纠错列
    out = []
    for col in range(maxd):
        for bd in block_data:
            out.append(bd[col])
    for col in range(ec_per):
        for be in block_ec:
            out.append(be[col])
    return out


def _make_matrix(data: bytes, version: int) -> list[list[int]]:
    size = 17 + 4 * version
    m = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]
    total_cw = _data_codeword_count(version) + _RS[version][0] * sum(n for n, _ in _RS[version][1])
    codewords = _interleave(_encode_data(data, version), version)
    assert len(codewords) == total_cw, f"{len(codewords)} != {total_cw}"

    def mark(r, c, val):
        if 0 <= r < size and 0 <= c < size:
            reserved[r][c] = True
            m[r][c] = val

    # --- finder + separator ---
    for (fr, fc) in [(0, 0), (0, size - 7), (size - 7, 0)]:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = fr + dr, fc + dc
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                reserved[rr][cc] = True
                if 0 <= dr < 7 and 0 <= dc < 7:
                    border = dr in (0, 6) or dc in (0, 6)
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    m[rr][cc] = 1 if (border or core) else 0
                else:
                    m[rr][cc] = 0  # separator

    # --- timing ---
    for i in range(8, size - 8):
        mark(6, i, 1 if i % 2 == 0 else 0)
        mark(i, 6, 1 if i % 2 == 0 else 0)

    # --- alignment ---
    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r, c) in [(centers[0], centers[0]),
                          (centers[0], centers[-1]),
                          (centers[-1], centers[0])]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    mark(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)

    # --- format info + dark module 位置（先 reserve，值后填） ---
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[size - 1 - i][8] = True
        reserved[8][size - 1 - i] = True
    reserved[8][8] = True
    reserved[size - 8][8] = True  # dark module

    # --- version info (v >= 7) ---
    if version >= 7:
        for i in range(6):
            for j in range(3):
                reserved[i][size - 11 + j] = True
                reserved[size - 11 + j][i] = True

    # --- 放置数据（Z 字形，从右下角起） ---
    data_bits = []
    for cw in codewords:
        for b in range(7, -1, -1):
            data_bits.append((cw >> b) & 1)

    di = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = list(range(size - 1, -1, -1)) if upward else list(range(size))
        for r in rows:
            for c in (col, col - 1):
                if not reserved[r][c]:
                    m[r][c] = data_bits[di] if di < len(data_bits) else 0
                    di += 1
        upward = not upward
        col -= 2

    # --- 掩码（跳过功能图案） ---
    def mask_cond(i, j, mask):
        if mask == 0:
            return (i + j) % 2 == 0
        if mask == 1:
            return i % 2 == 0
        if mask == 2:
            return j % 3 == 0
        if mask == 3:
            return (i + j) % 3 == 0
        if mask == 4:
            return (i // 2 + j // 3) % 2 == 0
        if mask == 5:
            return (i * j) % 2 + (i * j) % 3 == 0
        if mask == 6:
            return ((i * j) % 2 + (i * j) % 3) % 2 == 0
        if mask == 7:
            return ((i + j) % 2 + (i * j) % 3) % 2 == 0
        return False

    def apply_mask(mask):
        mm = [row[:] for row in m]
        for i in range(size):
            for j in range(size):
                if not reserved[i][j] and mask_cond(i, j, mask):
                    mm[i][j] ^= 1
        return mm

    def penalty(mm):
        score = 0
        for i in range(size):
            run = 1
            for j in range(1, size):
                if mm[i][j] == mm[i][j - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
        for j in range(size):
            run = 1
            for i in range(1, size):
                if mm[i][j] == mm[i - 1][j]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
        for i in range(size - 1):
            for j in range(size - 1):
                if mm[i][j] == mm[i + 1][j] == mm[i][j + 1] == mm[i + 1][j + 1]:
                    score += 3
        for i in range(size):
            for j in range(size - 6):
                if mm[i][j:j + 7] == [1, 0, 1, 1, 1, 0, 1]:
                    score += 40
        for j in range(size):
            for i in range(size - 6):
                if [mm[k][j] for k in range(i, i + 7)] == [1, 0, 1, 1, 1, 0, 1]:
                    score += 40
        dark = sum(sum(row) for row in mm)
        pct = dark * 100 // (size * size)
        score += (abs(pct - 50) // 5) * 10
        return score

    best_m, best_mask = None, 0
    best_score = float("inf")
    for mask in range(8):
        mm = apply_mask(mask)
        s = penalty(mm)
        if s < best_score:
            best_score, best_m, best_mask = s, mm, mask

    # --- 格式信息 ---
    fmt = (0b01 << 3) | best_mask  # 纠错 L = 01
    # BCH(15,5)：生成多项式 0x537，掩码 0x5412
    def _bch_format(data5: int) -> int:
        d = data5 << 10
        g = 0x537
        for i in range(14, 9, -1):
            if d & (1 << i):
                d ^= g << (i - 10)
        return ((data5 << 10) | (d & 0x3FF)) & 0x7FFF

    fmt_final = _bch_format(fmt) ^ 0x5412
    # 第一份（vertical，第 8 列 col=8，从上到下）
    for i in range(15):
        bit = (fmt_final >> i) & 1
        if i < 6:
            best_m[i][8] = bit
        elif i < 8:
            best_m[i + 1][8] = bit
        else:
            best_m[size - 15 + i][8] = bit
    # 第二份（horizontal，第 8 行 row=8，从右到左）
    for i in range(15):
        bit = (fmt_final >> i) & 1
        if i < 8:
            best_m[8][size - 1 - i] = bit
        elif i < 9:
            best_m[8][15 - i] = bit
        else:
            best_m[8][14 - i] = bit
    best_m[size - 8][8] = 1  # dark module

    # --- 版本信息（version >= 7） ---
    if version >= 7:
        bch = version
        for _ in range(12):
            bch = (bch << 1) ^ (0b1111100100101 if (bch >> 11) & 1 else 0)
        vi = (version << 12) | bch
        for i in range(18):
            bit = (vi >> i) & 1
            a, b = i // 3, i % 3
            best_m[size - 11 + b][a] = bit
            best_m[a][size - 11 + b] = bit

    return best_m


def make_qr_matrix(text: str) -> list[list[int]]:
    data = text.encode("utf-8")
    version = next(v for v in range(1, 11) if len(data) <= _CAP[v])
    return _make_matrix(data, version)


def render_qr(text: str) -> str:
    """把二维码渲染成终端字符（▀▄█ 半块，上下两模块合一字符）。"""
    m = make_qr_matrix(text)
    n = len(m)
    quiet = 4  # 每边 4 模块静区
    lines = []
    # 顶部静区（4 模块 = 2 行）
    lines.append(" " * (n + quiet * 2))
    lines.append(" " * (n + quiet * 2))
    row = 0
    while row < n:
        line = ""
        for _ in range(quiet):
            line += " "
        for c in range(n):
            up = m[row][c]
            down = m[row + 1][c] if row + 1 < n else 0
            if up and down:
                line += "█"
            elif up and not down:
                line += "▀"
            elif not up and down:
                line += "▄"
            else:
                line += " "
        for _ in range(quiet):
            line += " "
        lines.append(line)
        row += 2
    lines.append(" " * (n + quiet * 2))
    lines.append(" " * (n + quiet * 2))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 微信 OAuth 流程
# ---------------------------------------------------------------------------
def _opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def fetch_uuid(opener) -> str:
    """请求微信 qrconnect，拿到有效的 uuid。

    注意：qrconnect 页面静态 HTML 里的 /connect/qrcode/xxx 是占位 uuid，真实
    有效的 uuid 只能通过 `f=xml` 请求拿到（返回 XML，uuid 在 CDATA 里）。
    """
    qr_url = ("https://open.weixin.qq.com/connect/qrconnect"
              f"?appid={APPID}&redirect_uri={urllib.request.quote(REDIRECT_URI)}"
              "&response_type=code&scope=snsapi_login&state=null")
    xml_url = qr_url + "&f=xml&" + str(int(time.time() * 1000))
    req = urllib.request.Request(xml_url, headers={
        "User-Agent": UA, "Referer": qr_url})
    xml = opener.open(req, timeout=25).read().decode("utf-8", "replace")
    m = re.search(r"<uuid>\s*<!\[CDATA\[([A-Za-z0-9_\-]+)\]\]>\s*</uuid>", xml)
    if not m:
        m = re.search(r"<uuid>([A-Za-z0-9_\-]+)</uuid>", xml)
    if not m:
        raise RuntimeError(f"未从微信 XML 响应中解析到 uuid: {xml[:200]}")
    return m.group(1)


def poll_uuid(opener, uuid: str, qr_url: str, timeout: int) -> str | None:
    """轮询直到确认成功，返回授权 code；超时返回 None。

    qrconnect 轮询状态码（从页面 JS 确认）：
      408=未扫码，404=已扫描待确认，403=用户取消，405=成功(wx_code 即授权码)，
      402=需刷新二维码，500=服务器错误。
    404/408/403 都要继续轮询等 405。
    """
    poll = f"https://long.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}"
    deadline = time.time() + timeout
    last_state = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(poll, headers={
                "User-Agent": UA, "Referer": qr_url})
            html = opener.open(req, timeout=25).read().decode("utf-8", "replace")
        except Exception as e:
            print(f"  ! 轮询异常: {e}", file=sys.stderr)
            time.sleep(2)
            continue
        mcode = re.search(r"window\.wx_errcode\s*=\s*(\d+)", html)
        mval = re.search(r"window\.wx_code\s*=\s*'([^']*)'", html)
        code = mcode.group(1) if mcode else "?"
        if code != last_state:
            msg = {"408": "等待扫码…", "404": "已扫描，请在手机上确认…",
                   "403": "已取消，可重新扫码…", "405": "确认成功，正在换取登录态…",
                   "402": "二维码需刷新", "500": "服务器错误"}.get(code, f"状态 {code}")
            print(f"  [{time.strftime('%H:%M:%S')}] {msg}")
            last_state = code
        if code == "405":
            c = mval.group(1) if mval else ""
            if c:
                return c
            # 405 但 wx_code 为空，继续等
        if code in ("402", "500"):
            return None
        time.sleep(2)
    return None


def exchange_code(code: str):
    """拿授权 code 调 educoder 换 session。

    正确接口是 GET /api/auth/wechat/callback.json?code=xxx（不是 /otherloginstart，
    也不是 POST）。成功后响应头 `cs` 携带 _educoder_session 值。
    """
    import urllib.parse as _up

    ts, sig = educoder.gen_signature("GET")
    url = (f"https://data.educoder.net/api/auth/wechat/callback.json"
           f"?code={_up.quote(code)}")
    headers = {
        "X-EDU-Type": "pc",
        "X-EDU-Timestamp": str(ts),
        "X-EDU-Signature": sig,
        "Accept": "application/json",
        "X-Original-Protocol": "https",
        "X-Original-Host": "www.educoder.net",
        "User-Agent": UA,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    session = ""
    autologin = ""
    data: dict = {}
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", "replace")
        cs = resp.headers.get("cs") or ""
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        if cs:
            session = cs
        # Set-Cookie 可能有多个，用 get_all 更稳
        set_cookies = resp.headers.get_all("Set-Cookie") or []
        for sc in set_cookies:
            first = sc.split(";")[0]
            if first.startswith("_educoder_session="):
                session = first.split("=", 1)[1]
            elif first.startswith("autologin_trustie="):
                autologin = first.split("=", 1)[1]
        if data.get("status") not in (None, 0):
            print(f"  ! 微信回调返回: status={data.get('status')} "
                  f"message={data.get('message')}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        print(f"  ! 换 session HTTP {e.code}: {data.get('message') or raw[:120]}",
              file=sys.stderr)
    except Exception as e:
        print(f"  ! 换 session 异常: {e}", file=sys.stderr)
    return session, autologin


def download_qr_image(opener, uuid: str, qr_url: str, dest: str | None = None) -> str | None:
    """下载微信官方二维码图片（JPEG），作为终端块字符码的备用。"""
    dest = dest or f"/tmp/wx-qr-{uuid}.jpg"
    img_url = f"https://open.weixin.qq.com/connect/qrcode/{uuid}"
    req = urllib.request.Request(img_url, headers={
        "User-Agent": UA, "Referer": qr_url})
    try:
        img = opener.open(req, timeout=25).read()
        with open(dest, "wb") as f:
            f.write(img)
        return dest
    except Exception as e:
        print(f"  ! 下载微信原图失败: {e}", file=sys.stderr)
        return None


def main() -> int:
    print("== educoder 微信扫码登录 ==")
    op, _ = _opener()
    print("[1/4] 请求微信二维码…")
    try:
        uuid = fetch_uuid(op)
    except Exception as e:
        print(f"获取二维码失败: {e}", file=sys.stderr)
        return 1
    qr_url = ("https://open.weixin.qq.com/connect/qrconnect"
              f"?appid={APPID}&redirect_uri={urllib.request.quote(REDIRECT_URI)}"
              "&response_type=code&scope=snsapi_login&state=null")
    confirm = f"https://open.weixin.qq.com/connect/confirm?uuid={uuid}"

    print("[2/4] 请用微信扫下面的二维码（有效期约 60 秒）：\n")
    print(render_qr(confirm))
    print()
    img_path = download_qr_image(op, uuid, qr_url)
    if img_path:
        print(f"（备用：微信原图已存到 {img_path}，终端码扫不了再用）")

    print("[3/4] 等待扫码确认…")
    code = poll_uuid(op, uuid, qr_url, timeout=180)
    if not code:
        print("扫码未完成或二维码过期，请重跑。", file=sys.stderr)
        return 1

    print("[4/4] 换取登录态…")
    session, autologin = exchange_code(code)
    if not session:
        print("换取 session 失败（未拿到 _educoder_session cookie）。", file=sys.stderr)
        print("请改用浏览器扫码 + `educoder import-session` 导入。", file=sys.stderr)
        return 1

    api = educoder.Api()
    api.session = session
    api.autologin = autologin
    info = {}
    zzud = ""
    try:
        info = api.get("/users/get_user_info.json")
        zzud = str(info.get("login") or "")
    except Exception:
        info = {}
    if not zzud:
        zzud = input("无法自动探测账号，请手动输入登录账号(zzud): ").strip()
    api.zzud = zzud
    api.save()
    print(f"\n登录成功，session 已写入 {educoder.SESSION_FILE}")
    print("接下来可跑 `educoder courses` / `educoder homeworks --course <课程>`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
