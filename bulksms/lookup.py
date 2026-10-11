"""The optional remote check: which of these numbers does the site already know?

Contract (implement it on any backend):

    POST <url>
    Authorization: Bearer <token>
    Content-Type: application/x-www-form-urlencoded
    numbers=<one number per line>

    200 {"results": ["known" | "unknown" | "invalid", ...]}   same order as asked
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class LookupFailed(Exception):
    pass


def enabled(cfg):
    return bool(cfg["lookup"].get("url"))


def check(cfg, phones):
    """Return {phone: "known" | "unknown" | "invalid"}; raise LookupFailed otherwise."""
    lk = cfg["lookup"]
    url = lk["url"]
    host = urllib.parse.urlsplit(url).hostname or ""
    if not url.startswith("https://") and host not in ("127.0.0.1", "localhost"):
        raise LookupFailed("lookup.url must be https:// — the request carries phone numbers and a secret")
    phones = list(dict.fromkeys(phones))
    out = {}
    size = max(1, int(lk.get("chunk", 500)))
    for start in range(0, len(phones), size):
        chunk = phones[start:start + size]
        body = urllib.parse.urlencode({"numbers": "\n".join(chunk)}).encode()
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + lk.get("token", ""),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "termux-bulk-sms",
        })
        try:
            with urllib.request.urlopen(request, timeout=float(lk.get("timeout", 20))) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise LookupFailed(f"the lookup answered HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise LookupFailed(f"the lookup could not be reached ({getattr(error, 'reason', error)})") from None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(chunk) or any(
            r not in ("known", "unknown", "invalid") for r in results
        ):
            raise LookupFailed("the lookup gave an answer this app does not understand")
        out.update(zip(chunk, results))
    return out
