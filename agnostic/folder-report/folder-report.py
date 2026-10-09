#!/usr/bin/env python3
"""
folder-report.py - Describe a folder's structure and metadata WITHOUT sharing its contents.

Produces:
  <suffix>.md    human-readable summary (tree, stats, flags)
  <suffix>.json  machine-readable version (paste/upload to an AI model)

Privacy by default: the report holds names, sizes, dates, extensions, counts and
derived classifications (file kind, encoding, entropy, image dimensions). To
classify a file the script reads at most its first 64 KB and last 4 KB; text files
are read in full to count lines. None of those bytes are written to the report.
Content only appears if you opt in (--csv-headers, --outline, --json-keys).
Use --anonymize to also hide file/folder names.

Usage:
  python folder-report.py /path/to/folder
  python folder-report.py . --anonymize --save-map
  python folder-report.py . --hash --max-depth 4 --format md
  python folder-report.py . --no-collapse --compact      # full tree, smaller JSON
  python folder-report.py . --no-sniff                   # skip kind/entropy/dimension checks
  python folder-report.py --help

Identical sibling folders (backup snapshots, per-run output dirs) are folded into one
representative in the tree, with a per-member size list kept in the JSON.

Python 3.8+, standard library only.
"""

import argparse
import ast
import csv
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import re
import struct
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA_VERSION = 2

DEFAULT_IGNORE = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".idea", ".vscode", ".DS_Store", "Thumbs.db", ".mypy_cache", ".pytest_cache",
    ".cache", ".next", ".tox",
}

# Well-known filenames: kept readable even in --anonymize mode (useful, not private).
KNOWN_NAMES = {
    "README.md", "README", "README.txt", "LICENSE", "package.json", "package-lock.json",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "CMakeLists.txt", "Makefile", "Dockerfile",
    "docker-compose.yml", "tsconfig.json", ".gitignore", "__init__.py", "main.py",
    "index.js", "index.html", "app.py", "manage.py", "conftest.py",
}

PROJECT_MARKERS = {
    "package.json": "Node.js / JavaScript",
    "requirements.txt": "Python", "pyproject.toml": "Python", "setup.py": "Python",
    "Pipfile": "Python", "Cargo.toml": "Rust", "go.mod": "Go",
    "pom.xml": "Java (Maven)", "build.gradle": "Java/Kotlin (Gradle)",
    "CMakeLists.txt": "C/C++ (CMake)", "Makefile": "Make-based build",
    "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose",
    "tsconfig.json": "TypeScript", "manage.py": "Django",
}

SENSITIVE_RE = re.compile(
    r"(^\.env|secret|credential|passw(or)?d|id_rsa|id_ed25519|id_ecdsa|\.pem$|\.key$|\.pfx$|\.p12$"
    r"|(^|[._-])tokens?([._-]|$)|\.kdbx$|\.ovpn$|\.gpg$|wallet|\.htpasswd$|^shadow$)",
    re.I,
)

# Extensions people expect to be readable text. Binary content behind one of these is flagged.
TEXT_EXTS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml",
    ".html", ".htm", ".css", ".js", ".ts", ".py", ".sh", ".bash", ".zsh", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".java",
    ".kt", ".rb", ".php", ".pl", ".lua", ".sql", ".bat", ".ps1", ".svg",
}

