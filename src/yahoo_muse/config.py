import getpass
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class MailError(Exception):
    """A safe error that can be returned to an agent."""


def config_path():
    return Path(os.environ.get("YAHOO_MUSE_CONFIG", "~/.config/yahoo-muse/config.json")).expanduser()


@dataclass
class Config:
    email: str
    app_password: str
    state_dir: Path

    @classmethod
    def load(cls):
        path = config_path()
        try:
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise MailError("Config must be a private regular file (chmod 600).")
            if path.stat().st_uid != os.getuid():
                raise MailError("Config must be owned by the current user.")
            data = json.loads(path.read_text())
            email, password = data["email"], data["app_password"]
            if not isinstance(email, str) or "@" not in email or any(c in email for c in "\r\n"):
                raise ValueError()
            if not isinstance(password, str) or not password:
                raise ValueError()
            return cls(email, password, path.parent)
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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"email": email, "app_password": password}, f)
    return {"configured": True, "config_path": str(path), "next": "yahoo-mail doctor"}
