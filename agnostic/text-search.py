#!/usr/bin/env python3
"""
text-search.py

Search for a keyword across the "plain text" portions of files.

- .torrent files are parsed as bencode, and only the human-readable
  string fields are searched (file names, comment, created by,
  announce URLs, etc). Binary hash fields ('pieces', and the v2
  'piece layers' / 'pieces root') are skipped. If a .torrent file
  cannot be parsed, it is scanned like any other binary file.
- Ordinary text files are read and searched line by line. UTF-8
  (including non-ASCII text), UTF-16 with a BOM, and legacy 8-bit
  encodings are handled.
- Any other file (unknown/binary) is scanned the way the `strings`
  command works: runs of printable ASCII, and of UTF-16LE text (common
  in Windows binaries), are extracted and searched. Files are read in
  chunks, so large binaries do not need to fit in memory.

Usage:
    python3 text-search.py [keyword] [options]

If no keyword is given as an argument, you'll be prompted for one.

Exit status: 0 if anything matched, 1 if nothing matched, 2 on usage
errors, 130 if interrupted.

Examples:
    # Search current directory, non-recursive
    python3 text-search.py "ubuntu"

    # Search recursively, only .torrent files
    python3 text-search.py "ubuntu" -r --include-ext torrent

    # Search recursively, skip anything under a "cache" folder
    python3 text-search.py "ubuntu" -r --exclude-path "*/cache/*"

    # Case-sensitive search of a specific directory
    python3 text-search.py "Ubuntu" -p /path/to/dir --case-sensitive
"""

import argparse
import codecs
import fnmatch
import os
import re
import stat
import sys


# --------------------------------------------------------------------------
# Bencode decoding (minimal, read-only, tolerant of torrent-file quirks)
# --------------------------------------------------------------------------

class BencodeDecodeError(Exception):
    pass


_MAX_BENCODE_DEPTH = 200  # well below Python's recursion limit


def bdecode(data: bytes):
    """Decode a bencoded byte string into Python objects.

    Dict keys and byte-strings are returned as `bytes` (not str), since
    bencoded strings are not guaranteed to be valid text -- some, like
    the 'pieces' field, are raw binary hashes. Callers should decide
    per-field whether/how to turn bytes into text.
    """
    obj, _index = _bdecode_next(data, 0, 0)
    return obj


def _bdecode_next(data: bytes, index: int, depth: int):
    if index >= len(data):
        raise BencodeDecodeError("Unexpected end of data")
    if depth > _MAX_BENCODE_DEPTH:
        raise BencodeDecodeError("Nesting too deep")

    ch = data[index:index + 1]

    if ch == b"i":
        end = data.find(b"e", index)
        if end == -1:
            raise BencodeDecodeError("Unterminated integer")
        try:
            return int(data[index + 1:end]), end + 1
        except ValueError:
            raise BencodeDecodeError(f"Invalid integer at index {index}")

    if ch in (b"l", b"d"):
        index += 1
        items = []
        while data[index:index + 1] != b"e":
            item, index = _bdecode_next(data, index, depth + 1)
            items.append(item)
        index += 1
        if ch == b"l":
            return items, index
        if len(items) % 2:
            raise BencodeDecodeError("Dictionary has a key with no value")
        return dict(zip(items[0::2], items[1::2])), index

    if ch.isdigit():
        colon = data.find(b":", index)
        if colon == -1:
            raise BencodeDecodeError("Unterminated string length")
        try:
            length = int(data[index:colon])
        except ValueError:
            raise BencodeDecodeError(f"Invalid string length at index {index}")
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeDecodeError("String runs past end of data")
        return data[start:end], end

    raise BencodeDecodeError(f"Unexpected token at index {index}: {ch!r}")


# --------------------------------------------------------------------------
# Torrent-specific text extraction
# --------------------------------------------------------------------------

# Keys whose values are binary/non-text and should never be treated as
# searchable text, even though they live inside string fields.
# 'pieces' is v1; 'piece layers' and 'pieces root' are BitTorrent v2.
_TORRENT_BINARY_KEYS = {b"pieces", b"piece layers", b"pieces root"}


