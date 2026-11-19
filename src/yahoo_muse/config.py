import fnmatch
import getpass
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path


class MailError(Exception):
    """A safe error that can be returned to an agent."""


def config_path():
    return Path(os.environ.get("YAHOO_MUSE_CONFIG", "~/.config/yahoo-muse/config.json")).expanduser()


DEFAULT_IMAP_HOST = 'imap.mail.yahoo.com'
DEFAULT_SMTP_HOST = 'smtp.mail.yahoo.com'
DEFAULT_MAX_SENDS_PER_HOUR = 10

_PATTERN_RE = re.compile(r'^[^\s<>@]+@[^\s<>@]+$')
_HOST_RE = re.compile(r'^[A-Za-z0-9.-]+$')
_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_allow_list(patterns):
    if not isinstance(patterns, list):
        raise ValueError()
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern.isascii()
                or not _PATTERN_RE.match(pattern)):
            raise ValueError()
    return [p.lower() for p in patterns]


def _validate_host(value, default):
    if value is None:
        return default
    if not isinstance(value, str) or not _HOST_RE.match(value):
        raise ValueError()
    return value


def _validate_max_sends(value):
    if value is None:
        return DEFAULT_MAX_SENDS_PER_HOUR
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError()
    return value


def _resolve_password(data):
    """Return the app password, honoring indirection before the inline value.

    Precedence: app_password_env, then app_password_file, then app_password.
    The inline password is never required when indirection is configured.
    """
    env_name = data.get('app_password_env')
    if env_name is not None:
        if not isinstance(env_name, str) or not _ENV_NAME_RE.match(env_name):
            raise ValueError()
        password = os.environ.get(env_name, '')
        if not password:
            raise MailError(
                "Configured app_password_env %r is not set in the environment." % env_name)
        return password
    file_name = data.get('app_password_file')
    if file_name is not None:
        if not isinstance(file_name, str) or not file_name:
            raise ValueError()
        path = Path(file_name).expanduser()
        if path.is_symlink() or not path.is_file():
            raise MailError("Configured app_password_file is not a regular file.")
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError()
            content = path.read_text()
        except (OSError, UnicodeError):
            raise MailError("Configured app_password_file could not be read.") from None
        password = content.splitlines()[0].strip() if content.strip() else ''
        if not password:
            raise MailError("Configured app_password_file is empty.")
        return password
    password = data.get('app_password')
    if not isinstance(password, str) or not password:
        raise ValueError()
    return password


@dataclass
class Config:
    email: str
    app_password: str
    state_dir: Path
    allowed_recipients: list = field(default_factory=list)
    max_sends_per_hour: int = DEFAULT_MAX_SENDS_PER_HOUR
    imap_host: str = DEFAULT_IMAP_HOST
    smtp_host: str = DEFAULT_SMTP_HOST

    def recipient_allowed(self, address):
        """True when the allow-list is empty (no gate) or the address matches
        any pattern. Patterns are matched case-insensitively with fnmatch, so
        '*@example.com' allows every address at that domain."""
        if not self.allowed_recipients:
            return True
        address = address.lower()
        return any(fnmatch.fnmatchcase(address, pattern) for pattern in self.allowed_recipients)

    @classmethod
    def load(cls):
        path = config_path()
        try:
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise MailError("Config must be a private regular file (chmod 600).")
            if path.stat().st_uid != os.getuid():
                raise MailError("Config must be owned by the current user.")
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            email = data["email"]
            if not isinstance(email, str) or "@" not in email or any(c in email for c in "\r\n"):
                raise ValueError()
            password = _resolve_password(data)
            return cls(
                email,
                password,
                path.parent,
                _validate_allow_list(data.get('allowed_recipients', [])),
                _validate_max_sends(data.get('max_sends_per_hour')),
                _validate_host(data.get('imap_host'), DEFAULT_IMAP_HOST),
                _validate_host(data.get('smtp_host'), DEFAULT_SMTP_HOST),
            )
        except FileNotFoundError:
            raise MailError("Run yahoo-mail setup locally to configure your Yahoo account.") from None
        except (ValueError, KeyError, TypeError):
            raise MailError("Invalid configuration. Run yahoo-mail setup again.") from None


def setup():
    path = config_path()
    if path.exists() or path.is_symlink():
        raise MailError("Configuration already exists. Move it aside before replacing it.")
    email = input("Yahoo email address: ").strip()
    if "@" not in email or any(c in email for c in "\r\n"):
        raise MailError("Enter a valid Yahoo email address.")
    password = getpass.getpass("Yahoo app password (hidden): ").replace(" ", "")
    if not password:
        raise MailError("App password cannot be empty.")
    allow = input(
        "Restrict sending to specific recipients? Comma-separated email patterns "
        "(e.g. *@example.com), or leave blank for no restriction: ").strip()
    allowed = []
    if allow:
        try:
            allowed = _validate_allow_list([p.strip() for p in allow.split(',') if p.strip()])
        except ValueError:
            raise MailError("Allow-list entries must look like addresses (a * wildcard is allowed).") from None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        config = {"email": email, "app_password": password}
        if allowed:
            config["allowed_recipients"] = allowed
        json.dump(config, f)
    return {"configured": True, "config_path": str(path), "next": "yahoo-mail doctor"}
