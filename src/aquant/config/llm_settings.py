"""LLM API 设置的本地持久化(密钥文件权限 0600,读取时脱敏)。"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

ENV_SETTINGS_PATH = "AQUANT_LLM_SETTINGS_PATH"
DEFAULT_SETTINGS_PATH = Path.home() / ".aquant" / "llm_settings.json"
DEFAULTS: dict[str, Any] = {
    "provider": "custom",
    "base_url": "",
    "api_key": "",
    "model": "",
    "auth_scheme": "bearer",
    "timeout": 120.0,
    "max_retries": 2,
}


def settings_path() -> Path:
    return Path(os.environ.get(ENV_SETTINGS_PATH) or DEFAULT_SETTINGS_PATH).expanduser()


def load_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return payload


def clear_settings() -> None:
    path = settings_path()
    if path.exists():
        path.unlink()


def redact(data: dict[str, Any]) -> dict[str, Any]:
    key = str(data.get("api_key") or "")
    out = {k: data.get(k, DEFAULTS.get(k)) for k in DEFAULTS}
    out.pop("api_key", None)
    out["has_api_key"] = bool(key)
    out["api_key_hint"] = f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else ("••••" if key else "")
    out["configured"] = all(str(out.get(k) or "") for k in ("base_url", "model")) and bool(key)
    out["settings_path"] = str(settings_path())
    return out
