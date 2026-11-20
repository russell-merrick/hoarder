"""Catalog filters, header probe, and the rule that files stay put."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from hoarder.app import _copy_name, _disposition, create_app, reveal_command
from hoarder.clipboard import clear_clipboard, copy_file, files_on_clipboard
from hoarder.dragdrop import DRAGDROP_S_CANCEL, DRAGDROP_S_DROP, MK_LBUTTON, make_data_object, make_drop_source, paths_from_hdrop
from hoarder.db import (
    backfill_roles,
    connect,
    init_db,
    mark_duplicates,
    query_samples,
    set_sample_tags,
    upsert_many,
)
from hoarder.keys import parse_key, semitone_shift
from hoarder import livebpm
from hoarder.livebpm import newest_log, parse_bpm, project_status, read_set_bpm, tempo_ratio
from hoarder.link import compose_preview, repeat_seconds
from hoarder.stretch import browser_wav, match_sample
from hoarder.probe import _extended80, probe, repitch_wav
from hoarder.genres import classify as classify_genre
from hoarder.genres import decode as decode_genre
from hoarder.roles import classify, decode
from hoarder.roots import Root, load_roots
from hoarder.scan import _run, pack_and_folder

REPO = Path(__file__).resolve().parent.parent


def _wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)


def _aiff(path: Path, sample: int = 1000) -> None:
    rate = bytes.fromhex("400EAC44000000000000")
    comm = struct.pack(">HIH", 1, 1, 16) + rate
    ssnd = struct.pack(">II", 0, 0) + struct.pack(">h", sample)

    def chunk(cid: bytes, body: bytes) -> bytes:
        pad = b"\x00" if len(body) % 2 else b""
        return cid + struct.pack(">I", len(body)) + body + pad

    body = chunk(b"COMM", comm) + chunk(b"SSND", ssnd)
    path.write_bytes(b"FORM" + struct.pack(">I", 4 + len(body)) + b"AIFF" + body)


def test_roots_come_from_the_config_file(tmp_path: Path) -> None:
    example = load_roots(REPO / "roots.example.json")
    assert {root.id for root in example} == {"curated", "downloads", "user_library"}
    for root in example:
        assert "projects" not in root.path.parts
    assert load_roots(tmp_path / "roots.json") == ()


def test_extended_sample_rate() -> None:
    rate = _extended80(bytes.fromhex("400EAC44000000000000"))
    assert abs(rate - 44100) < 0.01


def test_wav_duration(tmp_path: Path) -> None:
    path = tmp_path / "kick.wav"
    _wav(path, seconds=0.5, rate=8000)
    meta = probe(path)
    assert meta.sample_rate == 8000
    assert meta.channels == 1
    assert meta.bit_depth == 16
    assert meta.duration is not None
    assert abs(meta.duration - 0.5) < 0.01


def test_aiff_preview_does_not_touch_the_file(tmp_path: Path) -> None:
    path = tmp_path / "GrandPiano C3 f.aif"
    _aiff(path, sample=1000)
    before = path.stat().st_mtime_ns
    wav = browser_wav(path)
    assert path.stat().st_mtime_ns == before
    assert wav.startswith(b"RIFF")
    with wave.open(io.BytesIO(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 44100
        assert handle.getsampwidth() == 3
        frame = handle.readframes(1)
        assert int.from_bytes(frame, "little", signed=True) == 1000 << 8


def test_pack_name_skips_packs_dir(tmp_path: Path) -> None:
    root = tmp_path / "Samples"
    file = root / "packs" / "Hard Techno" / "hat.wav"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"")
    pack, folder = pack_and_folder(root, file)
    assert pack == "Hard Techno"
    assert folder.endswith("Hard Techno")


def test_scan_ignores_projects_and_drops_deleted_files(tmp_path: Path) -> None:
    library = tmp_path / "samples"
    _wav(library / "From Brian" / "AED1 Kicks" / "Heavy" / "heavy.wav", seconds=0.25)
    _wav(library / "Kick" / "kick.wav", seconds=1.5)
    (library / "notes.txt").write_text("nope", encoding="utf-8")
    _wav(tmp_path / "projects" / "song" / "Samples" / "Imported" / "bounce.wav")
    db = tmp_path / "library.db"
    roots = (Root("curated", "Samples", library),)
    _run(db, roots)
    conn = connect(db)
    try:
        total, rows = query_samples(conn)
        assert total == 2
        packs = {row["pack"] for row in rows}
        assert packs == {"From Brian", "Kick"}
        heavy = next(row for row in rows if row["name"] == "heavy.wav")
        assert abs(heavy["duration"] - 0.25) < 0.02
        assert "kick" in heavy["roles"].split()
        assert heavy["genres"] == ""
    finally:
        conn.close()

    (library / "Kick" / "kick.wav").unlink()
    _run(db, roots)
    conn = connect(db)
    try:
        total, rows = query_samples(conn)
        assert total == 1
        assert rows[0]["name"] == "heavy.wav"
        assert (library / "From Brian" / "AED1 Kicks" / "Heavy" / "heavy.wav").is_file()
    finally:
        conn.close()


def test_length_and_search_filters(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    conn = connect(db)
    try:
        init_db(db)
        upsert_many(
            conn,
            [
                _row("a.wav", "Kick", 0.4),
                _row("b.wav", "From Brian", 3.0),
                _row("100%_wet.wav", "Vocals", 12.0),
                _row("unknown.wav", "Kick", None),
            ],
        )
        total, rows = query_samples(conn, max_sec=0.5)
        assert [row["name"] for row in rows] == ["a.wav"]
        total, rows = query_samples(conn, min_sec=0.5, max_sec=8)
        assert total == 1 and rows[0]["name"] == "b.wav"
        total, rows = query_samples(conn, min_sec=30)
        assert total == 0
        total, rows = query_samples(conn, q="100%")
        assert total == 1 and rows[0]["name"] == "100%_wet.wav"
        total, _rows = query_samples(conn, q="%", pack="From Brian")
        assert total == 0
        total, rows = query_samples(conn, pack="From Brian")
        assert total == 1
    finally:
        conn.close()


def test_parse_key_and_semitone_shift() -> None:
    assert parse_key("005_Bass_Stab_125bpm_C_-_GRIDTECHHOUSE_Zenhiser.wav") == "C"
    assert parse_key("007_Arp_122bpm_Fm_-_CONNECTPROGHOUSE_Zenhiser.wav") == "Fm"
    assert parse_key("019_Short_Synth_Loop_138bpm_G#_-_138BPMT_Zenhiser.wav") == "G#"
    assert parse_key("PML_Female_Voc_Hum_c#m_120Bpm_Hoa_03.wav") == "C#m"
    assert parse_key("91V_CM_vocal_one_shot_uh_oh_dry.wav") == ""
    assert parse_key("GrandPiano A# 1 f.aif") == "A#"
    assert parse_key("plain.wav", r"Vocals\Em_FemaleVocal") == "Em"
    assert parse_key("CM_120_vocal_loop_adlib_wet_Amin.wav") == "Am"
    assert parse_key("Pack - Melody Loop 1 - 140 BPM F Min.wav") == "Fm"
    assert parse_key("Pack - Bass Loop 05 - 150 BPM - G Minor.wav") == "Gm"
    # A lone capital on a drum is a take letter. On a bass it is still a key.
    assert parse_key("Kick_Punchy_A.wav") == ""
    assert parse_key("Tight B.wav", "Snares") == ""
    assert parse_key("Tops_Loop_128_A.wav") == ""
    assert parse_key("Kick_Tuned_F#.wav") == "F#"
    assert parse_key("Bass_Loop_120_C.wav") == "C"
    assert parse_key("reese_C.wav", r"Recoil - Drum & Bass\Bass") == "C"
    assert semitone_shift("C", "F") == 5
    # Relative keys share their notes, so A minor already fits C major.
    assert semitone_shift("Am", "C") == 0
    assert semitone_shift("Am", "Cm") == 3
    assert semitone_shift("C", "Am") == 0
    assert semitone_shift("F#m", "Fm") == -1
    assert semitone_shift("F#m", "F") == -4
    assert semitone_shift("Bbm", "A#m") == 0
    assert semitone_shift("", "C") is None


def test_repitch_rewrites_wav_rate(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    _wav(path, seconds=0.1, rate=44100)
    shifted = repitch_wav(path.read_bytes(), 12)
    rate = struct.unpack_from("<I", shifted, 24)[0]
    assert rate == 88200
    assert shifted[44:] == path.read_bytes()[44:]


def test_unique_names_keep_the_organized_copy(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        organized = _row("kick.wav", "Hard Techno", 0.4)
        organized["folder"] = r"Hard Techno\Kicks"
        dump = _row("kick.wav", "Imported", 0.4)
        dump["root"] = "user_library"
        dump["folder"] = "Imported"
        dump["path"] = r"G:\User Library\Samples\Imported\kick.wav"
        dump["size"] = organized["size"]
        other = _row("other.wav", "Kick", 0.3)
        bigger = _row("kick.wav", "Kick", 0.4)
        bigger["path"] = r"C:\samples\Kick\kick-big.wav"
        bigger["size"] = organized["size"] + 500
        upsert_many(conn, [organized, dump, other, bigger])
        mark_duplicates(conn)
        total, rows = query_samples(conn, unique_names=True)
        assert total == 3
        kept = [row for row in rows if row["name"] == "kick.wav"]
        assert len(kept) == 2
        assert any(row["root"] == "curated" and row["size"] == organized["size"] for row in kept)
        assert any(row["size"] == bigger["size"] for row in kept)
        assert query_samples(conn, unique_names=False)[0] == 4
    finally:
        conn.close()


def test_reveal_selects_the_file(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "library"
    sample = root / "Kick Drum" / "kick.wav"
    _wav(sample, seconds=0.2)
    db = tmp_path / "library.db"
    _run(db, (Root("curated", "Samples", root),))
    opened: list[str] = []

    def fake_popen(args, **_kwargs):
        opened.append(args)

    monkeypatch.setattr("hoarder.app.subprocess.Popen", fake_popen)
    client = TestClient(create_app(db, [Root("curated", "Samples", root)], scan_on_start=False))
    conn = connect(db)
    try:
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
    finally:
        conn.close()
    response = client.post(f"/api/samples/{sample_id}/reveal")
    assert response.status_code == 200
    assert opened == [f'explorer.exe /select,"{sample}"']


def test_reveal_on_mac_uses_finder(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "library"
    sample = root / "Kick Drum" / "kick.wav"
    _wav(sample, seconds=0.2)
    db = tmp_path / "library.db"
    _run(db, (Root("curated", "Samples", root),))
    opened: list[list[str]] = []

    def fake_popen(args, **_kwargs):
        opened.append(args)

    monkeypatch.setattr("hoarder.app.sys.platform", "darwin")
    monkeypatch.setattr("hoarder.app.subprocess.Popen", fake_popen)
    client = TestClient(create_app(db, [Root("curated", "Samples", root)], scan_on_start=False))
    conn = connect(db)
    try:
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
    finally:
        conn.close()
    response = client.post(f"/api/samples/{sample_id}/reveal")
    assert response.status_code == 200
    assert opened == [["open", "-R", str(sample)]]
    assert reveal_command(sample) == ["open", "-R", str(sample)]


def test_classify_sound_roles() -> None:
    assert decode(classify("RKTD4_Disto_Kick_04.wav", "Kick")) == ["kick"]
    assert decode(classify("hat_04.wav", "HiHat")) == ["hat"]
    assert "noise" in decode(classify("sweep.wav", "white-noise-sweeps"))
    assert "riser" in decode(classify("sweep.wav", "white-noise-sweeps"))
    shaker = decode(classify("shaker.wav", r"Shaker\Shaker Loops"))
    assert shaker == ["shaker", "loop"]
    assert "riser" in decode(classify("up.wav", "Risers-Downlifters"))
    assert decode(classify("chat.wav", "Unsorted")) == []
    assert decode(classify("leader.wav", "Misc")) == []
    assert decode(classify("snare_buildup.wav", "Snare")) == ["snare", "build"]
    vocal = decode(classify("phrase.wav", r"Vocals\Chops\Loops"))
    assert vocal == ["vocals", "chops", "loop"]
    eight = decode(classify("808.wav", r"AED1 Kicks\808"))
    assert eight == ["kick", "808"]
    assert decode(classify("sub kick.wav", "Drums")) == ["kick"]
    assert decode(classify("bass drum.wav", "Drums")) == ["kick"]
    assert decode(classify("reese_bass.wav", "Bass")) == ["bass"]
    assert decode(classify("kick.wav", r"Recoil - Drum & Bass\One Shots")) == ["kick"]
    assert "bass" in decode(classify("sub.wav", r"Recoil - Drum & Bass\Bass"))
    assert decode(classify("reese.wav", "Drum and Bass")) == ["bass"]
    assert decode(classify("SynthLoop.wav", "Misc")) == ["synth", "loop"]
    assert decode(classify("a.wav", "Synth Loops")) == ["synth", "loop"]
    effects = decode(classify("tone.wav", "Risers, Falls and Effects"))
    assert effects == ["riser"]
    assert decode(classify("hit.wav", "Impacts")) == ["fx"]
    assert decode(classify("zap.wav", "Fx")) == ["fx"]
    assert "hat" not in decode(classify("that_one.wav", "Unsorted"))
    assert decode(classify("Clap_01.wav", "Claps")) == ["clap"]
    assert decode(classify("Conga_03.wav", "Percussion")) == ["perc"]
    assert decode(classify("Rim_01.wav", "Rimshots")) == ["perc"]
    assert decode(classify("Tom_Low.wav", "Toms")) == ["tom"]
    assert decode(classify("Crash_02.wav", "Cymbals")) == ["cymbal"]
    assert decode(classify("808_Long_C.wav", "808s")) == ["808"]
    assert decode(classify("Pad_Warm_Cm.wav", "Pads")) == ["pad"]
    assert decode(classify("Pluck_04_F.wav", "Plucks")) == ["pluck"]
    assert decode(classify("Stab_02.wav", "Stabs")) == ["stab"]
    assert decode(classify("GrandPiano A# 1 f.aif", "Piano")) == ["keys"]
    assert decode(classify("Riff_90.wav", "Guitars")) == ["guitar"]


def test_genre_is_separate_from_sample_type(tmp_path: Path) -> None:
    assert decode_genre(classify_genre("Hard Techno", r"Hard Techno\Kicks")) == ["techno"]
    assert decode_genre(classify_genre("Recoil - Drum & Bass", r"Recoil - Drum & Bass\One Shots")) == ["dnb"]
    assert decode_genre(classify_genre("Psytrance Essentials", "Psytrance Essentials")) == ["trance"]
    assert decode_genre(
        classify_genre("Melodic House & Progressive Techno 2", "Melodic House & Progressive Techno 2")
    ) == ["techno", "house"]
    assert classify_genre("Kick", "Kick") == ""
    assert decode_genre(classify_genre("Cyclops Dubstep", r"Cyclops Dubstep\Kicks")) == ["dubstep"]

    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        def placed(name: str, pack: str, folder: str) -> dict:
            row = _row(name, pack, 0.4)
            row["folder"] = folder
            return row

        upsert_many(
            conn,
            [
                placed("kick_techno.wav", "Hard Techno", r"Hard Techno\Kicks"),
                placed("kick_dub.wav", "Cyclops Dubstep", r"Cyclops Dubstep\Kicks"),
                placed("kick_plain.wav", "Kick", "Kick"),
                placed("dubstep_kick.wav", "Kick", "Kick"),
            ],
        )
        total, rows = query_samples(conn, roles="kick", exclude_genres="dubstep")
        assert {row["name"] for row in rows} == {
            "kick_techno.wav",
            "kick_plain.wav",
            "dubstep_kick.wav",
        }
        total, rows = query_samples(conn, roles="kick", genres="techno")
        assert total == 1 and rows[0]["name"] == "kick_techno.wav"
        total, rows = query_samples(conn, genres="dubstep")
        assert [row["name"] for row in rows] == ["kick_dub.wav"]
        plain = next(row for row in query_samples(conn, q="kick_plain")[1])
        assert plain["genres"] == ""
        named = next(row for row in query_samples(conn, q="dubstep_kick")[1])
        assert named["genres"] == ""
        assert "kick" in named["roles"].split()
    finally:
        conn.close()

    client = TestClient(create_app(db, [], scan_on_start=False))
    listed = client.get("/api/samples", params={"roles": "kick", "exclude_genres": "dubstep"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 3
    assert all("dubstep" not in item["genres"] for item in body["items"])
    techno = client.get("/api/samples", params={"genres": "techno"})
    assert techno.json()["total"] == 1
    assert techno.json()["items"][0]["genres"] == ["techno"]
    assert techno.json()["items"][0]["roles"] == ["kick"]


def test_role_filter_ands_tags(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        upsert_many(
            conn,
            [
                _row("shaker_loop.wav", "Shaker Loops", 2.0),
                _row("shaker.wav", "Shaker", 0.4),
                _row("snare_buildup.wav", "Snare", 8.0),
                _row("kick.wav", "Kick", 0.3),
                _row("tone.wav", "Risers, Falls and Effects", 1.0),
            ],
        )
        total, rows = query_samples(conn, roles="shaker,loop")
        assert total == 1 and rows[0]["name"] == "shaker_loop.wav"
        total, rows = query_samples(conn, roles="snare,build")
        assert [row["name"] for row in rows] == ["snare_buildup.wav"]
        total, _rows = query_samples(conn, roles="not-a-role")
        assert total == 5
        total, _rows = query_samples(conn, roles="fx")
        assert total == 0
        total, rows = query_samples(conn, roles="riser")
        assert [row["name"] for row in rows] == ["tone.wav"]
    finally:
        conn.close()

    client = TestClient(create_app(db, [], scan_on_start=False))
    listed = client.get("/api/samples", params={"roles": "kick"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "kick.wav"
    assert body["items"][0]["roles"] == ["kick"]
    missing = client.get("/api/packs")
    assert missing.status_code == 404


def test_api_serves_original_and_blocks_paths_outside_roots(tmp_path: Path) -> None:
    library = tmp_path / "library"
    sample = library / "Kick" / "kick.wav"
    _wav(sample, seconds=0.1, rate=8000)
    outside = tmp_path / "secret.wav"
    _wav(outside, seconds=0.1, rate=8000)
    db = tmp_path / "library.db"
    roots = (Root("curated", "Samples", library),)
    _run(db, roots)
    conn = connect(db)
    try:
        conn.execute(
            """
            INSERT INTO samples (
              path, name, ext, root, pack, folder, size, mtime
            ) VALUES (?, 'secret.wav', '.wav', 'curated', 'Nope', '', 1, 0)
            """,
            (str(outside),),
        )
        conn.commit()
        outside_id = conn.execute(
            "SELECT id FROM samples WHERE name = 'secret.wav'"
        ).fetchone()["id"]
        inside_id = conn.execute(
            "SELECT id FROM samples WHERE name = 'kick.wav'"
        ).fetchone()["id"]
    finally:
        conn.close()

    client = TestClient(create_app(db, roots, scan_on_start=False))
    kept = sample.read_bytes()
    response = client.get(f"/api/samples/{inside_id}/file")
    assert response.status_code == 200
    assert response.content == kept
    assert sample.read_bytes() == kept
    blocked = client.get(f"/api/samples/{outside_id}/file")
    assert blocked.status_code == 403

    listed = client.get("/api/samples", params={"max_sec": 0.5, "min_sec": 0})
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["items"]}
    assert "kick.wav" in names


def test_bpm_parse_ratio_and_set_tempo(tmp_path: Path) -> None:
    assert parse_bpm("005_Bass_Stab_125bpm_C_-_GRID.wav") == 125
    assert parse_bpm("plain.wav") is None
    assert parse_bpm("019_138BPMT_Zenhiser.wav") is None
    # A bare number is a tempo only on a loop.
    assert parse_bpm("SO_TE_125_drum_loop_tops_berlin.wav") == 125
    assert parse_bpm("BL_bass_loop_reese_174_Bbmin.wav") == 174
    assert parse_bpm("phrase_92_wet.wav", r"Vocals\Loops") == 92
    assert parse_bpm("Snare_140.wav") is None
    assert parse_bpm("Hat_Closed_099.wav", "Hat Loops") is None
    assert parse_bpm("Kick_808_Long.wav") is None
    assert parse_bpm("Melody Loop 12.wav") is None
    assert tempo_ratio(120, 140) == 140 / 120
    assert tempo_ratio(90, 180) == 2
    assert tempo_ratio(80, 180) == 1.125

    song = tmp_path / "tune.als"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Ableton><LiveSet><Tempo><Manual Value="140" /></Tempo>
<TimeSignature><Manual Value="201" /></TimeSignature></LiveSet></Ableton>"""
    with gzip.open(song, "wt", encoding="utf-8") as handle:
        handle.write(xml)
    assert read_set_bpm(song) == 140

    log = tmp_path / "Log.txt"
    log.write_text(
        'info: Loading document "C:/Defaults/Audio Track/Default Audio Track.als"\n'
        f'info: Loading document "{song}"\n',
        encoding="utf-8",
    )
    status = project_status(running=True, log_path=log)
    assert status["bpm"] == 140
    assert status["name"] == "tune"
    assert project_status(running=False, log_path=log)["bpm"] is None


