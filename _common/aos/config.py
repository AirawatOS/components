"""aos.config — persistent CLI config at ~/.aos/config.json, so a developer sets the target ONCE
(`aos config set network=… issuer=…`) instead of exporting env vars every run. Companion to `aos login`.

Resolution precedence for every setting: explicit env var (CI / one-off) → this config file → built-in
default. So `export AIRAWAT_NETWORK=…` still overrides for a single run, but the common case is just
`aos config set …` once, then `aos login`, then `aos deploy --live`.
"""
import json, os

CONFIG = os.path.expanduser("~/.aos/config.json")

# canonical key -> (env var that overrides it, built-in default)
_KEYS = {
    "network":        ("AIRAWAT_NETWORK", "http://network:8090"),
    "kernel":         ("AIRAWAT_KERNEL", "http://kernel:8080"),
    "identity":       ("AIRAWAT_IDENTITY", None),
    "oidc_issuer":    ("AIRAWAT_OIDC_ISSUER", None),
    "oidc_client_id": ("AIRAWAT_OIDC_CLIENT_ID", None),
    "dev_user":       ("AIRAWAT_DEV_USER", "devrick"),
    "tenant":         ("AIRAWAT_TENANT", None),
    "login_tenant":   ("AIRAWAT_LOGIN_TENANT", None),
}
# convenient aliases accepted by `aos config set`
_ALIAS = {"net": "network", "kern": "kernel", "idn": "identity", "issuer": "oidc_issuer",
          "client_id": "oidc_client_id", "client-id": "oidc_client_id"}


def _load():
    try:
        return json.load(open(CONFIG)) or {}
    except Exception:
        return {}


def get(key, default=None):
    """Resolve one setting: env var → config file → built-in default (or the passed default)."""
    env, dflt = _KEYS.get(key, (None, None))
    if env and os.environ.get(env):
        return os.environ[env]
    v = _load().get(key)
    if v is not None:
        return v
    return default if default is not None else dflt


def set_many(pairs):
    d = _load()
    for k, v in pairs.items():
        k = _ALIAS.get(k, k)
        if k not in _KEYS:
            raise KeyError(f"unknown config key '{k}' (known: {', '.join(sorted(_KEYS))})")
        d[k] = v
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(d, f, indent=2)
    return d


def resolved():
    """All settings with their effective values + where each came from (env|config|default)."""
    out = {}
    saved = _load()
    for k, (env, dflt) in _KEYS.items():
        if env and os.environ.get(env):
            out[k] = (os.environ[env], "env")
        elif k in saved:
            out[k] = (saved[k], "config")
        else:
            out[k] = (dflt, "default")
    return out
