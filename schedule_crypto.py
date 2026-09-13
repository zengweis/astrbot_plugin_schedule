"""金智统一身份认证前端同款密码加密。

实现依据是登录页加载的 `/authserver/custom/static/common/encrypt.js`，其中关键三行：

    function getAesString(n, f, c) {
        f = f.replace(/(^\\s+)|(\\s+$)/g, "");
        f = CryptoJS.enc.Utf8.parse(f);      // key
        c = CryptoJS.enc.Utf8.parse(c);      // iv
        return CryptoJS.AES.encrypt(n, f, {iv: c, mode: CBC, padding: Pkcs7}).toString();
    }
    function encryptAES(n, f) {
        return f ? getAesString(randomString(64) + n, f, randomString(16)) : n;
    }
    function encryptPassword(n, f) { return encryptAES(n, f); }

其中 `randomString` 的字符集刻意剔除了 I/L/O/U/V/g/l/o/q/u/v/0/1/9 等易混淆字符。
CryptoJS 的 `toString()` 输出为 Base64。
"""

from __future__ import annotations

import base64
import secrets

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# encrypt.js 里的 $aes_chars，顺序保持原样以免与前端行为产生任何偏差。
_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
_BLOCK_SIZE = 16


def random_string(length: int) -> str:
    """等价于前端 `randomString(length)`，用 secrets 而非 random 提升不可预测性。"""
    if length <= 0:
        return ""
    return "".join(secrets.choice(_AES_CHARS) for _ in range(length))


def encrypt_password(password: str, salt: str) -> str:
    """把明文密码加密成登录表单 `password` 字段所需的值。

    Args:
        password: 明文密码。
        salt: 登录页隐藏域 `pwdEncryptSalt` 的值，长度 16。

    Returns:
        Base64 编码的密文。

    Raises:
        ValueError: salt 长度不足 16 字节，或密码含非 UTF-8 可编码字符时。
    """
    key = (salt or "").strip().encode("utf-8")
    if len(key) < _BLOCK_SIZE:
        raise ValueError(
            f"口令加密盐值长度不足（需要 >= {_BLOCK_SIZE} 字节，实际 {len(key)}）"
        )

    iv = random_string(_BLOCK_SIZE).encode("utf-8")
    plaintext = (random_string(64) + password).encode("utf-8")

    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(plaintext, _BLOCK_SIZE))).decode("ascii")
