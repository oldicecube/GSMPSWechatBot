"""WeFlow account folders and actual WeChat identities are different fields."""
import re


def canonical_wechat_user_id(account: str) -> str:
    """Match WeFlow ConfigService.cleanAccountDirName without changing route IDs."""
    value = str(account or "").strip()
    if value.lower().startswith("wxid_"):
        match = re.match(r"^(wxid_[^_]+)", value, re.IGNORECASE)
        return match[1] if match else value
    match = re.fullmatch(r"(.+)_([a-zA-Z0-9]{4})", value)
    return match[1] if match else value
