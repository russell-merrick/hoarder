# Hoarder

A sample browser that works on the files you already have, for the files already on your disk. Downloaded packs and any other folders you add show up in the same list.

Hoarder only reads those folders. It never moves, renames, or deletes the audio.

## Run

Needs Python 3.12 and [ffmpeg](https://ffmpeg.org/) on `PATH` (pitch and tempo use it).

```powershell
python -m pip install -r requirements.txt
python -m hoarder
```

Or run `run.bat`. Then open http://127.0.0.1:8770

List your sample folders in `roots.json` first. Copy `roots.example.json` to `roots.json` and change the paths. Keep the ids: `downloads`, `curated` and `user_library` decide which copy of a duplicate is shown. Leave project folders out. A folder that is offline is skipped; its files stay in the catalog until the drive is back and you rescan.

## What you can do

- Search by words in the name, folder or pack, in any order. "techno kick" finds kicks in a Hard Techno pack.
- Filter with sample tags (kick, clap, perc, bass, 808, pad, keys, loop, and so on) and genre tags. Genre chips cycle include, exclude, off. Several tags together mean all of them.
- Limit by length, BPM and key, and sort by name, BPM, length or pack.
- Preview in the browser. Down and up arrows play the next and previous sample. Space plays and pauses. `/` focuses search.
- Drag a row into Ableton. The file on disk stays as it is.
- Transpose to a key without changing the sample's tempo. Pick major or minor. A relative key already fits, so an A minor loop in C major doesn't move. If Ableton has a saved set, loops with a BPM also match that set's tempo.
- Use the gear for theme, hiding duplicate filenames, and rescan.
- Right-click a row and choose Open in Explorer.

Tempo comes from the filename: "125bpm", "125 BPM", or a bare 125 on a loop. Key comes from the filename or folder: "Am", "Amin", "A minor". A lone capital letter on a drum is read as a take letter, not a key.

The catalog is a local SQLite file, `library.db`, created on first run. It is not in this repo, and neither is `roots.json`.
