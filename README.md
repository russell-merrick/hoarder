# Hoarder

A sample browser for the files already on your disk. Downloaded packs and any other folders you add show up in the same list.

Hoarder only reads those folders. It never moves, renames, or deletes the audio.

## Run

Needs Python 3.12 and [ffmpeg](https://ffmpeg.org/) on `PATH` (pitch and tempo use it).

```powershell
python -m pip install -r requirements.txt
python -m hoarder
```

Or run `run.bat`, then open http://127.0.0.1:8770. The Hoarder shortcut on the desktop starts the server if it is not already running and opens the page in its own window.

On a Mac, double-click `Hoarder.app` in this folder. It starts the server the same way and opens an app window in Microsoft Edge or Google Chrome. The first launch may need a right-click and Open. If macOS says the app is damaged, run `chmod +x Hoarder.app/Contents/MacOS/launcher`.

List your sample folders in `roots.json` first. Copy `roots.example.json` to `roots.json` and change the paths. Keep the ids: `downloads`, `curated` and `user_library` decide which copy of a duplicate is shown. `downloads` wins over `curated`, and both win over any other folder. Leave project folders out. A folder that is offline is skipped; its files stay in the catalog until the drive is back and you rescan.

## What you can do

- Search by words in the name, folder or pack, in any order. "techno kick" finds kicks in a Hard Techno pack.
- Filter with sample tags (kick, clap, perc, bass, 808, pad, keys, loop, and so on) and genre tags. Click a tag under a sample to add that filter. Genre chips cycle include, exclude, off. Several tags together mean all of them.
- Limit by length, BPM and key. The File name, BPM, Key, and Time headings sort.
- Preview in the browser. Down and up arrows play the next and previous sample. Space plays and pauses. `/` focuses search.
- Drag a row into Ableton. The file on disk stays as it is.
- Transpose to a key without changing the sample's tempo. Pick major or minor. A relative key already fits, so an A minor loop in C major doesn't move. If Ableton has a saved set, loops with a BPM also match that set's tempo.
- Use the gear for theme, hiding duplicates, and rescan. A duplicate is the same filename and the same file size. With that on, the count is the samples you can search.
- Right-click a row and choose Open in Explorer.

Tempo comes from the filename: "125bpm", "125 BPM", or a bare 125 on a loop. Key comes from the filename or folder: "Am", "Amin", "A minor". A lone capital letter on a drum is read as a take letter, not a key.

The catalog is a local SQLite file, `library.db`, created on first run. It is not in this repo, and neither is `roots.json`.

## Future work

Sample tags are still a short list read from the filename and folder. Crash and ride both become Cymbal. Piano, organ, and Rhodes all become Keys.

A full instrument list is larger than the ten links on each family of a browse page. Keys is its own instrument. Crash and rides sit under cymbals. Bass, fx, fills, chords, leads, pads, and plucks are functional tags. Closed, open, and sub are sonic tags. There is no public classification API for that list.

Next step is to tag files with that fuller instrument list, and keep functional and sonic tags separate, still from the filename and folder.

Skins, later. A person should be able to make their own look for the page: a background image, plus colors for the panels, text, and tags. The built-in light and dark themes stay. A skin does not change search, tags, or playback.
