"""Configuration: one JSON file, defaults for everything."""

import copy
import hashlib
import hmac
import json
import os
import secrets

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8742,
    "password_hash": "",
    "data_dir": "data",
    # What the page calls this installation.
    "title": "Bulk SMS",
    # Optional remote check: "do you already know these numbers?" See README.
    "lookup": {"url": "", "token": "", "label": "our database", "chunk": 500, "timeout": 20},
    "country_code": "46",
    "default_delay": 20,
    "default_jitter": 5,
    "default_batch": 1,
    "window_start": "08:00",
    "window_end": "21:00",
    "sim_slot": None,
    "dry_run": True,
    "max_consecutive_failures": 3,
}

_ITERATIONS = 400_000


def path():
    return os.environ.get("BULKSMS_CONFIG") or os.path.join(os.getcwd(), "config.json")


def load(file=None):
    file = file or path()
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(file):
        with open(file, encoding="utf-8") as fh:
            given = json.load(fh)
        for key, value in given.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    # "dry_run": "false" is a non-empty string, and so would count as true.
    if not isinstance(cfg["dry_run"], bool):
        raise SystemExit(f'{file}: "dry_run" must be true or false, without quotes.')
    cfg["_file"] = file
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(os.path.dirname(os.path.abspath(file)), cfg["data_dir"])
    return cfg


def save_value(key, value, file=None):
    """Write one top-level key back, leaving the rest of the file as it was."""
    file = file or path()
    data = {}
    if os.path.exists(file):
        with open(file, encoding="utf-8") as fh:
            data = json.load(fh)
    data[key] = value
    tmp = file + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, file)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), digest)
