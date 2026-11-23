"""
Screen Translator
=================
Select any area of the screen, read the text inside it (OCR) and show the
translation right next to the box. Works both ways: Slovenian -> English and
English -> Slovenian - the direction is detected automatically, and the ⇄
button in the popup flips it if the guess was wrong.

    Ctrl+Alt+T   -> screen freezes, drag a box around the text
    Esc / right-click  -> cancel the selection
    Popup: drag it by its header, ⇄ flips the direction, "Copy" copies the
           translation, Esc or ✕ closes it
    Ctrl+Alt+Q (or Ctrl+C in the console) -> quit

Pipeline:  hotkey -> frozen-screen overlay -> crop -> Tesseract OCR -> translate -> popup
"""

import ctypes
import os
import platform
import queue
import re
import signal
import sys
import threading

# --- Windows: be DPI aware BEFORE any window exists -------------------------
# Without this, on a display scaled to 125%/150% the mouse coordinates and the
# screenshot pixels don't line up and you'd OCR the wrong area.
if platform.system() == "Windows":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk

import mss
import pytesseract
from PIL import Image, ImageEnhance, ImageOps, ImageStat, ImageTk
from pynput import keyboard

# mss >= 10 renamed mss.mss() to mss.MSS(); support both.
_MSS = getattr(mss, "MSS", None) or mss.mss

# =============================================================================
# Config
# =============================================================================
HOTKEY = "<ctrl>+<alt>+t"
QUIT_HOTKEY = "<ctrl>+<alt>+q"

# The two languages to translate between (ISO codes). Text detected as LANG_A is
# translated to LANG_B and vice versa.
LANG_A = "sl"   # Slovenian
LANG_B = "en"   # English
# Tesseract models used for reading the text (slv.traineddata + eng.traineddata).
TESSERACT_LANG = "slv+eng"

# If set, DeepL is used (better quality). Otherwise the free Google endpoint
# via the `deep-translator` package is used - no key needed.
#
# The key is read from (first one found wins):
#   1. the DEEPL_API_KEY environment variable
#   2. a file called deepl_key.txt next to this script (just the key, one line)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(SCRIPT_DIR, "deepl_key.txt")


def _load_deepl_key():
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if key:
        return key, "environment variable"
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8-sig") as f:
            key = f.read().strip().strip('"').strip("'")
        if key:
            return key, "deepl_key.txt"
    return "", None


DEEPL_API_KEY, DEEPL_KEY_SOURCE = _load_deepl_key()

# Tesseract isn't on PATH by default on Windows - point to the usual location.
_WIN_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD = os.environ.get("TESSERACT_CMD") or (
    _WIN_TESSERACT if platform.system() == "Windows" and os.path.exists(_WIN_TESSERACT) else None
)
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

POPUP_WIDTH = 460  # text wrap width in "96-dpi pixels", scaled for high-DPI screens

# Popup colours
BG, FG, MUTED, ACCENT, ERROR = "#1e1f2b", "#f2f2f5", "#9a9fb5", "#7aa2f7", "#f7768e"
FONT = "Segoe UI" if platform.system() == "Windows" else "Helvetica"


# =============================================================================
# OCR
# =============================================================================
def ocr(img: Image.Image) -> str:
    """Image -> text. Screen text is small and often light-on-dark, so we
    normalise it into what Tesseract likes: big, grey, dark text on white."""
    gray = ImageOps.grayscale(img)

    # Upscale small selections - this alone hugely improves accuracy on UI text.
    scale = 3 if gray.height < 60 else 2 if gray.width < 2000 else 1
    if scale > 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)

    # Dark mode / dark background -> invert to dark text on light background.
    if ImageStat.Stat(gray).mean[0] < 128:
        gray = ImageOps.invert(gray)

    gray = ImageOps.autocontrast(gray)
    # A white margin helps Tesseract when text touches the edge of the box.
    gray = ImageOps.expand(gray, border=20, fill=255)

    raw = pytesseract.image_to_string(gray, lang=TESSERACT_LANG, config="--psm 6")
    return clean_ocr_text(raw)