def extract_torrent_strings(obj, key_path="", in_file_tree=False):
    """Walk a decoded torrent dict and return (path, text) for every
    plain-text string field, skipping known-binary fields.

    In v2 torrents, file and directory names are dict KEYS under
    'info.file tree' rather than values, so those keys are emitted too.
    """
    results = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _TORRENT_BINARY_KEYS:
                continue
            k_name = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
            child_path = f"{key_path}.{k_name}" if key_path else k_name

            if in_file_tree and k:  # b"" marks the file-info leaf in v2
                results.append((child_path, k_name))

            child_in_tree = in_file_tree or k == b"file tree"
            results.extend(extract_torrent_strings(v, child_path, child_in_tree))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(extract_torrent_strings(item, f"{key_path}[{i}]", in_file_tree))

    elif isinstance(obj, bytes):
        text = _try_decode_text(obj)
        if text is not None:
            results.append((key_path, text))
        # if it doesn't decode cleanly as text, silently skip it
        # (this is how we avoid dumping binary garbage into results)

    elif isinstance(obj, int):
        results.append((key_path, str(obj)))

    return results


def _try_decode_text(data: bytes, printable_ratio_threshold: float = 0.95):
    """Attempt to interpret bytes as human text. Returns the decoded
    string if it looks like real text, otherwise None.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")  # never fails; ratio check filters junk

    if not text:
        return None

    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if printable / len(text) < printable_ratio_threshold:
        return None

    return text


def search_torrent_file(path, pattern):
    """Return (matches, error, mode)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return [], str(e), "torrent"

    try:
        decoded = bdecode(data)
    except BencodeDecodeError as e:
        # Not valid bencode: still search it rather than skipping it.
        matches, error = search_binary_file_strings(path, pattern)
        return matches, error, f"binary-strings, bencode parse failed: {e}"

    fields = extract_torrent_strings(decoded)
    matches = [(field_path, text) for field_path, text in fields if pattern.search(text)]
    return matches, None, "torrent"


# --------------------------------------------------------------------------
# Text files
# --------------------------------------------------------------------------

def detect_text_encoding(sample: bytes):
    """Return an encoding name if the sample looks like text, else None."""
    if not sample:
        return "utf-8"
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\x00" in sample:
        return None

    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError as e:
        # The sample may cut a multi-byte character in half at the end.
        if e.reason == "unexpected end of data" and e.start >= len(sample) - 3:
            text = sample[:e.start].decode("utf-8", errors="replace")
        else:
            text = None

    if text is not None:
        if not text:
            return "utf-8"
        printable = sum(1 for c in text if c.isprintable() or c in "\t\n\r\f")
        return "utf-8" if printable / len(text) > 0.85 else None

    # Not UTF-8: accept mostly-ASCII data as a legacy 8-bit encoding.
    printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 12, 13))
    return "latin-1" if printable / len(sample) > 0.85 else None


def search_plain_text_file(path, pattern, encoding):
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            matches = []
            for lineno, line in enumerate(f, start=1):
                if pattern.search(line):
                    matches.append((f"line {lineno}", line.strip()))
        return matches, None
    except OSError as e:
        return [], str(e)


# --------------------------------------------------------------------------
# Binary files: `strings`-style extraction, streamed in chunks
# --------------------------------------------------------------------------

_CHUNK_SIZE = 1 << 20   # 1 MiB per read
_MAX_CARRY = 1 << 20    # cap on a single run held across chunk boundaries


def _is_print(b):
    return 0x20 <= b <= 0x7e


class _RunScanner:
    """Finds printable runs in a byte stream fed in chunks.

    A run touching the end of the current buffer may continue in the next
    chunk, so it is held back ("carried") until it is known to be complete.
    """

    def __init__(self, regex, tail_start, decode, label):
        self.regex = regex
        self.tail_start = tail_start
        self.decode = decode
        self.label = label
        self.buf = b""
        self.base = 0  # file offset of buf[0]

    def feed(self, chunk, eof):
        self.buf += chunk
        keep_from = len(self.buf) if eof else self.tail_start(self.buf)
        if len(self.buf) - keep_from > _MAX_CARRY:
            keep_from = len(self.buf)  # pathological run: flush it as-is

        for m in self.regex.finditer(self.buf, 0, keep_from):
            yield self.base + m.start(), self.label, self.decode(m.group())

        self.base += keep_from
        self.buf = self.buf[keep_from:]


def _ascii_tail_start(buf):
    i = len(buf)
    while i > 0 and _is_print(buf[i - 1]):
        i -= 1
    return i


