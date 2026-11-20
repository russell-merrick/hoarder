"""The page, driven in headless Edge, follows the stand-in and then the browser."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect as ws_connect

from hoarder.app import create_app
from hoarder.roots import Root
from hoarder.scan import _run

HOARDER = Path(__file__).resolve().parent.parent
STANDIN = HOARDER.parent / "hoarder-link" / "standin.py"


def _wav(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _edge() -> Path:
    roots = [os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")]
    for root in roots:
        path = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if path.is_file():
            return path
    pytest.skip("Edge is not installed")


class _QuietServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return


def test_page_follows_the_stand_in_then_the_browser(tmp_path: Path) -> None:
    if not STANDIN.is_file():
        pytest.skip("stand-in is not next to this repo")
    library = tmp_path / "library"
    _wav(library / "kick loop.wav", seconds=8.0)
    database = tmp_path / "library.db"
    roots = (Root("curated", "Samples", library),)
    _run(database, roots)
    counts = {"link": 0, "browser": 0}
    app = create_app(database, roots, scan_on_start=False, link_port=0)

    @app.middleware("http")
    async def count_previews(request, call_next):
        path = request.url.path
        if path == "/api/link/preview":
            counts["link"] += 1
        elif path.endswith("/preview"):
            counts["browser"] += 1
        return await call_next(request)

    http_port = _free_port()
    server = _QuietServer(uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    profile = tmp_path / "edge"
    profile.mkdir()
    debug_port = _free_port()
    page = None
    standin = None
    lines: list[str] = []
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if server.started and getattr(app.state, "link", None) is not None and app.state.link.port:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("link socket did not start")
        edge = _edge()
        page_proc = subprocess.Popen(
            [
                str(edge),
                "--headless=new",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile}",
                f"http://127.0.0.1:{http_port}/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        page = _Attached(debug_port, page_proc)
        page.open(f"http://127.0.0.1:{http_port}/")
        before = page.wait(
            "(() => { const b = document.querySelector('#link');"
            " return b && b.disabled && !b.classList.contains('on'); })()"
        )
        assert before is True
        layout = page.eval(
            "(() => { const player = document.querySelector('.player');"
            " const results = document.querySelector('.results');"
            " const repeat = document.querySelector('#repeat');"
            " const key = document.querySelector('#target-key');"
            " if (document.querySelector('#stretch')) return false;"
            " if (!player || !results || !repeat || !key) return false;"
            " if (!player.contains(repeat) || !player.contains(key) || !player.contains(document.querySelector('#wave'))) return false;"
            " const box = player.getBoundingClientRect();"
            " const above = results.getBoundingClientRect();"
            " return box.top > above.top && box.bottom > window.innerHeight - 40; })()"
        )
        assert layout is True
        filters = page.eval(
            "(() => {"
            " const row = document.querySelector('.filter-row');"
            " const search = document.querySelector('#q').closest('.field');"
            " const tempo = document.querySelector('#tempo-label');"
            " const min = document.querySelector('#bpm-min');"
            " const max = document.querySelector('#bpm-max');"
            " const length = document.querySelector('#length-range');"
            " const tempoRange = document.querySelector('#tempo-range');"
            " if (!min || min.type !== 'range' || !max || max.type !== 'range') return 'type';"
            " if (!length || !tempoRange || tempoRange.getBoundingClientRect().left <= length.getBoundingClientRect().left) return 'order';"
            " if (!tempo || tempo.textContent !== 'Any tempo') return 'any';"
            " const sw = search.getBoundingClientRect().width;"
            " const rw = row.getBoundingClientRect().width;"
            " if (sw > rw * 0.55) return 'wide';"
            " const btn = document.querySelector('#genre-btn');"
            " btn.click();"
            " const panel = document.querySelector('#genre-panel');"
            " const box = panel && panel.querySelector('input[value=\"techno\"]');"
            " if (!box || panel.hidden) return 'menu';"
            " box.click();"
            " if (panel.hidden || !box.checked || btn.textContent !== 'Techno') return 'check';"
            " box.click();"
            " if (box.checked || panel.hidden) return 'uncheck';"
            " min.value = '128'; max.value = '128';"
            " min.dispatchEvent(new Event('input', { bubbles: true }));"
            " if (tempo.textContent !== '128 BPM') return 'exact';"
            " min.value = '100'; max.value = '140';"
            " min.dispatchEvent(new Event('input', { bubbles: true }));"
            " if (!/^100\\D+140$/.test(tempo.textContent)) return 'range';"
            " min.value = '40'; max.value = '250';"
            " min.dispatchEvent(new Event('input', { bubbles: true }));"
            " if (tempo.textContent !== 'Any tempo') return 'reset';"
            " const exact = document.querySelector('#tempo-exact');"
            " exact.value = '128';"
            " exact.dispatchEvent(new Event('change', { bubbles: true }));"
            " if (tempo.textContent !== '128 BPM' || min.value !== '128' || max.value !== '128') return 'typed';"
            " exact.value = '';"
            " exact.dispatchEvent(new Event('change', { bubbles: true }));"
            " if (tempo.textContent !== 'Any tempo') return 'cleared';"
            " const keyLabel = document.querySelector('.key-label');"
            " const keySelect = document.querySelector('#key-filter-btn');"
            " const kl = keyLabel.getBoundingClientRect();"
            " const ks = keySelect.getBoundingClientRect();"
            " if (!keySelect || ks.left < kl.right - 2) return 'key-beside';"
            " if (Math.abs((kl.top + kl.bottom) / 2 - (ks.top + ks.bottom) / 2) > 18) return 'key-row';"
            " const genreFont = getComputedStyle(document.querySelector('#genre-btn')).fontFamily;"
            " const repeatBtn = document.querySelector('#repeat-btn');"
            " const repeatItem = document.querySelector('#repeat-panel .pick-item');"
            " const genreItem = document.querySelector('#genre-panel .check');"
            " if (!repeatBtn || getComputedStyle(repeatBtn).fontFamily !== genreFont) return 'font';"
            " if (!repeatItem || !genreItem) return 'item';"
            " if (getComputedStyle(repeatItem).fontFamily !== getComputedStyle(genreItem).fontFamily) return 'item-font';"
            " if (getComputedStyle(document.querySelector('#wave')).cursor === 'pointer') return 'wave';"
            " return true;"
            " })()"
        )
        assert filters is True
        font = page.eval(
            "(() => {"
            " const down = document.querySelector('#font-down');"
            " const up = document.querySelector('#font-up');"
            " const label = document.querySelector('#font-size-label');"
            " if (!down || !up || !label || label.textContent !== '100%') return 'start';"
            " const before = parseFloat(getComputedStyle(document.body).fontSize);"
            " up.click();"
            " const larger = parseFloat(getComputedStyle(document.body).fontSize);"
            " if (label.textContent !== '110%' || !(larger > before + 0.5)) return 'up';"
            " down.click();"
            " const back = parseFloat(getComputedStyle(document.body).fontSize);"
            " if (label.textContent !== '100%' || Math.abs(back - before) > 0.5) return 'down';"
            " return true;"
            " })()"
        )
        assert font is True
        standin = subprocess.Popen(
            [
                "python",
                str(STANDIN),
                "--url",
                f"ws://127.0.0.1:{app.state.link.port}",
                "--bpm",
                "131",
                "--ppq",
                str(0.85 * 8 * 131 / 60),
                "--playing",
                "--freeze",
                "--hz",
                "20",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        reader = threading.Thread(target=_drain, args=(standin.stdout, lines), daemon=True)
        reader.start()
        linked = page.wait(
            "(() => { const b = document.querySelector('#link');"
            " const tempo = document.querySelector('#project-bpm');"
            " if (!b || b.disabled || !b.classList.contains('on')) return null;"
            " if (!tempo || tempo.textContent !== 'Live 131') return null;"
            " return getComputedStyle(b).color; })()"
        )
        assert linked == "rgb(61, 214, 140)"
        page.wait("document.querySelector('.hit') !== null")
        page.eval(
            "(() => { const add = document.querySelector('.tag-add');"
            " add.click();"
            " const editor = document.querySelector('#tag-editor');"
            " editor.querySelector('input[value=\"kick\"]').click();"
            " editor.querySelector('input[value=\"techno\"]').click();"
            " return !editor.hidden; })()"
        )
        tagged = page.wait(
            "(() => { const hit = document.querySelector('.hit');"
            " const tags = hit && hit.querySelector('.meta-tags');"
            " const editor = document.querySelector('#tag-editor');"
            " if (!hit || !tags || !editor || editor.hidden) return false;"
            " if (hit.dataset.savedTags !== 'loop|techno') return false;"
            " return tags.textContent.indexOf('Kick') < 0 && tags.textContent.indexOf('Loop') >= 0 && tags.textContent.indexOf('Techno') >= 0; })()"
        )
        assert tagged is True
        page.eval("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))")
        page.wait("document.querySelector('#tag-editor').hidden")
        page.eval("document.querySelector('.hit').click()")
        page.wait("document.querySelector('#play').textContent === 'Stop'")
        waved = page.wait(
            "(() => { const a = document.querySelector('#audio');"
            " return Boolean(a && a.muted && a.src.startsWith('blob:')); })()"
        )
        assert waved is True
        ratio_error = 1
        deadline = time.time() + 2
        while time.time() < deadline:
            ratio_error = page.eval(
                "(async () => {"
                " const link = await fetch('/api/link').then((r) => r.json());"
                " const beats = 8 * link.bpm / 60;"
                " const ppq = link.ppq + (Number(link.age_ms) || 0) / 1000 * link.bpm / 60;"
                " const phase = ((ppq % beats) + beats) % beats;"
                " const expected = phase / beats;"
                " const head = document.querySelector('#playhead');"
                " const wave = document.querySelector('#wave');"
                " if (!head || head.hidden || !wave.clientWidth) return 1;"
                " const found = /translate3d\\(([\\d.eE+-]+)px/.exec(head.style.transform || '');"
                " if (!found) return 1;"
                " return Math.abs(Number(found[1]) / wave.clientWidth - expected);"
                "})()"
            )
            if isinstance(ratio_error, (int, float)) and ratio_error < 0.06:
                break
            time.sleep(0.05)
        assert ratio_error < 0.06
        assert _seen(lines, "preview ok")
        assert counts["link"] >= 1
        assert counts["browser"] == counts["link"]
        page.eval("document.querySelector('#link').click()")
        stayed = page.wait(
            "(() => { const b = document.querySelector('#link');"
            " return b && b.classList.contains('on') && !b.disabled; })()"
        )
        assert stayed is True
        time.sleep(0.4)
        assert not any("clear" in line for line in lines)
        page.eval("document.querySelector('#play').click()")
        page.wait("document.querySelector('#play').textContent === 'Play'")
        page.eval("document.querySelector('.hit').click()")
        deadline = time.time() + 6
        while time.time() < deadline and (counts["link"] < 2 or counts["browser"] < 2):
            time.sleep(0.05)
        assert counts["link"] >= 2
        assert counts["browser"] == counts["link"]
    finally:
        if standin is not None and standin.poll() is None:
            standin.kill()
        if page is not None:
            page.close()
        server.should_exit = True
        thread.join(5)


def _drain(pipe, lines: list[str]) -> None:
    for line in pipe:
        lines.append(line)


def _seen(lines: list[str], text: str) -> bool:
    deadline = time.time() + 4
    while time.time() < deadline:
        if any(text in line for line in lines):
            return True
        time.sleep(0.05)
    return False


class _Attached:
    """Debug session for an Edge process that was already launched."""

    def __init__(self, port: int, proc: subprocess.Popen) -> None:
        self.port = port
        self.proc = proc
        self.sock = None
        self.seq = 0

    def open(self, url: str) -> None:
        import urllib.request

        deadline = time.time() + 10
        page = None
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json"))
            except OSError:
                time.sleep(0.1)
                continue
            page = next((tab for tab in tabs if tab.get("type") == "page" and url.rstrip("/") in tab.get("url", "")), None)
            if page and page.get("webSocketDebuggerUrl"):
                break
            page = None
            time.sleep(0.1)
        if page is None:
            raise AssertionError("Edge did not open the page")
        self.sock = ws_connect(page["webSocketDebuggerUrl"], open_timeout=5)

    def eval(self, expression: str):
        self.seq += 1
        ident = self.seq
        self.sock.send(json.dumps({
            "id": ident,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        deadline = time.time() + 5
        while time.time() < deadline:
            message = json.loads(self.sock.recv(timeout=5))
            if message.get("id") != ident:
                continue
            result = message["result"]["result"]
            if result.get("subtype") == "error":
                raise AssertionError(result.get("description"))
            return result.get("value")
        raise AssertionError(expression)

    def wait(self, expression: str, seconds: float = 6):
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            last = self.eval(expression)
            if last:
                return last
            time.sleep(0.1)
        raise AssertionError((expression, last))

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        if self.proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
