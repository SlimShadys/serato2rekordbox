import base64
import io
import logging
import re
import struct

from mutagen.flac import FLAC

from utils import convert_key_to_camelot

# ── Vorbis comment value extraction ──────────────────────────────────────────


def get_vorbis_value(vc, key):
    """Safely get a single Vorbis comment value, always returned as bytes."""
    if key not in vc:
        return None
    vals = vc[key]
    if not vals:
        return None
    val = vals[0]
    if hasattr(val, "data"):
        val = val.data
    if isinstance(val, str):
        return val.encode("utf-8")
    return val


# ── Double-base64 decode matching staddle/serato2rekordbox ───────────────────


def _first_decode(raw_value: bytes) -> bytes:
    """Run the first base64 layer and strip the MIME / descriptor header.

    Returns the inner base64-encoded payload (still as raw bytes).
    """
    # Clean — remove '=' entirely so _pad_base64 adds it only in valid positions
    clean = re.sub(rb"[^a-zA-Z0-9+/]", b"", raw_value)
    padded = _pad_base64(clean)

    data = base64.b64decode(padded)

    if not data.startswith(b"application/octet-stream\x00"):
        raise ValueError("Missing MIME header")

    fieldname_endpos = data.index(b"\x00", 26)
    return data[fieldname_endpos + 1:]


