import io
import logging
import struct

import mutagen
from mutagen.aiff import AIFF

from utils import convert_key_to_camelot


def extract_metadata(file_path: str) -> dict:
    """Extract metadata, hot cues, and beatgrid from an AIFF file.

    Serato embeds ID3v2 tags in AIFF files (same GEOB structure as WAV/FLAC).
    """
    results = {
        "metadata": {
            "title": "Unknown",
            "artist": "Unknown",
            "bpm": 0.0,
            "key": "Unknown",
            "duration_sec": 0.0,
            "sample_rate": 0,
        },
        "hot_cues": [],
        "beatgrid": {"markers": {"non_terminal": [], "terminal": None}},
    }

    try:
        audio = AIFF(file_path)
    except Exception as e:
        logging.error("Failed to open AIFF file '%s': %s", file_path, e)
        return results

    # Audio info
    if audio.info:
        results["metadata"]["duration_sec"] = round(audio.info.length, 3)
        results["metadata"]["sample_rate"] = audio.info.sample_rate or 0

    # Serato writes ID3 tags to AIFF — same structure as WAV
    tags = audio.tags
    if not tags:
        # Try loading as generic mutagen file for ID3
        try:
            tagfile = mutagen.File(file_path)
            if tagfile and tagfile.tags:
                tags = tagfile.tags
        except Exception:
            pass

    if tags:
        # Standard metadata
        tit2 = tags.get("TIT2")
        if tit2 and hasattr(tit2, "text") and tit2.text:
            results["metadata"]["title"] = str(tit2.text[0]).strip()

        tpe1 = tags.get("TPE1")
        if tpe1 and hasattr(tpe1, "text") and tpe1.text:
            results["metadata"]["artist"] = str(tpe1.text[0]).strip()

        tbpm = tags.get("TBPM")
        if tbpm and hasattr(tbpm, "text") and tbpm.text:
            try:
                results["metadata"]["bpm"] = float(str(tbpm.text[0]).strip())
            except (ValueError, TypeError):
                pass

        tkey = tags.get("TKEY", tags.get("initialkey"))
        if tkey and hasattr(tkey, "text") and tkey.text:
            results["metadata"]["key"] = convert_key_to_camelot(str(tkey.text[0]).strip())

        # Hot cues and beatgrid from GEOB frames
        from mutagen.id3 import GEOB

        for tag in tags.values():
            if isinstance(tag, GEOB):
                desc = getattr(tag, "desc", "")
                if desc == "Serato Markers2":
                    try:
                        results["hot_cues"] = parse_serato_hot_cues(tag.data)
                    except Exception as e:
                        logging.warning("Error reading hot cues from '%s': %s", file_path, e)
                elif desc == "Serato BeatGrid":
                    try:
                        results["beatgrid"] = parse_beatgrid(tag.data)
                    except Exception as e:
                        logging.warning("Error reading beatgrid from '%s': %s", file_path, e)

    return results


def parse_serato_hot_cues(base64_data: bytes) -> list:
    """Parse Serato Markers2 GEOB payload (shared with MP3/WAV/FLAC)."""
    import base64
    import re

    if isinstance(base64_data, str):
        base64_data = base64_data.encode("utf-8")

    clean_data = re.sub(rb"[^a-zA-Z0-9+/=]", b"", base64_data)
    padding_needed = 4 - (len(clean_data) % 4)
    if padding_needed != 4:
        clean_data += b"=" * padding_needed

    try:
        data = base64.b64decode(clean_data)
    except Exception:
        return []

    hot_cues = []
    idx = 0
    total = len(data)

    while idx < total:
        nxt = data[idx:].find(b"\x00")
        if nxt == -1:
            break

        entry_type = data[idx : idx + nxt].decode("utf-8", errors="replace")
        idx += nxt + 1

        if idx + 4 > total:
            break

        entry_len = struct.unpack(">I", data[idx : idx + 4])[0]
        idx += 4

        if entry_type == "CUE" and idx + entry_len <= total:
            raw = data[idx : idx + entry_len]
            if len(raw) >= 12:
                cue_index = raw[1]
                position_ms = struct.unpack(">I", raw[2:6])[0]
                r, g, b = raw[7:10]
                color = "#{:02X}{:02X}{:02X}".format(r, g, b)
                label = raw[12:].rstrip(b"\x00").decode("utf-8", errors="replace")

                hot_cues.append({
                    "index": cue_index,
                    "position_ms": position_ms,
                    "color": color,
                    "name": label,
                })

        idx += entry_len

    return hot_cues


def parse_beatgrid(raw_data: bytes) -> dict:
    """Parse Serato BeatGrid binary struct from a GEOB frame."""
    result = {"markers": {"non_terminal": [], "terminal": None}}

    fp = io.BytesIO(raw_data)
    version = struct.unpack("BB", fp.read(2))
    if version != (0x01, 0x00):
        logging.warning("Unsupported beatgrid version: %s", version)

    num_markers = struct.unpack(">I", fp.read(4))[0]
    markers = []

    for i in range(num_markers):
        pos = struct.unpack(">f", fp.read(4))[0]
        data = fp.read(4)
        if i == num_markers - 1:
            bpm = struct.unpack(">f", data)[0]
            markers.append(("terminal", pos, bpm))
        else:
            beats = struct.unpack(">I", data)[0]
            markers.append(("non_terminal", pos, beats))

    fp.read(1)  # trailing byte

    if not markers:
        return result

    non_terminal = []
    terminal = None
    for m in markers:
        if m[0] == "terminal":
            terminal = {"position": m[1], "bpm": m[2]}
        else:
            non_terminal.append({"position": m[1], "beats_till_next_marker": m[2]})

    result["markers"]["non_terminal"] = non_terminal
    result["markers"]["terminal"] = terminal
    return result
