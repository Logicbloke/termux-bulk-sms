"""termux-bulk-sms run | init | set-password | addresses | service <...>"""

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys

from . import __version__, config
from .server import is_local

USAGE = """termux-bulk-sms %s

  termux-bulk-sms init            create the config file
  termux-bulk-sms set-password    set the password for the page
  termux-bulk-sms run             serve the page (Ctrl+C stops it)
  termux-bulk-sms addresses       the address to open from another device

  termux-bulk-sms service install     once: register it with termux-services
  termux-bulk-sms service enable      on: now and whenever Termux starts
  termux-bulk-sms service disable     off: nothing is listening
  termux-bulk-sms service restart | status | logs

The config file is $BULKSMS_CONFIG, else ./config.json if there is one,
else ~/.config/termux-bulk-sms/config.json.
""" % __version__

SERVICE = "bulksms"

# What `init` writes: the settings people actually change. Everything else has a default.
STARTER = {
    "title": "Bulk SMS",
    "port": 8742,
    "dry_run": True,
    "lookup": {"url": "", "token": "", "label": "our database"},
    "country_code": "46",
    "default_delay": 20,
    "default_jitter": 5,
    "default_batch": 1,
    "window_start": "08:00",
    "window_end": "21:00",
    "sim_slot": None,
}


def addresses(port):
    """Best guess at the address to type on another device."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # nothing is sent; this only picks the route
        address = probe.getsockname()[0]
        # On mobile data alone there is no address the page would answer on.
        return [f"http://{address}:{port}/"] if is_local(address) else []
    except OSError:
        return []
    finally:
        probe.close()


def init():
    file = config.path()
    if os.path.exists(file):
        print("Already there:", file)
        return
    os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh:
        json.dump(STARTER, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("Created", file)
    print("Edit it, then: termux-bulk-sms set-password")


def set_password(cfg):
    first = getpass.getpass("New password: ")
    if len(first) < 8:
        sys.exit("Use at least 8 characters.")
    if getpass.getpass("Again: ") != first:
        sys.exit("The two did not match.")
    config.save_value("password_hash", config.hash_password(first))
    print("Saved to", cfg["_file"], "— restart the service for it to apply.")


def service(action, cfg):
    """A runit service under termux-services, so it can be switched on and off."""
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    directory = os.path.join(prefix, "var", "service", SERVICE)
    if shutil.which("sv") is None:
        sys.exit("termux-services is missing: pkg install termux-services, then restart Termux.")

    def sv(*args, check=True):
        done = subprocess.run(list(args))
        if check and done.returncode != 0:
            sys.exit(done.returncode)

    if action == "install":
        os.makedirs(os.path.join(directory, "log"), exist_ok=True)
        run = os.path.join(directory, "run")
        # The package's parent directory: the repository in a git checkout,
        # site-packages when installed — `-m` finds the package from either.
        home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(run, "w", encoding="utf-8") as fh:
            fh.write(f'#!{shutil.which("sh") or "/bin/sh"}\n'
                     "exec 2>&1\n"
                     f'export BULKSMS_CONFIG="{cfg["_file"]}"\n'
                     f'cd "{home}"\n'
                     f'exec "{sys.executable}" -u -m termux_bulk_sms run\n')
        os.chmod(run, 0o755)
        logger = os.path.join(directory, "log", "run")
        if os.path.lexists(logger):
            os.remove(logger)
        os.symlink(os.path.join(prefix, "share", "termux-services", "svlogger"), logger)
        # Installed is not the same as on: it stays down until `enable`.
        open(os.path.join(directory, "down"), "a").close()
        print("Installed, using", cfg["_file"])
        print("Next: termux-bulk-sms set-password, then termux-bulk-sms service enable")
        return
    if not os.path.isdir(directory):
        sys.exit("Not installed yet: termux-bulk-sms service install")
    if action == "enable":
        sv("sv-enable", SERVICE)
        sv("sv", "status", SERVICE, check=False)
        print("\n".join(addresses(cfg["port"])))
    elif action == "disable":
        sv("sv-disable", SERVICE)
        sv("sv", "down", SERVICE, check=False)
        sv("sv", "status", SERVICE, check=False)
    elif action in ("restart", "status"):
        sv("sv", action, SERVICE)
    elif action == "logs":
        log = os.path.join(prefix, "var", "log", "sv", SERVICE, "current")
        try:
            subprocess.run(["tail", "-n", "50", "-f", log])
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(USAGE)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    command = argv[1] if len(argv) > 1 else "help"
    if command in ("help", "-h", "--help"):
        print(USAGE)
        return
    if command in ("version", "--version"):
        print(__version__)
        return
    if command == "init":
        init()
        return
    cfg = config.load()
    if command == "run":
        from .server import serve
        for url in addresses(cfg["port"]):
            print("Open", url, flush=True)
        serve(cfg)
    elif command == "set-password":
        set_password(cfg)
    elif command == "addresses":
        print("\n".join(addresses(cfg["port"])) or "No network address found.")
    elif command == "service":
        service(argv[2] if len(argv) > 2 else "", cfg)
    else:
        sys.exit(USAGE)