def test_live_process_check_follows_the_platform(monkeypatch) -> None:
    monkeypatch.setattr(livebpm.sys, "platform", "win32")

    def tasklist(args, **kwargs):
        assert args[0] == "tasklist"
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
        return '"Ableton Live 12 Suite.exe","1234","Console","1","80,000 K"\n'

    monkeypatch.setattr(livebpm.subprocess, "check_output", tasklist)
    assert livebpm.live_is_running() is True

    monkeypatch.setattr(livebpm.subprocess, "check_output", lambda *args, **kwargs: '"notepad.exe","1","Console","1","1 K"\n')
    assert livebpm.live_is_running() is False

    def missing(*_args, **_kwargs):
        raise OSError("tasklist")

    monkeypatch.setattr(livebpm.subprocess, "check_output", missing)
    assert livebpm.live_is_running() is False

    monkeypatch.setattr(livebpm.sys, "platform", "darwin")
    calls: list[list[str]] = []

    def pgrep(args, **_kwargs):
        calls.append(args)
        return livebpm.subprocess.CompletedProcess(args, 1, stdout="")

    monkeypatch.setattr(livebpm.subprocess, "run", pgrep)
    assert livebpm.live_is_running() is False
    assert calls == [["pgrep", "-x", "Live"]]

    monkeypatch.setattr(
        livebpm.subprocess,
        "run",
        lambda args, **_kwargs: livebpm.subprocess.CompletedProcess(args, 0, stdout="42\n"),
    )
    assert livebpm.live_is_running() is True


