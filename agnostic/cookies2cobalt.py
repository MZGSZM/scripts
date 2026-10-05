#!/usr/bin/env python3
"""
cookies2cobalt.py: convert Netscape cookies.txt exports into the cookies.json
format read by a self-hosted cobalt API (github.com/imputnet/cobalt).

Built against cobalt api 11.7.1 (main @ a636575). The service list and the
string format come from api/src/processing/cookie/manager.js and cookie.js.
"""

import argparse
import json
import os
import sys
import time
from collections import OrderedDict

# Services cobalt's cookie manager accepts. Anything else gets ignored at load.
VALID_SERVICES = (
    "instagram",
    "instagram_bearer",
    "reddit",
    "twitter",
    "youtube",
    "vimeo_bearer",
)

# Services that can be filled from a browser cookie export.
# domains: registrable domains to match (subdomains included)
# keys:    cookies cobalt's docs/example list; None means take everything
# required / recommended: used for warnings only
BROWSER_SERVICES = OrderedDict([
    ("instagram", {
        "domains": ["instagram.com"],
        "keys": ["mid", "ig_did", "csrftoken", "ds_user_id", "sessionid"],
        "required": ["sessionid"],
        "recommended": ["csrftoken", "ds_user_id"],
    }),
    ("twitter", {
        # x.com first, since that's where the live session is these days
        "domains": ["x.com", "twitter.com"],
        "keys": ["auth_token", "ct0"],
        "required": ["auth_token"],
        "recommended": ["ct0"],
    }),
    ("youtube", {
        # youtubei.js gets the raw cookie header and builds SAPISIDHASH from it,
        # so the whole jar for the domain goes in
        "domains": ["youtube.com"],
        "keys": None,
        "required": [],
        "recommended": ["SAPISID", "__Secure-3PAPISID", "__Secure-3PSID"],
    }),
])

# Services that need API/OAuth tokens. These never live in a browser export.
TOKEN_SERVICES = {
    "reddit": "client_id=...; client_secret=...; refresh_token=...",
    "instagram_bearer": "token=IGT:2:...",
    "vimeo_bearer": "access_token=...",
}


class Cookie:
    __slots__ = ("domain", "path", "secure", "expires", "name", "value", "httponly")

    def __init__(self, domain, path, secure, expires, name, value, httponly):
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires
        self.name = name
        self.value = value
        self.httponly = httponly


def warn(msg):
    print(f"[!] {msg}", file=sys.stderr)


def info(msg):
    print(f"[-] {msg}", file=sys.stderr)


def parse_netscape(path):
    """Parse a Netscape/Mozilla cookies.txt. Returns (cookies, skipped_count)."""
    cookies = []
    skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue

            httponly = False
            # curl, yt-dlp and most browser extensions mark HttpOnly cookies
            # by prefixing the domain. These are the auth cookies, so treating
            # them as comments silently drops the login.
            if line.startswith("#HttpOnly_"):
                httponly = True
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue

            fields = line.split("\t")
            if len(fields) == 6:
                # some exporters drop the trailing tab on empty values
                fields.append("")
            if len(fields) < 7:
                warn(f"{path}:{lineno}: expected 7 tab-separated fields, got {len(fields)}, skipping")
                skipped += 1
                continue
            if len(fields) > 7:
                # a tab inside the value, rejoin it
                fields = fields[:6] + ["\t".join(fields[6:])]

            domain, _flag, cpath, secure, expires, name, value = fields
            try:
                expires = int(float(expires)) if expires.strip() else 0
            except ValueError:
                warn(f"{path}:{lineno}: bad expiry '{expires}', treating as session cookie")
                expires = 0

            cookies.append(Cookie(
                domain=domain.strip().lower(),
                path=cpath or "/",
                secure=secure.strip().upper() == "TRUE",
                expires=expires,
                name=name.strip(),
                value=value.strip(),
                httponly=httponly,
            ))
    return cookies, skipped


