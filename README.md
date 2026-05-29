# serato2rekordbox v1.4

This command line tool converts your Serato DJ Pro library (playlists, tracks, metadata, beatgrids, and hot cues) into a Rekordbox XML file that can be imported into Rekordbox DJ (tested on 6.8.5 and should work on older/newer versions) and can be exported easily to a USB or just used in Rekordbox in HID mode.

## Changelog

### v1.4:

- **FLAC hot cues and beatgrids** -- Serato writes hot cues and beatgrids as double-base64-encoded Vorbis comments (`SERATO_MARKERS_V2`, `SERATO_BEATGRID`) in FLAC files. The extractor now correctly decodes these, matching the upstream staddle/serato2rekordbox implementation. ID3 GEOB frames are tried as a fallback for third-party tools
- **USB drive / path remapping support** -- point at a USB drive containing both `_Serato_/` and your music files with a single path; the script auto-discovers crates and remaps track paths from the original library location to the USB
- **Interactive crate selector** -- pick which crates to convert before processing (curses-based on macOS/Linux, text-based on Windows)
- **New format support** -- `.flac`, `.alac`, `.aiff` files are now fully supported (metadata, hot cues, beatgrids)
- **CLI arguments** -- `--serato`, `--music`, `--output`, `--skip-crate-selection` for full control without editing code
- **Missing beatgrid is no longer fatal** -- tracks without Serato beatgrid data are still included in the XML (Rekordbox will analyse on import)
- **FLAC metadata fix** -- ID3 tags are now loaded correctly (previously crashed on all `.flac` files)
- **Code cleanup** -- removed unused imports, consolidated format dispatch, improved error messages

### v1.3:

- Fixed issue where subcrates inside subcrates were not processed at all
- Added auto update checker

### v1.2:

- Playlist song order now the same as in Serato
- `.wav` files now supported

### v1.1:

- Optimised code
- Fixed hot cue names not being correctly processed
- Fixed issue where hot cue slot 1 was never filled
- Fixed issue where key was not properly displayed
- Fixed issues with beatgrids in `.m4a` files (using fixed offset value)

### v1.0:

- Initial release

## Why this was developed

As a DJ who mostly uses Serato in HID mode with my laptop, it's still beneficial to have a working USB so I can go along to a gig with just a USB and headphones, or just to have it as a backup. I only use Serato and don't like to use Rekordbox in general but Serato doesn't have an easy way of exporting tracks to a USB.

I couldn't find any other free software that could convert it so I decided to make my own.

## Features

*   **Playlist Conversion:** Converts your Serato crates into Rekordbox playlists.
*   **Track Metadata:** Transfers essential metadata including Title, Artist, BPM, and Key.
*   **Hot Cue Transfer:** Extracts and transfers hot cues.
*   **Accurate Beatgrids:** Extracts the Serato beatgrid data directly from the audio files to extract the *first beat position* from the audio file's beatgrid data and includes it in the XML. This tells Rekordbox exactly where the first beat is, allowing it to correctly align the entire beatgrid without needing to re-analyse it itself.
*   **USB Drive Support:** Run the script against a USB drive that contains both the Serato metadata folder and your music files. Track paths stored in the original library location are automatically remapped to the USB via a fast filename index.
*   **Interactive Crate Selector:** Choose which crates to convert via an in-terminal checkbox UI (macOS/Linux) or numbered menu (Windows). Skip with `--skip-crate-selection`.
*   **Automatic Serato Folder Detection:** Automatically attempts to find your Serato `_Serato_` folder on standard Windows, macOS and Linux locations, or from within a USB drive root.
*   **Detailed Error Reporting:** Collects and reports errors (missing files, unsupported formats, processing errors, crate reading issues) in a clear, grouped summary at the end. Failed tracks are excluded from the output XML.
*   **File Support:** Supports conversion for `.mp3`, `.m4a`, `.alac`, `.wav`, `.flac`, and `.aiff` audio files.
*   Normal crates and subcrates are supported.

## Prerequisites

Before running the script, ensure you have the following:

1.  **Python 3:** The script is written in Python 3. You can download it from [python.org](https://python.org/downloads/).
2.  **Serato DJ Pro:** Serato must be installed and run at least once on the computer where you run this script (or your Serato metadata folder must be available on a USB drive).
3.  **Rekordbox DJ:** You will need Rekordbox DJ (version 6 recommended) to import the generated XML file.

## Installation

1.  **Clone or Download:** Get the script files. You can clone the repository by running:
    ```bash
    git clone https://github.com/BytePhoenixCoding/serato2rekordbox
    cd serato2rekordbox
    ```
    Or download the ZIP file and extract it.

2.  **Install Dependencies:** Open your terminal or command prompt, navigate to the script's directory, and install the required Python packages:
    ```bash
    pip install tqdm mutagen
    ```

## Usage

### Quick start (auto-detect everything)

If your Serato library is in the default location and music files are where Serato expects them:

```bash
python3 serato2rekordbox.py
```

### USB drive (one command)

Your USB drive contains `_Serato_/` (metadata) plus your music folders:

```bash
python3 serato2rekordbox.py /Volumes/YourUSB
```

The script will:
1.  Find `_Serato_/` inside the USB for crate files
2.  Index all music files in sibling folders (skipping `_Serato_`, `_Serato_Backup`, system folders)
3.  Remap crate paths from the original library location to the USB via filename lookup
4.  Open the crate selector so you can pick what to convert

### Separating Serato metadata from music files

```bash
python3 serato2rekordbox.py --serato ~/Music/_Serato_ --music /Volumes/USB/Music
```

### Command-line options

| Flag | Description |
|---|---|
| `root` | (positional) Path to a USB drive or folder containing both `_Serato_/` and music files |
| `--serato PATH` | Path to the Serato library folder (contains `subcrates/`). Auto-detected when using positional `root`; otherwise defaults to `~/Music/_Serato_` |
| `--music PATH` | Root path of the music files. Auto-detected when using positional `root`; otherwise defaults to the Serato folder |
| `--output PATH` | Output XML file path. Defaults to `serato2rekordbox.xml` beside the script |
| `--skip-crate-selection` | Skip the interactive crate selector and import all crates |

### Crate selector

On macOS and Linux a full-screen checkbox interface opens:
- **Arrow keys** navigate, **Space** toggles, **A** selects all, **N** deselects all, **Enter** confirms, **q** cancels

On Windows (or when curses is unavailable) a numbered menu appears:
- Type comma-separated numbers to toggle crates (e.g. `1,3,5`), **a** for all, **n** for none, **Enter** to confirm, **q** to cancel

Press **Enter** immediately to accept all crates and skip selection.

### Output

After completion the script prints a summary of successful / unsuccessful conversions. The output file `serato2rekordbox.xml` is generated in the script's directory (or at the path specified by `--output`).

## Importing into Rekordbox

Once `serato2rekordbox.xml` is generated:

1.  Open Rekordbox DJ.
2.  **Add your tracks first** -- drag the music folders from your USB into Rekordbox so the tracks appear in the library. This is required because Rekordbox only resolves playlist references for tracks it already knows about.
3.  Go to Settings (Gear icon at top) > Advanced > rekordbox xml and select the generated XML file.
4.  In the sidebar, the imported playlists will appear under the **Collection** tab. Drag them onto your USB export and wait for Rekordbox to finish processing.

Rekordbox will import the playlists with their track ordering, Hot Cues, and the accurate Beatgrids based on the first beat position provided in the XML. Rekordbox may still perform some background analysis (like waveform drawing), but it should respect the imported beatgrid and cue data.

## Limitations

*   This script primarily transfers playlists, basic metadata, hot cues, and the **first beat position** for the beatgrid. Other Serato-specific data like loops, specific track flags (e.g., played status) etc. may not work.
*   Some tracks may not have the correct beatgrid data or key.
*   Smart crates are not supported.
*   Some beatgrids in Rekordbox appear to be slightly off beat even though perfectly on beat in Serato.
*   There may be occasional tracks which are not correctly processed for some unknown reason, however across my library of almost 4000 tracks there's only been a handful which have been problematic for me.

## Notes

*   The script has been tested on my own Serato library which contains almost 4000 tracks and performs as intended. It was able to process the entire library in around 20 seconds and rekordbox exporting to my USB (HFS+) only took around 15 minutes.
*   Rekordbox 6.8.5 is recommended at the moment as Rekordbox 7 is reported to export to USB much slower.
*   `HFS+` seems to be the fastest USB file format but `FAT32` is more compatible with older Pioneer hardware.

## Future improvements

- Make a GUI?
- I thought about trying to reverse engineer the USB export structure so the program could directly export to a USB itself without needing Rekordbox at all, however the USB structure (analysis, database etc) is extremely complex, would require a lot of effort and likely wouldn't be as reliable.

## Contributing

If you find issues or have ideas for improvements, please feel free to open an issue or submit a pull request.