def test_newest_log_uses_the_platform_layout(tmp_path: Path, monkeypatch) -> None:
    windows = tmp_path / "windows"
    mac = tmp_path / "mac"
    win_log = windows / "Ableton" / "Live 12.0.5" / "Preferences" / "Log.txt"
    mac_log = mac / "Library" / "Preferences" / "Ableton" / "Live 12.1.10" / "Log.txt"
    decoy = mac / "Library" / "Preferences" / "Ableton" / "Live 12.1.10" / "Preferences" / "Log.txt"
    win_log.parent.mkdir(parents=True)
    mac_log.parent.mkdir(parents=True)
    decoy.parent.mkdir(parents=True)
    win_log.write_text("win", encoding="utf-8")
    mac_log.write_text("mac", encoding="utf-8")
    decoy.write_text("nope", encoding="utf-8")

    monkeypatch.setattr(livebpm.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(windows))
    assert newest_log() == win_log

    monkeypatch.setattr(livebpm.sys, "platform", "darwin")
    monkeypatch.setattr(livebpm.Path, "home", lambda: mac)
    assert newest_log() == mac_log


def test_pitch_keeps_duration_and_tempo_changes_it(tmp_path: Path) -> None:
    path = tmp_path / "loop_120bpm.wav"
    _wav(path, seconds=1.0, rate=16000)
    original = path.read_bytes()
    pitched = match_sample(path, 12, None, 120.0)
    stretched = match_sample(path, 0, 240.0, 120.0)
    assert pitched is not None and stretched is not None

    def seconds(data: bytes) -> float:
        with wave.open(io.BytesIO(data)) as handle:
            return handle.getnframes() / handle.getframerate()

    assert 0.85 < seconds(pitched) < 1.15
    assert 0.4 < seconds(stretched) < 0.65
    assert match_sample(path, 0, 120.0, 120.0) is None
    assert match_sample(path, 0, 240.0, None) is None
    assert path.read_bytes() == original
    with wave.open(io.BytesIO(pitched)) as handle:
        assert handle.getsampwidth() == 3


