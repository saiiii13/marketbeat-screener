"""Live run notes.

Every stage appends here as it works, flushed per line, so the file can be watched in an
editor (VS Code reloads it automatically) or Notepad while the run is in progress.
"""
import ctypes
import os
import time
import re
import subprocess
import sys
from datetime import datetime

from .config import OUT_DIR

NOTES_PATH = OUT_DIR / "live_notes.md"
HTML_PATH = OUT_DIR / "live_notes.html"
BODY_PATH = OUT_DIR / "live_notes_body.html"
_server_url: str | None = None
_fh = None
_title = "Live run notes"
_run_start = None

HTML_SHELL = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title><style>
:root {{ color-scheme: light dark; --bg:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --accent:#2a78d6; --good:#006300; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --grid:#2c2c2a; --accent:#3987e5; --good:#0ca30c; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:13px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:14px 16px 40px; }}
h1 {{ font-size:15px; margin:0 0 2px }}
h2 {{ font-size:13px; margin:16px 0 6px; padding-top:10px; border-top:1px solid var(--grid); color:var(--accent) }}
.meta {{ color:var(--muted); font-size:11px; margin-bottom:10px }}
.row {{ padding:2px 0; border-bottom:1px dotted var(--grid) }}
.t {{ color:var(--muted); font-variant-numeric:tabular-nums; margin-right:6px; font-size:11px }}
ul {{ margin:4px 0; padding-left:18px }}
table {{ border-collapse:collapse; width:100%; margin:6px 0 10px; font-size:12px }}
th {{ text-align:left; color:var(--muted); font-weight:500; border-bottom:1px solid var(--grid); padding:3px 6px }}
td {{ padding:3px 6px; border-bottom:1px solid var(--grid); font-variant-numeric:tabular-nums }}
strong {{ color:var(--accent) }}
.live {{ display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--good);
  margin-right:5px; animation:p 1.4s infinite }}
@keyframes p {{ 50% {{ opacity:.25 }} }}
</style></head><body><div id="notes">{body}</div>
<script>
// Poll the rendered fragment and swap it in. A meta-refresh would reload the whole page,
// which flashes every few seconds and loses the scroll position.
const NEAR_BOTTOM = 80;
let last = null;
async function tick() {{
  try {{
    const res = await fetch('live_notes_body.html', {{cache: 'no-store'}});
    if (res.ok) {{
      const html = await res.text();
      if (html !== last) {{
        const atBottom = window.innerHeight + window.scrollY
                         >= document.body.scrollHeight - NEAR_BOTTOM;
        document.getElementById('notes').innerHTML = html;
        last = html;
        if (atBottom) window.scrollTo(0, document.body.scrollHeight);
      }}
    }}
  }} catch (e) {{ /* file:// blocks fetch; the page still shows the snapshot it loaded with */ }}
  setTimeout(tick, 1200);
}}
window.scrollTo(0, document.body.scrollHeight);
tick();
</script></body></html>"""


def _md_to_html(md: str) -> str:
    """Just enough markdown for the notes: headings, bullets, tables, bold, timestamps."""
    out, rows = [], []

    def flush_table():
        if not rows:
            return
        head, body = rows[0], [r for r in rows[1:] if not set(r) <= {"---", ""}]
        out.append("<table><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr>")
        for r in body:
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
        out.append("</table>")
        rows.clear()

    for raw in md.split("\n"):
        line = raw.rstrip()
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip("|").split("|")])
            continue
        flush_table()
        if not line:
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("- "):
            out.append(f"<ul><li>{line[2:]}</li></ul>")
        else:
            m = re.match(r"^(\d\d:\d\d:\d\d)\s+(.*)$", line)
            if m:
                out.append(f'<div class="row"><span class="t">{m.group(1)}</span>{m.group(2)}</div>')
            else:
                out.append(f'<div class="meta">{line}</div>')
    flush_table()
    return "\n".join(out)


def _render_html():
    try:
        md = NOTES_PATH.read_text(encoding="utf-8")
        body = f'<h1><span class="live"></span>{_title}</h1>' + _md_to_html(md)
        # The page shell is written once; the fragment is what the page polls for updates.
        BODY_PATH.write_text(body, encoding="utf-8")
        HTML_PATH.write_text(HTML_SHELL.format(title=_title, body=body), encoding="utf-8")
    except Exception:
        pass


def serve() -> str | None:
    """Serve out/ on localhost so the notes page can fetch updates.

    Browsers block fetch() on file:// URLs, so without a server the page could only
    refresh by reloading itself - which is what made it flash every few seconds.
    """
    global _server_url
    if _server_url:
        return _server_url
    try:
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Quiet, directory=str(OUT_DIR))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _server_url = f"http://127.0.0.1:{httpd.server_address[1]}/live_notes.html"
        return _server_url
    except Exception as e:
        print(f"[notes] could not start the local notes server ({e}); falling back to a file URL")
        return None


def start(title: str) -> str:
    """Open a fresh notes file for this run and return its path."""
    global _fh, _title, _run_start
    _title = title
    _run_start = time.time()
    OUT_DIR.mkdir(exist_ok=True)
    if _fh:
        _fh.close()
    _fh = open(NOTES_PATH, "w", encoding="utf-8", buffering=1)
    _fh.write(f"# {title}\n\nStarted {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
    _fh.flush()
    _render_html()
    return str(NOTES_PATH)


def _write(text: str):
    if _fh is None:
        return
    _fh.write(text)
    _fh.flush()
    os.fsync(_fh.fileno())
    _render_html()


def elapsed() -> str:
    if _run_start is None:
        return ""
    m, sec = divmod(int(time.time() - _run_start), 60)
    return f"{m}m {sec:02d}s"


def eta(done: int, total: int, started: float) -> str:
    """Human ETA from work already finished. Empty until there is enough to extrapolate."""
    if done <= 0 or total <= 0 or done >= total:
        return ""
    rate = (time.time() - started) / done
    left = rate * (total - done)
    if left < 90:
        return f"~{int(left)}s left"
    return f"~{left / 60:.0f} min left"


def progress(done: int, total: int, label: str, started: float):
    bar = eta(done, total, started)
    line(f"{label}: **{done:,} of {total:,}**" + (f" · {bar}" if bar else "") + f" · {elapsed()} elapsed")


def stage(name: str):
    _write(f"\n## {name}  ·  {datetime.now():%H:%M:%S}\n\n")


def line(text: str):
    _write(f"{datetime.now():%H:%M:%S}  {text}\n")


def bullet(text: str):
    _write(f"- {text}\n")


def table(headers: list[str], rows: list[list]):
    _write("\n| " + " | ".join(headers) + " |\n")
    _write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in rows:
        _write("| " + " | ".join("" if c is None else str(c) for c in r) + " |\n")
    _write("\n")


def stocks_read(rows: list[dict], limit: int = 12):
    """Record the most interesting stocks from a freshly-read screener page."""
    rated = [r for r in rows if r.get("consensus_rating")]
    # Sort by MarketBeat's own rating score; target upside is often stale after reverse splits.
    interesting = sorted(rated, key=lambda r: (-(r.get("marketrank") or 0), -(r.get("price") or 0)))[:limit]
    if not interesting:
        prices = [r.get("price") for r in rows if r.get("price") is not None]
        if prices:
            bullet(f"{len(rows)} stocks read, none with analyst coverage "
                   f"(${min(prices):,.2f}–${max(prices):,.2f})")
        return
    table(
        ["Ticker", "Company", "Price", "Rating", "Score /5", "Target", "Upside"],
        [[r.get("ticker"), (r.get("company") or "")[:28],
          f"${r['price']:,.2f}" if r.get("price") is not None else "",
          r.get("consensus_rating"),
          f"{r['marketrank']:.2f}" if r.get("marketrank") is not None else "",
          f"${r['consensus_target']:,.2f}" if r.get("consensus_target") else "",
          f"{r['upside_pct']:+.0f}%" if r.get("upside_pct") is not None and abs(r["upside_pct"]) < 1000 else "",
          ] for r in interesting],
    )


def close(footer: str = ""):
    global _fh
    if _fh:
        if footer:
            _write(f"\n{footer}\n")
        _write(f"\nFinished {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        _fh.close()
        _fh = None


def screen_size() -> tuple[int, int]:
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:
        return 1920, 1080


def open_split_left() -> tuple[int, int, int, int]:
    """Put the live notes in a Chrome window on the left half and return the right half's
    geometry, so the scraper's own window can fill it."""
    w, h = screen_size()
    half = w // 2
    chrome = os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe")
    if not os.path.exists(chrome):
        chrome = os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe")
    _render_html()
    # Prefer the local server: it lets the page update in place instead of reloading.
    url = serve() or HTML_PATH.resolve().as_uri()
    if os.path.exists(chrome):
        try:
            subprocess.Popen(
                [chrome, f"--app={url}", f"--window-position=0,0", f"--window-size={half},{h - 60}",
                 "--user-data-dir=" + str((OUT_DIR / "notes_profile").resolve()), "--no-first-run"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[notes] live notes window open on the left half ({half}x{h - 60})")
        except Exception as e:
            print(f"[notes] could not open the notes window ({e}); file is at {HTML_PATH}")
    else:
        open_in_editor(str(NOTES_PATH))
    return half, 0, half, h - 60


def open_in_editor(path: str):
    """Show the notes file in VS Code if present (it live-reloads), else Notepad."""
    code = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd")
    try:
        if os.path.exists(code):
            subprocess.Popen([code, "--reuse-window", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            subprocess.Popen(["notepad.exe", path])
        else:
            return
        print(f"[notes] live notes open in an editor: {path}")
    except Exception as e:
        print(f"[notes] could not open an editor ({e}); notes are at {path}")
