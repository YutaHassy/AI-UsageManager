"""使用率から「どう見せるか」を決める、ただ1つの場所。

しきい値・丸印・1行要約はここだけで決めます。**デスクトップ版 (ui/styles.py) と
VSCode 拡張の両方がここを見ます。** 同じアカウントが exe と VSCode で違う色に
見えると、どちらが本当なのか分からなくなるためです。

**拡張の JavaScript 側には判定を持たせません。** backend/cli.py が annotate() を
通した「判定済みの状態 (丸印・色の段階・文言)」を載せて返し、拡張はそれを
そのまま画面に出すだけにしてあります。以前は同じしきい値が store.ts と
media/main.js と ui/styles.py の3箇所にあり、片方だけ直せば食い違う状態でした。
言語をまたいで数値を書き写すのをやめる、というのがこのファイルの目的です。

Qt には依存しません。デスクトップ版 (PySide6 入り) と拡張のバックエンド
(PySide6 なし) の両方から import されるので、ここに画面の部品を持ち込むと、
表示・更新だけの利用者に PySide6 を要求することになります。
"""

from typing import Any, Dict, Optional

from services.providers.base import MONEY

# ---- 使用率のしきい値 ----
#
# **必ずここだけで決めてください。** 丸印・文字・バーの色が食い違うと、
# 同じアカウントがどの状態なのか分からなくなります。
CAUTION_PERCENT = 60.0     # ここから「残りわずか」
LIMITED_PERCENT = 100.0    # ここで「制限中」

# ---- 色の段階 ----
#
# 値は Webview の CSS クラス名 (media/main.css の .bar-fill.ok など) と
# そのまま同じにしてあります。拡張側で名前を作り替えると、色の決め方が
# また JavaScript 側に戻ってしまうためです。
LEVEL_OK = "ok"              # まだ余裕がある
LEVEL_CAUTION = "caution"    # 残りわずか
LEVEL_LIMITED = "limited"    # 使い切っている
LEVEL_UNKNOWN = "unknown"    # ゲージに載せられない (課金額など)

# 状態を表す記号。一覧とサマリーで同じものを使います
# (同じアカウントが場所によって違う色に見えると、どちらが本当か分からない)。
DOT_DISABLED = "⚪"      # 無効化
DOT_UNKNOWN = "⚫"       # 未取得
DOT_ERROR = "🔴"        # 取得失敗・要再ログイン
DOT_WORKING = "🔵"      # 更新中
DOT_NO_GAUGE = "🔵"     # 取得済みだがゲージにできない (課金額など)

_LEVEL_DOTS = {
    LEVEL_OK: "🟢",
    LEVEL_CAUTION: "🟡",
    LEVEL_LIMITED: "🔴",
    LEVEL_UNKNOWN: DOT_NO_GAUGE,
}


def is_limited(percentage) -> bool:
    """枠を使い切って、実際に使えなくなっている状態か。

    「制限中」は使い切った状態だけを指します。以前は 85% で赤にしていましたが、
    まだ 15% 残っているものを「制限中」と呼ぶのは嘘でした。

    判定を四捨五入してから行うのは、画面の表示が小数第1位までのためです。
    99.96% は「100.0%」と表示されるので、これを黄色のままにすると
    「100.0% と出ているのに制限中ではない」という食い違いが起きます。
    """
    if percentage is None:
        return False
    return round(float(percentage), 1) >= LIMITED_PERCENT


def level(percentage) -> str:
    """使用率を色の段階に落とします。

    percentage が None のときは UNKNOWN です。ゲージに載せられる指標が1つも
    無い (課金額だけを取得した場合など) ことを意味するので、0% として
    「まだ余裕がある」色を付けると嘘になります。
    """
    if percentage is None:
        return LEVEL_UNKNOWN
    if is_limited(percentage):
        return LEVEL_LIMITED
    return LEVEL_OK if float(percentage) < CAUTION_PERCENT else LEVEL_CAUTION


def status_dot(percentage) -> str:
    """使用率に対応する丸印。

    以前は「有効なら常に 🟢」だったため、上限に達していても緑のままでした。
    一覧を見た瞬間に逼迫が分かることがこの画面の存在理由なので、
    しきい値は level と必ず同じにします (同じ関数から引いています)。
    """
    return _LEVEL_DOTS[level(percentage)]


def summarize(result: Dict[str, Any]) -> str:
    """一覧に出す1行要約。

    金額はパーセントで要約しません。上限は利用者が決めた任意の値なので、
    率だけでは元の金額が分からず判断材料になりません
    (money_metric の「実額を必ず読めるようにする」と同じ考え方)。

    ゲージに載せられる枠が複数あるときは、最も逼迫しているものを選びます。
    同率のときは取得先が返した順で先に来たものを採ります (並べ替えないのは、
    取得先が意味のある順で返しているため)。
    """
    metrics = result.get("metrics") or []
    worst: Optional[Dict[str, Any]] = None
    for metric in metrics:
        utilization = metric.get("utilization")
        if utilization is None:
            continue
        if worst is None or utilization > worst["utilization"]:
            worst = metric

    if worst is not None:
        if worst.get("kind") != MONEY:
            return f"{float(worst['utilization']):.1f}%"
        return worst.get("display") or "-"

    if metrics:
        return metrics[0].get("display") or "-"
    return "-"


def annotate(result: Dict[str, Any]) -> Dict[str, Any]:
    """取得結果に「判定済みの表示状態」を書き足して返します。

    足すのは次の2つだけで、取得先が返した値には触りません。

      各 metric に  level / dot … その枠ひとつぶんの色の段階と丸印
      result に     status      … アカウント1件としての段階・丸印・1行要約

    元の dict は書き換えず、新しい dict を返します (apply_budget と同じ理由:
    呼び出し元と取得スレッドが同じ辞書を触らないようにするため)。
    """
    metrics = []
    for metric in result.get("metrics") or []:
        utilization = metric.get("utilization")
        annotated = dict(metric)
        annotated["level"] = level(utilization)
        annotated["dot"] = status_dot(utilization)
        metrics.append(annotated)

    merged = dict(result)
    merged["metrics"] = metrics

    # 丸印は「最も逼迫している枠」で決めます。max_utilization は
    # build_result が入れたもので、ゲージに載せられる枠が1つも無ければ
    # None です (そのとき status は UNKNOWN / 🔵 になります)。
    max_utilization = result.get("max_utilization")
    merged["status"] = {
        "level": level(max_utilization),
        "dot": status_dot(max_utilization),
        "summary": summarize(merged),
    }
    return merged
