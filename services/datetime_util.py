"""日時の整形。**このリポジトリでは、どこからも import されていません。**

デスクトップ版 (このリポジトリには残っていません) の名残です。多言語対応
(services/i18n.py) の対象からも外してあります — 使われていないものを訳しても
確かめようがないためです。

**使い直すときは、先に文言を t() へ通してください。** 下の
get_remaining_time_str は「3日」「時間」「後」を f-string で繋いで作ります。
語順が言語ごとに違うので、この形のままでは日本語以外で意味を成しません。
同じ表示は現在 media/main.js の remainingText が Intl で作っており、
そちらが唯一の実装です。
"""

import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def parse_utc_to_local(utc_str: str) -> datetime:
    """
    UTCのISO8601文字列をローカルのdatetimeオブジェクトに変換します。
    例: "2026-08-05T13:45:00Z" -> JST datetime
    """
    if not utc_str or not isinstance(utc_str, str):
        return None
    try:
        # Z を +00:00 に置換して fromisoformat でパース可能にする
        normalized = utc_str.strip().replace("Z", "+00:00").replace("z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError) as e:
        logger.warning("日時 '%s' を解釈できませんでした: %s", utc_str, e)
        return None

    if dt.tzinfo is None:
        # タイムゾーン情報が無い場合、astimezone() はローカル時刻とみなしてしまう。
        # このAPIの時刻は UTC なので、明示的に UTC を付与してから変換する
        # (これを怠ると JST 環境で 9 時間ずれた値が黙って表示される)
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone()


def format_datetime(dt: datetime) -> str:
    """datetimeオブジェクトを読みやすい形式の文字列にフォーマットします。"""
    if not dt:
        return "N/A"
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def get_remaining_time_str(dt: datetime) -> str:
    """指定された時刻までの残り時間を '1日2時間3分' のような形式で返します。

    1分未満のときは秒まで表示します。1秒ごとのカウントダウン表示から呼ばれるため、
    残りわずかな場面で画面が止まって見えないようにするためです。
    """
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    if dt <= now:
        return "制限解除済み"

    # マイクロ秒を切り捨てると「残り30秒」が「29秒後」と表示されるため切り上げる
    total_seconds = math.ceil((dt - now).total_seconds())
    days, rest = divmod(total_seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}日")
    if hours > 0 or days > 0:
        parts.append(f"{hours}時間")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}分")
    else:
        # 残り1分未満。秒を出さないと最大59秒間まったく表示が変化しない
        return f"{seconds}秒後"

    return "".join(parts) + "後"
