# -*- coding: utf-8 -*-
"""
AMT – Automatic Movie Translator

Tłumaczy filmy lokalnie, bez internetu:
  1. wykrywa język filmu i jego napisy,
  2. bierze tekst z napisów wbudowanych w film / z pliku napisów / z rozpoznawania mowy,
  3. tłumaczy go (NLLB-200) i zapisuje napisy .srt albo od razu film z napisami (.mkv / .mp4).

Działa w Windows, macOS i Linuksie.
Uruchamianie: AMT.bat (Windows), AMT.command (macOS), amt.sh (Linux)  albo: python amt.py
Bez okna:     python amt.py --srt film.mkv [tgt=pl] [source=auto|whisper|napisy.srt] [export=srt|mkv|mp4]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from amt_core import (  # noqa: E402
    ABBREV, ACCENTS, COLORS, IS_WIN, ISO3, ISO3_OUT, LANG_NAME, LANGS, NLLB_CODE, NLLB_MODELS, NO_WINDOW,
    SENT_END, SR, UI_FONT, UI_SCALES, WHISPER_MODELS, AutoWrap, Cancelled, clean_text, fit_geometry, fmt_clock,
    fmt_dur, get_translator, guess_lang, init_ui, lang_label, load_settings, load_whisper, make_fonts, make_root,
    open_path, reveal_path, save_settings, set_app_id, set_window_icon, setup_logging, srt_time, ui_scaling,
    wrap_sub)

LOG_FILE = setup_logging("amt")

import collections  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

import numpy as np  # noqa: E402

APP = "amt"
ASSETS = os.path.join(HERE, "assets")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".flv")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac")
DEFAULTS = {
    "src": "auto", "tgt": "pl", "whisper": "small", "nllb": "600M", "accurate": True,
    "save_orig": False, "export": "srt", "out_dir": "", "last_dir": "", "ui_scale": "100%",
}

# ----------------------------------------------------------------- pliki napisów

TIME_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")
MARKUP_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _srt_t(m):
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text):
    cues = []
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\\N", "\n")
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                ta, tb = TIME_RE.search(a), TIME_RE.search(b)
                if ta and tb:
                    body = [MARKUP_RE.sub("", x).strip() for x in lines[i + 1:]]
                    body = [x for x in body if x]
                    if body:
                        cues.append(dict(start=_srt_t(ta), end=_srt_t(tb), lines=body))
                break
    return cues


def read_text_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    pl = sum(raw.count(bytes([c])) for c in (0xB9, 0xEA, 0xB3, 0x9C, 0x9F, 0xBF, 0xE6, 0xF1))
    return raw.decode("cp1250" if pl > len(raw) / 400 else "cp1252", errors="replace")


def lang_from_name(path):
    """film.en.srt / film.eng.srt / film.en.whisper.srt -> 'en'"""
    for p in reversed(os.path.basename(path).lower().split(".")[1:-1]):
        if p in LANG_NAME:
            return p
        if p in ISO3:
            return ISO3[p]
    return ""


def sidecar_subs(video, tgt):
    """Pliki napisów leżące obok filmu (np. Film.en.srt)."""
    folder, base = os.path.split(os.path.splitext(video)[0])
    out = []
    try:
        names = sorted(os.listdir(folder or "."))
    except OSError:
        return out
    for n in names:
        low = n.lower()
        if (low.startswith(base.lower() + ".") and low.endswith((".srt", ".ass", ".ssa", ".vtt"))
                and not (tgt and low.endswith(f".{tgt}.srt"))):
            out.append(os.path.join(folder, n))
    return out


def translate_sub_cues(tr, cues, src, tgt, beam, progress, stop_evt):
    """Tłumaczy gotowe napisy. Kwestie dialogowe („- …” w każdej linii) linia po linii."""
    units = []
    for ci, c in enumerate(cues):
        dialog = len(c["lines"]) > 1 and all(x.startswith(("-", "–")) for x in c["lines"])
        c["dialog"] = dialog
        parts = c["lines"] if dialog else [" ".join(c["lines"])]
        cleaned = [clean_text(p) for p in parts]
        c["orig"] = "\n".join(("- " + p) if dialog else p for p in cleaned if p)
        for p in cleaned:
            if p:
                units.append((ci, p))
    out = {}
    step = 32
    for i in range(0, len(units), step):
        if stop_evt.is_set():
            raise Cancelled()
        chunk = units[i:i + step]
        for (ci, _), r in zip(chunk, tr.translate([u[1] for u in chunk], src, tgt, beam=beam)):
            out.setdefault(ci, []).append(r)
        progress(min(1.0, (i + len(chunk)) / max(1, len(units))))
    for ci, c in enumerate(cues):
        parts = [p for p in out.get(ci, []) if p]
        if not parts:
            c["trans"] = ""
        elif c["dialog"]:
            c["trans"] = "\n".join("- " + p for p in parts)
        else:
            c["trans"] = " ".join(parts)
    return cues


def write_srt(path, cues, key):
    n = 0
    with open(path, "w", encoding="utf-8-sig") as f:
        for c in cues:
            text = c.get(key)
            if not text:
                continue
            n += 1
            body = text if "\n" in text else wrap_sub(text)
            f.write(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{body}\n\n")
    return n


# ----------------------------------------------------------------- napisy z rozpoznawania mowy

def segment_to_cues(seg, max_chars=90, max_dur=7.0):
    words = [w for w in (seg.words or []) if w.word.strip()]
    if not words:
        t = clean_text(seg.text)
        return [dict(start=seg.start, end=seg.end, orig=t)] if t else []
    out, cur = [], []

    def flush():
        if cur:
            t = clean_text("".join(w.word for w in cur))
            if t:
                out.append(dict(start=cur[0].start, end=cur[-1].end, orig=t))
            cur.clear()

    for w in words:
        if cur and w.start - cur[-1].end > 1.0:
            flush()
        cur.append(w)
        length = len("".join(x.word for x in cur).strip())
        dur = cur[-1].end - cur[0].start
        tok = w.word.strip()
        if SENT_END.search(tok) and not ABBREV.search(tok):
            flush()
        elif length >= max_chars and tok.endswith((",", ";", ":")):
            flush()
        elif length >= max_chars + 25 or dur >= max_dur:
            flush()
    flush()
    return out


def merge_short(cues):
    """Dokleja bardzo krótkie kwestie (np. „Obiecuję.” na 0,5 s) do sąsiedniej."""
    out = []
    for c in cues:
        p = out[-1] if out else None
        if (p and c["start"] - p["end"] < 0.6
                and min(p["end"] - p["start"], c["end"] - c["start"]) < 1.0
                and len(p["orig"]) + len(c["orig"]) <= 80):
            p["end"] = c["end"]
            for k in ("orig", "trans"):
                if p.get(k) or c.get(k):
                    p[k] = f"{p.get(k) or ''} {c.get(k) or ''}".strip()
        else:
            out.append(c)
    return out


def finalize_timing(cues):
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + 10
        chars = max(len(c.get("orig") or ""), len(c.get("trans") or ""))
        want = max(c["end"] - c["start"] + 0.3, 1.2, chars / 16)
        c["end"] = max(c["start"] + 0.5, min(c["start"] + want, nxt - 0.05))


# ----------------------------------------------------------------- ffmpeg

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
_FFMPEG = None


def ffmpeg_exe():
    """Windows: ffmpeg z pakietu imageio-ffmpeg. macOS/Linux: systemowy ffmpeg, jeśli jest
    (zwykle pełniejszy), a jeśli nie ma – też ten z imageio-ffmpeg."""
    global _FFMPEG
    if _FFMPEG is None:
        system = None if IS_WIN else shutil.which("ffmpeg")
        if system:
            _FFMPEG = system
        else:
            import imageio_ffmpeg
            _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG


def run_ffmpeg(args, duration, on_progress, stop_evt, cwd=None):
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-nostats", "-progress", "pipe:1"] + args
    print("FFMPEG", subprocess.list2cmdline(cmd), flush=True)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         cwd=cwd, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
    err = collections.deque(maxlen=25)
    threading.Thread(target=lambda: err.extend(p.stderr), daemon=True).start()
    for line in p.stdout:
        if stop_evt.is_set():
            p.kill()
            p.wait()
            raise Cancelled()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                t = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            on_progress(min(1.0, max(0.0, t / max(1.0, duration))))
    rc = p.wait()
    if stop_evt.is_set():
        raise Cancelled()
    if rc != 0:
        time.sleep(0.2)
        raise RuntimeError("ffmpeg zakończył się błędem:\n" + "".join(err)[-900:])


# sprzętowe kodowanie obrazu (dużo szybsze niż procesorem); kolejność prób zależy od systemu
HW_ENCODERS = {"win32": ["h264_qsv", "h264_nvenc", "h264_amf"], "darwin": ["h264_videotoolbox"],
               "linux": ["h264_nvenc", "h264_qsv"]}
ENCODER_ARGS = {
    "h264_qsv": (["-global_quality", "23", "-preset", "medium"], "nv12"),
    "h264_nvenc": (["-preset", "p5", "-rc", "vbr", "-cq", "23"], "yuv420p"),
    "h264_amf": (["-quality", "balanced", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22"], "nv12"),
    "h264_videotoolbox": (["-b:v", "6M"], "nv12"),
    "libx264": (["-preset", "veryfast", "-crf", "21"], "yuv420p"),
}
_ENCODER = None


def video_encoder():
    """Pierwszy działający koder H.264: karta graficzna (Intel/NVIDIA/AMD/Apple), a jak nie – procesor."""
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = "libx264"
        plat = "linux" if sys.platform.startswith("linux") else sys.platform
        for enc in HW_ENCODERS.get(plat, []):
            try:
                r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-f", "lavfi", "-i", "color=black:s=320x240:d=0.5",
                                    "-vf", f"format={ENCODER_ARGS[enc][1]}", "-c:v", enc, "-f", "null", "-"],
                                   capture_output=True, creationflags=NO_WINDOW, timeout=60)
                if r.returncode == 0:
                    _ENCODER = enc
                    break
            except Exception:
                pass
        print("ENCODER", _ENCODER, flush=True)
    return _ENCODER


def has_subtitles_filter():
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True,
                       creationflags=NO_WINDOW, timeout=60)
    return " subtitles " in r.stdout


def _stream_lang(s):
    lang = (s.metadata.get("language") or "").lower()
    return ISO3.get(lang, lang if lang in LANG_NAME else "")


def probe_media(path):
    """Czas, obraz, ścieżki dźwięku i napisów w pliku."""
    import av
    with av.open(path, metadata_errors="ignore") as c:
        subs, audio = [], []
        for s in c.streams.subtitles:
            try:
                codec = s.codec_context.name or ""
            except Exception:
                codec = ""
            subs.append(dict(index=s.index, codec=codec, lang=_stream_lang(s),
                             title=s.metadata.get("title") or "", text=codec in TEXT_SUB_CODECS))
        for s in c.streams.audio:
            audio.append(dict(index=s.index, lang=_stream_lang(s), title=s.metadata.get("title") or "",
                              channels=getattr(s.codec_context, "channels", 0) or 0))
        size = None
        has_video = False
        for v in c.streams.video:
            try:
                if v.disposition & av.stream.Disposition.attached_pic:
                    continue  # okładka w pliku audio
            except Exception:
                pass
            has_video = True
            size = (v.codec_context.width, v.codec_context.height)
            break
        return dict(duration=(c.duration or 0) / 1e6, fmt=c.format.name or "", has_video=has_video,
                    size=size, subs=subs, audio=audio)


def read_audio_clip(path, start, dur):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.1f}", "-t", f"{dur:.1f}",
           "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=180)
    return np.frombuffer(r.stdout, dtype=np.float32)


def detect_spoken_language(path, media, whisper_key, status):
    """Wykrywa język mowy z dwóch 30-sekundowych fragmentów filmu."""
    d = media.get("duration") or 0
    starts = [d * 0.3, d * 0.6] if d > 180 else [0.0]
    audio = np.concatenate([read_audio_clip(path, s, 30) for s in starts])
    if len(audio) < SR * 3:
        return None, 0.0
    model = load_whisper(whisper_key, status)
    lang, prob, _ = model.detect_language(audio, vad_filter=True, language_detection_segments=2)
    return lang, prob


def out_base(video, out_dir=None):
    name = os.path.splitext(os.path.basename(video))[0]
    return os.path.join(out_dir or os.path.dirname(os.path.abspath(video)), name)


def export_path(base, code, mode):
    return f"{base} [{(code or 'napisy').upper()}].{mode}"


def export_video(video, srt, code, mode, media, base, on_progress, stop_evt):
    """mode='mkv': napisy jako ścieżka (bez kodowania, szybko);
    mode='mp4': napisy wtopione w obraz (kodowanie, działa na każdym odtwarzaczu)."""
    video = os.path.abspath(video)
    out = export_path(base, code, mode)
    try:
        if mode == "mkv":
            args = ["-i", video, "-i", os.path.abspath(srt), "-map", "0:v?", "-map", "0:a?", "-map", "1:0"]
            if "matroska" in media["fmt"]:
                args += ["-map", "0:s?", "-map", "0:t?"]  # zostaw oryginalne napisy i czcionki
            args += ["-c", "copy", "-c:s:0", "srt",
                     "-metadata:s:s:0", f"language={ISO3_OUT.get(code, 'und')}",
                     "-metadata:s:s:0", f"title={lang_label(code)} (AMT)",
                     "-disposition:s", "0", "-disposition:s:0", "default", out]
            run_ffmpeg(args, media["duration"], on_progress, stop_evt)
        else:
            if not has_subtitles_filter():
                raise RuntimeError("Ten FFmpeg nie umie wtapiać napisów (brak libass). Zainstaluj pełny ffmpeg "
                                   "(macOS: brew install ffmpeg, Linux: sudo apt install ffmpeg) albo wybierz "
                                   "„Film .mkv z napisami”.")
            tmp = tempfile.mkdtemp(prefix="amt_")
            try:
                shutil.copyfile(srt, os.path.join(tmp, "subs.srt"))
                enc = video_encoder()
                enc_args, pix = ENCODER_ARGS[enc]
                vf = ("subtitles=subs.srt:force_style='FontName=Arial,FontSize=18,Bold=1,"
                      f"Outline=1.5,Shadow=0.5,MarginV=20',format={pix}")
                venc = ["-c:v", enc] + enc_args
                args = (["-i", video, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf] + venc +
                        ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-movflags", "+faststart", out])
                run_ffmpeg(args, media["duration"], on_progress, stop_evt, cwd=tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except BaseException:
        try:
            os.remove(out)
        except OSError:
            pass
        raise
    return out


# ----------------------------------------------------------------- zadanie: tłumaczenie filmu

class FileJob(threading.Thread):
    def __init__(self, cfg, path, source, events, media=None):
        super().__init__(daemon=True)
        self.cfg, self.path, self.source, self.ev = cfg, path, source, events
        self.media = media
        self.stop_evt = threading.Event()
        self.t0 = time.time()
        self.stages = 1
        self.base = out_base(path, cfg.get("out_dir") or None)

    def status(self, text):
        self.ev.put(("status", text))

    def progress(self, frac, msg):
        self.ev.put(("progress", frac, msg))

    def stage(self, n):
        return f"Etap {n}/2 · " if self.stages == 2 else ""

    def run(self):
        try:
            self._run()
        except Cancelled:
            self.status("Anulowano.")
            self.progress(0.0, "Anulowano.")
        except Exception as e:
            traceback.print_exc()
            self.ev.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self.ev.put(("file_stopped", self))

    def _run(self):
        if self.media is None:
            try:
                self.media = probe_media(self.path)
            except Exception:
                traceback.print_exc()
                self.media = dict(duration=0.0, fmt="", has_video=False, size=None, subs=[], audio=[])
        export = self.cfg["export"] if self.media["has_video"] else "srt"
        self.stages = 2 if export != "srt" else 1
        kind, d = self.source
        if kind == "whisper":
            written, final_srt, code = self._from_speech()
        else:
            written, final_srt, code = self._from_subtitles(kind, d)
        video_out = None
        if export != "srt":
            what = "z wtopionymi napisami (.mp4)" if export == "mp4" else "z napisami (.mkv)"
            self.status(f"Zapisywanie filmu {what}…")
            t1 = time.time()

            def prog(f):
                eta = f" · zostało ok. {fmt_dur((time.time() - t1) / f * (1 - f))}" if f > 0.03 else ""
                self.progress(f, f"{self.stage(2)}zapisywanie filmu {what} – {f * 100:.0f}%{eta}")

            video_out = export_video(self.path, final_srt, code, export, self.media, self.base, prog, self.stop_evt)
            written.append(video_out)
        self.progress(1.0, f"Gotowe w {fmt_dur(time.time() - self.t0)}.")
        self.ev.put(("file_done", written, video_out, final_srt))

    # --- z gotowych napisów (wbudowanych w film albo z pliku)
    def _from_subtitles(self, kind, d):
        cfg, tgt = self.cfg, self.cfg["tgt"]
        tmp = tempfile.mkdtemp(prefix="amt_")
        try:
            if kind == "stream":
                self.status("Wyciąganie napisów z pliku filmu…")
                sub = os.path.join(tmp, "napisy.srt")
                run_ffmpeg(["-i", os.path.abspath(self.path), "-map", f"0:{d['index']}", "-c:s", "srt", sub],
                           self.media["duration"],
                           lambda f: self.progress(f * 0.3, f"{self.stage(1)}wyciąganie napisów z filmu – {f * 100:.0f}%"),
                           self.stop_evt)
            elif d["path"].lower().endswith(".srt"):
                sub = d["path"]
            else:
                sub = os.path.join(tmp, "napisy.srt")
                run_ffmpeg(["-i", d["path"], "-c:s", "srt", sub], 1.0, lambda f: None, self.stop_evt)
            cues = parse_srt(read_text_file(sub))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not cues:
            raise RuntimeError("W wybranym źródle nie ma żadnych napisów tekstowych.")
        hint = d.get("lang", "")
        if cfg["src"] != "auto":
            src = cfg["src"]
        elif hint in NLLB_CODE:
            src = hint
        else:
            src = guess_lang([" ".join(c["lines"]) for c in cues[:400]])
        if not tgt or tgt == src or src not in NLLB_CODE:
            self.status(f"Napisy są już w języku: {lang_label(src)} – bez tłumaczenia.")
            if kind == "file" and d["path"].lower().endswith(".srt"):
                return [], d["path"], src
            for c in cues:
                c["orig"] = "\n".join(c["lines"])
            p = self.base + f".{src}.srt"
            if os.path.exists(p):
                p = self.base + f".{src}.z-filmu.srt"
            write_srt(p, cues, "orig")
            return [p], p, src
        tr = get_translator(cfg["nllb"], self.status)
        self.status(f"Tłumaczenie napisów: {lang_label(src)} → {lang_label(tgt)}…")
        t1 = time.time()
        lo = 0.3 if kind == "stream" else 0.0

        def prog(f):
            eta = f" · zostało ok. {fmt_dur((time.time() - t1) / f * (1 - f))}" if f > 0.03 else ""
            self.progress(lo + (1 - lo) * f, f"{self.stage(1)}tłumaczenie napisów – {int(f * len(cues))} z {len(cues)}{eta}")

        translate_sub_cues(tr, cues, src, tgt, 4 if cfg["accurate"] else 2, prog, self.stop_evt)
        tr_path = self.base + f".{tgt}.srt"
        written = [tr_path]
        write_srt(tr_path, cues, "trans")
        if cfg["save_orig"]:
            p = self.base + f".{src}.oryginal.srt"
            write_srt(p, cues, "orig")
            written.append(p)
        return written, tr_path, tgt

    # --- z rozpoznawania mowy
    def _from_speech(self):
        cfg = self.cfg
        model = load_whisper(cfg["whisper"], self.status)
        tr = get_translator(cfg["nllb"], self.status) if cfg["tgt"] else None
        self.status("Wczytywanie dźwięku z filmu i szukanie fragmentów z mową (1–3 min)…")
        self.progress(0.0, f"{self.stage(1)}przygotowanie…")
        acc = cfg["accurate"]
        segments, info = model.transcribe(
            self.path, language=None if cfg["src"] == "auto" else cfg["src"],
            beam_size=5 if acc else 1, best_of=5 if acc else 1,
            temperature=[0.0, 0.2, 0.4, 0.6] if acc else 0.0,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
            word_timestamps=True, condition_on_previous_text=False,
            hallucination_silence_threshold=2.0, language_detection_segments=4)
        src = info.language if cfg["src"] == "auto" else cfg["src"]
        tgt = cfg["tgt"]
        do_tr = bool(tr and tgt and src != tgt and src in NLLB_CODE)
        self.status(f"Język filmu: {lang_label(src)}. Rozpoznawanie mowy" + (" i tłumaczenie…" if do_tr else "…"))
        duration = max(1.0, info.duration)
        cues, pending = [], []
        t0 = time.time()
        beam_tr = 4 if acc else 2

        def translate_pending():
            if pending:
                for c, o in zip(pending, tr.translate([c["orig"] for c in pending], src, tgt, beam=beam_tr)):
                    c["trans"] = o
                pending.clear()

        for seg in segments:
            if self.stop_evt.is_set():
                raise Cancelled()
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            new = segment_to_cues(seg)
            cues.extend(new)
            if do_tr:
                pending.extend(new)
                if len(pending) >= 24:
                    translate_pending()
            frac = min(1.0, seg.end / duration)
            msg = f"{self.stage(1)}rozpoznawanie mowy – {fmt_clock(seg.end)} / {fmt_clock(duration)}"
            if frac > 0.02:
                msg += f" · zostało ok. {fmt_dur((time.time() - t0) / frac * (1 - frac))}"
            if new:
                msg += f"\n„{(new[-1].get('orig') or '')[:90]}”"
            self.progress(frac, msg)
        if do_tr:
            translate_pending()
        cues = merge_short(cues)
        finalize_timing(cues)
        tr_path = self.base + f".{tgt}.srt"
        orig_path = self.base + f".{src}.whisper.srt"
        written = []
        if do_tr:
            write_srt(tr_path, cues, "trans")
            written.append(tr_path)
        if cfg["save_orig"] or not do_tr:
            write_srt(orig_path, cues, "orig")
            written.append(orig_path)
        return written, (tr_path if do_tr else orig_path), (tgt if do_tr else src)


def choose_source(media, video, tgt):
    """Lista źródeł tekstu: [(rodzaj, dane, opis)] i indeks najlepszego."""
    opts = [("whisper", None, "Rozpoznawanie mowy (gdy brak napisów)")]
    for s in (media or {}).get("subs", []):
        if s["text"]:
            title = f" – {s['title']}" if s["title"] else ""
            opts.append(("stream", s, f"Napisy z filmu: {lang_label(s['lang'])}{title} (ścieżka {s['index']})"))
    for f in sidecar_subs(video, tgt):
        opts.append(("file", dict(path=f, lang=lang_from_name(f)), f"Plik obok filmu: {os.path.basename(f)}"))
    best = 0
    for i, (kind, d, _) in enumerate(opts):
        if kind in ("stream", "file") and d.get("lang") != tgt:
            best = i
            break
    return opts, best


# ----------------------------------------------------------------- okno

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox  # noqa: E402

import customtkinter as ctk  # noqa: E402

from amt_core import Dropdown  # noqa: E402

ACCENT, ACCENT_HOVER = ACCENTS["amt"]


def short_path(p, n=60):
    p = os.path.normpath(p)
    return p if len(p) <= n else p[:20] + " … " + p[-(n - 23):]


def round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def card(master, **kw):
    return ctk.CTkFrame(master, fg_color=COLORS["card"], corner_radius=14, border_width=1,
                        border_color=COLORS["card_border"], **kw)


class DropZone(tk.Canvas):
    """Duże pole „Przeciągnij tutaj film” (rysowane z uwzględnieniem powiększenia ekranu)."""
    H = 150

    def __init__(self, master, on_click, fonts):
        super().__init__(master, height=int(self.H * ui_scaling(master)), bg=COLORS["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.fonts = fonts
        self.hover = self.drag = False
        self.title = "Przeciągnij tutaj film"
        self.sub = "albo kliknij, aby wybrać plik   ·   .mkv  .mp4  .avi  .mov …"
        self.has_file = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self.set_state(hover=True))
        self.bind("<Leave>", lambda e: self.set_state(hover=False))
        self.bind("<Button-1>", lambda e: on_click())

    def set_text(self, title, sub, has_file):
        self.title, self.sub, self.has_file = title, sub, has_file
        self.rescale()

    def rescale(self):
        self.configure(height=int((112 if self.has_file else self.H) * ui_scaling(self)))
        self.redraw()

    def _fit(self, text, font, width):
        """Skraca tekst „…” w środku, żeby zmieścił się w podanej szerokości."""
        import tkinter.font as tkfont
        f = tkfont.Font(font=font)
        if f.measure(text) <= width:
            return text
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            cut = text[:mid // 2] + " … " + text[len(text) - (mid - mid // 2):]
            if f.measure(cut) <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo // 2] + " … " + text[len(text) - (lo - lo // 2):]

    def set_state(self, hover=None, drag=None):
        if hover is not None:
            self.hover = hover
        if drag is not None:
            self.drag = drag
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        s = ui_scaling(self)
        active = self.hover or self.drag
        col = ACCENT if (active or not self.has_file) else COLORS["text"]
        round_rect(self, 2 * s, 2 * s, w - 2 * s, h - 2 * s, 18 * s,
                   fill="#262833" if active else COLORS["card"],
                   outline=ACCENT if (active or not self.has_file) else COLORS["card_border"], width=max(2, int(2 * s)))
        f_title = (UI_FONT, -int(21 * s), "bold")  # tk.Canvas nie skaluje czcionek CTk – liczymy piksele sami
        f_sub = (UI_FONT, -int(14 * s))
        if self.has_file:
            x, y, r = 58 * s, h / 2, 24 * s
            round_rect(self, x - r, y - r * 0.75, x + r, y + r * 0.75, r * 0.25, fill="", outline=col,
                       width=max(2, int(2.5 * s)))
            for i in range(5):
                xx = x - r * 0.8 + i * r * 0.4
                for yy in (y - r * 0.55, y + r * 0.42):
                    self.create_rectangle(xx, yy, xx + r * 0.18, yy + r * 0.13, fill=col, outline="")
            self.create_polygon(x - r * 0.22, y - r * 0.28, x - r * 0.22, y + r * 0.28, x + r * 0.32, y,
                                fill=col, outline="")
            avail = w - 124 * s
            self.create_text(104 * s, h / 2 - 13 * s, text=self._fit(self.title, f_title, avail), anchor="w",
                             fill=COLORS["text"], font=f_title)
            self.create_text(104 * s, h / 2 + 16 * s, text=self._fit(self.sub, f_sub, avail), anchor="w",
                             fill=COLORS["muted"], font=f_sub)
        else:
            x, y, r = w / 2, h / 2 - 26 * s, 22 * s
            self.create_oval(x - r, y - r, x + r, y + r, outline=col, width=max(2, int(3 * s)))
            self.create_line(x, y - r * 0.5, x, y + r * 0.45, fill=col, width=max(3, int(4 * s)), capstyle="round")
            self.create_line(x - r * 0.4, y + r * 0.05, x, y + r * 0.48, x + r * 0.4, y + r * 0.05, fill=col,
                             width=max(3, int(4 * s)), capstyle="round", joinstyle="round")
            self.create_text(w / 2, h / 2 + 18 * s, text="Upuść film tutaj" if self.drag else self.title,
                             fill=COLORS["text"], font=f_title)
            self.create_text(w / 2, h / 2 + 46 * s, text=self.sub, fill=COLORS["muted"], font=f_sub)


class AMTApp:
    EXPORTS = [("srt", "Napisy .srt", "Sam plik napisów obok filmu – VLC i inne odtwarzacze wczytają go same."),
               ("mkv", "Film .mkv", "Film z dodaną ścieżką napisów – szybko (1–3 min), bez utraty jakości."),
               ("mp4", "Film .mp4", "Napisy wtopione w obraz – działa wszędzie (TV, telefon), ale wolniej.")]

    def _export_changed(self, title):
        self.v_export.set(next(k for k, t, _ in self.EXPORTS if t == title))

    def _sync_export(self):
        key = self.v_export.get()
        for k, t, d in self.EXPORTS:
            if k == key:
                self.v_export_btn.set(t)
                self.v_export_desc.set(d)

    def __init__(self, root, args, dnd):
        self.root, self.args = root, args
        self.s = load_settings(APP, DEFAULTS)
        self.events = queue.Queue()
        self.job = None
        self.path = None
        self.media = None
        self.sources, self.picked = [], []
        self.result = None
        self.f = make_fonts()
        self.wrap = AutoWrap()
        root.title("AMT – Automatic Movie Translator")
        set_window_icon(root, os.path.join(ASSETS, "amt.ico"), os.path.join(ASSETS, "amt_256.png"))
        fit_geometry(root, 1120, 680)
        root.minsize(900, 560)
        self._build()
        if dnd:
            for w in (root, self.drop):
                try:
                    w.drop_target_register("DND_Files")
                    w.dnd_bind("<<Drop>>", self._on_drop)
                    w.dnd_bind("<<DropEnter>>", lambda e: (self.drop.set_state(drag=True), e.action)[1])
                    w.dnd_bind("<<DropLeave>>", lambda e: (self.drop.set_state(drag=False), e.action)[1])
                except Exception:
                    traceback.print_exc()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll)
        if args.get("file"):
            root.after(300, lambda: self.set_file(args["file"]))
        if args.get("settings"):
            root.after(600, self._open_settings)
        if args.get("quit_after"):
            root.after(int(args["quit_after"] * 1000), self._on_close)

    # ------------------------------------------------------------- budowa okna
    def _label(self, master, text="", font="body", color="text", **kw):
        return ctk.CTkLabel(master, text=text, font=self.f[font], text_color=COLORS[color], **kw)

    def _menu(self, master, opts, current, width, command=None):
        names = [o[1] for o in opts]
        keys = [o[0] for o in opts]
        var = tk.StringVar(value=names[keys.index(current)] if current in keys else names[0])
        m = Dropdown(master, values=names, variable=var, width=width, command=command, accent=ACCENT)
        m._keys, m._names, m._var = keys, names, var
        return m

    @staticmethod
    def _key(m):
        return m._keys[m._names.index(m._var.get())]

    def _build(self):
        r = self.root
        r.grid_columnconfigure(0, weight=11, uniform="col")
        r.grid_columnconfigure(1, weight=10, uniform="col")
        r.grid_rowconfigure(1, weight=1)
        r.grid_propagate(False)

        # --- nagłówek
        head = ctk.CTkFrame(r, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=26, pady=(14, 4))
        try:
            from PIL import Image
            self.logo = ctk.CTkImage(Image.open(os.path.join(ASSETS, "amt_256.png")), size=(54, 54))
            ctk.CTkLabel(head, text="", image=self.logo).pack(side="left", padx=(0, 16))
        except Exception:
            traceback.print_exc()
        t = ctk.CTkFrame(head, fg_color="transparent")
        t.pack(side="left")
        self._label(t, "AMT", "title").pack(anchor="w")
        self._label(t, "Automatic Movie Translator  ·  tłumaczenie filmów na Twoim komputerze, bez internetu",
                    "small", "muted").pack(anchor="w")
        ctk.CTkButton(head, text="Ustawienia", width=120, height=36, font=self.f["body"], fg_color=COLORS["input"],
                      hover_color=COLORS["input_hover"], border_width=1, border_color=COLORS["card_border"],
                      command=self._open_settings).pack(side="right")

        # --- lewa kolumna: film i co wykryto
        left = ctk.CTkFrame(r, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=(26, 10), pady=(8, 20))
        self.drop = DropZone(left, self.choose_file, self.f)
        self.drop.pack(fill="x")
        self.info = card(left)
        self.info.pack(fill="both", expand=True, pady=(14, 0))
        self._label(self.info, "Co jest w filmie", "h2").grid(row=0, column=0, columnspan=2, sticky="w",
                                                             padx=20, pady=(16, 8))
        self.info.grid_columnconfigure(1, weight=1)
        self.info_rows = {}
        for i, (key, label) in enumerate([("time", "Czas trwania"), ("video", "Obraz"), ("audio", "Język mowy"),
                                          ("subs", "Napisy w pliku"), ("side", "Napisy obok filmu")]):
            self._label(self.info, label, "body", "muted").grid(row=i + 1, column=0, sticky="nw", padx=(20, 16), pady=3)
            v = tk.StringVar(value="—")
            lb = self.wrap(ctk.CTkLabel(self.info, textvariable=v, font=self.f["body"], text_color=COLORS["text"],
                                        anchor="w", justify="left"), self.info, 220)
            lb.grid(row=i + 1, column=1, sticky="w", padx=(0, 20), pady=3)
            self.info_rows[key] = v
        self.v_notice = tk.StringVar()
        self.notice = ctk.CTkLabel(self.info, textvariable=self.v_notice, font=self.f["body"],
                                   text_color=COLORS["ok"], anchor="w", justify="left")
        self.info_hint = self._label(self.info, "Wybierz film, a AMT sprawdzi jego język i napisy.", "small", "faint")
        self.info_hint.grid(row=7, column=0, columnspan=2, sticky="w", padx=20, pady=(10, 16))

        # --- prawa kolumna: ustawienia tłumaczenia i start
        right = ctk.CTkFrame(r, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 26), pady=(8, 20))
        sc = card(right)
        sc.pack(fill="x")
        sc.grid_columnconfigure(0, weight=1)
        self._label(sc, "Tłumaczenie", "h2").grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 8))
        self._label(sc, "Tekst weź z", "small", "muted").grid(row=1, column=0, columnspan=2, sticky="w", padx=20)
        self.m_source = self._menu(sc, [("whisper", "— najpierw wybierz film —")], "whisper", 330)
        self.m_source.grid(row=2, column=0, sticky="ew", padx=(20, 8), pady=(2, 10))
        ctk.CTkButton(sc, text="Plik napisów…", width=120, height=36, font=self.f["small"], fg_color=COLORS["input"],
                      hover_color=COLORS["input_hover"], command=self._pick_subs).grid(row=2, column=1, padx=(0, 20),
                                                                                       pady=(2, 10))
        lf = ctk.CTkFrame(sc, fg_color="transparent")
        lf.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 10))
        lf.grid_columnconfigure((0, 1), weight=1)
        self._label(lf, "Język filmu", "small", "muted").grid(row=0, column=0, sticky="w")
        self._label(lf, "Tłumacz na", "small", "muted").grid(row=0, column=1, sticky="w", padx=(12, 0))
        src_opts = [("auto", "wykryj automatycznie")] + [(c, n) for n, c, _ in LANGS if c != "pl"] + [("pl", "polski")]
        self.m_src = self._menu(lf, src_opts, self.s["src"], 190)
        self.m_src.grid(row=1, column=0, sticky="ew")
        self.m_tgt = self._menu(lf, [(c, n) for n, c, _ in LANGS], self.s["tgt"], 190,
                                command=lambda v: self._update_sources())
        self.m_tgt.grid(row=1, column=1, sticky="ew", padx=(12, 0))
        self._label(sc, "Wynik", "small", "muted").grid(row=4, column=0, columnspan=2, sticky="w", padx=20)
        self.v_export = tk.StringVar(value=self.s.get("export", "srt"))
        self.v_export_btn = tk.StringVar(value=next(t for k, t, _ in self.EXPORTS if k == self.v_export.get()))
        self.seg_export = ctk.CTkSegmentedButton(
            sc, values=[t for _, t, _ in self.EXPORTS], variable=self.v_export_btn, height=38, font=self.f["body"],
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, unselected_color=COLORS["input"],
            unselected_hover_color=COLORS["input_hover"], command=self._export_changed)
        self.seg_export.grid(row=5, column=0, columnspan=2, sticky="ew", padx=20, pady=(4, 4))
        self.v_export_desc = tk.StringVar()
        self._label(sc, "", "small", "faint", textvariable=self.v_export_desc).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 16))
        self.v_export.trace_add("write", lambda *a: self._sync_export())
        self._sync_export()

        act = card(right)
        act.pack(fill="both", expand=True, pady=(14, 0))
        act.grid_columnconfigure(0, weight=1)
        bf = ctk.CTkFrame(act, fg_color="transparent")
        bf.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        self.b_go = ctk.CTkButton(bf, text="Przetłumacz film", height=48, font=self.f["button"], fg_color=ACCENT,
                                  hover_color=ACCENT_HOVER, corner_radius=12, command=self.start,
                                  text_color_disabled="#c9ceff")
        self.b_go.pack(side="left", fill="x", expand=True)
        self.b_cancel = ctk.CTkButton(bf, text="Anuluj", width=110, height=48, font=self.f["button"],
                                      fg_color=COLORS["input"], hover_color=COLORS["input_hover"], corner_radius=12,
                                      command=self.cancel)
        self.pb = ctk.CTkProgressBar(act, height=10, progress_color=ACCENT, fg_color=COLORS["input"])
        self.pb.set(0)
        self.v_status = tk.StringVar(value="Wybierz film, żeby zacząć.")
        self.l_status = self.wrap(ctk.CTkLabel(act, textvariable=self.v_status, font=self.f["body"],
                                               text_color=COLORS["text"], anchor="w", justify="left"), act, 50)
        self.v_prog = tk.StringVar(value="")
        self.l_prog = self.wrap(ctk.CTkLabel(act, textvariable=self.v_prog, font=self.f["small"],
                                             text_color=COLORS["muted"], anchor="w", justify="left"), act, 50)
        self.done = ctk.CTkFrame(act, fg_color=COLORS["input"], corner_radius=12)
        self._show_progress()
        self._label(self.done, "✔  Gotowe", "big", "ok").pack(anchor="w", padx=16, pady=(12, 2))
        self.v_done = tk.StringVar()
        self.wrap(ctk.CTkLabel(self.done, textvariable=self.v_done, font=self.f["small"], text_color=COLORS["muted"],
                               anchor="w", justify="left"), self.done, 40).pack(anchor="w", padx=16)
        db = ctk.CTkFrame(self.done, fg_color="transparent")
        db.pack(anchor="w", padx=16, pady=(8, 14))
        ctk.CTkButton(db, text="Pokaż w folderze", width=150, height=36, font=self.f["small"], fg_color=COLORS["card"],
                      hover_color=COLORS["input_hover"], command=self._open_folder).pack(side="left")
        self.b_play = ctk.CTkButton(db, text="Odtwórz film", width=150, height=36, font=self.f["small"],
                                    fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._play)
        self.b_play.pack(side="left", padx=(8, 0))
        self._update_sources()

    def _show_progress(self):
        """Pasek postępu i komunikaty (zamiast karty „Gotowe”)."""
        self.done.grid_forget()
        self.pb.grid(row=1, column=0, sticky="ew", padx=20, pady=(6, 8))
        self.l_status.grid(row=2, column=0, sticky="w", padx=20)
        self.l_prog.grid(row=3, column=0, sticky="w", padx=20, pady=(0, 14))

    def _show_done(self):
        for w in (self.pb, self.l_status, self.l_prog):
            w.grid_forget()
        self.done.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 18))

    # ------------------------------------------------------------- okno ustawień
    def _open_settings(self):
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.focus()
            return
        w = ctk.CTkToplevel(self.root, fg_color=COLORS["bg"])
        self._settings_win = w
        w.title("AMT – ustawienia")
        set_window_icon(w, os.path.join(ASSETS, "amt.ico"), os.path.join(ASSETS, "amt_256.png"))
        w.transient(self.root)
        w.resizable(False, False)
        c = card(w)
        c.pack(fill="both", expand=True, padx=18, pady=18)
        c.grid_columnconfigure(1, weight=1)
        row = 0

        def label(text):
            self._label(c, text, "body", "muted").grid(row=row, column=0, sticky="w", padx=(20, 16), pady=8)

        self._label(c, "Ustawienia", "h2").grid(row=row, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 6))
        row += 1
        label("Rozpoznawanie mowy")
        self.m_wh = self._menu(c, [(k, v[1]) for k, v in WHISPER_MODELS.items()], self.s["whisper"], 380)
        self.m_wh.grid(row=row, column=1, sticky="w", padx=(0, 20))
        row += 1
        label("Model tłumaczenia")
        self.m_nl = self._menu(c, [(k, v[1]) for k, v in NLLB_MODELS.items()], self.s["nllb"], 380)
        self.m_nl.grid(row=row, column=1, sticky="w", padx=(0, 20))
        row += 1
        self.v_acc = tk.BooleanVar(value=self.s["accurate"])
        ctk.CTkSwitch(c, text="Dokładniej (wolniej, lepsza jakość)", variable=self.v_acc, font=self.f["body"],
                      progress_color=ACCENT).grid(row=row, column=0, columnspan=2, sticky="w", padx=20, pady=8)
        row += 1
        self.v_orig = tk.BooleanVar(value=self.s["save_orig"])
        ctk.CTkSwitch(c, text="Zapisuj też napisy w oryginalnym języku", variable=self.v_orig, font=self.f["body"],
                      progress_color=ACCENT).grid(row=row, column=0, columnspan=2, sticky="w", padx=20, pady=8)
        row += 1
        label("Zapisuj do")
        of = ctk.CTkFrame(c, fg_color="transparent")
        of.grid(row=row, column=1, sticky="w", padx=(0, 20))
        self.v_out_lbl = tk.StringVar(value=short_path(self.s["out_dir"], 40) if self.s["out_dir"] else "folderu z filmem")
        ctk.CTkLabel(of, textvariable=self.v_out_lbl, font=self.f["body"]).pack(side="left")
        ctk.CTkButton(of, text="Zmień…", width=90, height=32, font=self.f["small"], fg_color=COLORS["input"],
                      hover_color=COLORS["input_hover"], command=self._pick_out).pack(side="left", padx=(12, 0))
        ctk.CTkButton(of, text="Obok filmu", width=100, height=32, font=self.f["small"], fg_color=COLORS["input"],
                      hover_color=COLORS["input_hover"], command=lambda: self._set_out("")).pack(side="left", padx=(6, 0))
        row += 1
        label("Wielkość okna i tekstu")
        sf = ctk.CTkFrame(c, fg_color="transparent")
        sf.grid(row=row, column=1, sticky="w", padx=(0, 20))
        ctk.CTkSegmentedButton(sf, values=list(UI_SCALES), font=self.f["small"], selected_color=ACCENT,
                               selected_hover_color=ACCENT_HOVER, command=self._set_ui_scale,
                               variable=tk.StringVar(value=self.s.get("ui_scale", "100%"))).pack(side="left")
        row += 1
        self._label(c, "Wielkość dopasowuje się też do powiększenia ustawionego w systemie.", "small", "faint").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 6))
        row += 1
        ctk.CTkButton(c, text="Gotowe", width=120, height=38, font=self.f["button"], fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=lambda: (self._cfg(), w.destroy())).grid(
            row=row, column=0, columnspan=2, sticky="e", padx=20, pady=(10, 18))
        w.protocol("WM_DELETE_WINDOW", lambda: (self._cfg(), w.destroy()))
        w.after(100, w.lift)

    def _set_ui_scale(self, value):
        self.s["ui_scale"] = value
        ctk.set_widget_scaling(UI_SCALES[value])
        ctk.set_window_scaling(UI_SCALES[value])
        self.root.after(200, self.drop.rescale)
        save_settings(APP, self.s)

    def _set_out(self, path):
        self.s["out_dir"] = path
        if getattr(self, "v_out_lbl", None):
            self.v_out_lbl.set(short_path(path, 40) if path else "folderu z filmem")

    def _pick_out(self):
        p = filedialog.askdirectory(title="Gdzie zapisywać gotowe pliki?", initialdir=self.s["out_dir"] or None)
        if p:
            self._set_out(os.path.normpath(p))

    # ------------------------------------------------------------- wybór filmu
    def choose_file(self):
        if self.job:
            return
        p = filedialog.askopenfilename(
            title="Wybierz film", initialdir=self.s.get("last_dir") or None,
            filetypes=[("Filmy", " ".join("*" + e for e in VIDEO_EXT)), ("Dźwięk", " ".join("*" + e for e in AUDIO_EXT)),
                       ("Wszystkie pliki", "*.*")])
        if p:
            self.set_file(p)

    def _on_drop(self, e):
        self.drop.set_state(drag=False)
        files = [f for f in self.root.tk.splitlist(e.data) if os.path.isfile(f)]
        if files and not self.job:
            self.set_file(files[0])
        return e.action

    def set_file(self, path):
        path = os.path.normpath(path)
        self.path, self.media, self.result = path, None, None
        self.s["last_dir"] = os.path.dirname(path)
        self.done.grid_forget()
        self.drop.set_text(os.path.basename(path), f"{os.path.dirname(path)}   ·   kliknij, aby zmienić", True)
        for v in self.info_rows.values():
            v.set("…")
        self.v_notice.set("")
        self.notice.grid_forget()
        self.info_hint.grid_forget()
        self._show_progress()
        self.pb.set(0)
        self.v_prog.set("")
        self.v_status.set("Sprawdzam film…")
        self._update_sources()
        threading.Thread(target=self._analyze, args=(path, self.s["whisper"]), daemon=True).start()

    def _analyze(self, path, whisper_key):
        try:
            media = probe_media(path)
        except Exception as e:
            traceback.print_exc()
            self.events.put(("probe_error", path, str(e)))
            return
        self.events.put(("probe", path, media))
        if media["audio"]:
            try:
                lang, prob = detect_spoken_language(path, media, whisper_key, lambda t: self.events.put(("status", t)))
                self.events.put(("lang", path, lang, prob))
            except Exception:
                traceback.print_exc()
                self.events.put(("lang", path, None, 0.0))

    def _audio_text(self, detected=None):
        tags = [lang_label(a["lang"]) for a in self.media["audio"] if a["lang"]]
        pre = ("ścieżki: " + ", ".join(tags) + "  ·  ") if tags else ""
        return pre + (detected or "wykrywam z dźwięku…")

    def _show_probe(self, media):
        r = self.info_rows
        r["time"].set(fmt_clock(media["duration"]) if media["duration"] else "—")
        r["video"].set(f"{media['size'][0]} × {media['size'][1]}" if media["size"] else "brak obrazu (plik audio)")
        r["audio"].set(self._audio_text() if media["audio"] else "brak dźwięku")
        subs = [f"{lang_label(s['lang']) if s['lang'] else 'język nieznany'} ({'tekst' if s['text'] else 'obraz'})"
                for s in media["subs"]]
        r["subs"].set(", ".join(subs) if subs else "brak")
        side = sidecar_subs(self.path, "")
        r["side"].set(", ".join(os.path.basename(f) for f in side) if side else "brak")
        tgt = self._key(self.m_tgt)
        have = [s for s in media["subs"] if s["lang"] == tgt] + [f for f in side if lang_from_name(f) == tgt]
        if have:
            self.v_notice.set(f"Film ma już napisy w języku: {lang_label(tgt)}.")
            self.notice.grid(row=6, column=0, columnspan=2, sticky="w", padx=20, pady=(8, 0))
        if not media["has_video"] and self.v_export.get() != "srt":
            self.v_export.set("srt")

    def _update_sources(self):
        tgt = self._key(self.m_tgt)
        if not self.path:
            self.sources, best = [("whisper", None, "— najpierw wybierz film —")], 0
        else:
            self.sources, best = choose_source(self.media, self.path, tgt)
            known = {o[1]["path"] for o in self.sources if o[0] == "file"}
            self.sources += [o for o in self.picked if o[1]["path"] not in known]
        names = [o[2] for o in self.sources]
        self.m_source.configure(values=names)
        self.m_source._names, self.m_source._keys = names, list(range(len(names)))
        self.m_source._var.set(names[best])

    def _pick_subs(self):
        p = filedialog.askopenfilename(
            title="Wybierz plik napisów do przetłumaczenia",
            initialdir=os.path.dirname(self.path) if self.path else (self.s.get("last_dir") or None),
            filetypes=[("Napisy", "*.srt *.ass *.ssa *.vtt"), ("Wszystkie pliki", "*.*")])
        if not p:
            return
        p = os.path.normpath(p)
        self.picked.append(("file", dict(path=p, lang=lang_from_name(p)), f"Plik napisów: {os.path.basename(p)}"))
        self._update_sources()
        for o in self.sources:
            if o[0] == "file" and o[1]["path"] == p:
                self.m_source._var.set(o[2])

    # ------------------------------------------------------------- praca
    def _cfg(self):
        s = self.s
        s["src"], s["tgt"] = self._key(self.m_src), self._key(self.m_tgt)
        if getattr(self, "m_wh", None) and self.m_wh.winfo_exists():
            s["whisper"], s["nllb"] = self._key(self.m_wh), self._key(self.m_nl)
            s["accurate"], s["save_orig"] = self.v_acc.get(), self.v_orig.get()
        s["export"] = self.v_export.get()
        save_settings(APP, s)
        return dict(s)

    def _lock(self, running):
        st = "disabled" if running else "normal"
        for m in (self.m_source, self.m_src, self.m_tgt):
            m.configure(state=st)
        if running:
            self.b_go.configure(state="disabled", text="Trwa tłumaczenie…")
            self.b_cancel.pack(side="left", padx=(10, 0))
            self.b_cancel.configure(state="normal", text="Anuluj")
        else:
            self.b_go.configure(state="normal", text="Przetłumacz film")
            self.b_cancel.pack_forget()

    def start(self):
        if self.job:
            return
        if not self.path or not os.path.isfile(self.path):
            self.choose_file()
            return
        cfg = self._cfg()
        kind, d, _ = self.sources[self.m_source._names.index(self.m_source._var.get())]
        if cfg["out_dir"] and not os.path.isdir(cfg["out_dir"]):
            messagebox.showwarning("AMT", "Folder zapisu nie istnieje – wybierz inny w „Ustawieniach”.")
            return
        base = out_base(self.path, cfg["out_dir"] or None)
        targets = [base + f".{cfg['tgt']}.srt"]
        if cfg["export"] != "srt" and (self.media is None or self.media["has_video"]):
            targets.append(export_path(base, cfg["tgt"], cfg["export"]))
        existing = [p for p in targets if os.path.exists(p)]
        if existing and not messagebox.askyesno("AMT", "Te pliki już istnieją:\n\n" + "\n".join(existing)
                                                + "\n\nNadpisać je?"):
            return
        self._show_progress()
        self.result = None
        self.pb.set(0)
        self.job = FileJob(cfg, self.path, (kind, d), self.events, media=self.media)
        self.job.start()
        self._lock(True)

    def cancel(self):
        if self.job:
            self.job.stop_evt.set()
            self.b_cancel.configure(state="disabled", text="Anulowanie…")

    def _open_folder(self):
        target = (self.result or {}).get("first") or self.path
        if target:
            reveal_path(target)

    def _play(self):
        target = (self.result or {}).get("video") or self.path
        if target and os.path.exists(target):
            open_path(target)

    # ------------------------------------------------------------- zdarzenia
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.v_status.set(ev[1])
                elif kind == "progress":
                    self.pb.set(ev[1])
                    self.v_prog.set(ev[2])
                elif kind == "probe" and ev[1] == self.path:
                    self.media = ev[2]
                    self._show_probe(self.media)
                    self._update_sources()
                    self.v_status.set("Film sprawdzony. Wybierz wynik i kliknij „Przetłumacz film”.")
                elif kind == "probe_error" and ev[1] == self.path:
                    self.v_status.set("Nie mogę odczytać tego pliku – czy to na pewno film?")
                    for v in self.info_rows.values():
                        v.set("—")
                elif kind == "lang" and ev[1] == self.path and self.media:
                    _, _, lang, prob = ev
                    self.info_rows["audio"].set(self._audio_text(
                        f"wykryto: {lang_label(lang)} ({prob * 100:.0f}%)" if lang else "nie udało się wykryć"))
                    if not self.job:
                        self.v_status.set("Film sprawdzony. Wybierz wynik i kliknij „Przetłumacz film”.")
                    if self.args.pop("autostart", False):
                        self.start()
                elif kind == "file_done":
                    _, written, video, srt = ev
                    self.result = dict(first=video or (written[0] if written else srt), video=video)
                    folder = os.path.dirname(self.result["first"])
                    self.v_done.set(f"W folderze {short_path(folder, 50)}:\n"
                                    + "\n".join("•  " + os.path.basename(p) for p in written))
                    self.b_play.configure(text="Odtwórz film z napisami" if video else "Odtwórz film")
                    self._show_done()
                    self.v_status.set("Gotowe.")
                    if self.args.get("quit_after"):
                        print("FILE_DONE", written, flush=True)
                elif kind == "file_stopped":
                    if ev[1] is self.job:
                        self.job = None
                        self._lock(False)
                elif kind == "error":
                    self.v_status.set("Błąd – szczegóły w okienku.")
                    if self.args.get("quit_after"):
                        print("ERROR", ev[1], flush=True)
                    else:
                        messagebox.showerror("AMT – błąd", f"{ev[1]}\n\nSzczegóły w pliku:\n{LOG_FILE}")
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _on_close(self):
        if self.job and not self.args.get("quit_after"):
            if not messagebox.askyesno("AMT", "Tłumaczenie jeszcze trwa. Przerwać i zamknąć?"):
                return
        if self.job:
            self.job.stop_evt.set()
        try:
            self._cfg()
        except Exception:
            traceback.print_exc()
        self.root.destroy()


# ----------------------------------------------------------------- start

def run_cli(path, opts):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in opts.items() if k in DEFAULTS})
    cfg["accurate"] = opts.get("accurate", "1") == "1"
    media = probe_media(path)
    source = opts.get("source", "auto")
    if source == "whisper":
        sel = ("whisper", None)
    elif source != "auto":
        sel = ("file", dict(path=source, lang=lang_from_name(source)))
    else:
        opts_, best = choose_source(media, path, cfg["tgt"])
        sel = opts_[best][:2]
    print("SOURCE", sel, flush=True)
    ev = queue.Queue()
    job = FileJob(cfg, path, sel, ev, media=media)
    job.start()
    while True:
        e = ev.get()
        if e[0] == "progress":
            print(f"[{e[1] * 100:5.1f}%] {e[2].splitlines()[0]}", flush=True)
        elif e[0] in ("status", "error"):
            print(e[0].upper(), e[1], flush=True)
        elif e[0] == "file_done":
            print("FILE_DONE", e[1], flush=True)
        elif e[0] == "file_stopped":
            break


def main():
    argv = sys.argv[1:]
    if "--srt" in argv:
        run_cli(argv[argv.index("--srt") + 1], dict(a.split("=", 1) for a in argv if "=" in a))
        return
    args = {}
    if "--quit-after" in argv:
        args["quit_after"] = float(argv[argv.index("--quit-after") + 1])
    args["autostart"] = "--autostart" in argv  # test: od razu tłumacz wybrany film
    args["settings"] = "--settings" in argv    # test: od razu pokaż okno ustawień
    files = [a for a in argv if not a.startswith("--") and os.path.isfile(a)]
    if files:
        args["file"] = files[0]
    set_app_id("AMT.AutomaticMovieTranslator")
    init_ui(UI_SCALES.get(load_settings(APP, DEFAULTS).get("ui_scale"), 1.0))
    root, dnd = make_root(dnd=True)
    AMTApp(root, args, dnd)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            messagebox.showerror("AMT", traceback.format_exc())
        except Exception:
            pass
        raise