# Magic signatures: matched against the first bytes only.
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"), (b"GIF89a", "GIF image"),
    (b"%PDF", "PDF document"),
    (b"PK\x03\x04", "ZIP container"), (b"PK\x05\x06", "ZIP container"),
    (b"\x1f\x8b", "gzip archive"), (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\x00", "xz archive"), (b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (b"(\xb5/\xfd", "zstd archive"), (b"Rar!\x1a\x07", "RAR archive"),
    (b"\x7fELF", "ELF executable"),
    (b"SQLite format 3\x00", "SQLite database"),
    (b"OggS", "Ogg media"), (b"fLaC", "FLAC audio"), (b"ID3", "MP3 audio"),
    (b"\x1aE\xdf\xa3", "Matroska/WebM media"),
    (b"%!PS", "PostScript"),
    (b"\x00asm", "WebAssembly"),
    (b"-----BEGIN ", "PEM key/certificate"),
    (b"#!", "script (shebang)"),
]

# A kind that shows up behind an extension not in its set gets flagged as a mismatch.
KIND_EXTS = {
    "PNG image": {".png"},
    "JPEG image": {".jpg", ".jpeg", ".jpe", ".jfif"},
    "GIF image": {".gif"},
    "PDF document": {".pdf"},
    "ZIP container": {".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx", ".odt", ".ods",
                      ".odp", ".epub", ".whl", ".xpi", ".3mf", ".kmz", ".cbz", ".ipa",
                      ".nupkg", ".vsix", ".aar", ".war", ".appx", ".msix"},
    "gzip archive": {".gz", ".tgz"},
    "SQLite database": {".db", ".sqlite", ".sqlite3", ".db3"},
    "Windows executable": {".exe", ".dll", ".sys", ".scr", ".ocx", ".cpl", ".efi"},
}

SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

HEAD_BYTES = 64 * 1024          # most the script ever reads from a file to classify it
TAIL_BYTES = 4 * 1024
ENTROPY_BYTES = 16 * 1024
TEXT_LINECOUNT_LIMIT = 50 * 1024 * 1024   # don't count lines in files bigger than this
JSON_PARSE_LIMIT = 20 * 1024 * 1024
HASH_LIMIT = 200 * 1024 * 1024
HIGH_ENTROPY = 7.5

SIZE_BUCKETS = [
    (0, "empty"), (1024, "< 1 KB"), (64 * 1024, "1-64 KB"), (1024 ** 2, "64 KB-1 MB"),
    (16 * 1024 ** 2, "1-16 MB"), (256 * 1024 ** 2, "16-256 MB"), (None, ">= 256 MB"),
]

TS_PATTERNS = [
    re.compile(r"^(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?:[-_T ]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})?)?$"),
    re.compile(r"^(?P<Y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})"
               r"(?:[-_T ](?P<H>\d{2})[-_.:]?(?P<M>\d{2})(?:[-_.:]?(?P<S>\d{2}))?)?$"),
]
NUMERIC_STEM_RE = re.compile(r"^[\d._-]+$")


# ---------------------------------------------------------------- helpers
def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def iso(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def parse_ts(name):
    """Return a datetime if a folder/file name is a timestamp like 20250520-213146."""
    for rx in TS_PATTERNS:
        m = rx.match(name)
        if not m:
            continue
        g = m.groupdict()
        try:
            t = dt.datetime(int(g["Y"]), int(g["m"]), int(g["d"]),
                            int(g["H"] or 0), int(g["M"] or 0), int(g["S"] or 0))
        except ValueError:
            return None
        return t if 1980 <= t.year <= 2100 else None
    return None


def ts_pattern(path):
    """Replace timestamp segments so paths inside different snapshots group together."""
    return "/".join("<snapshot>" if parse_ts(seg) else seg for seg in path.split("/"))


def size_bucket(n):
    if n == 0:
        return SIZE_BUCKETS[0][1]
    for limit, label in SIZE_BUCKETS[1:]:
        if limit is None or n < limit:
            return label
    return SIZE_BUCKETS[-1][1]


def entropy(sample):
    if not sample:
        return 0.0
    n = len(sample)
    return -sum((c / n) * math.log2(c / n) for c in Counter(sample).values())


def utf8_ok(b, partial):
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError as ex:
        # a multibyte char may be cut at the read boundary
        return partial and ex.start >= len(b) - 3


CONTROL_BYTES = bytes(b for b in range(32) if b not in (9, 10, 12, 13, 27)) + b"\x7f"


def control_ratio(b):
    return sum(b.count(bytes([c])) for c in CONTROL_BYTES) / len(b) if b else 0.0


def text_encoding(head, tail, partial):
    """Best guess at the text encoding, or None if the bytes don't look like text."""
    if not head:
        return None
    # Tiny files decode as "text" by accident far too often; demand plain printable ASCII.
    if len(head) < 16 and not head.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        ok = all(32 <= c < 127 or c in (9, 10, 13) for c in head)
        return "ascii" if ok else None
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-bom" if utf8_ok(head[3:], partial) else None
    if head.startswith(b"\xff\xfe"):
        return "utf-16le-bom"
    if head.startswith(b"\xfe\xff"):
        return "utf-16be-bom"
    if b"\0" in head:
        if len(head) >= 64:
            ev, od = head[0::2], head[1::2]
            if od.count(0) > 0.9 * len(od) and ev.count(0) < 0.1 * len(ev):
                return "utf-16le"
            if ev.count(0) > 0.9 * len(ev) and od.count(0) < 0.1 * len(od):
                return "utf-16be"
        stripped = head.rstrip(b"\0")
        if (stripped and b"\0" not in stripped and not tail.strip(b"\0")
                and utf8_ok(stripped, False) and control_ratio(stripped) < 0.01):
            return "utf-8 (zero-padded)"
        return None
    if control_ratio(head) >= 0.01:
        return None
    if utf8_ok(head, partial):
        return "ascii" if max(head) < 128 else "utf-8"
    return "8-bit (not utf-8)" if len(head) >= 256 else None


def magic_kind(h, ext):
    for sig, label in MAGIC:
        if h.startswith(sig):
            return label
    if len(h) >= 12 and h[4:8] == b"ftyp":
        return "MP4/MOV/HEIF media"
    if len(h) >= 12 and h[:4] == b"RIFF":
        return {b"WAVE": "WAV audio", b"AVI ": "AVI video", b"WEBP": "WebP image"}.get(h[8:12], "RIFF container")
    if len(h) >= 262 and h[257:262] == b"ustar":
        return "tar archive"
    if h[:2] == b"MZ" and len(h) >= 0x40:
        off = struct.unpack_from("<I", h, 0x3C)[0]
        if off + 4 <= len(h) and h[off:off + 4] == b"PE\0\0":
            return "Windows executable"
    if ext == ".bmp" and h[:2] == b"BM":
        return "BMP image"
    return None


def image_dims(h, kind):
    """Width/height from the image header only. EXIF and pixel data are never parsed."""
    try:
        if kind == "PNG image" and h[12:16] == b"IHDR":
            return struct.unpack(">II", h[16:24])
        if kind == "GIF image":
            return struct.unpack("<HH", h[6:10])
        if kind == "BMP image":
            w, hh = struct.unpack("<ii", h[18:26])
            return w, abs(hh)
        if kind == "JPEG image":
            i = 2
            while i + 9 < len(h):
                if h[i] != 0xFF:
                    i += 1
                    continue
                marker = h[i + 1]
                if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                    i += 1 if marker == 0xFF else 2
                    continue
                seglen = struct.unpack(">H", h[i + 2:i + 4])[0]
                if marker in SOF_MARKERS:
                    hh, w = struct.unpack(">HH", h[i + 5:i + 9])
                    return w, hh
                i += 2 + seglen
    except struct.error:
        pass
    return None


def text_stats(path, utf16):
    """Line count, line-ending style and longest line (bytes) in one pass."""
    lf = crlf = cr = 0
    maxlen = cur = 0
    last = b""
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            lf += block.count(b"\n")
            if not utf16:
                crlf += block.count(b"\r\n")
                cr += block.count(b"\r")
                parts = block.split(b"\n")
                cur += len(parts[0])
                if len(parts) > 1:
                    maxlen = max(maxlen, cur, *(len(p) for p in parts[1:-1]))
                    cur = len(parts[-1])
            last = block
    lines = lf + (1 if last and not last.endswith(b"\n") else 0)
    out = {"lines": lines}
    if not utf16:
        bare_cr = cr - crlf
        if lf == 0 and bare_cr == 0:
            eol = "none"
        elif crlf == lf and bare_cr == 0:
            eol = "CRLF"
        elif crlf == 0 and bare_cr == 0:
            eol = "LF"
        elif lf == 0:
            eol = "CR"
        else:
            eol = "mixed"
        out["eol"] = eol
        out["max_line_bytes"] = max(maxlen, cur)
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def csv_info(path, want_headers):
    """Return column count + row count (and optionally header names)."""
    info = {}
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(f, dialect)
            header = next(reader, None)
            if header is None:
                return info
            info["columns"] = len(header)
            if want_headers:
                info["headers"] = header
            info["rows"] = sum(1 for _ in reader)
    except (OSError, csv.Error):
        pass
    return info


def json_info(path, want_keys):
    """Shape of a JSON document: top-level type, key/item count, nesting depth. Values never leave."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="strict") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return {"json": "invalid"}
    info = {"json": type(data).__name__.replace("dict", "object").replace("list", "array")}
    if isinstance(data, dict):
        info["json_keys_count"] = len(data)
        if want_keys:
            info["json_keys"] = list(data.keys())[:100]
    elif isinstance(data, list):
        info["json_items"] = len(data)
    depth, stack = 0, [(data, 1)]
    while stack:
        node, d = stack.pop()
        depth = max(depth, d)
        if d >= 64:
            continue
        kids = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        stack.extend((k, d + 1) for k in kids if isinstance(k, (dict, list)))
    info["json_depth"] = depth
    return info


def zip_info(path):
    """Entry count and sizes from the ZIP central directory. Member names are not reported."""
    try:
        with zipfile.ZipFile(path) as z:
            items = z.infolist()
            return {
                "archive_entries": len(items),
                "archive_uncompressed": sum(i.file_size for i in items),
                "archive_encrypted": any(i.flag_bits & 0x1 for i in items),
            }
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return {"archive": "unreadable"}


def sqlite_info(head):
    try:
        page = struct.unpack(">H", head[16:18])[0]
        page = 65536 if page == 1 else page
        return {"sqlite_page_size": page, "sqlite_pages": struct.unpack(">I", head[28:32])[0]}
    except struct.error:
        return {}


def python_outline(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError, ValueError):
        return None
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            out.append(f"class {node.name}" + (f" [{', '.join(methods)}]" if methods else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            out.append(f"def {node.name}({args})")
    return out or None


class Namer:
    """Optionally replaces names with placeholders (extensions are preserved).

    Placeholders are keyed by the real name, so the same name always maps to the
    same placeholder. That keeps repeated layouts visible (and collapsible) at the
    cost of revealing that two items share a name. Names that are purely numeric or
    timestamps (000, 20250520-213146, 01.dat) are kept; mtimes reveal that anyway.
    """

    def __init__(self, enabled):
        self.enabled = enabled
        self.map = {}          # (is_dir, real name) -> placeholder
        self.dir_n = 0
        self.file_n = 0

    def name(self, real_name, is_dir):
        if not self.enabled or real_name in KNOWN_NAMES:
            return real_name
        stem = real_name if is_dir else os.path.splitext(real_name)[0]
        if NUMERIC_STEM_RE.match(stem):
            return real_name
        key = (is_dir, real_name)
        if key in self.map:
            return self.map[key]
        if is_dir:
            self.dir_n += 1
            new = f"dir_{self.dir_n:03d}"
        else:
            self.file_n += 1
            new = f"file_{self.file_n:04d}{os.path.splitext(real_name)[1]}"
        self.map[key] = new
        return new


# ---------------------------------------------------------------- scanning
class Scanner:
    def __init__(self, args):
        self.args = args
        self.namer = Namer(args.anonymize)
        self.ignore = set() if args.no_default_ignore else set(DEFAULT_IGNORE)
        self.ignore |= set(args.ignore or [])
        self.files = []              # flat records for stats
        self.empty_dirs = []
        self.markers = defaultdict(list)
        self.sensitive = []
        self.mismatches = []
        self.series = []
        self.skipped = Counter()
        self.max_depth_seen = 0
        self.dir_count = 0

    def skip(self, name):
        if any(fnmatch.fnmatch(name, pat) for pat in self.ignore):
            self.skipped["ignored by name"] += 1
            return True
        if name.startswith(".") and not self.args.include_hidden and name not in KNOWN_NAMES:
            self.skipped["hidden"] += 1
            return True
        return False

    def progress(self):
        if not self.args.quiet and len(self.files) % 1000 == 0:
            print(f"\r  {len(self.files):,} files ...", end="", file=sys.stderr, flush=True)

    def scan_dir(self, real_path, disp, depth):
        self.max_depth_seen = max(self.max_depth_seen, depth)
        node = {"name": disp.split("/")[-1] if disp else "ROOT", "type": "dir", "children": []}
        try:
            entries = list(os.scandir(real_path))
        except OSError:
            node["error"] = "unreadable"
            self.skipped["unreadable dirs"] += 1
            self.finish_dir(node, disp)
            return node

        def is_dir(e):
            try:
                return e.is_dir(follow_symlinks=False)
            except OSError:
                return False

        entries.sort(key=lambda e: (not is_dir(e), e.name.lower()))
        for e in entries:
            if self.skip(e.name):
                continue
            d = is_dir(e)
            e_disp_name = self.namer.name(e.name, d)
            e_disp = f"{disp}/{e_disp_name}" if disp else e_disp_name

            if e.is_symlink():
                node["children"].append({"name": e_disp_name, "type": "symlink"})
                self.skipped["symlinks (not followed)"] += 1
                continue

            if d:
                if self.args.max_depth is not None and depth >= self.args.max_depth:
                    node["children"].append({"name": e_disp_name, "type": "dir", "truncated": True,
                                             "children": [], "total_size": 0, "file_count": 0})
                    self.skipped["dirs beyond --max-depth"] += 1
                    continue
                self.dir_count += 1
                child = self.scan_dir(e.path, e_disp, depth + 1)
                if not child["children"] and "error" not in child:
                    self.empty_dirs.append(e_disp)
                node["children"].append(child)
            else:
                try:
                    regular = e.is_file(follow_symlinks=False)
                except OSError:
                    regular = False
                if regular:
                    node["children"].append(self.scan_file(e, e_disp, e_disp_name))
                else:
                    self.skipped["special files (fifo/socket/device)"] += 1

        self.finish_dir(node, disp)
        return node

    def finish_dir(self, node, disp):
        """Aggregate sizes, compute a layout fingerprint and detect timestamped snapshot series."""
        kids = node["children"]
        node["total_size"] = sum(c.get("total_size", c.get("size", 0)) for c in kids)
        node["file_count"] = sum(c.get("file_count", 1 if c["type"] == "file" else 0) for c in kids)
        newest = max((c.get("_mtime", 0) for c in kids), default=0)
        node["_mtime"] = newest
        if newest:
            node["newest"] = iso(newest)

        parts = []
        for c in kids:
            nm = "<ts>" if parse_ts(c["name"]) else c["name"]
            parts.append((c["type"], nm, c.get("_shape", "")))
        node["_shape"] = hashlib.md5(repr(sorted(parts)).encode()).hexdigest()

        stamped = sorted(((parse_ts(c["name"]), c) for c in kids if c["type"] == "dir"
                          and parse_ts(c["name"])), key=lambda x: x[0])
        if len(stamped) >= 3:
            times = [t for t, _ in stamped]
            gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:])]
            run_gaps = sorted(g for g in gaps if g > 1 / 24)
            runs = 1 + len(run_gaps)
            self.series.append({
                "path": disp or node["name"],
                "count": len(stamped),
                "runs": runs,
                "first": times[0].strftime("%Y-%m-%d %H:%M"),
                "last": times[-1].strftime("%Y-%m-%d %H:%M"),
                "median_gap_days": round(run_gaps[len(run_gaps) // 2], 1) if run_gaps else 0.0,
                "first_size": stamped[0][1]["total_size"],
                "last_size": stamped[-1][1]["total_size"],
                "_times": times,
            })

    def scan_file(self, e, disp, disp_name):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            self.skipped["unreadable files"] += 1
            return {"name": disp_name, "type": "file", "error": "unreadable", "size": 0}

        size = st.st_size
        ext = os.path.splitext(e.name)[1].lower() or "(none)"
        rec = {"name": disp_name, "type": "file", "ext": ext, "size": size,
               "modified": iso(st.st_mtime), "_mtime": st.st_mtime}
        if st.st_mode & 0o111 and os.name != "nt":
            rec["exec"] = True
        if st.st_nlink > 1:
            rec["hardlinks"] = st.st_nlink

        head = tail = b""
        if size:
            try:
                with open(e.path, "rb") as fh:
                    head = fh.read(HEAD_BYTES)
                    if size > len(head):
                        fh.seek(max(size - TAIL_BYTES, len(head)))
                        tail = fh.read(TAIL_BYTES)
                    else:
                        tail = head[-TAIL_BYTES:]
            except OSError:
                self.skipped["unreadable files"] += 1
                rec["error"] = "unreadable"

        if not size:
            rec.update(kind="empty", text=True)
        elif head:
            self.classify(rec, e.path, head, tail, size, ext)

        if e.name in PROJECT_MARKERS:
            self.markers[PROJECT_MARKERS[e.name]].append(disp)
        if SENSITIVE_RE.search(e.name):
            self.sensitive.append(disp)
        if not self.args.no_sniff:
            kind = rec.get("kind")
            if kind in KIND_EXTS and ext not in KIND_EXTS[kind] and ext != "(none)":
                self.mismatches.append({"path": disp, "ext": ext, "kind": kind})
            elif ext in TEXT_EXTS and size and not rec.get("text", True):
                self.mismatches.append({"path": disp, "ext": ext, "kind": kind or "binary"})

        flat = dict(rec)
        flat["path"] = disp
        flat["_real"] = e.path
        self.files.append(flat)
        self.progress()
        return rec

    def classify(self, rec, path, head, tail, size, ext):
        partial = size > len(head)
        enc = text_encoding(head, tail, partial)
        rec["text"] = enc is not None
        if self.args.no_sniff:
            if enc and size <= TEXT_LINECOUNT_LIMIT:
                self.add_text_stats(rec, path, enc)
            if ext == ".csv" and enc:
                rec.update(csv_info(path, self.args.csv_headers))
            return

        kind = magic_kind(head, ext)
        ent = entropy(head[:ENTROPY_BYTES])
        rec["entropy"] = round(ent, 2)
        if not kind:
            if not head.strip(b"\0") and not tail.strip(b"\0"):
                kind = "zero-filled"
            elif enc:
                kind = "text"
            elif ent >= HIGH_ENTROPY:
                kind = "high-entropy binary"
            else:
                kind = "binary"
                zr = head.count(0) / len(head)
                if zr >= 0.5:
                    rec["zero_ratio"] = round(zr, 2)
        rec["kind"] = kind
        if enc:
            rec["encoding"] = enc
            if size <= TEXT_LINECOUNT_LIMIT:
                self.add_text_stats(rec, path, enc)

        dims = image_dims(head, kind)
        if dims:
            rec["dimensions"] = f"{dims[0]}x{dims[1]}"
        if kind == "ZIP container":
            rec.update(zip_info(path))
        elif kind == "SQLite database":
            rec.update(sqlite_info(head))
        if ext == ".csv" and enc:
            rec.update(csv_info(path, self.args.csv_headers))
        if ext == ".json" and enc and size <= JSON_PARSE_LIMIT:
            rec.update(json_info(path, self.args.json_keys))
        if self.args.outline and ext == ".py" and enc:
            outline = python_outline(path)
            if outline:
                rec["outline"] = outline

    @staticmethod
    def add_text_stats(rec, path, enc):
        try:
            rec.update(text_stats(path, enc.startswith("utf-16")))
        except OSError:
            pass


# ---------------------------------------------------------------- post-processing
def collapse(node, min_count):
    """Fold sibling folders with an identical layout into one representative (the last one)."""
    kids = node.get("children", [])
    for c in kids:
        if c["type"] == "dir":
            collapse(c, min_count)
    groups = defaultdict(list)
    for i, c in enumerate(kids):
        if c["type"] == "dir" and not c.get("truncated") and "error" not in c:
            groups[c["_shape"]].append(i)
    drop = set()
    for idxs in groups.values():
        if len(idxs) < min_count:
            continue
        members = [kids[i] for i in idxs]
        rep = members[-1]
        rep["series"] = {
            "count": len(members),
            "members": [{"name": m["name"], "total_size": m["total_size"],
                         "file_count": m["file_count"], "newest": m.get("newest")} for m in members],
        }
        drop.update(idxs[:-1])
    if drop:
        node["children"] = [c for i, c in enumerate(kids) if i not in drop]


def strip_private(obj):
    """Remove internal keys (underscore-prefixed) before writing output."""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_private(v) for v in obj]
    return obj


def find_duplicates(files, do_hash, quiet):
    """Same name + same size groups (cheap hint), plus verified groups when --hash is on."""
    by_name = defaultdict(list)
    for f in files:
        if f["size"] > 0 and "error" not in f:
            by_name[(f["name"], f["size"])].append(f)
    likely = [{"name": k[0], "size": k[1], "copies": len(v),
               "wasted_bytes": k[1] * (len(v) - 1), "example": v[-1]["path"]}
              for k, v in by_name.items() if len(v) > 1]
    likely.sort(key=lambda g: -g["wasted_bytes"])

    verified = []
    if do_hash:
        by_size = defaultdict(list)
        for f in files:
            if 0 < f["size"] <= HASH_LIMIT and "error" not in f:
                by_size[f["size"]].append(f)
        candidates = [f for group in by_size.values() if len(group) > 1 for f in group]
        if not quiet:
            print(f"\n  hashing {len(candidates):,} same-size candidates ...", file=sys.stderr)
        by_hash = defaultdict(list)
        for f in candidates:
            try:
                f["sha256"] = sha256(f["_real"])
                by_hash[f["sha256"]].append(f)
            except OSError:
                pass
        verified = [{"size": g[0]["size"], "copies": len(g), "wasted_bytes": g[0]["size"] * (len(g) - 1),
                     "files": [x["path"] for x in g]} for g in by_hash.values() if len(g) > 1]
        verified.sort(key=lambda g: -g["wasted_bytes"])
    return likely, verified


def opt_in_details(files):
    """Every file with opted-in extras. Copies with the same name and identical extras are listed once."""
    groups = {}
    for f in files:
        extra = {k: f[k] for k in ("headers", "json_keys", "outline") if f.get(k)}
        if not extra:
            continue
        key = (f["name"], json.dumps(extra, sort_keys=True))
        if key in groups:
            groups[key]["copies"] += 1
            groups[key]["path"] = f["path"]
        else:
            groups[key] = dict(path=f["path"], copies=1, **extra)
            if "json_keys_count" in f:
                groups[key]["json_keys_count"] = f["json_keys_count"]
    return sorted(groups.values(), key=lambda g: g["path"].lower())


def backup_runs(series):
    """Cluster every snapshot timestamp in the tree into runs (gaps over an hour start a new run)."""
    times = sorted(t for s in series for t in s["_times"])
    runs = []
    for t in times:
        if runs and (t - runs[-1]["_end"]).total_seconds() <= 3600:
            runs[-1]["_end"] = t
            runs[-1]["folders"] += 1
        else:
            runs.append({"start": t.strftime("%Y-%m-%d %H:%M"), "_end": t, "folders": 1})
    for r in runs:
        r["end"] = r.pop("_end").strftime("%Y-%m-%d %H:%M")
    return runs


# ---------------------------------------------------------------- summary
def build_summary(sc, tree, args):
    files = sc.files
    total_size = sum(f["size"] for f in files)

    by_ext = defaultdict(lambda: {"count": 0, "bytes": 0, "lines": 0})
    for f in files:
        d = by_ext[f["ext"]]
        d["count"] += 1
        d["bytes"] += f["size"]
        d["lines"] += f.get("lines", 0)

    by_kind = defaultdict(lambda: {"count": 0, "bytes": 0})
    for f in files:
        d = by_kind[f.get("kind", "unclassified")]
        d["count"] += 1
        d["bytes"] += f["size"]

    buckets = Counter(size_bucket(f["size"]) for f in files)
    size_dist = {label: buckets.get(label, 0) for _, label in SIZE_BUCKETS}

    by_month = Counter(f["modified"][:7] for f in files if "modified" in f)

    top_level = [{"name": c["name"] + ("/" if c["type"] == "dir" else ""),
                  "size": c.get("total_size", c.get("size", 0)),
                  "files": c.get("file_count", 1 if c["type"] == "file" else 0)}
                 for c in tree["children"]]
    top_level.sort(key=lambda x: -x["size"])

    groups = defaultdict(list)
    for f in files:
        groups[(f["name"], f["size"])].append(f)
    largest = sorted(groups.values(), key=lambda g: -g[0]["size"])[:10]

    likely, verified = find_duplicates(files, args.hash, args.quiet)

    ordered = sorted(files, key=lambda f: f.get("_mtime", 0))
    series = sorted(sc.series, key=lambda s: s["path"].lower())

    return {
        "total_files": len(files),
        "total_dirs": sc.dir_count,
        "total_size_bytes": total_size,
        "total_text_lines": sum(f.get("lines", 0) for f in files),
        "text_files": sum(1 for f in files if f.get("text")),
        "binary_files": sum(1 for f in files if not f.get("text", True)),
        "max_depth": sc.max_depth_seen,
        "top_level": top_level,
        "by_extension": dict(sorted(by_ext.items(), key=lambda kv: -kv[1]["bytes"])),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["bytes"])),
        "text_encodings": dict(Counter(f["encoding"] for f in files if "encoding" in f).most_common()),
        "line_endings": dict(Counter(f["eol"] for f in files if "eol" in f).most_common()),
        "size_distribution": size_dist,
        "modified_by_month": dict(sorted(by_month.items())),
        "largest_files": [{"name": g[0]["name"], "size": g[0]["size"], "copies": len(g),
                           "example": g[-1]["path"]} for g in largest],
        "newest_files": [{"path": f["path"], "modified": f["modified"]} for f in ordered[::-1][:5]],
        "oldest_files": [{"path": f["path"], "modified": f["modified"]} for f in ordered[:5]],
        "snapshot_series": series,
        "backup_runs": backup_runs(series),
        "likely_duplicates": {
            "basis": "same file name and size (not content-verified)",
            "wasted_bytes": sum(g["wasted_bytes"] for g in likely),
            "groups": len(likely),
            "top": likely[:15],
        },
        "verified_duplicates": {
            "wasted_bytes": sum(g["wasted_bytes"] for g in verified),
            "groups": len(verified),
            "top": verified[:15],
        } if args.hash else None,
        "empty_files": [f["path"] for f in files if f["size"] == 0][:50],
        "empty_files_count": sum(1 for f in files if f["size"] == 0),
        "empty_dirs": sc.empty_dirs[:50],
        "empty_dirs_by_parent": dict(Counter(ts_pattern(p.rsplit("/", 1)[0]) if "/" in p else "(root)"
                                             for p in sc.empty_dirs).most_common(25)),
        "empty_dirs_count": len(sc.empty_dirs),
        "executable_files": sum(1 for f in files if f.get("exec")),
        "high_entropy_files": sum(1 for f in files if f.get("entropy", 0) >= HIGH_ENTROPY),
        "extension_mismatches": sc.mismatches[:100],
        "extension_mismatches_count": len(sc.mismatches),
        "project_markers": dict(sc.markers),
        "possibly_sensitive_filenames": sc.sensitive,
        "opt_in_details": opt_in_details(files),
        "skipped": dict(sc.skipped),
    }


# ---------------------------------------------------------------- markdown
EXTRA_LIST_LIMIT = 40
EXTRA_NAME_LIMIT = 60


def short_list(items):
    """Join opted-in names for display, trimming very long ones and very long lists."""
    shown = [x if len(x) <= EXTRA_NAME_LIMIT else x[:EXTRA_NAME_LIMIT - 3] + "..." for x in map(str, items)]
    more = len(shown) - EXTRA_LIST_LIMIT
    return ", ".join(shown[:EXTRA_LIST_LIMIT]) + (f" (+{more} more)" if more > 0 else "")


def extra_lines(n):
    """Opted-in details for one file: CSV headers, JSON keys, Python outline."""
    out = []
    if n.get("headers"):
        out.append(f"columns: {short_list(n['headers'])}")
    if n.get("json_keys"):
        more = n.get("json_keys_count", 0) - len(n["json_keys"])
        out.append(f"keys: {short_list(n['json_keys'])}" + (f" (+{more} more)" if more > 0 else ""))
    out += n.get("outline", [])[:25]
    if len(n.get("outline", [])) > 25:
        out.append(f"... +{len(n['outline']) - 25} more definitions")
    return out


def file_line(n):
    bits = [human_size(n["size"])]
    if "rows" in n:
        bits.append(f"{n['rows']} rows x {n['columns']} cols")
    elif "lines" in n:
        bits.append(plural(n["lines"], "line"))
    kind = n.get("kind")
    if kind and kind not in ("text", "empty"):
        bits.append(kind)
    elif not n.get("text", True):
        bits.append("binary")
    if n.get("encoding") not in (None, "ascii", "utf-8"):
        bits.append(n["encoding"])
    if n.get("eol") in ("CRLF", "CR", "mixed"):
        bits.append(n["eol"])
    if "dimensions" in n:
        bits.append(n["dimensions"])
    if "archive_entries" in n:
        bits.append(plural(n["archive_entries"], "entry").replace("entrys", "entries"))
    if "json_keys_count" in n:
        bits.append(f"JSON object, {n['json_keys_count']} keys")
    elif "json_items" in n:
        bits.append(f"JSON array, {n['json_items']} items")
    if n.get("exec"):
        bits.append("exec")
    bits.append(n["modified"])
    return f"{n['name']}  ({', '.join(bits)})"


def dir_line(d):
    if d.get("truncated"):
        return f"{d['name']}/  [depth limit]"
    if d.get("error"):
        return f"{d['name']}/  [unreadable]"
    if not d["file_count"]:
        if any(c.get("truncated") for c in d.get("children", [])):
            return f"{d['name']}/  (contents beyond depth limit)"
        return f"{d['name']}/  (empty)"
    return f"{d['name']}/  ({human_size(d['total_size'])}, {plural(d['file_count'], 'file')})"


def series_note(s):
    m = s["members"]
    sizes = [x["total_size"] for x in m]
    size_txt = (f"all {human_size(sizes[0])}" if min(sizes) == max(sizes)
                else f"{human_size(min(sizes))} to {human_size(max(sizes))}")
    return (f"[x{s['count']} with identical layout: {m[0]['name']} .. {m[-1]['name']}; "
            f"sizes {size_txt}; showing the last]")


def render_tree(node, prefix, file_limit, dir_limit, out):
    children = node.get("children", [])
    dirs = [c for c in children if c["type"] == "dir"]
    others = [c for c in children if c["type"] != "dir"]
    items = [(d, "dir") for d in dirs[:dir_limit]] + [(f, "file") for f in others[:file_limit]]
    extras = []
    if len(dirs) > dir_limit:
        extras.append(f"... +{len(dirs) - dir_limit} more folders")
    if len(others) > file_limit:
        hidden = others[file_limit:]
        c = Counter(h.get("ext", "?") for h in hidden)
        extras.append(f"... +{len(hidden)} more files ({', '.join(f'{e} x{n}' for e, n in c.most_common(5))})")

    total = len(items) + len(extras)
    for i, (c, kind) in enumerate(items):
        last = (i == total - 1)
        branch = "└── " if last else "├── "
        cont = "    " if last else "│   "
        if kind == "dir":
            out.append(f"{prefix}{branch}{dir_line(c)}")
            if "series" in c:
                out.append(f"{prefix}{cont}  {series_note(c['series'])}")
            render_tree(c, prefix + cont, file_limit, dir_limit, out)
        elif c["type"] == "symlink":
            out.append(f"{prefix}{branch}{c['name']}  (symlink)")
        elif c.get("error"):
            out.append(f"{prefix}{branch}{c['name']}  (unreadable)")
        else:
            out.append(f"{prefix}{branch}{file_line(c)}")
            for o in extra_lines(c):
                out.append(f"{prefix}{cont}    · {o}")
    for j, x in enumerate(extras):
        out.append(f"{prefix}{'└── ' if j == len(extras) - 1 else '├── '}{x}")


def plural(n, word):
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "-"


def to_markdown(meta, s, tree, args):
    L = []
    total = s["total_size_bytes"]
    L.append(f"# Folder Report: {meta['root_label']}")
    L.append("")
    note = "Names anonymized. " if meta["anonymized"] else ""
    extra = f" (except opted-in extras: {', '.join(meta['opt_ins'])})" if meta["opt_ins"] else ""
    L.append(f"_Generated {meta['generated']} by folder-report.py v{SCHEMA_VERSION}. {note}"
             f"Structure and metadata only, no file contents{extra}._")
    L.append("")

    L.append("## Overview")
    L.append(f"- **Files:** {s['total_files']:,} ({s['text_files']:,} text, {s['binary_files']:,} binary)")
    L.append(f"- **Folders:** {s['total_dirs']:,} (max depth {s['max_depth']})")
    L.append(f"- **Total size:** {human_size(total)}")
    L.append(f"- **Total text lines:** {s['total_text_lines']:,}")
    ld = s["likely_duplicates"]
    if ld["groups"]:
        L.append(f"- **Repeated copies (same name and size):** {ld['groups']:,} groups, "
                 f"{human_size(ld['wasted_bytes'])} beyond the first copy ({pct(ld['wasted_bytes'], total)} of total)")
    vd = s.get("verified_duplicates")
    if vd:
        L.append(f"- **Verified identical content:** {vd['groups']:,} groups, {human_size(vd['wasted_bytes'])} reclaimable")
    if s["snapshot_series"]:
        L.append(f"- **Timestamped snapshot folders:** {len(s['snapshot_series'])} series across "
                 f"{len(s['backup_runs'])} runs")
    if s["executable_files"]:
        L.append(f"- **Executable files:** {s['executable_files']:,}")
    if s["project_markers"]:
        L.append("- **Project type hints:** " + "; ".join(
            f"{k} ({', '.join(v[:2])}{'...' if len(v) > 2 else ''})" for k, v in s["project_markers"].items()))
    L.append("")

    L.append("## Where the space is")
    L.append("| Top-level item | Size | Share | Files |")
    L.append("|---|---:|---:|---:|")
    for t in s["top_level"][:20]:
        L.append(f"| `{t['name']}` | {human_size(t['size'])} | {pct(t['size'], total)} | {t['files']:,} |")
    L.append("")

    L.append("## File kinds (by content, not extension)")
    L.append("| Kind | Files | Size |")
    L.append("|---|---:|---:|")
    for k, d in s["by_kind"].items():
        L.append(f"| {k} | {d['count']:,} | {human_size(d['bytes'])} |")
    if s["text_encodings"]:
        L.append("")
        L.append("Text encodings: " + ", ".join(f"{k} x{v}" for k, v in s["text_encodings"].items())
                 + ". Line endings: " + (", ".join(f"{k} x{v}" for k, v in s["line_endings"].items()) or "n/a") + ".")
    L.append("")

    L.append("## File types (by extension)")
    L.append("| Extension | Files | Size | Lines |")
    L.append("|---|---:|---:|---:|")
    for ext, d in list(s["by_extension"].items())[:25]:
        L.append(f"| `{ext}` | {d['count']:,} | {human_size(d['bytes'])} | {d['lines']:,} |")
    if len(s["by_extension"]) > 25:
        L.append(f"| _{len(s['by_extension']) - 25} more types_ | | | |")
    L.append("")

    L.append("## Size distribution")
    L.append("| " + " | ".join(s["size_distribution"]) + " |")
    L.append("|" + "---:|" * len(s["size_distribution"]))
    L.append("| " + " | ".join(f"{v:,}" for v in s["size_distribution"].values()) + " |")
    L.append("")

    L.append("## Modification timeline (files per month)")
    L.append("| Month | Files |")
    L.append("|---|---:|")
    for m, n in s["modified_by_month"].items():
        L.append(f"| {m} | {n:,} |")
    L.append("")

    if s["snapshot_series"]:
        L.append("## Snapshot series")
        L.append("Folders whose subfolders are named by timestamp. A run is a cluster of snapshots less than an hour apart.")
        L.append("")
        L.append("| Folder | Snapshots | Runs | First | Last | Median gap between runs | Size first to last |")
        L.append("|---|---:|---:|---|---|---:|---|")
        for x in s["snapshot_series"][:40]:
            L.append(f"| `{x['path']}` | {x['count']} | {x['runs']} | {x['first'][:10]} | {x['last'][:10]} | "
                     f"{x['median_gap_days']} d | {human_size(x['first_size'])} to {human_size(x['last_size'])} |")
        if len(s["snapshot_series"]) > 40:
            L.append(f"| _{len(s['snapshot_series']) - 40} more_ | | | | | | |")
        L.append("")
        runs = s["backup_runs"]
        L.append(f"**Runs ({len(runs)}):** " + ", ".join(f"{r['start']} ({r['folders']})" for r in runs[:60])
                 + (" ..." if len(runs) > 60 else ""))
        L.append("")

    L.append("## Largest files")
    for f in s["largest_files"]:
        copies = f" x{f['copies']} copies, e.g." if f["copies"] > 1 else ""
        L.append(f"- `{f['name']}`: {human_size(f['size'])}{copies} `{f['example']}`")
    L.append("")

    if ld["groups"]:
        L.append("## Repeated copies")
        L.append(f"Grouped by {ld['basis']}. Run with `--hash` to confirm the bytes match.")
        L.append("")
        L.append("| File | Size | Copies | Beyond first copy |")
        L.append("|---|---:|---:|---:|")
        for g in ld["top"][:10]:
            L.append(f"| `{g['name']}` | {human_size(g['size'])} | {g['copies']} | {human_size(g['wasted_bytes'])} |")
        L.append("")

    L.append("## Recently modified")
    for f in s["newest_files"]:
        L.append(f"- `{f['path']}`: {f['modified']}")
    L.append("")

    flags = []
    if s["empty_files_count"]:
        flags.append(f"**Empty files ({s['empty_files_count']}):** " + ", ".join(f"`{p}`" for p in s["empty_files"][:10]))
    if s["empty_dirs_count"]:
        bp = s["empty_dirs_by_parent"]
        flags.append(f"**Empty folders ({s['empty_dirs_count']}), by parent:** "
                     + ", ".join(f"`{p}` x{n}" for p, n in list(bp.items())[:10])
                     + (" ..." if len(bp) > 10 else ""))
    if s["extension_mismatches_count"]:
        mm = Counter((m["ext"], m["kind"]) for m in s["extension_mismatches"])
        flags.append(f"**Extension does not match content ({s['extension_mismatches_count']}):** "
                     + ", ".join(f"`{e}` holding {k} x{n}" for (e, k), n in mm.most_common(8)))
    if s["possibly_sensitive_filenames"]:
        flags.append(f"**Filenames that look sensitive ({len(s['possibly_sensitive_filenames'])}):** "
                     + ", ".join(f"`{p}`" for p in s["possibly_sensitive_filenames"][:10])
                     + " (contents not reported)")
    if vd and vd["groups"]:
        flags.append(f"**Verified duplicate groups: {vd['groups']}**")
        for g in vd["top"][:10]:
            flags.append("  - " + ", ".join(f"`{p}`" for p in g["files"][:6])
                         + (f" (+{len(g['files']) - 6} more)" if len(g["files"]) > 6 else ""))
    if flags:
        L.append("## Flags")
        L += [f"- {x}" if not x.startswith("  ") else x for x in flags]
        L.append("")

    if meta["opt_ins"]:
        L.append("## Opted-in extras")
        L.append(f"Included because you asked for: {', '.join(meta['opt_ins'])}. "
                 "These are the only parts of this report taken from inside files.")
        L.append("")
        details = s["opt_in_details"]
        if not details:
            L.append("_No matching files found._")
        for d in details[:200]:
            copies = f"  (x{d['copies']} identical copies, showing the last)" if d["copies"] > 1 else ""
            L.append(f"- `{d['path']}`{copies}")
            for x in extra_lines(d):
                L.append(f"  - {x}")
        if len(details) > 200:
            L.append(f"- _{len(details) - 200} more files, see the JSON_")
        L.append("")

    if s["skipped"]:
        L.append("## Skipped / not scanned")
        for k, v in s["skipped"].items():
            L.append(f"- {k}: {v}")
        L.append("")

    L.append("## Folder structure")
    if meta["options"]["collapsed"]:
        L.append(f"_Sibling folders with an identical layout (at least {args.collapse_min}) are shown once. "
                 "Use `--no-collapse` for the full tree._")
    L.append("```")
    L.append(f"{tree['name']}/  ({human_size(tree['total_size'])}, {plural(tree['file_count'], 'file')})")
    render_tree(tree, "", args.tree_limit, args.dir_limit, L)
    L.append("```")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Summarize a folder's structure and metadata without sharing contents.")
    p.add_argument("path", nargs="?", default=".", help="Folder to analyze (default: current)")
    p.add_argument("-o", "--output", help="Output suffix (default: <folder>_report)")
    p.add_argument("--format", choices=["md", "json", "both"], default="both")
    p.add_argument("--max-depth", type=int, help="Don't descend below this depth")
    p.add_argument("--ignore", action="append", help="Extra name or glob to ignore, e.g. '*.tmp' (repeatable)")
    p.add_argument("--no-default-ignore", action="store_true", help="Don't skip .git, node_modules, venv, etc.")
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dotfolders")
    p.add_argument("--tree-limit", type=int, default=15, help="Max files listed per folder in the Markdown tree")
    p.add_argument("--dir-limit", type=int, default=50, help="Max subfolders listed per folder in the Markdown tree")
    p.add_argument("--no-collapse", action="store_true", help="Show every folder instead of folding identical siblings")
    p.add_argument("--collapse-min", type=int, default=3, help="Siblings with the same layout needed to fold (default 3)")
    p.add_argument("--no-sniff", action="store_true",
                   help="Skip content classification (kind, entropy, dimensions). Still reads bytes to detect text")
    p.add_argument("--compact", action="store_true", help="Write JSON without indentation")
    p.add_argument("--anonymize", action="store_true", help="Replace file/folder names with placeholders")
    p.add_argument("--save-map", action="store_true", help="With --anonymize: write a LOCAL map to translate names back")
    p.add_argument("--hash", action="store_true", help="SHA-256 same-size files to verify duplicates")
    p.add_argument("--csv-headers", action="store_true", help="OPT-IN: include CSV column names")
    p.add_argument("--json-keys", action="store_true", help="OPT-IN: include top-level JSON key names")
    p.add_argument("--outline", action="store_true", help="OPT-IN: include Python class/function names")
    p.add_argument("-q", "--quiet", action="store_true", help="No progress output")
    args = p.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    sc = Scanner(args)
    if not args.quiet:
        print(f"Scanning {root} ...", file=sys.stderr)
    tree = sc.scan_dir(str(root), "", 0)
    label = "ROOT" if args.anonymize else root.name
    tree["name"] = label
    for s in sc.series:
        if s["path"] == "ROOT":
            s["path"] = label
    summary = build_summary(sc, tree, args)
    if not args.no_collapse:
        collapse(tree, max(2, args.collapse_min))
    if not args.quiet:
        print(f"\r  {len(sc.files):,} files scanned.", file=sys.stderr)

    opt_ins = [n for n, on in (("csv headers", args.csv_headers), ("json keys", args.json_keys),
                               ("python outline", args.outline)) if on]
    meta = {
        "schema_version": SCHEMA_VERSION,
        "root_label": label,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "anonymized": args.anonymize,
        "opt_ins": opt_ins,
        "contains_file_contents": bool(opt_ins),
        "options": {
            "collapsed": not args.no_collapse,
            "hashed": args.hash,
            "sniffed": not args.no_sniff,
            "max_depth": args.max_depth,
            "include_hidden": args.include_hidden,
        },
        "privacy_note": "Up to 64 KB from the start and 4 KB from the end of each file were read to "
                        "classify it; text files were read in full to count lines. No bytes from "
                        "files appear in this report except the opted-in extras listed.",
    }

    prefix = args.output or ("folder_report" if args.anonymize else f"{root.name}_report")
    written = []
    if args.format in ("md", "both"):
        Path(prefix + ".md").write_text(to_markdown(meta, summary, tree, args), encoding="utf-8")
        written.append(prefix + ".md")
    if args.format in ("json", "both"):
        doc = strip_private({"meta": meta, "summary": summary, "tree": tree})
        Path(prefix + ".json").write_text(
            json.dumps(doc, indent=None if args.compact else 1, ensure_ascii=False), encoding="utf-8")
        written.append(prefix + ".json")
    if args.anonymize and args.save_map:
        mp = prefix + ".map.json"
        Path(mp).write_text(json.dumps({v: k[1] for k, v in sc.namer.map.items()}, indent=2), encoding="utf-8")
        written.append(mp + "  <-- KEEP LOCAL, do not share")

    if not args.quiet:
        print("Wrote:\n  " + "\n  ".join(written), file=sys.stderr)


if __name__ == "__main__":
    main()