def domain_matches(cookie_domain, target):
    d = cookie_domain.lstrip(".")
    return d == target or d.endswith("." + target)


def pick_cookies(cookies, svc, now, include_expired):
    """Return OrderedDict name->value for one service, or None if nothing matched."""
    conf = BROWSER_SERVICES[svc]

    # walk domains in priority order and use the first one that has cookies,
    # so twitter.com leftovers don't get mixed into an x.com session
    for target in conf["domains"]:
        matched = [c for c in cookies if domain_matches(c.domain, target)]
        if not matched:
            continue

        chosen = {}
        expired_names = set()
        for c in matched:
            if not c.name:
                continue
            if c.expires and c.expires < now and not include_expired:
                expired_names.add(c.name)
                continue
            prev = chosen.get(c.name)
            if prev is None or rank(c) > rank(prev):
                chosen[c.name] = c

        # only report names that are expired and have no live replacement
        dead = sorted(expired_names - set(chosen))
        if dead:
            warn(f"{svc}: dropped expired cookies: {', '.join(dead)}")

        if conf["keys"] is not None and not ALL_COOKIES:
            names = [k for k in conf["keys"] if k in chosen]
        else:
            names = sorted(chosen)

        if not names:
            continue

        out = OrderedDict((n, chosen[n].value) for n in names)
        return target, out

    return None, None


def rank(c):
    # prefer cookies that apply site-wide, then root path, then later expiry.
    # session cookies (0) rank as newest since the browser had them live.
    site_wide = 1 if c.domain.startswith(".") else 0
    root = 1 if c.path == "/" else 0
    exp = c.expires if c.expires else 2 ** 62
    return (site_wide, root, exp)


def to_cobalt_string(values, svc):
    """Build the 'k=v; k=v' string. cobalt splits on '; ' exactly."""
    parts = []
    for k, v in values.items():
        if "; " in v:
            warn(f"{svc}: value of '{k}' contains '; ', cobalt will split it wrong. Skipping that cookie.")
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def check_service(svc, values):
    conf = BROWSER_SERVICES[svc]
    missing_req = [k for k in conf["required"] if k not in values]
    missing_rec = [k for k in conf["recommended"] if k not in values]
    if missing_req:
        warn(f"{svc}: missing {', '.join(missing_req)}. You are probably not logged in, entry skipped.")
        return False
    if missing_rec:
        warn(f"{svc}: missing {', '.join(missing_rec)}. It may still work, but expect failures.")
    return True


