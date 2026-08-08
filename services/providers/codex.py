"""OpenAI Codex CLI のレート上限 (5時間枠・週間枠など)。

Codex CLI はモデル呼び出しのたびにレート上限のスナップショットを
セッションの記録ファイル (rollout JSONL) へ書き出します。
ここではそれを**読むだけ**です。認証情報 (auth.json) には一切触れません。

  %USERPROFILE%\\.codex\\sessions\\YYYY\\MM\\DD\\rollout-<日時>-<thread_id>.jsonl

各行は 1 つの JSON で、レート上限は type=event_msg かつ payload.type=token_count
の行に入ります:

  {"timestamp": "...", "type": "event_msg",
   "payload": {"type": "token_count",
               "info": {...},
               "rate_limits": {"limit_id": "codex",
                               "primary":   {"used_percent": 12.5,
                                             "window_minutes": 300,
                                             "resets_at": 1704069000},
                               "secondary": {"used_percent": 40.0,
                                             "window_minutes": 10080,
                                             "resets_at": 1704470400},
                               "plan_type": "plus", ...}}}

フィールド名は、このPCに入っているバイナリと同一タグ (rust-v0.147.0-alpha.1.2) の
`codex-rs/protocol/src/protocol.rs` の RateLimitSnapshot / RateLimitWindow で確認。

**命名の層を混同しないこと。** 同じ概念に3通りの名前があります:
  - rollout JSONL / core protocol … used_percent / window_minutes / resets_at   ← ここで使う
  - app-server の JSON-RPC        … usedPercent / windowDurationMins / resetsAt
  - backend の /wham/usage        … used_percent / limit_window_seconds / reset_after_seconds
`resets_in_seconds` は現行バージョンには存在しません (旧版の遺物で、使うと必ず None)。

resets_at は Unix 秒です。
"""

import glob
import json
import logging
import os
from typing import Any, Dict, List

from services.i18n import t
from services.providers.base import (
    AUTH_OAUTH, UsageError, UsageProvider, build_result, percent_metric, unix_to_iso,
)

logger = logging.getLogger(__name__)

# 新しい順に見るファイル数の上限。モデル呼び出しの無いセッションには
# レート上限が載らないので、1つ見て諦めずに少し遡る。
_MAX_FILES = 20

# window_minutes と表示名の対応。Codex 側も厳密一致ではなく近似で判定している。
# 表示名は訳す前の原文 (英語)。訳すのは _window_label です。
_WINDOWS = [
    (300, "5-hour"),
    (1440, "Daily"),
    (10080, "Weekly"),
    (43200, "Monthly"),
]
# 近似の許容幅 (±20%)
_WINDOW_TOLERANCE = 0.2


def codex_home() -> str:
    """Codex CLI の設定ディレクトリ。"""
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def sessions_dir() -> str:
    return os.path.join(codex_home(), "sessions")


def rollout_files(limit: int = _MAX_FILES) -> List[str]:
    """rollout ファイルを新しい順に返します。

    通常は sessions/YYYY/MM/DD/ の3階層ですが、階層を決め打ちすると
    Codex 側が構造を変えたときに黙って0件になります。再帰で探します。
    """
    pattern = os.path.join(sessions_dir(), "**", "rollout-*.jsonl")
    try:
        paths = glob.glob(pattern, recursive=True)
    except OSError as e:
        logger.warning("rollout ファイルの探索に失敗しました: %s", e)
        return []

    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    paths.sort(key=mtime, reverse=True)
    return paths[:limit]


def _window_label(window_minutes) -> str:
    """window_minutes を「5時間」「週間」などに直します。

    対応表に無い値でも、分かる形で出します
    (勝手に既知の枠へ丸めると、別の枠を誤った名前で表示することになる)。
    """
    if not isinstance(window_minutes, (int, float)) or window_minutes <= 0:
        return t("Rate limit")
    for minutes, label in _WINDOWS:
        if abs(window_minutes - minutes) <= minutes * _WINDOW_TOLERANCE:
            return t(label)
    if window_minutes >= 1440:
        return t("{days}-day window", days=f"{window_minutes / 1440:.0f}")
    return t("{minutes}-minute window", minutes=f"{window_minutes:.0f}")


_to_iso = unix_to_iso


