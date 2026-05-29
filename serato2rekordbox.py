print(r'''
                     _       ___           _                 _ _
                    | |     |__ \         | |               | | |
  ___  ___ _ __ __ _| |_ ___   ) |_ __ ___| | _____  _ __ __| | |__   _____  __
 / __|/ _ \ '__/ _` | __/ _ \ / /| '__/ _ \ |/ / _ \| '__/ _` | '_ \ / _ \ \/ /
 \__ \  __/ | | (_| | || (_) / /_| | |  __/   < (_) | | | (_| | |_) | (_) >  <
 |___/\___|_|  \__,_|\__\___/____|_|  \___|_|\_\___/|_|  \__,_|_.__/ \___/_/\_\
''')

__version__ = "serato2rekordbox v1.5"
print(f"\n{__version__}\n")

_IS_LOCAL_BUILD = True  # disable update checker (not on the original repo yet)

import argparse
import base64
import curses
import logging
import os
import platform
import re
import ssl
import struct
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from tqdm import tqdm

import extract_flac
import extract_m4a
import extract_mp3
import extract_wav
import extract_aiff

# ── Constants ────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {
    ".mp3", ".m4a", ".alac", ".wav", ".flac", ".aiff",
}

EXTRACTOR_MAP = {
    ".mp3": extract_mp3,
    ".m4a": extract_m4a,
    ".alac": extract_m4a,
    ".wav": extract_wav,
    ".flac": extract_flac,
    ".aiff": extract_aiff,
}

KIND_MAP = {
    ".mp3": "MP3 File",
    ".m4a": "M4A File",
    ".alac": "ALAC File",
    ".wav": "WAV File",
    ".flac": "FLAC File",
    ".aiff": "AIFF File",
}

START_MARKER = b"ptrk"
PATH_LENGTH_OFFSET = 4
START_MARKER_FULL_LENGTH = len(START_MARKER) + PATH_LENGTH_OFFSET
M4A_BEATGRID_OFFSET = 0.07
M4A_HOTCUE_OFFSET = 0.03

# ── Helpers ──────────────────────────────────────────────────────────────────


def prettify(elem):
    rough = tostring(elem, "utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ")


def check_for_updates():
    try:
        url = "https://raw.githubusercontent.com/BytePhoenixCoding/serato2rekordbox/main/README.md"
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(url, timeout=5, context=ctx) as resp:
            content = resp.read().decode("utf-8")
        if __version__ not in content:
            print("──────────────────────────────────────────────────────────")
            print("A new version of serato2rekordbox is available!")
            print("Please update: https://github.com/BytePhoenixCoding/serato2rekordbox")
            print("──────────────────────────────────────────────────────────\n")
        else:
            print("serato2rekordbox is up to date.")
    except Exception as e:
        print(f"(Update check skipped: {e})")


def find_serato_folder():
    """Auto-detect the Serato library folder (used as default for --serato)."""
    home = os.path.expanduser("~")
    candidates = []
    if platform.system() == "Windows":
        candidates = [
            os.path.join(home, "Music", "_Serato_"),
            os.path.join(home, "Documents", "_Serato_"),
        ]
    else:
        candidates = [os.path.join(home, "Music", "_Serato_")]

    for p in candidates:
        if os.path.isdir(p):
            return p
    return None


def resolve_track_path(crate_path, music_root, path_cache):
    """Resolve a file path stored in a .crate file to an actual filesystem path.

    Strategy:
    1. If the path exists as-is, return it.
    2. Look up the cache (built from music_root) by filename for O(1) lookup.
    """
    # Normalise separators
    norm = crate_path.replace("\\", os.sep)
    if platform.system() != "Windows" and not norm.startswith(os.sep):
        norm = os.sep + norm

    # Already seen?
    if norm in path_cache:
        return path_cache[norm]

    # Exists verbatim?
    if os.path.isfile(norm):
        path_cache[norm] = norm
        return norm

    # Fallback: look up by basename in the prebuilt cache
    basename = os.path.basename(norm)
    resolved = path_cache.get(("__basename__", basename))
    if resolved:
        path_cache[norm] = resolved
        return resolved

    # Not found
    return None