def load_existing(path):
    if not path or not os.path.exists(path):
        return OrderedDict()
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh, object_pairs_hook=OrderedDict)
        except json.JSONDecodeError as e:
            sys.exit(f"[x] {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        sys.exit(f"[x] {path} must be a JSON object")
    return data


def parse_add(spec):
    if "=" not in spec.split(":", 1)[0] and ":" in spec:
        svc, val = spec.split(":", 1)
    else:
        sys.exit(f"[x] --add expects SERVICE:'k=v; k=v', got: {spec}")
    svc = svc.strip()
    if svc not in VALID_SERVICES:
        sys.exit(f"[x] --add: unknown service '{svc}'. Valid: {', '.join(VALID_SERVICES)}")
    return svc, val.strip()


ALL_COOKIES = False


def main():
    global ALL_COOKIES

    ap = argparse.ArgumentParser(
        description="Convert Netscape cookies.txt files into cobalt's cookies.json.",
        epilog="Each input file is treated as one account. Several files give cobalt "
               "several entries per service, and it picks one at random per request.",
    )
    ap.add_argument("inputs", nargs="*", metavar="cookies.txt",
                    help="Netscape-format cookie files (browser export, yt-dlp --cookies output)")
    ap.add_argument("-o", "--output", default="cookies.json",
                    help="output path, or - for stdout (default: cookies.json)")
    ap.add_argument("-m", "--merge", action="store_true",
                    help="merge into the existing output file instead of overwriting it")
    ap.add_argument("--replace", action="store_true",
                    help="with --merge, replace a service's entries instead of appending")
    ap.add_argument("-s", "--services", default=",".join(BROWSER_SERVICES),
                    help=f"comma list to extract (default: {','.join(BROWSER_SERVICES)})")
    ap.add_argument("--all-cookies", action="store_true",
                    help="include every cookie for instagram/twitter, not just the ones cobalt documents")
    ap.add_argument("--include-expired", action="store_true",
                    help="keep cookies whose expiry has passed")
    ap.add_argument("--add", action="append", default=[], metavar="SERVICE:STRING",
                    help="add a token entry by hand, e.g. "
                         "--add 'reddit:client_id=x; client_secret=y; refresh_token=z'. Repeatable.")
    ap.add_argument("--list-services", action="store_true",
                    help="print what cobalt accepts and where each one comes from, then exit")
    args = ap.parse_args()

    if args.list_services:
        print("From cookies.txt:")
        for svc, conf in BROWSER_SERVICES.items():
            keys = ", ".join(conf["keys"]) if conf["keys"] else "all cookies for the domain"
            print(f"  {svc:<17} {'/'.join(conf['domains']):<22} {keys}")
        print("Tokens only (use --add):")
        for svc, fmt in TOKEN_SERVICES.items():
            print(f"  {svc:<17} {fmt}")
        return 0

    if not args.inputs and not args.add:
        ap.error("give at least one cookies.txt or --add")

    ALL_COOKIES = args.all_cookies
    wanted = [s.strip() for s in args.services.split(",") if s.strip()]
    for s in wanted:
        if s not in BROWSER_SERVICES:
            sys.exit(f"[x] '{s}' can't come from a cookies.txt. "
                     f"Choices: {', '.join(BROWSER_SERVICES)}. Tokens go through --add.")

    now = int(time.time())
    new_entries = OrderedDict()

    for path in args.inputs:
        if not os.path.isfile(path):
            sys.exit(f"[x] no such file: {path}")
        cookies, skipped = parse_netscape(path)
        info(f"{path}: {len(cookies)} cookies read" + (f", {skipped} bad lines" if skipped else ""))

        for svc in wanted:
            domain, values = pick_cookies(cookies, svc, now, args.include_expired)
            if values is None:
                continue
            if not check_service(svc, values):
                continue
            s = to_cobalt_string(values, svc)
            if s:
                new_entries.setdefault(svc, []).append(s)
                info(f"{path}: {svc} from {domain} ({len(values)} cookies)")

    for spec in args.add:
        svc, val = parse_add(spec)
        new_entries.setdefault(svc, []).append(val)
        info(f"--add: {svc}")

    if not new_entries:
        warn("nothing to write. No matching logged-in cookies found.")
        return 1

    out = load_existing(args.output) if (args.merge and args.output != "-") else OrderedDict()
    for svc, entries in new_entries.items():
        if args.replace or svc not in out or not isinstance(out[svc], list):
            out[svc] = []
        for e in entries:
            if e not in out[svc]:
                out[svc].append(e)

    for svc in out:
        if svc not in VALID_SERVICES:
            warn(f"'{svc}' in output is not a service cobalt accepts, it will be ignored at load"
                 + (" (cobalt's own example uses 'vimeo', the code wants 'vimeo_bearer')" if svc == "vimeo" else ""))

    text = json.dumps(out, indent=4) + "\n"

    if args.output == "-":
        sys.stdout.write(text)
        return 0

    # credentials, so 0600. cobalt's container runs as uid 1000 (node) and
    # writes refreshed values back to this file, so ownership matters too.
    tmp = args.output + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, args.output)
    info(f"wrote {args.output}: " + ", ".join(f"{k} x{len(v)}" for k, v in out.items()))
    if hasattr(os, "getuid") and os.getuid() != 1000:
        warn("you are not uid 1000. If this is for the docker image, run: "
             f"sudo chown 1000:1000 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
