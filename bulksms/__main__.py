"""python -m bulksms run | set-password | addresses"""

import getpass
import socket
import sys

from . import config
from .server import is_local


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


def main(argv):
    command = argv[1] if len(argv) > 1 else "run"
    cfg = config.load()
    if command == "run":
        from .server import serve
        for url in addresses(cfg["port"]):
            print("Open", url, flush=True)
        serve(cfg)
    elif command == "set-password":
        first = getpass.getpass("New password: ")
        if len(first) < 8:
            sys.exit("Use at least 8 characters.")
        if getpass.getpass("Again: ") != first:
            sys.exit("The two did not match.")
        config.save_value("password_hash", config.hash_password(first))
        print("Saved to", cfg["_file"], "— restart the service for it to apply.")
    elif command == "addresses":
        print("\n".join(addresses(cfg["port"])) or "No network address found.")
    else:
        sys.exit("Usage: python -m bulksms [run | set-password | addresses]")


if __name__ == "__main__":
    main(sys.argv)
