"""アイコンなどの同梱ファイルを、実行形態を問わず同じ書き方で参照するためのヘルパー。

PyInstaller の onefile で固めた exe は、起動時に同梱ファイルを一時ディレクトリへ
展開し、その場所を sys._MEIPASS に入れます。ソースから直接動かしたときは
プロジェクトディレクトリがそのまま基準になるため、両者を吸収します。
"""

import os
import sys


def resource_root() -> str:
    """同梱ファイルの基準ディレクトリを返します。"""
    # onefile で固めた exe の実行時のみ存在する
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return bundled
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts: str) -> str:
    """プロジェクトルートからの相対パスを、実際のファイルパスへ変換します。

    例: resource_path("icons", "icon.png")
    """
    return os.path.join(resource_root(), *parts)


def app_icon_path() -> str:
    """アプリアイコンのパスを返します。

    多解像度の ICO を使います。QIcon は ICO の全フレーム (16〜256px) を
    読むので、タスクバーの小さな表示から Alt+Tab の大きな表示まで、
    その場に合った解像度が選ばれます。
    """
    return resource_path("icons", "icon.ico")
