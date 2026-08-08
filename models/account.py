import uuid

from services import providers
from services.i18n import t


def _coerce_budget(value) -> float:
    """上限金額を float に正規化します。

    **0 が「未設定」です。** 別のセンチネル値を作らないのは、
    money_metric() が既に `budget > 0` で判定しているためです。

    手編集された config.json に文字列や負数が入っていても、
    起動を止めずに「未設定」へ倒します (設定ファイルの他の項目と同じ方針)。
    """
    try:
        budget = float(value)
    except (TypeError, ValueError):
        return 0.0
    return budget if budget > 0 else 0.0


class Account:
    """1つの取得先の登録内容。

    cookie は「その取得先の資格情報」という意味で、Cookie 以外
    (API キー等) もここに入ります。設定ファイル上のキー名は互換のため
    cookie のままにしています。
    """

    def __init__(self, name: str, organization_id: str, cookie: str, enabled: bool = True,
                 id: str = None, provider: str = None, budget: float = 0.0):
        self.id = id if id else str(uuid.uuid4())
        # 手編集された config.json では値が null になっていることがあるため、
        # None を空文字として吸収する (以前は .strip() で AttributeError になっていた)
        self.name = (name or "").strip()
        self.organization_id = (organization_id or "").strip()
        self.cookie = (cookie or "").strip()
        self.enabled = bool(enabled)
        # 未指定は Claude。provider を持たない既存の設定ファイルを
        # そのまま読めるようにするため、ここで既定へ倒す。
        self.provider = (provider or "").strip() or providers.DEFAULT_PROVIDER_ID
        # 課金系の取得先だけが使う上限金額。0 は未設定。
        self.budget = _coerce_budget(budget)

    @property
    def credential(self) -> str:
        """資格情報。Cookie 認証以外の取得先でも読めるようにした別名。"""
        return self.cookie

    def get_provider(self) -> providers.UsageProvider:
        return providers.get(self.provider)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "organization_id": self.organization_id,
            "cookie": self.cookie,
            "enabled": self.enabled,
            "budget": self.budget,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Account':
        if not isinstance(data, dict):
            raise ValueError(t(
                "The account definition is not an object: {kind}",
                kind=type(data).__name__,
            ))
        # data.get(key, "") はキーが存在して値が null の場合に None を返してしまうため、
        # or "" で明示的な null も空文字に倒す
        return cls(
            name=data.get("name") or "",
            organization_id=data.get("organization_id") or "",
            cookie=data.get("cookie") or "",
            enabled=data.get("enabled", True),
            id=data.get("id") or None,
            provider=data.get("provider") or None,
            # このキーを持たない既存の config.json は 0 (未設定) に倒れる
            budget=data.get("budget", 0),
        )

    def __repr__(self) -> str:
        # 資格情報がトレースバックやログに紛れ込まないよう必ず伏せる
        return (
            f"Account(id={self.id!r}, name={self.name!r}, provider={self.provider!r}, "
            f"organization_id={self.organization_id!r}, "
            f"cookie=<{len(self.cookie)} chars redacted>, enabled={self.enabled})"
        )