def build_path_cache(music_root):
    """Walk music_root and build a {filename: full_path} lookup dict.

    Returns a dict where:
      - ( '__basename__', 'song.mp3' ) → '/Volumes/USB/Music/song.mp3'
    Only the first occurrence of each filename is kept (matching original dedup).
    """
    cache = {}
    if not music_root or not os.path.isdir(music_root):
        return cache

    for dirpath, _dirs, files in os.walk(music_root):
        for fname in files:
            key = ("__basename__", fname)
            if key not in cache:
                cache[key] = os.path.join(dirpath, fname)
    return cache


# ── Crate I/O ────────────────────────────────────────────────────────────────


def find_serato_crates(subcrates_path):
    """Recursively find all .crate files under subcrates_path."""
    crates = []
    if not os.path.isdir(subcrates_path):
        print(f"Error: subcrates folder not found: {subcrates_path}")
        return crates

    print(f"Searching for .crate files in: {subcrates_path}")
    for root, _dirs, files in os.walk(subcrates_path):
        for f in files:
            if f.endswith(".crate"):
                crates.append(os.path.join(root, f))
    print(f"Found {len(crates)} crate file(s).\n")
    return crates


def extract_file_paths_from_crate(crate_path, encoding="utf-16-be"):
    """Parse raw track paths from a .crate binary file."""
    paths = []
    seen = set()
    errors = []

    try:
        with open(crate_path, "rb") as fh:
            blob = fh.read()

        blob_len = len(blob)
        i = 0

        while i < blob_len - START_MARKER_FULL_LENGTH:
            idx = blob.find(START_MARKER, i)
            if idx == -1:
                break
            i = idx + len(START_MARKER)

            if i + PATH_LENGTH_OFFSET > blob_len:
                errors.append(("parse_eof", crate_path, f"Unexpected EOF after marker at byte {idx}"))
                break

            path_len = struct.unpack(">I", blob[i : i + PATH_LENGTH_OFFSET])[0]
            i += PATH_LENGTH_OFFSET

            if i + path_len > blob_len:
                errors.append(("parse_overflow", crate_path, f"Path size {path_len} exceeds data at byte {i}"))
                break

            raw = blob[i : i + path_len]
            i += path_len

            try:
                abs_path = raw.decode(encoding).strip()
            except UnicodeDecodeError:
                errors.append(("decode", crate_path, f"UTF-16 decode failure at byte {i - path_len}"))
                continue

            if abs_path not in seen:
                paths.append(abs_path)
                seen.add(abs_path)

    except FileNotFoundError:
        print(f"Error: Crate file not found: {crate_path}")
    except Exception as exc:
        errors.append(("read", crate_path, str(exc)))

    return paths, errors


def format_crate_name(raw_name):
    """Turn 'Folder%%Subfolder%%ID' into 'Folder [Subfolder] [ID]'."""
    segments = raw_name.split("%%")
    if len(segments) == 1:
        return segments[0]
    return segments[0] + "".join(f" [{s}]" for s in segments[1:])


# ── Crate selector ───────────────────────────────────────────────────────────


