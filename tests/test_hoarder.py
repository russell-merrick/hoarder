"""Catalog filters, header probe, and the rule that files stay put."""

from __future__ import annotations

import gzip
import io
import struct
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from hoarder.app import create_app
from hoarder.db import connect, init_db, query_samples, upsert_many
from hoarder.keys import parse_key, semitone_shift
from hoarder.livebpm import parse_bpm, project_status, read_set_bpm, tempo_ratio
from hoarder.stretch import match_sample
from hoarder.probe import _extended80, aiff_to_wav_bytes, probe, repitch_wav
from hoarder.genres import classify as classify_genre
from hoarder.genres import decode as decode_genre
from hoarder.roles import classify, decode
from hoarder.roots import DEFAULT_ROOTS, Root
from hoarder.scan import _run, pack_and_folder


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


def test_default_roots_skip_projects() -> None:
    ids = {root.id for root in DEFAULT_ROOTS}
    assert ids == {"curated", "splice", "user_library"}
    for root in DEFAULT_ROOTS:
        assert "projects" not in root.path.parts


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
    wav = aiff_to_wav_bytes(path)
    assert path.stat().st_mtime_ns == before
    assert wav.startswith(b"RIFF")
    out = tmp_path / "preview.wav"
    out.write_bytes(wav)
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 44100
        assert struct.unpack("<h", handle.readframes(1))[0] == 1000


def test_pack_name_skips_splice_packs_dir(tmp_path: Path) -> None:
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
    assert semitone_shift("C", "F") == 5
    assert semitone_shift("Am", "C") == 3
    assert semitone_shift("F#m", "F") == -1
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
        dump = _row("kick.wav", "Splice", 0.4)
        dump["root"] = "user_library"
        dump["folder"] = "Splice"
        dump["path"] = r"G:\User Library\Samples\Splice\kick.wav"
        dump["size"] = organized["size"]
        other = _row("other.wav", "Kick", 0.3)
        upsert_many(conn, [organized, dump, other])
        total, rows = query_samples(conn, unique_names=True)
        assert total == 2
        kept = next(row for row in rows if row["name"] == "kick.wav")
        assert kept["root"] == "curated"
        assert query_samples(conn, unique_names=False)[0] == 3
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
    assert eight == ["kick"]
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


def test_pitch_keeps_duration_and_tempo_changes_it(tmp_path: Path) -> None:
    path = tmp_path / "loop_120bpm.wav"
    _wav(path, seconds=1.0, rate=16000)
    original = path.read_bytes()
    pitched = match_sample(path, 12, None)
    stretched = match_sample(path, 0, 240.0)
    assert pitched is not None and stretched is not None

    def seconds(data: bytes) -> float:
        with wave.open(io.BytesIO(data)) as handle:
            return handle.getnframes() / handle.getframerate()

    assert 0.85 < seconds(pitched) < 1.15
    assert 0.4 < seconds(stretched) < 0.65
    assert match_sample(path, 0, 120.0) is None
    assert path.read_bytes() == original


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
