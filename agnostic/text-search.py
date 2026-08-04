#!/usr/bin/env python3
"""
text-search.py

Search for a keyword across the "plain text" portions of files.

- .torrent files are parsed as bencode, and only the human-readable
  string fields are searched (file names, comment, created by,
  announce URLs, etc). The binary 'pieces' hash blob is skipped.
- Ordinary text files are read and searched directly.
- Any other file (unknown/binary) is scanned the way the `strings`
  command works: runs of printable characters are extracted and
  searched, so text embedded in binary files is still found.

Usage:
    python3 text-search.py [keyword] [options]

If no keyword is given as an argument, you'll be prompted for one.

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
import fnmatch
import os
import re
import sys


# --------------------------------------------------------------------------
# Bencode decoding (minimal, read-only, tolerant of torrent-file quirks)
# --------------------------------------------------------------------------

class BencodeDecodeError(Exception):
    pass


def bdecode(data: bytes):
    """Decode a bencoded byte string into Python objects.

    Dict keys and byte-strings are returned as `bytes` (not str), since
    bencoded strings are not guaranteed to be valid text -- some, like
    the 'pieces' field, are raw binary hashes. Callers should decide
    per-field whether/how to turn bytes into text.
    """
    obj, index = _bdecode_next(data, 0)
    return obj


def _bdecode_next(data: bytes, index: int):
    if index >= len(data):
        raise BencodeDecodeError("Unexpected end of data")

    ch = data[index:index + 1]

    if ch == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1

    if ch == b"l":
        index += 1
        items = []
        while data[index:index + 1] != b"e":
            item, index = _bdecode_next(data, index)
            items.append(item)
        return items, index + 1

    if ch == b"d":
        index += 1
        result = {}
        while data[index:index + 1] != b"e":
            key, index = _bdecode_next(data, index)
            value, index = _bdecode_next(data, index)
            result[key] = value
        return result, index + 1

    if ch.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end

    raise BencodeDecodeError(f"Unexpected token at index {index}: {ch!r}")


# --------------------------------------------------------------------------
# Torrent-specific text extraction
# --------------------------------------------------------------------------

# Keys whose values are binary/non-text and should never be treated as
# searchable text, even though they live inside string fields.
_TORRENT_BINARY_KEYS = {b"pieces"}


def extract_torrent_strings(obj, key_path="", _depth=0):
    """Walk a decoded torrent dict and yield (path, text) for every
    plain-text string field, skipping known-binary fields like 'pieces'.
    """
    results = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            k_name = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
            child_path = f"{key_path}.{k_name}" if key_path else k_name

            if k in _TORRENT_BINARY_KEYS:
                continue  # skip binary hash blobs etc.

            results.extend(extract_torrent_strings(v, child_path, _depth + 1))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            child_path = f"{key_path}[{i}]"
            results.extend(extract_torrent_strings(item, child_path, _depth + 1))

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
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError:
            return None

    if not text:
        return None

    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if printable / len(text) < printable_ratio_threshold:
        return None

    return text


def search_torrent_file(path, pattern):
    with open(path, "rb") as f:
        data = f.read()

    try:
        decoded = bdecode(data)
    except (BencodeDecodeError, IndexError, ValueError) as e:
        return [], f"could not parse as bencode ({e})"

    fields = extract_torrent_strings(decoded)
    matches = [(field_path, text) for field_path, text in fields if pattern.search(text)]
    return matches, None


# --------------------------------------------------------------------------
# Generic text / binary-with-embedded-text extraction
# --------------------------------------------------------------------------

_STRINGS_RE = re.compile(rb"[\x20-\x7e]{4,}")  # printable ASCII runs, min length 4


def looks_like_text_file(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
    return printable / len(sample) > 0.85


def search_plain_text_file(path, pattern):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            matches = []
            for lineno, line in enumerate(f, start=1):
                if pattern.search(line):
                    matches.append((f"line {lineno}", line.strip()))
        return matches, None
    except OSError as e:
        return [], str(e)


def search_binary_file_strings(path, pattern):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return [], str(e)

    matches = []
    for m in _STRINGS_RE.finditer(data):
        chunk = m.group().decode("ascii", errors="ignore")
        if pattern.search(chunk):
            matches.append((f"offset {m.start()}", chunk))
    return matches, None


# --------------------------------------------------------------------------
# File discovery with whitelist/blacklist filtering
# --------------------------------------------------------------------------

def iter_candidate_files(root, recursive, include_ext, exclude_ext,
                          include_path, exclude_path):
    if os.path.isfile(root):
        candidates = [root]
    else:
        candidates = []
        if recursive:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    candidates.append(os.path.join(dirpath, name))
        else:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isfile(full):
                    candidates.append(full)

    for path in candidates:
        if not _passes_ext_filter(path, include_ext, exclude_ext):
            continue
        if not _passes_path_filter(path, include_path, exclude_path):
            continue
        yield path


def _passes_ext_filter(path, include_ext, exclude_ext):
    ext = os.path.splitext(path)[1].lstrip(".").lower()

    if include_ext:
        # whitelist: only these extensions are allowed
        if ext not in include_ext:
            return False

    if exclude_ext:
        if ext in exclude_ext:
            return False

    return True


def _passes_path_filter(path, include_path, exclude_path):
    norm = path.replace(os.sep, "/")

    if include_path:
        if not any(fnmatch.fnmatch(norm, pat) for pat in include_path):
            return False

    if exclude_path:
        if any(fnmatch.fnmatch(norm, pat) for pat in exclude_path):
            return False

    return True


# --------------------------------------------------------------------------
# Main search dispatch
# --------------------------------------------------------------------------

def search_file(path, pattern):
    """Return (matches, error, mode) for a single file."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".torrent":
        matches, error = search_torrent_file(path, pattern)
        return matches, error, "torrent"

    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
    except OSError as e:
        return [], str(e), "unreadable"

    if looks_like_text_file(sample):
        matches, error = search_plain_text_file(path, pattern)
        return matches, error, "text"
    else:
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
    parser.add_argument("--include-ext", nargs="*", default=None,
                         help="Whitelist: only search files with these extensions "
                              "(e.g. --include-ext torrent txt)")
    parser.add_argument("--exclude-ext", nargs="*", default=None,
                         help="Blacklist: skip files with these extensions "
                              "(e.g. --exclude-ext jpg png)")
    parser.add_argument("--include-path", nargs="*", default=None,
                         help="Whitelist: only search paths matching these glob "
                              "patterns (e.g. --include-path '*/downloads/*')")
    parser.add_argument("--exclude-path", nargs="*", default=None,
                         help="Blacklist: skip paths matching these glob patterns "
                              "(e.g. --exclude-path '*/node_modules/*' '*/.git/*')")
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
            sys.exit(1)
        if not keyword:
            print("No keyword provided, exiting.")
            sys.exit(1)

    flags = 0 if args.case_sensitive else re.IGNORECASE
    search_text = keyword if args.regex else re.escape(keyword)
    try:
        pattern = re.compile(search_text, flags)
    except re.error as e:
        print(f"Invalid regex pattern: {e}")
        sys.exit(1)

    search_path = os.path.expanduser(os.path.expandvars(args.path))

    if not os.path.exists(search_path):
        print(f"Path does not exist: {search_path}")
        sys.exit(1)

    include_ext = parse_ext_list(args.include_ext)
    exclude_ext = parse_ext_list(args.exclude_ext)
    include_path = args.include_path or []
    exclude_path = args.exclude_path or []

    total_files = 0
    total_matches = 0
    files_with_matches = 0

    for path in iter_candidate_files(
        search_path, args.recursive, include_ext, exclude_ext,
        include_path, exclude_path,
    ):
        total_files += 1
        matches, error, mode = search_file(path, pattern)

        if error:
            print(f"[!] {path}: {error}", file=sys.stderr)
            continue

        if matches:
            files_with_matches += 1
            total_matches += len(matches)
            print(f"\n{path}  ({mode}, {len(matches)} match{'es' if len(matches) != 1 else ''})")
            if not args.quiet:
                for location, text in matches:
                    snippet = text if len(text) <= 200 else text[:200] + "..."
                    print(f"    [{location}] {snippet}")

    print(f"\n--- Searched {total_files} file(s), "
          f"found {total_matches} match(es) in {files_with_matches} file(s) ---")


if __name__ == "__main__":
    main()
