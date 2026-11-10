# WeChat QR login (微信扫码登录)

Everything here was verified against the live platform and the WeChat Open
Platform `qrconnect` flow. It documents what `scripts/qrlogin.py` depends on,
so a future change can be checked against reality instead of guesswork.

`educoder.py` only ships password login (`/accounts/login.json`). `qrlogin.py`
adds a no-password WeChat QR login: it renders a QR code in the terminal, polls
WeChat until the phone confirms, then exchanges the authorization code for an
`_educoder_session` and writes `~/.educoder/session.json` — the same cache
`educoder.py` reuses.

## Overview

```
qrconnect (f=xml)  ->  uuid
                      |
                      +-- render confirm?uuid=<uuid> as terminal QR
                      |
user scans, confirms ->  poll long.open.weixin.qq.com
                      |
                      405 + wx_code
                      |
GET /api/auth/wechat/callback.json?code=<wx_code>
                      |
                      session (cs header or Set-Cookie)
```

`qrlogin.py` talks to two parties:

- **WeChat Open Platform** — `open.weixin.qq.com`, `long.open.weixin.qq.com`.
  These requests need no WeChat account credential; the QR code is WeChat's
  own, and the user confirms inside the WeChat app.
- **EduCoder** — `data.educoder.net/api`, signed exactly like `educoder.py`
  (see [api.md](api.md)). The final code exchange is a signed GET.

## Endpoints

### 1. Get the real uuid — `f=xml`

The static `qrconnect` HTML page contains `/connect/qrcode/xxxxxxxx` as a
**placeholder**. The real, scannable uuid only comes from an `f=xml` request:

```text
GET https://open.weixin.qq.com/connect/qrconnect
    ?appid=wx6b119e2d829c13fa
    &redirect_uri=https%3A%2F%2Fwww.educoder.net%2fotherloginstart
    &response_type=code&scope=snsapi_login&state=null
    &f=xml&<epoch-ms>
```

Response:

```xml
<root>
  <uuid><![CDATA[081QY62U3nwCFa1R]]></uuid>
  <appname><![CDATA[EduCoder云网]]></appname>
  ...
</root>
```

The `uuid` inside `CDATA` is the value used for the QR content and the poll.
The static HTML uuid and the `f=xml` uuid are **different**; using the static
one makes WeChat report the code as expired the moment it is scanned.

### 2. QR content

The scannable content is:

```text
https://open.weixin.qq.com/connect/confirm?uuid=<uuid>
```

`qrlogin.py` encodes this string with its built-in QR generator (byte mode,
error-correction level L, version auto-selected from the data length) and
renders it with `▀▄█` half-blocks. WeChat's own `/connect/qrcode/<uuid>` image
is the exact same content; it is also downloaded as a fallback.

### 3. Poll — `long.open.weixin.qq.com`

```text
GET https://long.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid>
Referer: <the qrconnect URL>
```

The response is a JavaScript assignment, not JSON:

```text
window.wx_errcode=405;window.wx_code='<code>';
```

`wx_errcode` meanings (verified from the qrconnect page's own `switch`):

| Code | Meaning | Action |
| --- | --- | --- |
| `408` | not scanned yet | keep polling |
| `404` | scanned, waiting for phone confirmation | keep polling (NOT "expired"!) |
| `403` | user cancelled | keep polling, user may re-scan |
| `405` | confirmed — `wx_code` is the authorization code | stop, exchange the code |
| `402` | needs QR refresh | treat as fatal |
| `500` | server error | treat as fatal |

Two traps cost a lot of time and are easy to get wrong:

- `404` looks like "expired" but means **already scanned** — do not bail out.
- `405` is the **success** state; `wx_code` is the authorization code, not a
  "still waiting" marker.

### 4. Exchange the code — `auth/wechat/callback.json`

```text
GET https://data.educoder.net/api/auth/wechat/callback.json?code=<wx_code>
```

Signed exactly like every other EduCoder API call, plus the two extra headers
the web frontend sends:

```text
X-EDU-Type: pc
X-EDU-Timestamp: <epoch-ms>
X-EDU-Signature: md5(base64("method=GET&ak=<AK>&sk=<SK>&time=<ts>"))
X-Original-Protocol: https
X-Original-Host: www.educoder.net
```

The success response has `status: 0` and carries the session in either the
`cs` response header or a `Set-Cookie: _educoder_session=...`. An invalid or
already-used code returns HTTP 200 with `{"status": -1, "message": "授权已失效，请重新授权"}`.

Note the endpoint lives under `/api/` and is a **GET** — the frontend calls
`auth/wechat/callback.json` through a request wrapper whose base URL already
includes `/api/`. A POST to the same path is a 404.

## QR generator

`qrlogin.py` has a self-contained QR encoder (no third-party dependency):

- byte mode, error-correction level **L**, versions 1–10, auto-selected.
- Reed–Solomon over GF(256) with generator polynomial roots α⁰…α^(n-1).
- Standard format-info BCH(15,5) and placement, all 8 masks with penalty
  selection.

Cross-checked against the reference `qrcode` Python package: the data +
error-correction codewords are byte-identical, and the rendered matrix decodes
cleanly with `zbarimg`.

## Usage

```bash
python3 scripts/qrlogin.py
```

The session is written to `~/.educoder/session.json` (mode 0600), the same
file `educoder.py` reads, so after one scan both tools share the login state.

## Safety notes

- The QR code is one-time: once scanned, it is bound to that scan. Do not scan
  both the terminal code and the fallback image — the second uuid is dead.
- `qrlogin.py` writes to the platform (the code exchange creates a session),
  and drives your own account only.
- Credentials live in `~/.educoder/session.json` (0600). Do not echo the
  session value or the `wx_code` into a conversation.