def test_search_matches_words_in_any_order(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        kick = _row("Kick_Dark_01.wav", "Hard Techno", 0.4)
        kick["folder"] = r"Hard Techno\Kicks"
        snare = _row("Snare_Bright.wav", "House Tools", 0.3)
        upsert_many(conn, [kick, snare])
        for q in ["dark kick", "kick dark", "techno kick", "  KICK  "]:
            total, rows = query_samples(conn, q=q)
            assert [row["name"] for row in rows] == ["Kick_Dark_01.wav"], q
        assert query_samples(conn, q="kick bright")[0] == 0
        # The drive and root folders are in every path. They don't match.
        assert query_samples(conn, q="samples")[0] == 0
    finally:
        conn.close()


def test_bpm_and_key_filters_and_bpm_sort(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        upsert_many(
            conn,
            [
                _row("Bass_Loop_174_Bbmin.wav", "Loops", 4.0),
                _row("Synth_Loop_128_A#min.wav", "Loops", 4.0),
                _row("Synth_Loop_124_Amin.wav", "Loops", 4.0),
                _row("Kick_01.wav", "Kick", 0.3),
            ],
        )
        total, rows = query_samples(conn, min_bpm=125, max_bpm=180)
        assert {row["name"] for row in rows} == {"Bass_Loop_174_Bbmin.wav", "Synth_Loop_128_A#min.wav"}
        total, rows = query_samples(conn, key="A#m")
        assert {row["name"] for row in rows} == {"Bass_Loop_174_Bbmin.wav", "Synth_Loop_128_A#min.wav"}
        assert query_samples(conn, key="Am")[0] == 1
        assert query_samples(conn, key="A")[0] == 0
        assert query_samples(conn, key="nonsense")[0] == 4
        _total, rows = query_samples(conn, sort="bpm", direction="desc")
        assert [row["bpm"] for row in rows] == [174, 128, 124, None]
        _total, rows = query_samples(conn, sort="key", direction="asc")
        assert [row["musical_key"] for row in rows] == ["Am", "Bbm", "A#m", ""]
        _total, rows = query_samples(conn, sort="key", direction="desc")
        assert [row["musical_key"] for row in rows] == ["Bbm", "A#m", "Am", ""]
    finally:
        conn.close()

    client = TestClient(create_app(db, [], scan_on_start=False))
    body = client.get("/api/samples", params={"min_bpm": 125, "key": "A#m", "sort": "bpm"}).json()
    assert [item["bpm"] for item in body["items"]] == [128, 174]
    assert client.get("/api/samples", params={"min_bpm": -1}).status_code == 400


def test_manual_tags_survive_a_rescan(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        upsert_many(conn, [_row("kick_plain.wav", "Kick", 0.4)])
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
        saved = set_sample_tags(conn, sample_id, ["snare"], ["techno"])
        assert saved == {"roles": ["snare"], "genres": ["techno"]}
        assert query_samples(conn, roles="kick")[0] == 0
        assert query_samples(conn, roles="snare", genres="techno")[0] == 1
        upsert_many(conn, [_row("kick_plain.wav", "Kick", 0.4)])
        row = conn.execute(
            "SELECT roles, role_add, role_remove, genre_add FROM samples"
        ).fetchone()
        assert "kick" in row["roles"].split()
        assert "snare" in row["role_add"].split()
        assert "kick" in row["role_remove"].split()
        assert "techno" in row["genre_add"].split()
        conn.execute("DELETE FROM meta WHERE key = 'rules'")
        conn.commit()
    finally:
        conn.close()
    backfill_roles(db)
    conn = connect(db)
    try:
        assert query_samples(conn, roles="snare", genres="techno")[0] == 1
        assert query_samples(conn, roles="kick")[0] == 0
        assert "snare" in conn.execute("SELECT role_add FROM samples").fetchone()["role_add"].split()
    finally:
        conn.close()

    client = TestClient(create_app(db, [], scan_on_start=False))
    assert client.post(f"/api/samples/{sample_id}/tags", json={"roles": ["nope"], "genres": []}).status_code == 400
    saved = client.post(f"/api/samples/{sample_id}/tags", json={"roles": ["hat"], "genres": []})
    assert saved.status_code == 200
    assert saved.json() == {"roles": ["hat"], "genres": []}
    listed = client.get("/api/samples", params={"roles": "hat"}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["roles"] == ["hat"]
    assert listed["items"][0]["genres"] == []
    assert "role_add" not in listed["items"][0]


def test_backfill_runs_once_per_rule_change(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    init_db(db)
    conn = connect(db)
    try:
        upsert_many(conn, [_row("Bass_Loop_120.wav", "Bass", 2.0)])
        conn.execute("UPDATE samples SET bpm = NULL, roles = ''")
        conn.commit()
    finally:
        conn.close()
    backfill_roles(db)
    conn = connect(db)
    try:
        row = conn.execute("SELECT bpm, roles FROM samples").fetchone()
        assert row["bpm"] == 120 and "bass" in row["roles"].split()
        conn.execute("UPDATE samples SET bpm = NULL")
        conn.commit()
    finally:
        conn.close()
    backfill_roles(db)
    conn = connect(db)
    try:
        assert conn.execute("SELECT bpm FROM samples").fetchone()["bpm"] is None
    finally:
        conn.close()


def test_download_name_can_hold_any_text() -> None:
    assert _disposition("Pad.wav") == 'inline; filename="Pad.wav"'
    for name in ["Pad – Warm +3.wav", "ベース +3.wav", 'say "hi".wav']:
        header = _disposition(name)
        header.encode("latin-1")
        assert header.startswith("inline; filename*=utf-8''")


def _plugin(port: int, hello: dict, host: dict, inbox: list, ready: threading.Event) -> None:
    from websockets.asyncio.client import connect

    async def session() -> None:
        try:
            async with connect(f"ws://127.0.0.1:{port}", max_size=8192) as socket:
                await socket.send(json.dumps(hello))
                await socket.send(json.dumps(host))
                ready.set()
                while True:
                    inbox.append(json.loads(await socket.recv()))
        except Exception as exc:
            inbox.append({"_error": type(exc).__name__})
            ready.set()

    asyncio.run(session())


def test_preview_message_keeps_the_repeat_rate(tmp_path: Path) -> None:
    sample = tmp_path / "kick.wav"
    _wav(sample, seconds=0.5)
    message = compose_preview(
        sample,
        semitones=0,
        project_bpm=None,
        sample_bpm=None,
        duration=0.5,
        loop=False,
        stretch=1.0,
        repeat="1/8",
    )
    assert message["type"] == "preview"
    assert message["repeat"] == "1/8"
    assert message["loop"] is False
    assert message["path"] == str(sample)


def _same_file(left: str, right: Path) -> bool:
    return os.path.normcase(str(Path(left))) == os.path.normcase(str(right.resolve()))


def test_clipboard_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "tone.wav"
    _wav(target, seconds=0.05)
    try:
        copy_file(target)
        found = files_on_clipboard()
        assert len(found) == 1
        assert _same_file(found[0], target)
    finally:
        clear_clipboard()
        assert files_on_clipboard() == []


def test_copy_modified_puts_the_wav_on_the_clipboard(tmp_path: Path) -> None:
    root = tmp_path / "library"
    sample = root / "Kicks" / "kick.wav"
    _wav(sample, seconds=0.25)
    db = tmp_path / "library.db"
    _run(db, (Root("curated", "Samples", root),))
    conn = connect(db)
    try:
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
    finally:
        conn.close()
    app = create_app(db, [Root("curated", "Samples", root)], scan_on_start=False, link_port=None)
    try:
        with TestClient(app) as client:
            plain = client.post(f"/api/samples/{sample_id}/copy", json={})
            assert plain.status_code == 200
            assert plain.json()["name"] == "kick.wav"
            assert _same_file(plain.json()["path"], sample)
            assert _same_file(files_on_clipboard()[0], sample)
            before = sample.read_bytes()
            stretched = client.post(f"/api/samples/{sample_id}/copy", json={"stretch": 2, "semitones": 1})
            assert stretched.status_code == 200
            body = stretched.json()
            assert body["name"] == _copy_name(sample, 1, None, None, 2)
            assert body["name"].endswith(" 2x.wav")
            rendered = Path(body["path"])
            assert rendered.is_file()
            assert rendered.read_bytes()[:4] == b"RIFF"
            assert rendered.resolve() != sample.resolve()
            assert _same_file(files_on_clipboard()[0], rendered)
            assert sample.read_bytes() == before
            rejected = client.post(f"/api/samples/{sample_id}/copy", json={"stretch": 3})
            assert rejected.status_code == 400
    finally:
        clear_clipboard()
        assert files_on_clipboard() == []


def test_copy_repeat_keeps_one_bar(tmp_path: Path) -> None:
    assert repeat_seconds("1bar", 120, 4) == 2
    assert repeat_seconds(None, 120, 4) is None
    root = tmp_path / "library"
    sample = root / "Kicks" / "kick.wav"
    _wav(sample, seconds=4.0, rate=8000)
    before = sample.read_bytes()
    db = tmp_path / "library.db"
    _run(db, (Root("curated", "Samples", root),))
    conn = connect(db)
    try:
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
    finally:
        conn.close()
    app = create_app(db, [Root("curated", "Samples", root)], scan_on_start=False, link_port=None)
    try:
        with TestClient(app) as client:
            copied = client.post(
                f"/api/samples/{sample_id}/copy",
                json={"bpm": 120, "repeat": "1bar", "meter": 4},
            )
            assert copied.status_code == 200
            body = copied.json()
            assert body["name"] == "kick 1bar.wav"
            rendered = Path(body["path"])
            with wave.open(str(rendered), "rb") as handle:
                assert handle.getnframes() / handle.getframerate() == 2
            assert sample.read_bytes() == before
    finally:
        clear_clipboard()


def test_drag_payload_is_one_file(tmp_path: Path) -> None:
    import ctypes

    from hoarder.dragdrop import _STGMEDIUM, _file_format

    target = tmp_path / "tone.wav"
    target.write_bytes(b"RIFF")
    obj, vtbl = make_data_object(target)
    medium = _STGMEDIUM()
    result = vtbl.GetData(ctypes.addressof(obj), ctypes.byref(_file_format()), ctypes.byref(medium))
    assert result == 0
    found = paths_from_hdrop(medium.hGlobal)
    assert len(found) == 1
    assert _same_file(found[0], target)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree(medium.hGlobal)
    source, source_vtbl = make_drop_source()
    addr = ctypes.addressof(source)
    assert source_vtbl.QueryContinueDrag(addr, False, 0) == DRAGDROP_S_CANCEL
    assert source_vtbl.QueryContinueDrag(addr, False, MK_LBUTTON) == 0
    assert source_vtbl.QueryContinueDrag(addr, False, 0) == DRAGDROP_S_DROP


def test_render_cleanup_keeps_a_named_wav(tmp_path: Path, monkeypatch) -> None:
    import hoarder.link as link_mod

    monkeypatch.setattr(link_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / "hoarder-link"
    directory.mkdir()
    named = directory / "kick 2x.wav"
    named.write_bytes(b"RIFFKEEP")
    for index in range(10):
        (directory / f"{index:032x}.wav").write_bytes(b"RIFF")
    sample = tmp_path / "tone.wav"
    _wav(sample, seconds=0.5)
    compose_preview(
        sample,
        semitones=0,
        project_bpm=None,
        sample_bpm=None,
        duration=0.05,
        loop=False,
        stretch=2.0,
        repeat=None,
    )
    assert named.read_bytes() == b"RIFFKEEP"
    hex_left = [item for item in directory.glob("*.wav") if link_mod._RENDER_NAME.match(item.name)]
    assert len(hex_left) <= 8


def test_link_preview_is_a_wav(tmp_path: Path) -> None:
    root = tmp_path / "library"
    sample = root / "Loops" / "kick loop 120.wav"
    _wav(sample, seconds=1.0)
    db = tmp_path / "library.db"
    _run(db, (Root("curated", "Samples", root),))
    conn = connect(db)
    try:
        sample_id = conn.execute("SELECT id FROM samples").fetchone()["id"]
    finally:
        conn.close()
    app = create_app(db, [Root("curated", "Samples", root)], scan_on_start=False, link_port=0)
    inbox: list = []
    ready = threading.Event()
    with TestClient(app) as client:
        port = app.state.link.port
        thread = threading.Thread(
            target=_plugin,
            args=(
                port,
                {"type": "hello", "format": "stand-in", "host": "stand-in", "session": "one"},
                {
                    "type": "host",
                    "bpm": 126,
                    "ppq": 1.5,
                    "playing": False,
                    "recording": False,
                    "meter": [4, 4],
                    "sample_rate": 44100,
                },
                inbox,
                ready,
            ),
            daemon=True,
        )
        thread.start()
        assert ready.wait(2)
        deadline = time.time() + 2
        while time.time() < deadline:
            body = client.get("/api/link").json()
            if body["connected"] and body["session"] == "one":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(body)
        assert body["armed"] is True
        assert body["bpm"] == 126
        project = client.get("/api/project").json()
        assert project["bpm"] == 126
        assert project["link"]["connected"] is True
        preview = client.post("/api/link/preview", json={"id": sample_id})
        assert preview.status_code == 200
        assert preview.json()["loop"] is True
        assert preview.json()["beats"] == 2
        deadline = time.time() + 2
        while time.time() < deadline and not any(item.get("type") == "preview" for item in inbox):
            time.sleep(0.02)
        message = next(item for item in inbox if item.get("type") == "preview")
        path = Path(message["path"])
        assert path == sample
        assert path.read_bytes()[:4] == b"RIFF"
        assert message["loop"] is True
        assert message["beats"] == 2
        assert message["repeat"] is None


def test_link_replaces_the_plugin_and_disarm_stops_previews(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library.db", [], scan_on_start=False, link_port=0)
    first: list = []
    second: list = []
    ready_a = threading.Event()
    ready_b = threading.Event()
    host = {
        "type": "host",
        "bpm": 100,
        "ppq": 0,
        "playing": True,
        "recording": False,
        "meter": [3, 4],
        "sample_rate": 48000,
    }
    with TestClient(app) as client:
        port = app.state.link.port
        threading.Thread(
            target=_plugin,
            args=(port, {"type": "hello", "format": "au", "host": "Logic", "session": "a"}, host, first, ready_a),
            daemon=True,
        ).start()
        assert ready_a.wait(2)
        threading.Thread(
            target=_plugin,
            args=(port, {"type": "hello", "format": "vst3", "host": "Live", "session": "b"}, host, second, ready_b),
            daemon=True,
        ).start()
        assert ready_b.wait(2)
        deadline = time.time() + 2
        body = {}
        while time.time() < deadline:
            body = client.get("/api/link").json()
            if body["session"] == "b" and body["format"] == "vst3":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(body)
        assert any(item.get("_error") for item in first)
        cleared = client.post("/api/link/arm", json={"on": False})
        assert cleared.status_code == 200
        assert cleared.json()["armed"] is False
        assert cleared.json()["connected"] is True
        deadline = time.time() + 2
        while time.time() < deadline and not any(item.get("type") == "clear" for item in second):
            time.sleep(0.02)
        assert any(item.get("type") == "clear" for item in second)
        missing = client.post("/api/link/preview", json={"id": 1})
        assert missing.status_code == 409
        again = client.post("/api/link/arm", json={"on": True})
        assert again.status_code == 200
        assert again.json()["armed"] is True


def test_link_rejects_a_host_message_before_hello(tmp_path: Path) -> None:
    app = create_app(tmp_path / "library.db", [], scan_on_start=False, link_port=0)
    inbox: list = []
    ready = threading.Event()

    def sneak(port: int) -> None:
        async def session() -> None:
            from websockets.asyncio.client import connect

            try:
                async with connect(f"ws://127.0.0.1:{port}") as socket:
                    await socket.send(json.dumps({"type": "host"}))
                    await socket.recv()
            except Exception as exc:
                inbox.append(type(exc).__name__)
            ready.set()

        asyncio.run(session())

    with TestClient(app) as client:
        threading.Thread(target=sneak, args=(app.state.link.port,), daemon=True).start()
        assert ready.wait(2)
        assert inbox == ["ConnectionClosedError"]
        assert client.get("/api/link").json()["connected"] is False


def _row(name: str, pack: str, duration: float | None) -> dict:
    return {
        "path": f"C:\\samples\\{pack}\\{name}",
        "name": name,
        "ext": ".wav",
        "root": "curated",
        "pack": pack,
        "folder": pack,
        "size": 10,
        "mtime": 1,
        "duration": duration,
        "sample_rate": 44100,
        "channels": 2,
        "bit_depth": 24,
    }