def clean_ocr_text(text: str) -> str:
    """Re-join lines into sentences so the translator sees whole sentences,
    but keep blank-line paragraph breaks."""
    text = text.replace("\x0c", "").strip()
    paragraphs = re.split(r"\n\s*\n", text)
    out = []
    for p in paragraphs:
        p = re.sub(r"(\w)-\n(\w)", r"\1\2", p)  # "pri-\nmer" -> "primer"
        p = re.sub(r"\s*\n\s*", " ", p)          # line breaks -> spaces
        p = re.sub(r"[ \t]{2,}", " ", p).strip()
        if p:
            out.append(p)
    return "\n\n".join(out)


# =============================================================================
# Translation
# =============================================================================
_deepl_client = None


# Small offline language detector for the two languages: characters only one
# language uses + very common short words. Free, instant, no extra API call.
_HINTS = {
    "sl": {
        "chars": set("čšžČŠŽ"),
        "words": {"je", "na", "za", "se", "da", "so", "ki", "pri", "od", "ali", "kot",
                  "pa", "tudi", "bo", "ni", "smo", "ste", "sem", "biti", "lahko", "če",
                  "ter", "iz", "po", "do", "ne", "vas", "vaš", "vaše", "naš", "kaj",
                  "kako", "zelo", "samo", "še", "že", "tem", "te", "ta", "to", "jih"},
    },
    "en": {
        "chars": set(),
        "words": {"the", "and", "is", "are", "of", "to", "for", "with", "this", "that",
                  "you", "your", "it", "be", "on", "was", "were", "have", "has", "not",
                  "will", "can", "from", "at", "by", "or", "we", "our", "they", "a",
                  "an", "please", "if", "all", "which", "would", "should", "been"},
    },
}


