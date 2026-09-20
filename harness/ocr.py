"""OCR helpers (tesseract) so text can be located on any screenshot, including
VNC frames from a physical iPhone where no accessibility tree is available."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .util import HarnessError, run, which, tmp_path


@dataclass
class OCRHit:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    line: tuple = (0, 0, 0)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


_CACHE: dict[tuple, list] = {}


def _tsv(path: Path, upscale: float = 1.0, invert: bool | str = False, psm: int = 11) -> list[OCRHit]:
    """invert: False | True | 'bright' (keep only bright pixels as black text on white; catches white-on-colour buttons)."""
    key = (str(path), path.stat().st_mtime_ns, upscale, invert, psm)
    if key in _CACHE:
        return _CACHE[key]
    hits = _tsv_uncached(path, upscale, invert, psm)
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[key] = hits
    return hits


def _tsv_uncached(path: Path, upscale: float, invert, psm: int) -> list[OCRHit]:
    if not which("tesseract"):
        raise HarnessError("tesseract not installed (brew install tesseract)")
    src = path
    if upscale != 1.0 or invert:
        from PIL import Image, ImageOps
        im = Image.open(path).convert("RGB")
        if upscale != 1.0:
            im = im.resize((int(im.width * upscale), int(im.height * upscale)))
        if invert == "bright":
            g = im.convert("L")
            im = g.point(lambda v: 0 if v > 190 else 255).convert("RGB")   # bright text -> black on white
        elif invert:
            im = ImageOps.invert(im)
        src = tmp_path(f"ocr-{upscale}-{invert}.png")
        im.save(src)
    cp = run(["tesseract", str(src), "stdout", "--psm", str(psm), "tsv"], quiet=True,
             env={"OMP_THREAD_LIMIT": "1"})
    hits: list[OCRHit] = []
    for row in csv.DictReader(io.StringIO(cp.stdout), delimiter="\t"):
        txt = (row.get("text") or "").strip()
        if not txt:
            continue
        try:
            conf = float(row.get("conf", "0"))
        except ValueError:
            conf = 0.0
        hits.append(OCRHit(txt, int(int(row["left"]) / upscale), int(int(row["top"]) / upscale),
                           int(int(row["width"]) / upscale), int(int(row["height"]) / upscale), conf,
                           (int(row.get("block_num", 0)), int(row.get("par_num", 0)), int(row.get("line_num", 0)))))
    return hits


def words(path: Path) -> list[OCRHit]:
    return _tsv(path)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_words(ws: list[OCRHit], parts: list[str]) -> list[OCRHit]:
    out: list[OCRHit] = []
    for i, w in enumerate(ws):
        if not _norm(w.text) or _norm(parts[0]) not in _norm(w.text):
            continue
        run_ = [w]
        ok = True
        for j, p in enumerate(parts[1:], 1):
            if i + j >= len(ws) or _norm(p) not in _norm(ws[i + j].text) or abs(ws[i + j].y - w.y) > max(w.h, 10) * 1.5:
                ok = False
                break
            run_.append(ws[i + j])
        if ok:
            x = min(r.x for r in run_); y = min(r.y for r in run_)
            x2 = max(r.x + r.w for r in run_); y2 = max(r.y + r.h for r in run_)
            out.append(OCRHit(" ".join(r.text for r in run_), x, y, x2 - x, y2 - y, min(r.conf for r in run_), w.line))
    return out


def find_text(path: Path, needle: str, *, min_conf: float = 30) -> list[OCRHit]:
    """Find a word or phrase. Multi-word needles match consecutive words on the same line.

    Passes: normal, inverted (white-on-color buttons), then 2x upscaled variants for small images.
    """
    parts = [p.lower() for p in needle.split()]
    from PIL import Image
    width = Image.open(path).width
    # psm 11 = sparse text (fast, good for labels); psm 6 = uniform block (catches text inside filled buttons)
    passes = [(1.0, False, 11), (1.0, "bright", 11), (1.0, True, 6), (1.0, False, 6), (1.0, "bright", 6), (1.0, True, 11)]
    if width < 900:
        passes += [(2.0, False, 11), (2.0, "bright", 11), (2.0, True, 6)]
    for upscale, invert, psm in passes:
        ws = [w for w in _tsv(path, upscale, invert, psm) if w.conf >= min_conf]
        out = _match_words(ws, parts)
        if out:
            return out
    return []


def dump_text(path: Path, *, invert_too: bool = True) -> str:
    """Rough reading-order text of a screenshot (for logs / assertions)."""
    ws = [w for w in _tsv(path) if w.conf >= 30]
    if invert_too:
        seen = {(w.x // 15, w.y // 15) for w in ws}
        ws += [w for w in _tsv(path, 1.0, True, 6) if w.conf >= 30 and (w.x // 15, w.y // 15) not in seen]
    lines: dict[tuple, list[OCRHit]] = {}
    for w in ws:
        lines.setdefault((w.line if w.line != (0, 0, 0) else (w.y // 25,)), []).append(w)
    ordered = sorted(lines.values(), key=lambda l: (min(w.y for w in l), min(w.x for w in l)))
    return "\n".join(" ".join(w.text for w in sorted(l, key=lambda w: w.x)) for l in ordered)