class CodexProvider(UsageProvider):
    id = "codex"
    label = "Codex CLI"
    description = ("The Codex CLI rate limits. Reads a local record file only — "
                   "no credentials are used.")

    # 別ツール (Codex CLI) のログイン状態に乗るので、このアプリ側に保存するものは無い
    auth_kind = AUTH_OAUTH
    credential_label = "the Codex CLI sign-in state"

    @classmethod
    def diagnose(cls) -> str:
        """取得に失敗したとき、原因の切り分けに必要な情報だけを集めます。

        記録ファイルには会話の内容が入るため、**キー名と件数だけ**を出します。
        値は一切出しません (プロンプトや応答が診断結果に混ざらないようにする)。

        **この出力は英語のままにしてあります。** 利用者が開発者へ転送するための
        塊で、読むのは開発者です。転送元の表示言語で語が変わると、報告を
        突き合わせられなくなります (_diagnostic_footer の案内文だけは訳します)。
        """
        lines = [
            f"CODEX_HOME: {codex_home()} (exists: {os.path.isdir(codex_home())})",
            f"sessions  : {sessions_dir()} (exists: {os.path.isdir(sessions_dir())})",
        ]

        files = rollout_files()
        lines.append(f"record files: {len(files)}")
        if not files:
            return "\n".join(lines)

        newest = files[0]
        lines.append(f"newest: {os.path.basename(newest)}")

        total = candidates = 0
        last_record = None
        try:
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total += 1
                    if "token_count" not in line:
                        continue
                    candidates += 1
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        last_record = record
        except OSError as e:
            lines.append(f"read failed: {e}")
            return "\n".join(lines)

        lines.append(f"lines: {total} / lines containing token_count: {candidates}")

        if last_record is None:
            lines.append("the token_count lines could not be read as JSON.")
            return "\n".join(lines)

        # ここから下はキー名のみ。値は出さない。
        lines.append(f"type of last line: {last_record.get('type')!r}")
        lines.append(f"keys of last line: {sorted(last_record.keys())}")
        payload = last_record.get("payload")
        if isinstance(payload, dict):
            lines.append(f"payload.type: {payload.get('type')!r}")
            lines.append(f"keys of payload: {sorted(payload.keys())}")
            limits = payload.get("rate_limits") or payload.get("rateLimits")
            if isinstance(limits, dict):
                lines.append(f"keys of rate_limits: {sorted(limits.keys())}")
                for key in ("primary", "secondary"):
                    window = limits.get(key)
                    if isinstance(window, dict):
                        lines.append(f"  keys of {key}: {sorted(window.keys())}")
            else:
                lines.append("no rate_limits (the key name may have changed).")

        return "\n".join(lines)

    def fetch_usage(self, credential: str = "", organization_id: str = "") -> Dict[str, Any]:
        if not os.path.isdir(codex_home()):
            raise UsageError(
                t("The Codex CLI settings directory was not found: {path}\n\n"
                  "If it lives somewhere else, point the CODEX_HOME "
                  "environment variable at it.",
                  path=codex_home()),
                auth_error=True,
            )

        files = rollout_files()
        if not files:
            raise UsageError(
                t("No rate-limit record was found.\n"
                  "Searched: {path}\n\n"
                  "Sign in to the Codex CLI and send at least one prompt "
                  "to a model.\n"
                  "Signing in alone does not create a record.",
                  path=sessions_dir()),
                auth_error=True,
            )

        for path in files:
            snapshot = self._latest_snapshot(path)
            if not snapshot:
                continue
            try:
                return self.parse_snapshot(snapshot, source=path)
            except UsageError as e:
                # 記録は取れているのに中身の名前が違う場合。
                # ここが最も起こりやすい失敗なので、必ず診断を添える。
                raise UsageError(f"{e}\n\n{self._diagnostic_footer()}") from e

        # 記録はあるが token_count / rate_limits が1件も無い場合
        raise UsageError(t(
            "{count} record files were found, but no rate limit could be read.\n"
            "{diagnosis}",
            count=len(files), diagnosis=self._diagnostic_footer(),
        ))

    @classmethod
    def _diagnostic_footer(cls) -> str:
        return t(
            "If sending at least one prompt to a model does not fix this, "
            "pass the following to the developer\n"
            "(it contains no conversation content — only key names and counts).\n\n"
            "{diagnosis}",
            diagnosis=cls.diagnose(),
        )

    @staticmethod
    def _latest_snapshot(path: str):
        """1ファイルから最後の rate_limits を取り出します。

        1行ずつ読むのは、記録ファイルが大きくなりうるためです。
        壊れた行は読み飛ばします (書き込み中の末尾が途切れることがある)。
        """
        latest = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or "token_count" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "event_msg":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    # 層によって rate_limits / rateLimits の両方がありうる
                    limits = payload.get("rate_limits") or payload.get("rateLimits")
                    if isinstance(limits, dict):
                        latest = limits
        except OSError as e:
            logger.warning("記録ファイルを読めませんでした (%s): %s", path, e)
        return latest

    @staticmethod
    def _field(window: Dict[str, Any], *names):
        """同じ意味の別名を順に探します。

        Codex は同じ構造を層ごとに別の名前で出します
        (rollout JSONL は used_percent、app-server は usedPercent で
         window_minutes は windowDurationMins に改名される)。
        どちらが書かれていても意味は同じなので、両方受け付けます。
        名前を発明しているのではなく、実在する2層の別名を許容するだけです。
        """
        for name in names:
            if name in window:
                return window[name]
        return None

    @classmethod
    def parse_snapshot(cls, snapshot: Dict[str, Any], source: str = "") -> Dict[str, Any]:
        metrics = []

        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue

            used = cls._field(window, "used_percent", "usedPercent")
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                logger.warning("%s の使用率が数値ではありません: %r", key, used)
                continue

            metrics.append(percent_metric(
                key,
                _window_label(cls._field(window, "window_minutes", "windowDurationMins")),
                float(used),
                resets_at=_to_iso(cls._field(window, "resets_at", "resetsAt")),
            ))

        if not metrics:
            # ここで 0% を返すと「まだ使っていない」と誤解される。
            logger.error("レート上限を解釈できませんでした: %s", sorted(snapshot.keys()))
            raise UsageError(t(
                "The rate-limit format was not recognised "
                "(the Codex CLI may have changed). Keys received: {keys}",
                keys=', '.join(map(str, sorted(snapshot.keys()))) or t("(none)"),
            ))

        return build_result(
            metrics,
            plan_type=snapshot.get("plan_type"),
            source=source,
            raw=snapshot,
        )
