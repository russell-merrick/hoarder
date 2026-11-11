# Hoarder

A sample browser that works like Splice, for the files already on your disk. Splice downloads and any other folders you add show up in the same list.

Hoarder only reads those folders. It never moves, renames, or deletes the audio.

## Run

Needs Python 3.12 and [ffmpeg](https://ffmpeg.org/) on `PATH` (pitch and tempo use it).

```powershell
python -m pip install -r requirements.txt
python -m hoarder
```

Or run `run.bat`. Then open http://127.0.0.1:8770

Point it at your own library by editing `DEFAULT_ROOTS` in `hoarder/roots.py`, then restart. Leave project folders out of that list. A folder that is offline is skipped; its files stay in the catalog until the drive is back and you rescan.

## What you can do

- Search by filename.
- Filter with sample tags (kick, hat, bass, loop, and so on) and genre tags. Genre chips cycle include, exclude, off. Several tags together mean all of them.
- Limit by length.
- Preview in the browser. Down and up arrows play the next and previous sample. Space plays and pauses. `/` focuses search.
- Drag a row into Ableton. The file on disk stays as it is.
- Transpose to a key without changing the sample's tempo. If the filename has a BPM and Ableton has a saved set, Hoarder also matches that set's tempo.
- Use the gear for theme, hiding duplicate filenames, and rescan.
- Right-click a row and choose Open in Explorer.

The catalog is a local SQLite file, `library.db`, created on first run. It is not in this repo.