def select_crates(stdscr, crate_list):
    """Curses-based crate selector (macOS / Linux).

    Returns a list of (crate_path, display_name) tuples the user selected.
    """
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(False)

    height, width = stdscr.getmaxyx()
    if height < 5 or width < 40:
        curses.echo()
        curses.endwin()
        print("Terminal too small for interactive selector. Converting all crates.")
        return [(p, format_crate_name(os.path.basename(p)[:-6])) for p in crate_list]

    title = " Select crates to convert (Arrows=move, Space=toggle, A=all, N=none, Enter=go) "
    footer_sel = " Selected: {sel}/{total}  |  q=cancel"

    selected = [True] * len(crate_list)
    cursor = 0
    scroll_offset = 0

    def draw():
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        stdscr.attron(curses.A_BOLD | curses.A_REVERSE)
        stdscr.addnstr(0, 0, title.center(w), w - 1)
        stdscr.attroff(curses.A_BOLD | curses.A_REVERSE)

        stdscr.addnstr(1, 0, " " * (w - 1), curses.A_DIM)

        visible = h - 4

        nonlocal scroll_offset
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + visible:
            scroll_offset = cursor - visible + 1
        scroll_offset = max(0, min(scroll_offset, len(crate_list) - visible))

        for i in range(visible):
            idx = scroll_offset + i
            row = i + 2
            if idx >= len(crate_list):
                break

            name = crate_list[idx][1]
            checked = " X " if selected[idx] else "   "
            prefix = f"[{checked}] "

            max_name_len = w - len(prefix) - 2
            displayed = name[:max_name_len] if len(name) > max_name_len else name

            attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
            try:
                stdscr.addstr(row, 0, prefix, attr)
                stdscr.addstr(row, len(prefix), displayed, attr)
            except curses.error:
                pass

        sel_count = sum(selected)
        footer = footer_sel.format(sel=sel_count, total=len(crate_list)).ljust(width - 1)
        stdscr.attron(curses.A_BOLD | curses.A_REVERSE)
        stdscr.addnstr(h - 1, 0, footer, width - 1)
        stdscr.attroff(curses.A_BOLD | curses.A_REVERSE)
        stdscr.refresh()

    try:
        while True:
            draw()
            ch = stdscr.getch()

            if ch in (curses.KEY_ENTER, 10, 13):
                break
            elif ch == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif ch == curses.KEY_DOWN:
                cursor = min(len(crate_list) - 1, cursor + 1)
            elif ch == curses.KEY_PPAGE:
                cursor = max(0, cursor - (height - 4))
            elif ch == curses.KEY_NPAGE:
                cursor = min(len(crate_list) - 1, cursor + (height - 4))
            elif ch == curses.KEY_HOME:
                cursor = 0
            elif ch == curses.KEY_END:
                cursor = len(crate_list) - 1
            elif ch in (32,):
                selected[cursor] = not selected[cursor]
            elif ch in (97, 65):
                selected = [True] * len(crate_list)
            elif ch in (110, 78):
                selected = [False] * len(crate_list)
            elif ch in (113, 27):
                curses.echo()
                curses.endwin()
                print("Crate selection cancelled.")
                return None

    finally:
        curses.echo()
        curses.endwin()

    return [
        (crate_list[i][0], crate_list[i][1])
        for i in range(len(crate_list))
        if selected[i]
    ]