def decode_serato_hot_cues_payload(raw_value: bytes) -> bytes:
    """Decode SERATO_MARKERS_V2 to raw binary cue data."""
    fielddata = _first_decode(raw_value)
    fielddata = fielddata.replace(b"\n", b"")

    # Skip the first 2 bytes (descriptor prefix) and take up to the next null
    null_pos = fielddata.find(b"\x00")
    inner_b64 = fielddata[2:null_pos] if null_pos >= 2 else b""

    try:
        return base64.b64decode(_pad_base64(inner_b64))
    except Exception as e1:
        logging.debug("Cue inner b64 decode failed, retrying truncated: %s", e1)
        truncated = inner_b64[: (len(inner_b64) // 4) * 4]
        return base64.b64decode(_pad_base64(truncated))


def decode_serato_beatgrid_payload(raw_value: bytes) -> bytes:
    """Decode SERATO_BEATGRID to raw binary beatgrid struct."""
    fielddata = _first_decode(raw_value)

    # Clean — remove '=' entirely so _pad_base64 adds it only in valid terminal positions
    clean = re.sub(rb"[^a-zA-Z0-9+/]", b"", fielddata)
    try:
        return base64.b64decode(_pad_base64(clean))
    except Exception:
        # Truncate to multiple-of-4 and retry
        truncated = clean[: (len(clean) // 4) * 4]
        return base64.b64decode(_pad_base64(truncated))


def _pad_base64(b64_bytes: bytes) -> bytes:
    """Pad base64 bytes, truncating to last valid 4-char boundary if needed."""
    missing = len(b64_bytes) % 4
    if missing:
        # 1 mod 4 is invalid for base64 — truncate the trailing byte
        if missing == 1:
            b64_bytes = b64_bytes[: len(b64_bytes) - 1]
            return b64_bytes  # now 0 mod 4
        b64_bytes += b"=" * (4 - missing)
    return b64_bytes


# ── Hot cue parsing ──────────────────────────────────────────────────────────


def parse_serato_hot_cues(binary_data: bytes) -> list:
    """Parse raw binary cue entries from the decoded payload.

    Format: version (2 bytes 0x01 0x01), then null-terminated entry name +
    4-byte big-endian length + entry data, repeated until empty name.
    """
    hot_cues = []
    fp = io.BytesIO(binary_data)

    try:
        version = struct.unpack("BB", fp.read(2))
    except struct.error:
        return hot_cues

    if version != (0x01, 0x01):
        # Maybe no version header — try simple null-separated parsing
        return _parse_hot_cues_simple(binary_data)

    while True:
        name_bytes = _read_null_terminated(fp)
        if not name_bytes:
            break
        name = name_bytes.decode("utf-8", errors="replace")

        try:
            entry_len = struct.unpack(">I", fp.read(4))[0]
        except struct.error:
            break

        if entry_len <= 0:
            break

        entry_data = fp.read(entry_len)

        if name == "CUE" and len(entry_data) >= 12:
            cue_index = entry_data[1]
            position_ms = struct.unpack(">I", entry_data[2:6])[0]
            r, g, b = entry_data[7:10]
            color = "#{:02X}{:02X}{:02X}".format(r, g, b)
            label = entry_data[12:].rstrip(b"\x00").decode("utf-8", errors="replace")

            hot_cues.append({
                "index": cue_index,
                "position_ms": position_ms,
                "color": color,
                "name": label,
            })

    return hot_cues


def _parse_hot_cues_simple(data: bytes) -> list:
    """Fallback: parse null-separated CUE entries without version header."""
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
                hot_cues.append({
                    "index": raw[1],
                    "position_ms": struct.unpack(">I", raw[2:6])[0],
                    "color": "#{:02X}{:02X}{:02X}".format(*raw[7:10]),
                    "name": raw[12:].rstrip(b"\x00").decode("utf-8", errors="replace"),
                })
        idx += entry_len

    return hot_cues


def _read_null_terminated(fp: io.BytesIO) -> bytes:
    chunks = []
    while True:
        b = fp.read(1)
        if not b or b == b"\x00":
            break
        chunks.append(b)
    return b"".join(chunks)


# ── Beatgrid parsing ─────────────────────────────────────────────────────────


def parse_beatgrid(binary_data: bytes) -> dict:
    """Parse the Serato BeatGrid binary struct.

    Format: version (2 bytes), marker count (4 bytes BE), markers (8 bytes
    each for non-terminal, 8 bytes for terminal), trailing byte.
    """
    result = {"markers": {"non_terminal": [], "terminal": None}}
    fp = io.BytesIO(binary_data)

    try:
        version = struct.unpack("BB", fp.read(2))
        num_markers = struct.unpack(">I", fp.read(4))[0]
    except struct.error:
        return result

    if version != (0x01, 0x00):
        logging.warning("Unsupported beatgrid version: %s", version)

    markers = []
    for i in range(num_markers):
        pos_bytes = fp.read(4)
        data_bytes = fp.read(4)
        if len(pos_bytes) < 4 or len(data_bytes) < 4:
            break
        pos = struct.unpack(">f", pos_bytes)[0]
        if i == num_markers - 1:
            bpm = struct.unpack(">f", data_bytes)[0]
            markers.append(("terminal", pos, bpm))
        else:
            beats = struct.unpack(">I", data_bytes)[0]
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


# ── ID3 GEOB fallback ────────────────────────────────────────────────────────


def try_id3_geob(file_path: str, need_cues: bool, need_beatgrid: bool) -> tuple:
    """Try to read Serato hot cues and beatgrid from ID3 GEOB frames in a FLAC.

    Returns (hot_cues_list, beatgrid_dict).
    """
    hot_cues = []
    beatgrid = {"markers": {"non_terminal": [], "terminal": None}}

    try:
        from mutagen.id3 import ID3, GEOB

        id3_tags = ID3(file_path)
    except Exception:
        return hot_cues, beatgrid

    for tag in id3_tags.values():
        if not isinstance(tag, GEOB):
            continue
        desc = getattr(tag, "desc", "")
        if desc == "Serato Markers2" and need_cues:
            hot_cues = _parse_geob_markers(tag.data)
        elif desc == "Serato BeatGrid" and need_beatgrid:
            beatgrid = parse_beatgrid(tag.data)

    return hot_cues, beatgrid


def _parse_geob_markers(raw_data: bytes) -> list:
    """Parse GEOB Serato Markers2 data (single base64, no MIME wrapper)."""
    clean = re.sub(rb"[^a-zA-Z0-9+/]", b"", raw_data)
    clean = clean.rstrip(b"=")
    missing = len(clean) % 4
    if missing:
        clean += b"=" * (4 - missing)

    try:
        data = base64.b64decode(clean)
    except Exception:
        return []

    return _parse_hot_cues_simple(data)


# ── Main entry point ─────────────────────────────────────────────────────────


def extract_metadata(file_path: str) -> dict:
    """Extract metadata, hot cues, and beatgrid from a FLAC file."""
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
        audio = FLAC(file_path)
    except Exception as e:
        logging.error("Failed to open FLAC file '%s': %s", file_path, e)
        return results

    if audio.info:
        results["metadata"]["duration_sec"] = round(audio.info.length, 3)
        results["metadata"]["sample_rate"] = audio.info.sample_rate or 0

    # ── Vorbis comments ──────────────────────────────────────────────────

    vc = audio.tags
    if vc:
        if "TITLE" in vc:
            results["metadata"]["title"] = str(vc["TITLE"][0]).strip()
        if "ARTIST" in vc:
            results["metadata"]["artist"] = str(vc["ARTIST"][0]).strip()
        if "BPM" in vc:
            try:
                results["metadata"]["bpm"] = float(vc["BPM"][0])
            except (ValueError, TypeError, IndexError):
                pass
        if results["metadata"]["key"] == "Unknown":
            for key_tag in ("KEY", "INITIALKEY", "MUSICBRAINZ_TRACKKEY", "TBPM_KEY"):
                if key_tag in vc:
                    results["metadata"]["key"] = convert_key_to_camelot(str(vc[key_tag][0]))
                    break

        # Hot cues
        raw = get_vorbis_value(vc, "SERATO_MARKERS_V2")
        if raw is not None:
            try:
                binary = decode_serato_hot_cues_payload(raw)
                cues = parse_serato_hot_cues(binary)
                if cues:
                    results["hot_cues"] = cues
            except Exception as e:
                logging.warning("Failed to decode hot cues from '%s': %s", file_path, e)

        # Beatgrid
        raw = get_vorbis_value(vc, "SERATO_BEATGRID")
        if raw is not None and results["beatgrid"]["markers"]["terminal"] is None:
            try:
                binary = decode_serato_beatgrid_payload(raw)
                bg = parse_beatgrid(binary)
                if bg["markers"]["terminal"] is not None:
                    results["beatgrid"] = bg
            except Exception as e:
                logging.warning("Failed to decode beatgrid from '%s': %s", file_path, e)

    # ── ID3 GEOB fallback (only if Vorbis didn't provide cues or beatgrid) ─

    need_cues = not results["hot_cues"]
    need_bg = results["beatgrid"]["markers"]["terminal"] is None

    has_id3 = False
    try:
        from mutagen.id3 import ID3

        ID3(file_path)
        has_id3 = True
    except Exception:
        pass

    if has_id3 and (need_cues or need_bg):
        cues, bg = try_id3_geob(file_path, need_cues, need_bg)
        if cues:
            results["hot_cues"] = cues
        if bg["markers"]["terminal"] is not None:
            results["beatgrid"] = bg

    # Fill missing basic metadata from ID3 if Vorbis was empty
    if has_id3 and (results["metadata"]["title"] == "Unknown" or results["metadata"]["artist"] == "Unknown"):
        try:
            from mutagen.id3 import ID3

            id3_tags = ID3(file_path)
            if results["metadata"]["title"] == "Unknown":
                tit2 = id3_tags.get("TIT2")
                if tit2 and hasattr(tit2, "text") and tit2.text:
                    results["metadata"]["title"] = str(tit2.text[0]).strip()
            if results["metadata"]["artist"] == "Unknown":
                tpe1 = id3_tags.get("TPE1")
                if tpe1 and hasattr(tpe1, "text") and tpe1.text:
                    results["metadata"]["artist"] = str(tpe1.text[0]).strip()
            if results["metadata"]["bpm"] == 0.0:
                tbpm = id3_tags.get("TBPM")
                if tbpm and hasattr(tbpm, "text") and tbpm.text:
                    try:
                        results["metadata"]["bpm"] = float(str(tbpm.text[0]).strip())
                    except (ValueError, TypeError):
                        pass
            if results["metadata"]["key"] == "Unknown":
                tkey = id3_tags.get("TKEY")
                if tkey and hasattr(tkey, "text") and tkey.text:
                    results["metadata"]["key"] = convert_key_to_camelot(str(tkey.text[0]).strip())
        except Exception:
            pass

    return results
