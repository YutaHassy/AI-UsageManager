"""Cookie などの機微な文字列を、保存時に Windows DPAPI で暗号化するためのヘルパー。

pywin32 などの追加パッケージには依存せず、ctypes から crypt32.dll の
CryptProtectData / CryptUnprotectData を直接呼び出します。

DPAPI で保護されたデータは「同じ Windows ユーザー・同じマシン」でしか復号できないため、
config.json を丸ごとコピーされたり、クラウド同期で外部に流出したりしても
セッション Cookie をそのまま悪用されることはありません。

Windows 以外の環境や DPAPI が利用できない環境では平文にフォールバックします
(暗号化できなかったことは is_encryption_available() で判別できます)。
"""

import base64
import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

# 暗号化済みの値であることを示す接頭辞。平文との区別に使う。
_PREFIX = "dpapi:v1:"

# 復号時に「どのアプリが暗号化したか」を照合するための説明文字列
_DESCRIPTION = "AI-UsageManager"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _load_crypt32():
    """crypt32.dll / kernel32.dll をロードして関数シグネチャを設定する。"""
    if sys.platform != "win32":
        return None, None

    try:
        crypt32 = ctypes.WinDLL("crypt32.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
    except OSError as e:  # pragma: no cover - Windows 以外では到達しない
        logger.warning("DPAPI を利用できません: %s", e)
        return None, None

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    return crypt32, kernel32


_crypt32, _kernel32 = _load_crypt32()


def is_encryption_available() -> bool:
    """DPAPI による暗号化が使える環境かどうかを返します。"""
    return _crypt32 is not None


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob: _DataBlob) -> None:
    if blob.pbData:
        _kernel32.LocalFree(blob.pbData)


def is_encrypted(value: str) -> bool:
    """与えられた文字列が本モジュールで暗号化された値かどうかを判定します。"""
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(plain: str) -> str:
    """平文を DPAPI で暗号化して `dpapi:v1:<base64>` 形式の文字列にします。

    すでに暗号化済みの値はそのまま返します。
    暗号化できない環境では平文をそのまま返します (この場合 is_encrypted() は False)。
    """
    if not plain:
        return plain
    if is_encrypted(plain):
        return plain
    if _crypt32 is None:
        return plain

    raw = plain.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()

    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        _DESCRIPTION,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        # 失敗しても保存自体は継続させたいので、平文にフォールバックする
        logger.warning(
            "DPAPI による暗号化に失敗しました (エラーコード %s)。平文で保存します。",
            ctypes.GetLastError(),
        )
        return plain

    try:
        encrypted = _blob_to_bytes(blob_out)
    finally:
        _free_blob(blob_out)

    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def decrypt(value: str) -> str:
    """encrypt() で暗号化された文字列を復号します。

    平文 (接頭辞なし) はそのまま返すため、暗号化前に保存された既存の
    config.json をそのまま読み込むことができます。
    """
    if not value or not is_encrypted(value):
        return value
    if _crypt32 is None:
        logger.error("暗号化された Cookie を復号できません (この環境では DPAPI が使えません)。")
        return ""

    try:
        encrypted = base64.b64decode(value[len(_PREFIX):])
    except (ValueError, TypeError) as e:
        logger.error("暗号化データの形式が不正です: %s", e)
        return ""

    buffer = ctypes.create_string_buffer(encrypted, len(encrypted))
    blob_in = _DataBlob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    description = wintypes.LPWSTR()

    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if description.value is not None:
        _kernel32.LocalFree(description)

    if not ok:
        # 別ユーザー/別マシンの config.json を持ち込んだ場合はここに来る
        logger.error(
            "Cookie の復号に失敗しました (エラーコード %s)。"
            "別の Windows ユーザーまたは別の PC で保存された設定の可能性があります。"
            "該当アカウントは再ログインが必要です。",
            ctypes.GetLastError(),
        )
        return ""

    try:
        return _blob_to_bytes(blob_out).decode("utf-8")
    finally:
        _free_blob(blob_out)
