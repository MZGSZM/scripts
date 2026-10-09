# folder-report.py

A single Python script that describes a folder's layout, sizes, dates and file types without copying any file contents into the report. It's for when you want an AI model, or another person, to understand what's in a folder without handing over the files themselves.

It writes two files: a Markdown report you can read, and a JSON version you can paste or upload somewhere.

## Prerequisites

- Python 3.8 or newer
- Nothing else. It only uses the standard library, so there's no pip install step.

I'm on openSUSE, where `python3` is already installed. If yours isn't, it's `sudo zypper install python3` in my case, or `sudo apt install python3`, `sudo dnf install python3`, or whichever package manager your system uses. On Windows, install Python from python.org and use `python` instead of `python3` below.

## Usage

1. Download `folder-report.py` and put it somewhere handy, `~/bin` or wherever you keep scripts.
2. Make it executable with `chmod +x folder-report.py`, so you can run it directly instead of typing `python3` every time. Skip this on Windows.
3. Run it against a folder: `./folder-report.py /path/to/folder`
4. Look in your current directory for `<folder>_report.md` and `<folder>_report.json`. The report lands wherever you ran the command from, not inside the scanned folder.

That's it for a basic run. A 4,300-file, 1.2 GB folder takes around 8 seconds on my machine.

## What it reads, and what it writes

Why read files at all? Because a name and a size can't tell you that a `.txt` file is really a PNG, or that a 128 KB "text" file is mostly zero padding.

To classify each file it reads at most the first 64 KB and the last 4 KB. Text files are read in full, but only to count lines. **None of those bytes are written to the report.**

The report contains:

- Names, sizes, modification dates and extensions
- File kind, detected from the file's first bytes: PNG, JPEG, ZIP, SQLite, ELF, Windows executable, PEM key, gzip and so on. Anything unrecognized is labelled zero-filled, high-entropy (compressed or encrypted) or plain binary.
- For text: encoding, line endings, line count and longest line
- Image width and height, read from the header only. EXIF data is never touched.
- ZIP entry count and total uncompressed size. Member names are not reported.
- SQLite page size and page count. No queries are run.
- JSON shape: top-level type, key or item count, and nesting depth. Keys and values stay out.
- CSV row and column counts
- Executable bit and hard-link count

Some extras do include content. They're off unless you ask for them, and the report header says which ones were used:

- `--csv-headers` adds CSV column names
- `--json-keys` adds top-level JSON key names
- `--outline` adds Python class and function names

They show up in three places: under each file in the folder tree, in an "Opted-in extras" section of the Markdown report, and in the JSON. The section lists every matching file even when the tree hides it behind collapsing or `--tree-limit`. Identical copies, like the same CSV in 12 snapshots, are listed once with a count. Very long lists are trimmed to 40 names in the Markdown; the JSON keeps up to 100 JSON keys and every CSV header.

## What the report shows

- **Overview:** totals, plus how much space is taken by repeated copies of the same file
- **Where the space is:** top-level folders and files, sorted by size
- **File kinds and file types:** by content and by extension, side by side
- **Size distribution and a month-by-month modification timeline**
- **Snapshot series:** folders whose subfolders are named by timestamp, like `20250520-213146`. For each one you get the snapshot count, number of backup runs, first and last date, gap between runs, and how the size changed.
- **Largest files:** grouped, so 24 copies of the same file show up once with a count
- **Repeated copies:** files with the same name and size, and how much space everything past the first copy uses
- **Flags:** empty files and folders, extensions that don't match content (a ZIP named `.dat`, a PNG named `.txt`), filenames that look sensitive like `.env` or `id_rsa`, and verified duplicates if you used `--hash`
- **Folder tree:** with sizes and file counts on every folder

### Collapsing

If a folder has three or more subfolders with identical layouts, like backup snapshots, the tree shows only the newest one with a note saying how many there were and their size range. The JSON still keeps the size, file count and date of every copy.

This is what keeps reports readable. On a backup folder with 12 to 24 snapshots per game, collapsing took the JSON from 1.4 MB to about 250 KB. If you want every folder spelled out, use `--no-collapse`.

## Options

| Option | What it does |
|---|---|
| `-o PREFIX` | Output filename prefix. Default is `<folder>_report`. |
| `--format md\|json\|both` | Which files to write. Default is both. |
| `--max-depth N` | Don't go deeper than N levels. |
| `--ignore NAME` | Skip a name or glob, like `'*.tmp'`. Repeat it for more. |
| `--no-default-ignore` | Stop skipping `.git`, `node_modules`, `venv` and similar. |
| `--include-hidden` | Include dotfiles and dotfolders. |
| `--tree-limit N` | Max files listed per folder in the Markdown tree. Default 15. |
| `--dir-limit N` | Max subfolders listed per folder in the Markdown tree. Default 50. |
| `--no-collapse` | Show every folder instead of folding identical siblings. |
| `--collapse-min N` | How many identical siblings it takes to fold. Default 3. |
| `--hash` | SHA-256 files that share a size, to confirm real duplicates. |
| `--no-sniff` | Skip kind, entropy and image checks. Text detection still runs. |
| `--compact` | Write the JSON without indentation. |
| `--anonymize` | Replace file and folder names with placeholders. |
| `--save-map` | With `--anonymize`, also write a file that maps placeholders back to real names. |
| `--csv-headers` | Opt-in: include CSV column names. |
| `--json-keys` | Opt-in: include top-level JSON key names. |
| `--outline` | Opt-in: include Python class and function names. |
| `-q` | No progress output. |

## Examples

Basic report of the current folder:

```
./folder-report.py .
```

Anonymized, with a map kept locally so you can translate the AI's answers back:

```
./folder-report.py ~/Documents --anonymize --save-map
```

Find out how much of a backup folder is truly duplicated:

```
./folder-report.py /mnt/backups --hash
```

Quick Markdown-only look at the top few levels of a big drive:

```
./folder-report.py /mnt/data --max-depth 3 --format md
```

## Anonymizing

`--anonymize` turns names into `dir_001`, `file_0042.dat` and so on. Extensions are kept, since they're useful and rarely private. A handful of well-known names like `README.md`, `package.json` and `Makefile` are also kept.

Two things to know:

- The same real name always gets the same placeholder. That's what lets identical snapshot folders still collapse, but it means someone can tell that two files share a name.
- Names made only of digits, dashes, dots and underscores are left alone, like `20250520-213146` or `01.dat`. The modification dates in the report already reveal that much.

Folder and file sizes, dates and kinds are not hidden by `--anonymize`. If those are sensitive for you, this isn't the right tool.

**DO NOT SHARE THE `.map.json` FILE.** It's the key that turns every placeholder back into a real name. It's there so you can read the AI's answer and know which `file_0042.dat` it means. Keep it on your machine.

## Limitations

- Repeated copies without `--hash` are grouped by name and size only. Two files can match on both and still differ inside. Use `--hash` to confirm.
- Symlinks are listed but not followed.
- Line counting is skipped for text files over 50 MB, and JSON parsing for files over 20 MB.
- Snapshot detection only recognizes date-style names: `YYYYMMDD`, `YYYYMMDD-HHMMSS`, `YYYY-MM-DD` and close variants.
- The sensitive-filename check is a name pattern match. It doesn't look inside files, and it will miss things with ordinary names.

Now you've got a report that describes the folder well enough for someone to answer questions about it, and nothing from inside the files left your machine. Tinker with the limits and opt-ins as you see fit.
