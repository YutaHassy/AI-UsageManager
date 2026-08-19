"""ブラウザプロファイルの置き場所と、その破棄。

**このモジュールは PySide6 を import しません。** 同じことを扱う
browser_profile.py のほうは QtWebEngine を引き込むため、QApplication を
持たないプロセス (VSCode 拡張のバックエンド cli.py) からは触れません。

ところが「どこに保存されているか」と「消す」だけなら Qt は要りません。
アカウントの削除にログイン画面は要らないのに、その2つが Qt 側にしか
なかったため、**削除するだけで PySide6 を要求される**状態になっていました。
Qt が要らないぶんをここへ分けてあるのは、そのためです。

browser_profile.py はこのモジュールを取り込み、Qt 側の後始末 (get_profile が
使い回す辞書) を足したものを公開します。**置き場所の決め方をこちらと
あちらで二重に持たないでください。** 食い違うと、消したはずのログイン状態が
別の場所に残ります。
"""

import logging
import os
import shutil

from services.config_manager import default_config_dir

logger = logging.getLogger(__name__)


def profiles_root() -> str:
    return os.path.join(default_config_dir(), "profiles")


def profile_dir(account_id: str) -> str:
    # account_id は uuid4 なのでそのままディレクトリ名に使える
    safe = "".join(ch for ch in str(account_id) if ch.isalnum() or ch in "-_")
    return os.path.join(profiles_root(), safe or "default")


def has_saved_session(account_id: str) -> bool:
    """このアカウントのログイン状態がディスクに残っているかの目安。"""
    return os.path.exists(os.path.join(profile_dir(account_id), "Cookies"))


def remove_profile(account_id: str) -> None:
    """保存されたログイン状態 (Cookie を含む) をディスクから消します。

    **消せなくても例外は投げません。** QtWebEngine がファイルを掴んだままだと
    消せないことがあり、そこで削除そのものを失敗させると、設定からは消えた
    アカウントが「削除できませんでした」と報告されることになります。
    残っても実害はない (次に作るアカウントは別の uuid を持つので、この
    ディレクトリを引き継ぎません) ので、警告に留めます。
    """
    directory = profile_dir(account_id)
    if not os.path.exists(directory):
        return
    try:
        shutil.rmtree(directory)
        logger.info("ブラウザプロファイルを削除しました: %s", directory)
    except OSError as e:
        logger.warning("ブラウザプロファイルを削除できませんでした: %s", e)