def detect_lang(text: str) -> str:
    """Return LANG_A or LANG_B - whichever the text looks more like."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    scores = {}
    for lang in (LANG_A, LANG_B):
        h = _HINTS.get(lang, {"chars": set(), "words": set()})
        scores[lang] = (sum(w in h["words"] for w in words)
                        + 3 * sum(c in h["chars"] for c in text))
    # Ties (e.g. a single word like "Nastavitve") go to LANG_A.
    return LANG_B if scores[LANG_B] > scores[LANG_A] else LANG_A


def other_lang(lang: str) -> str:
    return LANG_B if lang == LANG_A else LANG_A


def _deepl_code(lang: str, as_target: bool) -> str:
    code = lang.upper()
    if as_target and code == "EN":
        return "EN-US"      # DeepL needs a variant for English targets (or EN-GB)
    if as_target and code == "PT":
        return "PT-PT"
    return code


def translate(text: str, source: str, target: str) -> str:
    global _deepl_client
    if DEEPL_API_KEY:
        import deepl  # pip install deepl

        if _deepl_client is None:
            # deepl >= 1.21 renamed Translator to DeepLClient; support both.
            client_cls = getattr(deepl, "DeepLClient", None) or deepl.Translator
            _deepl_client = client_cls(DEEPL_API_KEY)
        return _deepl_client.translate_text(
            text, source_lang=_deepl_code(source, False),
            target_lang=_deepl_code(target, True),
        ).text

    from deep_translator import GoogleTranslator

    tr = GoogleTranslator(source=source, target=target)
    # Free endpoint limit is 5000 chars per request -> translate in chunks.
    return "\n\n".join(tr.translate(chunk) or "" for chunk in _chunks(text, 4500))


def _chunks(text: str, limit: int):
    buf = ""
    for para in text.split("\n\n"):
        if len(para) > limit:  # a single giant paragraph: flush, then split it
            if buf:
                yield buf
                buf = ""
            while len(para) > limit:
                yield para[:limit]
                para = para[limit:]
        if buf and len(buf) + len(para) + 2 > limit:
            yield buf
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        yield buf


# =============================================================================
# Popup shown next to the selected box
# =============================================================================
class Popup:
    def __init__(self, app: "App", box):
        self.app, self.box = app, box  # box = absolute screen coords (x1, y1, x2, y2)
        self.translation = ""
        self.user_moved = False
        self.closed = False

        self.win = win = tk.Toplevel(app.root)
        win.overrideredirect(True)          # no title bar
        win.attributes("-topmost", True)
        win.configure(bg=ACCENT)            # shows as a 1px accent border

        inner = tk.Frame(win, bg=BG, padx=14, pady=10)
        inner.pack(padx=1, pady=1, fill="both", expand=True)

        header = tk.Frame(inner, bg=BG)
        header.pack(fill="x")
        self.source_text = ""
        self.src = self.tgt = None
        self.title = title = tk.Label(header, text="…", bg=BG, fg=ACCENT,
                                      font=(FONT, 9, "bold"))
        title.pack(side="left")
        self.flip_btn = tk.Label(header, text="⇄", bg=BG, fg=MUTED, cursor="hand2",
                                 font=(FONT, 11))
        self.flip_btn.bind("<Button-1>", lambda e: self.flip())
        close = tk.Label(header, text="✕", bg=BG, fg=MUTED, cursor="hand2", font=(FONT, 10))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())
        self.copy_btn = tk.Label(header, text="Copy", bg=BG, fg=MUTED, cursor="hand2",
                                 font=(FONT, 9))
        self.copy_btn.bind("<Button-1>", lambda e: self.copy())

        wrap = int(POPUP_WIDTH * app.ui_scale)
        self.body = tk.Label(inner, text="Reading text…", bg=BG, fg=FG, font=(FONT, 11),
                             justify="left", anchor="w", wraplength=wrap)
        self.body.pack(fill="x", pady=(6, 0))
        self.original = tk.Label(inner, text="", bg=BG, fg=MUTED, font=(FONT, 9),
                                 justify="left", anchor="w", wraplength=wrap)

        # Drag the popup around by its header.
        for w in (header, title):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<Button-1>", lambda e: win.focus_force(), add="+")

        self.reposition()
        win.after(50, lambda: App._focus_if_alive(win))

    # --- content ------------------------------------------------------------
    def set_status(self, text):
        self.body.config(text=text, fg=MUTED)
        self.reposition()

    def set_result(self, original, translation, src, tgt):
        self.source_text, self.translation, self.src, self.tgt = original, translation, src, tgt
        self.title.config(text=f"{src.upper()} → {tgt.upper()}")
        self.body.config(text=translation, fg=FG)
        self.original.config(text=original)
        self.original.pack(fill="x", pady=(8, 0))
        if not self.copy_btn.winfo_ismapped():
            self.copy_btn.pack(side="right", padx=(0, 12))
            self.flip_btn.pack(side="left", padx=(8, 0))
        self.reposition()

    def flip(self):
        """Wrong direction guessed? Translate the same text the other way."""
        if not self.source_text:
            return
        self.translation = ""
        self.title.config(text=f"{self.tgt.upper()} → {self.src.upper()}")
        self.set_status("Translating…")
        self.app.start_translation(self.pid, self.source_text, self.tgt, self.src)

    def set_error(self, msg):
        self.body.config(text=msg, fg=ERROR)
        self.reposition()

    def copy(self):
        if not self.translation:
            return
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(self.translation)
        self.copy_btn.config(text="Copied ✓", fg=ACCENT)
        self.win.after(1200, lambda: self.copy_btn.config(text="Copy", fg=MUTED))

    def close(self):
        if not self.closed:
            self.closed = True
            self.win.destroy()

    # --- placement ----------------------------------------------------------
    def reposition(self):
        """Right of the box if it fits, else left, else below/above -
        always kept inside the monitor the box is on."""
        if self.user_moved or self.closed:
            return
        self.win.update_idletasks()
        pw, ph = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        x1, y1, x2, y2 = self.box
        m = self.app.monitor_at((x1 + x2) // 2, (y1 + y2) // 2)
        left, top = m["left"], m["top"]
        right, bottom = left + m["width"], top + m["height"]
        gap = 10

        if x2 + gap + pw <= right:
            x, y = x2 + gap, y1
        elif x1 - gap - pw >= left:
            x, y = x1 - gap - pw, y1
        else:
            x = x1
            y = y2 + gap if y2 + gap + ph <= bottom else y1 - gap - ph

        x = max(left, min(x, right - pw))
        y = max(top, min(y, bottom - ph))
        self.win.geometry(f"+{x}+{y}")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.user_moved = True
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")


# =============================================================================
# App: hotkey -> frozen-screen selection overlay -> worker thread -> popup
# =============================================================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # no main window, we only use popups/overlays
        self.ui_scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)

        self.events: "queue.Queue[tuple]" = queue.Queue()  # thread -> UI messages
        self.popups: dict[int, Popup] = {}
        self._next_id = 0
        self.overlay = None
        self.monitors = []

        self.root.after(30, self._poll)

    # Tkinter is not thread-safe: the hotkey listener and OCR worker only put
    # messages on a queue, and the Tk main loop handles them here.
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "select":
                    self.start_selection()
                elif kind == "quit":
                    self.root.quit()
                    return
                elif kind in ("status", "result", "error"):
                    popup = self.popups.get(ev[1])
                    if popup and not popup.closed:
                        getattr(popup, f"set_{kind}")(*ev[2:])
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def monitor_at(self, x, y):
        for m in self.monitors[1:]:
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return m
        return self.monitors[1] if len(self.monitors) > 1 else self.monitors[0]

    # --- selection overlay ---------------------------------------------------
    def start_selection(self):
        if self.overlay is not None:
            return
        # 1) Freeze: screenshot every monitor (monitors[0] = whole virtual desktop).
        with _MSS() as sct:
            self.monitors = [dict(m) for m in sct.monitors]
            vs = self.monitors[0]
            raw = sct.grab(vs)
        self.shot = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        self.origin = (vs["left"], vs["top"])

        # 2) Show the frozen screenshot, dimmed, as a fullscreen overlay.
        ov = self.overlay = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.geometry(f"{vs['width']}x{vs['height']}+{vs['left']}+{vs['top']}")

        self._bg_img = ImageTk.PhotoImage(ImageEnhance.Brightness(self.shot).enhance(0.55))
        c = self.canvas = tk.Canvas(ov, width=vs["width"], height=vs["height"],
                                    highlightthickness=0, cursor="crosshair", bg="black")
        c.pack()
        c.create_image(0, 0, image=self._bg_img, anchor="nw")
        self._sel_img = None
        self._sel_photo = None
        self._rect = c.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=2)
        c.create_text(*self._hint_pos(),
                      text="Drag around the text  ·  Esc to cancel",
                      fill="white", font=(FONT, 12, "bold"))

        c.bind("<ButtonPress-1>", self._sel_start)
        c.bind("<B1-Motion>", self._sel_move)
        c.bind("<ButtonRelease-1>", self._sel_end)
        c.bind("<Button-3>", lambda e: self._cancel())
        ov.bind("<Escape>", lambda e: self._cancel())
        # Don't call ov.update() here: it would process stray clicks/keys that are
        # already queued (e.g. from pressing the hotkey) and could close the overlay
        # before we focus it. Focus a moment later, and only if it still exists.
        self._x0 = self._y0 = None
        ov.update_idletasks()
        ov.after(50, lambda: self._focus_if_alive(ov))

    @staticmethod
    def _focus_if_alive(win):
        try:
            if win.winfo_exists():
                win.lift()
                win.focus_force()
        except tk.TclError:
            pass  # window was closed in the meantime - nothing to do

    def _hint_pos(self):
        # Centre the hint near the top of the primary monitor (canvas coordinates).
        prim = self.monitors[1] if len(self.monitors) > 1 else self.monitors[0]
        ox, oy = self.origin
        return (prim["left"] - ox + prim["width"] // 2, prim["top"] - oy + 40)

    def _sel_start(self, e):
        self._x0, self._y0 = e.x, e.y

    def _sel_move(self, e):
        if self._x0 is None:  # drag that started before the overlay appeared
            return
        x1, y1 = min(self._x0, e.x), min(self._y0, e.y)
        x2, y2 = max(self._x0, e.x), max(self._y0, e.y)
        self.canvas.coords(self._rect, x1, y1, x2, y2)
        # Show the selected area un-dimmed so you can see exactly what you get.
        if x2 - x1 > 2 and y2 - y1 > 2:
            self._sel_photo = ImageTk.PhotoImage(self.shot.crop((x1, y1, x2, y2)))
            if self._sel_img is None:
                self._sel_img = self.canvas.create_image(x1, y1, image=self._sel_photo, anchor="nw")
            else:
                self.canvas.itemconfig(self._sel_img, image=self._sel_photo)
                self.canvas.coords(self._sel_img, x1, y1)
            self.canvas.tag_raise(self._rect)

    def _sel_end(self, e):
        if self._x0 is None:  # release of a click that started before the overlay
            return
        x1, y1 = min(self._x0, e.x), min(self._y0, e.y)
        x2, y2 = max(self._x0, e.x), max(self._y0, e.y)
        self._close_overlay()
        if x2 - x1 < 8 or y2 - y1 < 8:
            return  # just a click, not a selection
        crop = self.shot.crop((x1, y1, x2, y2))
        ox, oy = self.origin
        self.translate_region(crop, (x1 + ox, y1 + oy, x2 + ox, y2 + oy))

    def _cancel(self):
        self._close_overlay()

    def _close_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
            self._bg_img = self._sel_photo = None

    # --- OCR + translation in a background thread ---------------------------
    def translate_region(self, img, box):
        pid = self._next_id
        self._next_id += 1
        popup = self.popups[pid] = Popup(self, box)
        popup.pid = pid
        # forget closed popups so the dict doesn't grow forever
        self.popups = {k: p for k, p in self.popups.items() if not p.closed}
        threading.Thread(target=self._worker, args=(pid, img), daemon=True).start()

    def start_translation(self, pid, text, src, tgt):
        threading.Thread(target=self._translate_worker, args=(pid, text, src, tgt),
                         daemon=True).start()

    def _translate_worker(self, pid, text, src, tgt):
        try:
            self.events.put(("result", pid, text, translate(text, src, tgt), src, tgt))
        except Exception as ex:
            self._report(pid, ex)

    def _worker(self, pid, img):
        try:
            text = ocr(img)
            if not text:
                self.events.put(("error", pid, "No text found in the selection."))
                return
            src = detect_lang(text)
            self.events.put(("status", pid, "Translating…"))
            self._translate_worker(pid, text, src, other_lang(src))
        except Exception as ex:
            self._report(pid, ex)

    def _report(self, pid, ex):
        try:
            raise ex
        except pytesseract.TesseractNotFoundError:
            self.events.put(("error", pid,
                             "Tesseract not found. Install it, or set TESSERACT_CMD "
                             "to the full path of tesseract.exe."))
        except pytesseract.TesseractError as ex:
            self.events.put(("error", pid, f"OCR failed: {ex}. Is the '{TESSERACT_LANG}' "
                                           "language data installed?"))
        except Exception as ex:  # network errors, API errors, ...
            if type(ex).__name__ == "TooManyRequests" or "too many requests" in str(ex).lower():
                msg = ("Google's free translator blocked the request (it rate-limits "
                       "unofficial use). Put your DeepL key in deepl_key.txt next to "
                       "the script and restart.")
            else:
                msg = f"Error: {ex}"
            self.events.put(("error", pid, msg))

    def run(self):
        self.root.mainloop()


# =============================================================================
def check_setup():
    try:
        langs = pytesseract.get_languages(config="")
    except pytesseract.TesseractNotFoundError:
        sys.exit("Tesseract is not installed or not found. See README.md (set TESSERACT_CMD).")
    missing = [l for l in TESSERACT_LANG.split("+") if l not in langs]
    if missing:
        sys.exit(f"Tesseract is missing language data: {', '.join(missing)}. "
                 f"Installed: {', '.join(langs)}\n"
                 f"Download {', '.join(m + '.traineddata' for m in missing)} into the "
                 "tessdata folder (see README.md).")


def main():
    check_setup()
    app = App()

    hotkeys = keyboard.GlobalHotKeys({
        HOTKEY: lambda: app.events.put(("select",)),
        QUIT_HOTKEY: lambda: app.events.put(("quit",)),
    })
    hotkeys.daemon = True
    hotkeys.start()
    signal.signal(signal.SIGINT, lambda *a: app.events.put(("quit",)))

    if DEEPL_API_KEY:
        try:
            import deepl  # noqa: F401
        except ImportError:
            sys.exit("A DeepL key was found but the 'deepl' package isn't installed. "
                     "Run:  pip install deepl")
        engine = f"DeepL (key …{DEEPL_API_KEY[-6:]} from {DEEPL_KEY_SOURCE})"
    else:
        engine = "Google (free) - no DeepL key found in DEEPL_API_KEY or deepl_key.txt"
    pretty = lambda hk: hk.replace("<", "").replace(">", "").title()
    print(f"Screen Translator running  ·  {pretty(HOTKEY)} to select  ·  "
          f"{pretty(QUIT_HOTKEY)} or Ctrl+C to quit  ·  translator: {engine}")
    app.run()
    hotkeys.stop()


if __name__ == "__main__":
    main()
