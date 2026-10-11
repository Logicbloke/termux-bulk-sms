# termux-bulk-sms

Send a personalised SMS to a list of people from your own Android phone, through
[Termux:API](https://wiki.termux.com/wiki/Termux:API), driven from a web page on your local network.

- Paste a CSV, write a message with `[name]`, set the delay, press Start, watch it go.
- Sendings are **campaigns**: every recipient can be edited, removed or put on the block list.
- Nobody is texted twice: duplicates in the paste, everyone ever texted (history) and a manual
  block list are all skipped.
- Optional **remote lookup**: before sending, ask your own backend which numbers it already
  knows (existing customers, members …) and skip those.
- Python standard library only. No pip, no npm, no build step, no CDN.

It is one process on the phone. The page is served only to the local network and needs a password.

## Install (in Termux)

```sh
pkg install python git termux-api termux-services   # plus the Termux:API app, with SMS permission
git clone <this repo> && cd termux-bulk-sms
cp config.example.json config.json                   # then edit it — see below
python -m bulksms set-password
python -m bulksms run                                # prints the address to open
```

Restart Termux once after installing `termux-services`.

### As a service you can switch on and off

```sh
./service.sh install     # once
./service.sh enable      # on: now and whenever Termux starts
./service.sh disable     # off: nothing is listening
./service.sh status | restart | logs
```

`config.json` is read at start, so `./service.sh restart` after editing it.

## config.json

| key | default | meaning |
| --- | --- | --- |
| `title` | `"Bulk SMS"` | Name shown on the page. |
| `port` | `8742` | Port the page is served on. |
| `dry_run` | `true` | **Nothing is sent while this is true.** A rehearsal: rows are marked sent, the history is left alone. |
| `lookup.url` | `""` | Remote lookup endpoint. Empty = no lookup. Must be `https://`. |
| `lookup.token` | `""` | Sent as `Authorization: Bearer …`. |
| `lookup.label` | `"our database"` | Shown as "Already in …" on skipped rows. |
| `country_code` | `"46"` | Country that numbers starting with `0` belong to. |
| `default_delay`, `default_jitter` | `20`, `5` | Seconds between messages, and a random ± on top. Per campaign on the page. |
| `window_start`, `window_end` | `08:00`, `21:00` | Default allowed hours (the phone's clock). Empty = any time. |
| `sim_slot` | `null` | SIM slot for dual-SIM phones (`0` or `1`). |
| `max_consecutive_failures` | `3` | A campaign pauses itself after this many failures in a row. |
| `host`, `data_dir` | `0.0.0.0`, `data` | Listen address; where the SQLite file lives. |

`password_hash` is written by `set-password`. `config.json` and `data/` are git-ignored.

## The remote lookup

Any backend can implement it:

```
POST <lookup.url>
Authorization: Bearer <lookup.token>
Content-Type: application/x-www-form-urlencoded

numbers=%2B46701234567%0A%2B46735556677        (one E.164 number per line)

200 {"results": ["known", "unknown"]}          (same order; "invalid" is the third value)
```

With a lookup configured, a row is sendable only after the lookup has answered `unknown` for the
number it has *now*. Numbers are asked again every time you press Start, and **if the lookup cannot
be reached the campaign does not start** — it never falls back to "probably fine".

## How it avoids sending twice

Each row is marked `sending` in SQLite *before* `termux-sms-send` is called and `sent` after. If
the app or the phone dies in between, the row comes back as **Uncertain** and the campaign as
**Paused**; nothing resumes by itself and you settle the row by hand ("It was sent" / "It was not").
The history is kept when a campaign is deleted.

"Sent" means Android accepted the message from Termux. Delivery reports are not available through
Termux:API.

## Recipients and placeholders

Comma, semicolon or tab separated; a header row is optional; the phone column is found by content.
Free-form lines (`Anna Andersson 070-123 45 67`) work too. `[name]` is the name column; with a
header row every other column is a placeholder as well (`[town]`). A row whose placeholder would be
empty is held back until you fix it. Placeholders that match nothing are flagged before you start.

## Security

- Only loopback and private-network clients (RFC 1918, link-local, ULA) are answered; a request
  arriving over mobile data gets 403.
- Password login (PBKDF2), throttled; the session cookie is `HttpOnly; SameSite=Strict`.
- The page is plain **HTTP**: anyone who can sniff your Wi-Fi can read it. Use it on a network you
  trust, and `./service.sh disable` when you are done.

## Good to know

- **Android's own SMS limit.** Android asks for confirmation when an app sends many messages in a
  short time (historically 30 per 30 minutes; it varies by version and vendor), and that dialog
  stalls an unattended run. Watch the first larger campaign. The limit can be raised over adb:
  `adb shell settings put global sms_outgoing_check_max_count 500`.
- **Battery.** A wake lock is held while a campaign runs. Also exempt Termux and Termux:API from
  battery optimisation, or Android may suspend them with the screen off.
- **The law.** In most countries unsolicited marketing texts to private persons need prior consent,
  and every message must say who it is from and how to decline. The page reminds you when the
  message has no "stop" wording; the block list is where a "stop" reply belongs. Your carrier's
  terms may also limit bulk sending from a consumer subscription.

## Tests

```sh
python -m unittest discover -s tests
```
