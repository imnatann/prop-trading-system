"""
Secret redaction / scrubbing layer.

Single choke point for every log line, error message, repr and telemetry payload in
the FundingPips execution layer. Nothing that might contain a credential is allowed
to bypass this module.

Threat model
------------
Credentials arrive from the environment:
    FUNDINGPIPS_MT5_LOGIN / FUNDINGPIPS_MT5_PASSWORD / FUNDINGPIPS_MT5_SERVER

They can leak through:
  * f-strings that interpolate the login into a log line ("Connected to #12345678")
  * exception messages
  * repr() of config objects
  * JSON manifests / telemetry payloads
  * argv (we never put secrets in argv by design)

Mitigations
-----------
1. Secret wrapper whose __repr__/__str__ return a mask and which requires an
   explicit .reveal() call to obtain the value.
2. redact(text) scrubs value-shaped secrets (long digit runs, key=value pairs).
3. register_secret(value) lets the process register live secrets so exact values
   are scrubbed even when they appear in a form the regexes do not catch.
4. mask_account(login) for the one case where a partially-visible account number is
   genuinely useful in an operator-facing CLI.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Union

MASK = "***REDACTED***"

#: Values registered at runtime that must never appear in any output.
_REGISTERED_SECRETS: set = set()

#: Environment variables that are secret by definition.
SECRET_ENV_VARS = ("FUNDINGPIPS_MT5_PASSWORD", "MT5_PASSWORD")

#: Long digit runs are login numbers / account numbers.
_ACCOUNT_RE = re.compile(r"(?<![0-9])(?:[0-9]{7,})(?![0-9])")
#: key=value / "password": "value" style pairs.
_KV_RE = re.compile(
    r"(?i)(?P<q1>[\"']?)"
    r"(?P<key>\b(?:password|passwd|pwd|investor_password|token|secret|api_?key)\b)"
    r"(?P<q2>[\"']?)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
#: Explicit sentinel used by callers that know a field is secret.
_SENTINEL_RE = re.compile(r"__SECRET__[^\s,;\]}\"]*")


def register_secret(value: Optional[Union[str, int]]) -> None:
    """Register a live secret so redact() scrubs its exact value later."""
    if value is None:
        return
    text = str(value)
    if text:
        _REGISTERED_SECRETS.add(text)


def clear_registered_secrets() -> None:
    """Test helper: drop every registered secret."""
    _REGISTERED_SECRETS.clear()


def redact(text: Any) -> str:
    """Return text as a string with every known secret shape removed.

    Safe to call on any object; None becomes an empty string, and objects are
    converted via str(). Registered secrets are replaced first (longest-first so
    that a shorter secret cannot survive inside a longer one).
    """
    if text is None:
        return ""
    out = str(text)
    if not out:
        return out

    for secret in sorted(_REGISTERED_SECRETS, key=len, reverse=True):
        if secret and secret in out:
            out = out.replace(secret, MASK)

    out = _SENTINEL_RE.sub(MASK, out)
    out = _KV_RE.sub(
        lambda m: m.group("q1") + m.group("key") + m.group("q2") + m.group("sep") + MASK,
        out,
    )
    out = _ACCOUNT_RE.sub(MASK, out)
    return out


def scrub(text: Any) -> str:
    """Alias for redact(), kept because it is the conventional name at call sites."""
    return redact(text)


def mask_account(login: Optional[Union[str, int]], visible: int = 4) -> str:
    """Mask an MT5 account number, revealing only the trailing digits.

    The operator-facing CLIs genuinely need to confirm WHICH account is connected;
    showing the last 4 digits achieves that without publishing the full number.
    """
    if login is None:
        return MASK
    text = str(login).strip()
    if not text:
        return MASK
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * (len(text) - visible) + text[-visible:]


class Secret:
    """A credential wrapper that refuses to render itself.

    repr/str return a fixed mask; the real value is only reachable through the
    explicit reveal() call, which makes every use site greppable in review.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Union[str, int, None]) -> None:
        self._value = None if value is None else str(value)
        if self._value:
            register_secret(self._value)

    def reveal(self) -> str:
        """Return the underlying secret. Every call site is intentional."""
        return self._value or ""

    def is_set(self) -> bool:
        return bool(self._value)

    def __bool__(self) -> bool:
        return self.is_set()

    def __len__(self) -> int:
        return len(self._value or "")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return "Secret(%s)" % (MASK if self._value else "<unset>")

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Secret objects must not be serialized")


def redact_mapping(data: Dict[str, Any], secret_keys: Iterable[str] = ()) -> Dict[str, Any]:
    """Return a copy of data with secret-looking values replaced.

    Used before anything reaches JSON, telemetry, or a manifest.
    """
    keys = {k.lower() for k in secret_keys}
    defaults = {
        "password", "passwd", "pwd", "investor_password", "token",
        "secret", "api_key", "apikey", "authorization",
    }
    blocked = keys | defaults
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in blocked:
            out[key] = MASK
        elif isinstance(value, Secret):
            out[key] = MASK
        elif isinstance(value, str):
            out[key] = redact(value)
        else:
            out[key] = value
    return out