def _utf16le_tail_start(buf):
    i = len(buf)
    if i > 0 and _is_print(buf[i - 1]):
        i -= 1  # possibly the first half of a character pair
    while i >= 2 and buf[i - 1] == 0 and _is_print(buf[i - 2]):
        i -= 2
    return i


_ASCII_RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")
_UTF16LE_RUN_RE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")


def search_binary_file_strings(path, pattern):
    scanners = [
        _RunScanner(_ASCII_RUN_RE, _ascii_tail_start,
                    lambda b: b.decode("ascii"), ""),
        _RunScanner(_UTF16LE_RUN_RE, _utf16le_tail_start,
                    lambda b: b.decode("utf-16-le"), " utf-16le"),
    ]
    matches = []
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                eof = not chunk
                for scanner in scanners:
                    for offset, label, text in scanner.feed(chunk, eof):
                        if pattern.search(text):
                            matches.append((offset, f"offset {offset}{label}", text))
                if eof:
                    break
    except OSError as e:
        return [], str(e)

    matches.sort(key=lambda m: m[0])
    return [(loc, text) for _off, loc, text in matches], None


# --------------------------------------------------------------------------
# File discovery with whitelist/blacklist filtering
# --------------------------------------------------------------------------

def _is_regular_file(path):
    """True for regular files (following symlinks). Excludes FIFOs, sockets
    and device nodes, which would block forever or never end when read."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _walk_error(err):
    print(f"[!] {err.filename}: {err.strerror}", file=sys.stderr)


def iter_candidate_files(root, recursive, include_ext, exclude_ext,
                         include_path, exclude_path):
    if os.path.isfile(root):
        candidates = [root]
    elif recursive:
        # Exclude patterns ending in '*' that match a directory also match
        # everything inside it, so the directory can be skipped entirely
        # instead of walked and filtered file by file.
        prune_patterns = [p for p in exclude_path if p.endswith("*")]

        def generate():
            for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
                if prune_patterns:
                    dirnames[:] = [
                        d for d in dirnames
                        if not any(fnmatch.fnmatch(
                            os.path.join(dirpath, d).replace(os.sep, "/") + "/", p)
                            for p in prune_patterns)
                    ]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if _is_regular_file(full):
                        yield full
        candidates = generate()
    else:
        try:
            names = sorted(os.listdir(root))
        except OSError as e:
            _walk_error(e)
            names = []
        candidates = (os.path.join(root, n) for n in names
                      if _is_regular_file(os.path.join(root, n)))

    for path in candidates:
        if not _passes_ext_filter(path, include_ext, exclude_ext):
            continue
        if not _passes_path_filter(path, include_path, exclude_path):
            continue
        yield path


def _passes_ext_filter(path, include_ext, exclude_ext):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if include_ext and ext not in include_ext:
        return False
    if exclude_ext and ext in exclude_ext:
        return False
    return True


def _passes_path_filter(path, include_path, exclude_path):
    norm = path.replace(os.sep, "/")
    if include_path and not any(fnmatch.fnmatch(norm, pat) for pat in include_path):
        return False
    if exclude_path and any(fnmatch.fnmatch(norm, pat) for pat in exclude_path):
        return False
    return True


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

# C0/C1 control characters and Unicode bidi overrides. Printing these raw
# lets file contents move the cursor, recolor the terminal, or visually
# reorder text.
_UNSAFE_CHARS_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _sanitize(text):
    return _UNSAFE_CHARS_RE.sub(lambda m: f"\\x{ord(m.group()):02x}"
                                if ord(m.group()) < 0x100
                                else f"\\u{ord(m.group()):04x}", text)


def make_snippet(text, pattern, width=200):
    """Return up to `width` characters of text, centered on the first match
    so long lines do not hide the part that actually matched."""
    if len(text) > width:
        m = pattern.search(text)
        start = 0
        if m:
            start = max(0, min(m.start() - width // 3, len(text) - width))
        end = start + width
        text = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
    return _sanitize(text)


# --------------------------------------------------------------------------
# Main search dispatch
# --------------------------------------------------------------------------

def search_file(path, pattern):
    """Return (matches, error, mode) for a single file."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".torrent":
        return search_torrent_file(path, pattern)

    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
    except OSError as e:
        return [], str(e), "unreadable"

    encoding = detect_text_encoding(sample)
    if encoding:
        matches, error = search_plain_text_file(path, pattern, encoding)
        return matches, error, "text"
    matches, error = search_binary_file_strings(path, pattern)
    return matches, error, "binary-strings"