def select_crates_fallback(crate_list):
    """Plain-text crate selector for Windows or when curses is unavailable.

    Shows a numbered list; user types comma-separated numbers to toggle.
    Returns a list of (crate_path, display_name) tuples the user selected.
    """
    print("\n Select crates to convert (all selected by default)\n")
    for i, (_path, name) in enumerate(crate_list, 1):
        print(f"  [{chr(9732)}] {i:3d}. {name}")

    print(f"\n {len(crate_list)} crate(s) found.")
    print(" Commands:  <numbers>  toggle (e.g. 1,3,5)")
    print("            <a>        select all")
    print("            <n>        deselect all")
    print("            <enter>    confirm and start conversion")
    print("            <q>        cancel\n")

    selected = [True] * len(crate_list)

    while True:
        try:
            choice = input(">>> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return None

        if choice == "":
            break
        if choice == "q":
            print("Crate selection cancelled.")
            return None
        if choice == "a":
            selected = [True] * len(crate_list)
            print("All selected.")
            continue
        if choice == "n":
            selected = [False] * len(crate_list)
            print("All deselected.")
            continue

        # Parse comma-separated numbers
        parts = [p.strip() for p in choice.split(",")]
        for part in parts:
            try:
                idx = int(part) - 1
                if 0 <= idx < len(crate_list):
                    selected[idx] = not selected[idx]
                    name = crate_list[idx][1]
                    state = "selected" if selected[idx] else "deselected"
                    print(f"  {name} -> {state}")
                else:
                    print(f"  Invalid number: {part}")
            except ValueError:
                print(f"  Invalid input: {part}")

        sel_count = sum(selected)
        print(f"  ({sel_count}/{len(crate_list)} selected)\n")

    return [
        (crate_list[i][0], crate_list[i][1])
        for i in range(len(crate_list))
        if selected[i]
    ]


# ── XML generation ───────────────────────────────────────────────────────────


def generate_rekordbox_xml(playlists, tracks):
    """Build a Rekordbox-compatible XML document and write to disk."""
    root = Element("DJ_PLAYLISTS", Version="1.0.0")
    SubElement(root, "PRODUCT", Name="rekordbox", Version="6.0.0", Company="AlphaTheta")
    collection = SubElement(root, "COLLECTION", Entries=str(len(tracks)))
    playlists_elem = SubElement(root, "PLAYLISTS")
    root_playlist = SubElement(playlists_elem, "NODE", Type="0", Name="ROOT", Count="0")

    track_id_map = {}
    next_id = 1

    for path, data in tqdm(tracks.items(), desc="(4/4) Adding tracks"):
        track_id_map[path] = next_id

        # Build file URI
        if platform.system() == "Windows":
            uri_path = path.replace("\\", "/")
            if re.match(r"^[A-Za-z]:", uri_path):
                uri_path = "/" + uri_path
            uri = "file://localhost" + urllib.parse.quote(uri_path)
        else:
            uri = "file://localhost/" + urllib.parse.quote(path.lstrip("/"))

        ext = os.path.splitext(path)[1].lower()
        kind = KIND_MAP.get(ext, "File")

        tr = SubElement(
            collection, "TRACK",
            TrackID=str(next_id),
            Name=data["title"].strip(),
            Artist=data["artist"].strip(),
            Kind=kind,
            Location=uri,
            AverageBpm=f"{data['bpm']:.2f}",
            Tonality=data["key"],
            TotalTime=f"{data['totalTime_sec']:.3f}",
        )

        is_m4a = ext in (".m4a", ".alac")
        sr = data.get("sample_rate", 0)
        delay = (2 * 1024 / sr) if (is_m4a and sr) else 0.0

        # Beatgrid TEMPO elements
        raw_grid = data.get("beatgrid")
        seg_positions, seg_bpms = [], []

        if isinstance(raw_grid, dict):
            markers = raw_grid.get("markers", {})
            non_term = markers.get("non_terminal") or []
            terminal = markers.get("terminal")

            if terminal:
                for i, nt in enumerate(non_term):
                    pos = float(nt["position"])
                    nxt = float(non_term[i + 1]["position"]) if i + 1 < len(non_term) else float(terminal["position"])
                    beats = nt.get("beats_till_next_marker", 0)
                    dur = nxt - pos
                    seg_bpms.append((beats * 60.0 / dur) if dur > 0 else data["bpm"])
                    seg_positions.append(pos)

                seg_positions.append(float(terminal["position"]))
                seg_bpms.append(float(terminal.get("bpm", data["bpm"])))
            else:
                seg_positions, seg_bpms = [data.get("first_beat_pos_sec") or 0.0], [data["bpm"]]
        elif isinstance(raw_grid, list) and raw_grid:
            seg_positions, seg_bpms = [float(raw_grid[0])], [data["bpm"]]
        else:
            seg_positions, seg_bpms = [0.0], [data["bpm"]]

        for pos, bpm_val in zip(seg_positions, seg_bpms):
            if is_m4a:
                pos += M4A_BEATGRID_OFFSET
            pos += delay / 1000.0
            SubElement(tr, "TEMPO", Inizio=f"{pos:.3f}", Bpm=f"{bpm_val:.2f}", Battito="1")

        # Hot cue POSITION_MARK elements
        for cue in data.get("hot_cues", []):
            sec = cue["position_ms"] / 1000.0
            if is_m4a:
                sec += M4A_HOTCUE_OFFSET
            r, g, b = (int(cue["color"][i : i + 2], 16) for i in (1, 3, 5))
            SubElement(
                tr, "POSITION_MARK",
                Name=cue["name"], Type="0",
                Start=f"{sec:.3f}", Num=str(cue["index"]),
                Red=str(r), Green=str(g), Blue=str(b),
            )

        next_id += 1

    root_playlist.set("Count", str(len(playlists)))

    for plist_name, plist_tracks in playlists.items():
        pnode = SubElement(
            root_playlist, "NODE",
            Name=plist_name, Type="1", KeyType="0", Entries=str(len(plist_tracks)),
        )
        for t in plist_tracks:
            tid = track_id_map.get(t["file_location"])
            if tid:
                SubElement(pnode, "TRACK", Key=str(tid))

    xml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serato2rekordbox.xml")
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write(prettify(root))

    return xml_path


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Convert a Serato DJ library to Rekordbox XML.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Everything on a USB drive — one command:
  python serato2rekordbox.py /Volumes/Gianmarco

  # Auto-detect Serato folder on this Mac:
  python serato2rekordbox.py

  # Serato metadata in one place, music files on a USB:
  python serato2rekordbox.py --serato ~/Music/_Serato_ --music /Volumes/USB

  # Skip crate selection and convert everything:
  python serato2rekordbox.py /Volumes/Gianmarco --skip-crate-selection

  # Custom output file:
  python serato2rekordbox.py /Volumes/Gianmarco --output ~/Desktop/my_library.xml
""",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Single path to a USB drive or folder containing both "
             "_Serato_/ (metadata) and your music files. "
             "If used, --serato and --music are auto-detected from this path.",
    )
    parser.add_argument(
        "--serato",
        default=None,
        help="Path to the Serato library folder (contains subcrates/). "
             "Auto-detected when using positional <root>, otherwise defaults "
             "to ~/Music/_Serato_.",
    )
    parser.add_argument(
        "--music",
        default=None,
        help="Root path of the music files (e.g. a USB folder with songs). "
             "Auto-detected when using positional <root>, otherwise defaults "
             "to the Serato folder (original library location).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output XML file path. Defaults to serato2rekordbox.xml beside this script.",
    )
    parser.add_argument(
        "--skip-crate-selection",
        action="store_true",
        help="Skip the interactive crate selector and import all crates "
             "(default behaviour when omitted is to open the selector).",
    )

    args = parser.parse_args()

    # Check for updates (non-blocking) — skip for local/dev builds
    if not _IS_LOCAL_BUILD:
        check_for_updates()

    # ── Resolve paths ────────────────────────────────────────────────────

    serato_path = args.serato
    music_root = args.music

    # If a positional root was given, auto-detect serato + music from it
    if args.root:
        root = os.path.expanduser(args.root)
        if not os.path.isdir(root):
            print(f"Error: Root path not found: {root}")
            return

        # Find _Serato_ inside root (or root itself if it has subcrates/)
        if not serato_path:
            serato_in_root = os.path.join(root, "_Serato_")
            if os.path.isdir(serato_in_root):
                serato_path = serato_in_root
            elif os.path.isdir(os.path.join(root, "subcrates")):
                serato_path = root

        # Find music folders: all top-level dirs in root except _Serato_,
        # _Serato_Backup, and hidden/system dirs
        if not music_root:
            excluded = {"_Serato_", "_Serato_Backup", ".Spotlight-V100",
                        ".fseventsd", "Contents", "System Volume Information",
                        ".Trashes", ".com.apple.timemachine.donotpresent"}
            candidates = []
            try:
                for entry in os.scandir(root):
                    if entry.is_dir() and entry.name not in excluded and not entry.name.startswith("."):
                        candidates.append(entry.path)
            except PermissionError:
                pass

            if candidates:
                # Build cache from all candidate dirs (union)
                _combined_cache = {}
                _music_labels = []
                for c in sorted(candidates):
                    _music_labels.append(os.path.basename(c))
                    _combined_cache.update(build_path_cache(c))
                # Temporarily stash; we'll rebuild properly below
                music_root = root  # signal to build from all siblings
                print(f"Found music folders in {root}: {', '.join(_music_labels)}")
            else:
                music_root = root
    else:
        serato_path = serato_path or find_serato_folder()

    if not serato_path or not os.path.isdir(serato_path):
        print("Error: Serato folder not found. Specify the path or use:")
        print("  python serato2rekordbox.py /Volumes/YourUSB")
        return

    if not music_root:
        music_root = serato_path

    print(f"Serato library: {serato_path}")
    if music_root != serato_path:
        print(f"Music files root: {music_root} (path remapping enabled)")
    else:
        print("Music files: same location as Serato library")

    subcrates_path = os.path.join(serato_path, "subcrates")

    # ── Find crates ──────────────────────────────────────────────────────

    crate_paths = find_serato_crates(subcrates_path)
    if not crate_paths:
        print("No .crate files found. Nothing to do.")
        return

    # Build (path, display_name) list for selector
    crate_entries = [
        (p, format_crate_name(os.path.basename(p)[:-6]))
        for p in crate_paths
    ]

    # ── Crate selection ──────────────────────────────────────────────────

    if not args.skip_crate_selection:
        if platform.system() == "Windows":
            # curses is unreliable on Windows — use plain-text fallback
            selected = select_crates_fallback(crate_entries)
        else:
            try:
                selected = curses.wrapper(select_crates, crate_entries)
            except Exception as e:
                # curses failed (e.g. macOS Secure Terminal) — use fallback
                print(f"(curses unavailable: {e} — using simple selector)")
                selected = select_crates_fallback(crate_entries)

        if selected is None:
            print("Aborted by user.")
            return
        if not selected:
            print("No crates selected. Nothing to do.")
            return
        print(f"\nConverting {len(selected)} crate(s)...\n")
        crate_entries_to_process = selected
    else:
        crate_entries_to_process = crate_entries

    # ── Phase 1: Read crate contents ─────────────────────────────────────

    # Build a fast filename → resolved path cache
    # When a positional root was given and --music wasn't set, scan all
    # sibling folders (excluding _Serato_, hidden dirs, etc.)
    excluded_dirs = {"_Serato_", "_Serato_Backup", ".Spotlight-V100",
                     ".fseventsd", "Contents", "System Volume Information",
                     ".Trashes", ".com.apple.timemachine.donotpresent"}
    if args.root and not args.music:
        path_cache = {}
        try:
            for entry in os.scandir(args.root):
                if entry.is_dir() and entry.name not in excluded_dirs and not entry.name.startswith("."):
                    path_cache.update(build_path_cache(entry.path))
        except PermissionError:
            pass
    else:
        path_cache = build_path_cache(music_root)

    if path_cache:
        print(f"Path cache built: {len(path_cache)} file(s) indexed\n")

    track_to_crates = defaultdict(list)
    all_track_paths = set()
    crate_errors = []

    for crate_path, display_name in tqdm(crate_entries_to_process, desc="(1/4) Reading crates"):
        raw_paths, errors = extract_file_paths_from_crate(crate_path)
        crate_errors.extend(errors)

        for raw in raw_paths:
            resolved = resolve_track_path(raw, music_root, path_cache)
            if resolved:
                track_to_crates[resolved].append(display_name)
                all_track_paths.add(resolved)

    if not all_track_paths:
        print("No tracks resolved from selected crates. Check --music path.")
        return

    # ── Phase 2: Extract metadata ────────────────────────────────────────

    all_tracks = {}
    errors = list(crate_errors)

    for path in tqdm(all_track_paths, desc="(2/4) Processing tracks"):
        ext = os.path.splitext(path)[1].lower()
        if ext not in EXTRACTOR_MAP:
            errors.append(("unsupported_format", path, f"Unsupported format: {ext}"))
            continue

        try:
            extracted = EXTRACTOR_MAP[ext].extract_metadata(path)
        except Exception as e:
            errors.append(("processing_error", path, str(e)))
            continue

        meta = extracted.get("metadata", {})
        all_tracks[path] = {
            "file_location": path,
            "title": meta.get("title", os.path.basename(path)),
            "artist": meta.get("artist", "Unknown Artist"),
            "bpm": meta.get("bpm", 0.0),
            "key": meta.get("key", "Unknown"),
            "totalTime_sec": meta.get("duration_sec", 0),
            "hot_cues": extracted.get("hot_cues", []),
            "beatgrid": extracted.get("beatgrid"),
            "sample_rate": meta.get("sample_rate", 0),
        }

    # ── Phase 3: Build playlists (preserving crate order) ────────────────

    playlists: "OrderedDict[str, list]" = OrderedDict()

    for crate_path, display_name in tqdm(
        [c for c in crate_entries_to_process if c[1] in
         {dn for _, dn in crate_entries_to_process}],
        desc="(3/4) Structuring playlists",
    ):
        playlists[display_name] = []
        raw_paths, _ = extract_file_paths_from_crate(crate_path)

        for raw in raw_paths:
            resolved = resolve_track_path(raw, music_root, path_cache)
            if resolved and resolved in all_tracks:
                playlists[display_name].append(all_tracks[resolved])

    # Drop empty crates
    playlists = {n: t for n, t in playlists.items() if t}

    # ── Phase 4: Generate XML ────────────────────────────────────────────

    if not playlists:
        print("\nNo tracks successfully processed. XML not generated.")
        return

    xml_path = generate_rekordbox_xml(playlists, all_tracks)

    # ── Summary ──────────────────────────────────────────────────────────

    print()
    print(f"Found {len(all_track_paths)} unique track(s) across selected crates.")
    print(f"{len(all_tracks)} / {len(all_track_paths)} track(s) converted.")

    if args.output:
        # Move XML to requested path
        import shutil
        dst = os.path.abspath(args.output)
        shutil.move(xml_path, dst)
        print(f"XML written to: {dst}")
    else:
        print(f"XML written to: {xml_path}")

    print()

    if errors:
        print(f"{len(errors)} issue(s) encountered:")

        grouped = defaultdict(list)
        for err_type, err_path, err_msg in errors:
            grouped[err_type].append((err_path, err_msg))

        titles = {
            "file_not_found": "Files Not Found",
            "unsupported_format": "Unsupported Formats",
            "processing_error": "Processing Errors",
            "parse_eof": "Crate Parse Errors",
            "parse_overflow": "Crate Parse Errors",
            "decode": "Crate Decode Errors",
            "read": "Crate Read Errors",
        }

        for etype in sorted(grouped, key=lambda t: list(titles.keys()).index(t) if t in titles else 999):
            print(f"\n  {titles.get(etype, etype)}:")
            for epath, emsg in grouped[etype]:
                if etype in ("file_not_found", "unsupported_format", "processing_error"):
                    fname = os.path.basename(epath)
                    crates = track_to_crates.get(epath, ["?"])
                    print(f"    - {fname} [{', '.join(crates)}]: {emsg}")
                else:
                    crate_fname = os.path.basename(epath)
                    print(f"    - Crate {crate_fname}: {emsg}")
    else:
        print("All tracks processed successfully.")


if __name__ == "__main__":
    main()
