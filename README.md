# Screen Translator (SL ⇄ EN)

Press a hotkey, drag a box around any Slovenian or English text on your screen, and
the translation into the other language pops up right next to it. The direction is
detected automatically. If it guesses wrong (most likely on a single word), click ⇄.

| Action | Keys |
|---|---|
| Start a selection | **Ctrl + Alt + T**, then drag a box |
| Cancel the selection | Esc or right-click |
| Flip the direction (SL→EN / EN→SL) | ⇄ in the popup |
| Copy the translation | "Copy" in the popup |
| Move a popup | drag its header |
| Close a popup | ✕ or Esc |
| Quit the app | Ctrl + Alt + Q, or Ctrl + C in the console |

## How it works

```
hotkey (pynput) → freeze screen (mss) → you drag a box (tkinter overlay)
      → crop → OCR (Tesseract, Slovenian model) → translate (Google free / DeepL)
      → popup next to the box (tkinter)
```

The screen is frozen at the moment you press the hotkey, so what you see is exactly
what gets read, even for video or text that changes.

## Setup (Windows)

1. **Python 3.10+**: https://www.python.org/downloads/ (tick "Add python.exe to PATH").

2. **Tesseract OCR**: install from the UB Mannheim build:
   https://github.com/UB-Mannheim/tesseract/wiki
   During install, open **Additional language data** and tick **Slovenian**.
   Keep the default path `C:\Program Files\Tesseract-OCR\`, and the script finds it
   automatically.
   *Forgot Slovenian?* Download `slv.traineddata` from
   https://github.com/tesseract-ocr/tessdata_best and put it in
   `C:\Program Files\Tesseract-OCR\tessdata\`.

3. **Python packages** (in this folder):
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. **Run it**:
   ```
   python screen_translator.py
   ```
   To run it without a console window: `pythonw screen_translator.py`.
   To start it with Windows, put a shortcut to that command in
   `shell:startup` (Win + R → `shell:startup`).

## DeepL key

Easiest: create a file called `deepl_key.txt` next to the script and paste only the
key into it. Alternatively, set the `DEEPL_API_KEY` environment variable. The startup
line shows which engine and key are in use.

## Translation engine

- **Default:** Google Translate's free endpoint (via `deep-translator`). No key needed,
  but it's unofficial, so it may be rate-limited if you use it heavily.
- **DeepL (recommended, better Slovenian):** get a free API key at
  https://www.deepl.com/pro-api, then:
  ```
  pip install deepl
  setx DEEPL_API_KEY "your-key-here"
  ```
  Open a new terminal afterwards. The console line at startup shows which engine is
  active.

## Configuration

At the top of `screen_translator.py`:

- `HOTKEY` / `QUIT_HOTKEY`: pynput format, e.g. `"<ctrl>+<shift>+<f9>"`
- `LANG_A`, `LANG_B`: the two languages to translate between (default `"sl"` / `"en"`)
- `TESSERACT_LANG`: OCR models for both, joined with `+` (default `"slv+eng"`).
  English is installed with Tesseract by default; Slovenian must be ticked.
  For other pairs (e.g. German), also add a few common words to `_HINTS` in the
  script so detection works.
- `POPUP_WIDTH`: wrap width of the popup
- Environment variable `TESSERACT_CMD`: full path to `tesseract.exe` if it's not in
  the default location

## Tips and troubleshooting

- **Hotkey does nothing:** some apps running as admin swallow keys. Run the script
  as admin too, or pick a different hotkey.
- **Selection is offset on a scaled display (125%/150%):** the script sets DPI
  awareness on start, so this shouldn't happen. If it does, tell me your monitor
  setup.
- **Garbage OCR:** select only the text, with a bit of margin, not whole windows with
  icons. Very small or stylised fonts are the hardest. Zoom the page in (Ctrl +) first.
- **macOS:** allow Terminal/Python under *Screen Recording* and *Accessibility*.
  **Linux:** works on X11 only (not Wayland).