def parse_ext_list(raw):
    if not raw:
        return set()
    result = set()
    for item in raw:
        for piece in item.split(","):
            piece = piece.strip().lstrip(".").lower()
            if piece:
                result.add(piece)
    return result


def main():
    # Never crash on output: e.g. Windows consoles or redirected output in
    # a legacy code page cannot encode every character found in files.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Search the plain-text elements of files (including "
                    ".torrent bencode fields) for a keyword.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("keyword", nargs="?", default=None,
                        help="Keyword or regex pattern to search for. "
                             "If omitted, you'll be prompted.")
    parser.add_argument("-p", "--path", default=".",
                        help="File or directory to search (default: current directory)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Recurse into subdirectories")
    parser.add_argument("--include-ext", action="append", default=None,
                        help="Whitelist: only search files with this extension. "
                             "Repeatable or comma-separated "
                             "(e.g. --include-ext torrent --include-ext txt, "
                             "or --include-ext torrent,txt)")
    parser.add_argument("--exclude-ext", action="append", default=None,
                        help="Blacklist: skip files with this extension. "
                             "Repeatable or comma-separated "
                             "(e.g. --exclude-ext jpg --exclude-ext png)")
    parser.add_argument("--include-path", action="append", default=None,
                        help="Whitelist: only search paths matching this glob "
                             "pattern. Repeatable "
                             "(e.g. --include-path '*/downloads/*')")
    parser.add_argument("--exclude-path", action="append", default=None,
                        help="Blacklist: skip paths matching this glob pattern. "
                             "Repeatable "
                             "(e.g. --exclude-path '*/node_modules/*' "
                             "--exclude-path '*/.git/*')")
    parser.add_argument("--case-sensitive", action="store_true",
                        help="Case-sensitive search (default: case-insensitive)")
    parser.add_argument("--regex", action="store_true",
                        help="Treat the keyword as a regular expression "
                             "instead of a literal string")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Only print matching file paths, not the matched text")

    args = parser.parse_args()

    keyword = args.keyword
    if not keyword:
        try:
            keyword = input("Enter keyword to search for: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo keyword provided, exiting.")
            sys.exit(2)
        if not keyword:
            print("No keyword provided, exiting.")
            sys.exit(2)

    flags = 0 if args.case_sensitive else re.IGNORECASE
    search_text = keyword if args.regex else re.escape(keyword)
    try:
        pattern = re.compile(search_text, flags)
    except re.error as e:
        print(f"Invalid regex pattern: {e}")
        sys.exit(2)

    search_path = os.path.expanduser(os.path.expandvars(args.path))

    if not (os.path.isfile(search_path) or os.path.isdir(search_path)):
        print(f"Path does not exist or is not a regular file/directory: {search_path}")
        sys.exit(2)

    include_ext = parse_ext_list(args.include_ext)
    exclude_ext = parse_ext_list(args.exclude_ext)
    include_path = args.include_path or []
    exclude_path = args.exclude_path or []

    total_files = 0
    total_matches = 0
    files_with_matches = 0
    interrupted = False

    try:
        for path in iter_candidate_files(
            search_path, args.recursive, include_ext, exclude_ext,
            include_path, exclude_path,
        ):
            total_files += 1
            matches, error, mode = search_file(path, pattern)

            if error:
                print(f"[!] {_sanitize(path)}: {error}", file=sys.stderr)
                continue

            if matches:
                files_with_matches += 1
                total_matches += len(matches)
                print(f"\n{_sanitize(path)}  ({mode}, {len(matches)} "
                      f"match{'es' if len(matches) != 1 else ''})")
                if not args.quiet:
                    for location, text in matches:
                        print(f"    [{_sanitize(location)}] {make_snippet(text, pattern)}")
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n[!] Interrupted by user.", file=sys.stderr)

    status = "Interrupted after" if interrupted else "Searched"
    print(f"\n--- {status} {total_files} file(s), "
          f"found {total_matches} match(es) in {files_with_matches} file(s) ---")

    if interrupted:
        sys.exit(130)  # conventional exit code for SIGINT
    sys.exit(0 if files_with_matches else 1)


if __name__ == "__main__":
    main()
