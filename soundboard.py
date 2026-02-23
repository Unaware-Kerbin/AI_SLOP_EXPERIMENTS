"""
SoundDrop - Windows Soundboard with Audio Passthrough
======================================================
Requirements: pip install pygame pynput sounddevice numpy
Optional:     VB-CABLE (https://vb-audio.com/Cable/) for mic passthrough

Run: python soundboard.py
EXE: pyinstaller --onefile --windowed --name SoundDrop soundboard.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import json
import os
import sys
import time
import math

# ── Graceful imports ──────────────────────────────────────────────────────────
try:
    import pygame
    import pygame.mixer
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

try:
    import sounddevice as sd
    import numpy as np
    SD_OK = True
except ImportError:
    SD_OK = False

try:
    from pynput import keyboard as pynput_kb
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False

# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME    = "SoundDrop"
VERSION     = "1.0.0"
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".sounddrop_config.json")
AUDIO_EXTS  = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac")

# ── Palette ───────────────────────────────────────────────────────────────────
C = {
    "bg":        "#0d0f14",
    "panel":     "#13161e",
    "card":      "#1a1d27",
    "card_h":    "#21253a",
    "border":    "#252836",
    "accent":    "#5b6af5",
    "accent2":   "#8b5cf6",
    "green":     "#22d3a5",
    "red":       "#f43f5e",
    "yellow":    "#fbbf24",
    "text":      "#e8eaf6",
    "muted":     "#6b7280",
    "white":     "#ffffff",
}

FONT_TITLE  = ("Segoe UI", 22, "bold")
FONT_HEAD   = ("Segoe UI", 11, "bold")
FONT_BODY   = ("Segoe UI", 10)
FONT_SMALL  = ("Segoe UI", 8)
FONT_MONO   = ("Consolas", 9)

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "sounds": [],          # [{name, path, hotkey, volume, color}]
    "master_volume": 80,
    "passthrough_device": None,
    "speaker_device": None,
    "stop_hotkey": "f4",
    "folders": [],
}

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
    except Exception:
        return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# ── Audio Engine ──────────────────────────────────────────────────────────────
class AudioEngine:
    def __init__(self):
        self.ready = False
        self.channels = {}      # sound_id -> pygame.Channel
        self.pt_stream = None   # passthrough stream
        self.pt_device = None
        self.sp_device = None
        self._init_pygame()

    def _init_pygame(self):
        if not PYGAME_OK:
            return
        try:
            pygame.mixer.pre_init(44100, -16, 2, 512)
            pygame.mixer.init()
            pygame.mixer.set_num_channels(32)
            self.ready = True
        except Exception as e:
            print(f"[Audio] pygame init failed: {e}")

    def get_output_devices(self):
        """Return list of (index, name) for output devices."""
        devices = [(-1, "Default System Output")]
        if not SD_OK:
            return devices
        try:
            devs = sd.query_devices()
            for i, d in enumerate(devs):
                if d["max_output_channels"] > 0:
                    devices.append((i, d["name"]))
        except Exception:
            pass
        return devices

    def set_passthrough_device(self, device_index):
        """Set which device is used as the virtual mic / passthrough sink."""
        self.pt_device = device_index

    def set_speaker_device(self, device_index):
        self.sp_device = device_index

    def play(self, path: str, volume: float = 1.0, sound_id: str = None, passthrough: bool = True):
        """Play a sound file on speaker + optionally on passthrough device."""
        if not self.ready:
            return False

        try:
            sound = pygame.mixer.Sound(path)
            sound.set_volume(volume)
            ch = sound.play()
            if ch and sound_id:
                self.channels[sound_id] = ch

            # Passthrough via sounddevice (plays raw audio to VB-CABLE)
            if passthrough and SD_OK and self.pt_device is not None and self.pt_device >= 0:
                threading.Thread(
                    target=self._play_passthrough,
                    args=(path, volume),
                    daemon=True
                ).start()

            return True
        except Exception as e:
            print(f"[Audio] play error: {e}")
            return False

    def _play_passthrough(self, path: str, volume: float):
        """Stream audio file to the passthrough (VB-CABLE) device."""
        try:
            import wave, struct
            # Only supports WAV natively without extra libs; for mp3 etc pygame reads it
            # We re-open with pygame and extract samples
            snd = pygame.mixer.Sound(path)
            raw = pygame.sndarray.array(snd)
            arr = raw.astype(np.float32) / 32768.0 * volume
            sr = pygame.mixer.get_init()[0]
            sd.play(arr, samplerate=sr, device=self.pt_device, blocking=False)
        except Exception as e:
            print(f"[Passthrough] error: {e}")

    def stop(self, sound_id: str = None):
        if sound_id and sound_id in self.channels:
            ch = self.channels.pop(sound_id)
            if ch:
                ch.stop()
        else:
            pygame.mixer.stop()
            self.channels.clear()

    def stop_all(self):
        if self.ready:
            pygame.mixer.stop()
        self.channels.clear()

    def is_playing(self, sound_id: str) -> bool:
        ch = self.channels.get(sound_id)
        return bool(ch and ch.get_busy())

    def set_master_volume(self, v: float):
        """v = 0.0 – 1.0"""
        if self.ready:
            pygame.mixer.music.set_volume(v)

    def shutdown(self):
        if self.ready:
            pygame.mixer.quit()

# ── Hotkey Manager ────────────────────────────────────────────────────────────
class HotkeyManager:
    def __init__(self, engine: AudioEngine):
        self.engine  = engine
        self.binds   = {}   # key_str -> callback
        self.listener = None
        self.recording = False
        self.record_cb = None
        self._pressed  = set()
        self._start()

    def _start(self):
        if not PYNPUT_OK:
            return
        try:
            self.listener = pynput_kb.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
                daemon=True
            )
            self.listener.start()
        except Exception as e:
            print(f"[Hotkey] listener error: {e}")

    def _key_str(self, key):
        try:
            return key.char.lower() if key.char else str(key).replace("Key.", "")
        except AttributeError:
            return str(key).replace("Key.", "").lower()

    def _on_press(self, key):
        ks = self._key_str(key)
        self._pressed.add(ks)
        if self.recording and self.record_cb:
            self.recording = False
            self.record_cb(ks)
            return
        if ks in self.binds:
            self.binds[ks]()

    def _on_release(self, key):
        ks = self._key_str(key)
        self._pressed.discard(ks)

    def bind(self, key_str: str, callback):
        if key_str:
            self.binds[key_str.lower()] = callback

    def unbind(self, key_str: str):
        self.binds.pop(key_str.lower(), None)

    def start_recording(self, callback):
        self.recording = True
        self.record_cb = callback

    def stop(self):
        if self.listener:
            self.listener.stop()

# ── Widgets ───────────────────────────────────────────────────────────────────
class RoundedButton(tk.Canvas):
    def __init__(self, parent, text="", command=None, color=None,
                 width=120, height=36, radius=8, font=None, **kw):
        super().__init__(parent, width=width, height=height,
                         bg=C["card"], highlightthickness=0, **kw)
        self.command = command
        self.color   = color or C["accent"]
        self.text    = text
        self.radius  = radius
        self.font    = font or FONT_BODY
        self._hover  = False
        self._draw()
        self.bind("<Enter>",    self._on_enter)
        self.bind("<Leave>",    self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _draw(self):
        self.delete("all")
        w, h, r = int(self["width"]), int(self["height"]), self.radius
        col = self._lighten(self.color, 20) if self._hover else self.color
        self._rounded_rect(0, 0, w, h, r, col)
        self.create_text(w//2, h//2, text=self.text, fill=C["white"],
                         font=self.font, anchor="center")

    def _rounded_rect(self, x1, y1, x2, y2, r, fill):
        pts = [
            x1+r, y1, x2-r, y1,
            x2, y1, x2, y1+r,
            x2, y2-r, x2, y2,
            x2-r, y2, x1+r, y2,
            x1, y2, x1, y2-r,
            x1, y1+r, x1, y1,
        ]
        self.create_polygon(pts, fill=fill, smooth=True)

    def _lighten(self, hex_col, amount=20):
        try:
            h = hex_col.lstrip("#")
            r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            r = min(255, r+amount); g = min(255, g+amount); b = min(255, b+amount)
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return hex_col

    def _on_enter(self, _): self._hover = True;  self._draw()
    def _on_leave(self, _): self._hover = False; self._draw()
    def _on_click(self, _):
        if self.command: self.command()

    def config_text(self, text):
        self.text = text
        self._draw()

class SoundCard(tk.Frame):
    """A single sound button card."""
    COLORS = [C["accent"], C["accent2"], C["green"], C["yellow"], C["red"],
              "#06b6d4", "#f97316", "#10b981", "#ec4899", "#84cc16"]

    def __init__(self, parent, sound_data: dict, engine: AudioEngine,
                 on_edit=None, on_delete=None, **kw):
        super().__init__(parent, bg=C["card"], bd=0, relief="flat",
                         highlightthickness=1,
                         highlightbackground=C["border"], **kw)
        self.sound    = sound_data
        self.engine   = engine
        self.on_edit  = on_edit
        self.on_delete= on_delete
        self._playing = False
        self._build()
        self._anim_id = None

    def _build(self):
        color = self.sound.get("color", C["accent"])
        sid   = self.sound["id"]

        # Colour strip
        strip = tk.Frame(self, bg=color, width=4)
        strip.pack(side="left", fill="y")

        body = tk.Frame(self, bg=C["card"])
        body.pack(side="left", fill="both", expand=True, padx=8, pady=6)

        # Name
        name_row = tk.Frame(body, bg=C["card"])
        name_row.pack(fill="x")
        self.name_lbl = tk.Label(name_row, text=self.sound.get("name","Sound"),
                                 fg=C["text"], bg=C["card"],
                                 font=FONT_HEAD, anchor="w")
        self.name_lbl.pack(side="left")

        # Hotkey badge
        hk = self.sound.get("hotkey", "")
        self.hk_lbl = tk.Label(name_row,
                                text=f"  [{hk.upper()}]" if hk else "",
                                fg=C["muted"], bg=C["card"],
                                font=FONT_SMALL, anchor="w")
        self.hk_lbl.pack(side="left")

        # File name
        path = self.sound.get("path","")
        fname = os.path.basename(path)
        tk.Label(body, text=fname, fg=C["muted"], bg=C["card"],
                 font=FONT_SMALL, anchor="w").pack(fill="x")

        # Controls row
        ctrl = tk.Frame(body, bg=C["card"])
        ctrl.pack(fill="x", pady=(4,0))

        self.play_btn = RoundedButton(ctrl, text="▶  Play",
                                      command=self._toggle_play,
                                      color=color, width=90, height=28)
        self.play_btn.pack(side="left")

        tk.Label(ctrl, text="  Vol:", fg=C["muted"], bg=C["card"],
                 font=FONT_SMALL).pack(side="left")

        self.vol_var = tk.IntVar(value=int(self.sound.get("volume",100)))
        vol_sl = tk.Scale(ctrl, from_=0, to=100, orient="horizontal",
                          variable=self.vol_var, length=80,
                          bg=C["card"], fg=C["muted"],
                          troughcolor=C["border"], activebackground=color,
                          highlightthickness=0, bd=0, showvalue=False,
                          command=self._on_vol)
        vol_sl.pack(side="left")

        # Edit / Delete
        tk.Label(ctrl, text="  ", bg=C["card"]).pack(side="left")
        tk.Button(ctrl, text="✏", fg=C["muted"], bg=C["card"],
                  activeforeground=C["text"], activebackground=C["card"],
                  bd=0, cursor="hand2", font=FONT_BODY,
                  command=lambda: self.on_edit and self.on_edit(self.sound)
                  ).pack(side="left")
        tk.Button(ctrl, text="✕", fg=C["red"], bg=C["card"],
                  activeforeground=C["white"], activebackground=C["card"],
                  bd=0, cursor="hand2", font=FONT_BODY,
                  command=lambda: self.on_delete and self.on_delete(self.sound)
                  ).pack(side="left")

    def _toggle_play(self):
        sid = self.sound["id"]
        if self.engine.is_playing(sid):
            self.engine.stop(sid)
            self._set_playing(False)
        else:
            vol  = self.vol_var.get() / 100.0
            pt   = self.sound.get("passthrough", True)
            ok   = self.engine.play(self.sound["path"], vol, sid, passthrough=pt)
            if ok:
                self._set_playing(True)
                self._poll_playing(sid)

    def _poll_playing(self, sid):
        if self.engine.is_playing(sid):
            self._anim_id = self.after(200, lambda: self._poll_playing(sid))
        else:
            self._set_playing(False)

    def _set_playing(self, state: bool):
        self._playing = state
        color = self.sound.get("color", C["accent"])
        if state:
            self.play_btn.color = C["red"]
            self.play_btn.text  = "■  Stop"
        else:
            self.play_btn.color = color
            self.play_btn.text  = "▶  Play"
        self.play_btn._draw()

    def _on_vol(self, val):
        self.sound["volume"] = int(float(val))

    def refresh(self):
        """Re-draw after edit."""
        for w in self.winfo_children():
            w.destroy()
        self._build()

# ── Edit Dialog ───────────────────────────────────────────────────────────────
class EditDialog(tk.Toplevel):
    def __init__(self, parent, sound_data: dict, hotkey_mgr: HotkeyManager,
                 on_save=None):
        super().__init__(parent)
        self.title("Edit Sound")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self.sound    = dict(sound_data)
        self.on_save  = on_save
        self.hk_mgr   = hotkey_mgr
        self._build()
        self.geometry("+%d+%d" % (parent.winfo_rootx()+80,
                                   parent.winfo_rooty()+80))

    def _build(self):
        pad = {"padx": 16, "pady": 6}

        tk.Label(self, text="Edit Sound", fg=C["text"], bg=C["bg"],
                 font=FONT_TITLE).pack(**pad, pady=(16,4))

        frm = tk.Frame(self, bg=C["bg"])
        frm.pack(padx=16, pady=4, fill="x")

        def row(label, widget_fn, **kw):
            r = tk.Frame(frm, bg=C["bg"])
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, fg=C["muted"], bg=C["bg"],
                     font=FONT_SMALL, width=14, anchor="e").pack(side="left")
            w = widget_fn(r, **kw)
            w.pack(side="left", fill="x", expand=True, padx=(8,0))
            return w

        # Name
        self.name_var = tk.StringVar(value=self.sound.get("name",""))
        row("Name", lambda p, **kw: tk.Entry(p, textvariable=self.name_var,
            bg=C["card"], fg=C["text"], insertbackground=C["text"],
            relief="flat", font=FONT_BODY, bd=4))

        # Path
        path_frm = tk.Frame(frm, bg=C["bg"])
        path_frm.pack(fill="x", pady=3)
        tk.Label(path_frm, text="File", fg=C["muted"], bg=C["bg"],
                 font=FONT_SMALL, width=14, anchor="e").pack(side="left")
        self.path_var = tk.StringVar(value=self.sound.get("path",""))
        tk.Entry(path_frm, textvariable=self.path_var,
                 bg=C["card"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", font=FONT_BODY, bd=4
                 ).pack(side="left", fill="x", expand=True, padx=(8,0))
        tk.Button(path_frm, text="Browse", bg=C["accent"], fg=C["white"],
                  activebackground=C["accent2"], bd=0, padx=8,
                  font=FONT_SMALL, command=self._browse).pack(side="left", padx=(4,0))

        # Hotkey
        hk_frm = tk.Frame(frm, bg=C["bg"])
        hk_frm.pack(fill="x", pady=3)
        tk.Label(hk_frm, text="Hotkey", fg=C["muted"], bg=C["bg"],
                 font=FONT_SMALL, width=14, anchor="e").pack(side="left")
        self.hk_var = tk.StringVar(value=self.sound.get("hotkey",""))
        self.hk_entry = tk.Entry(hk_frm, textvariable=self.hk_var,
                                  bg=C["card"], fg=C["text"],
                                  insertbackground=C["text"],
                                  relief="flat", font=FONT_BODY, bd=4)
        self.hk_entry.pack(side="left", fill="x", expand=True, padx=(8,0))
        tk.Button(hk_frm, text="Record", bg=C["card"], fg=C["accent"],
                  activebackground=C["card"], bd=0, padx=8,
                  font=FONT_SMALL, command=self._record_hotkey
                  ).pack(side="left", padx=(4,0))

        # Volume
        self.vol_var = tk.IntVar(value=int(self.sound.get("volume",100)))
        row("Volume", lambda p, **kw: tk.Scale(p, from_=0, to=100,
            orient="horizontal", variable=self.vol_var,
            bg=C["bg"], fg=C["text"], troughcolor=C["border"],
            highlightthickness=0, bd=0))

        # Color
        color_frm = tk.Frame(frm, bg=C["bg"])
        color_frm.pack(fill="x", pady=3)
        tk.Label(color_frm, text="Color", fg=C["muted"], bg=C["bg"],
                 font=FONT_SMALL, width=14, anchor="e").pack(side="left")
        self.color_var = tk.StringVar(value=self.sound.get("color", C["accent"]))
        for col in SoundCard.COLORS:
            b = tk.Label(color_frm, bg=col, width=2, cursor="hand2",
                         relief="ridge" if col==self.color_var.get() else "flat")
            b.pack(side="left", padx=2, padx2=2)
            b.bind("<Button-1>", lambda e, c=col: self._pick_color(c))
        self.color_dots = [w for w in color_frm.winfo_children()
                           if isinstance(w, tk.Label)]

        # Passthrough toggle
        self.pt_var = tk.BooleanVar(value=self.sound.get("passthrough", True))
        tk.Checkbutton(frm, text="Send to passthrough device (VB-CABLE)",
                       variable=self.pt_var,
                       bg=C["bg"], fg=C["text"], activebackground=C["bg"],
                       activeforeground=C["text"], selectcolor=C["card"],
                       font=FONT_BODY).pack(anchor="w", pady=4)

        # Save button
        RoundedButton(self, text="Save", command=self._save,
                      color=C["green"], width=200, height=38).pack(pady=12)

    def _browse(self):
        path = filedialog.askopenfilename(
            filetypes=[("Audio Files", " ".join(f"*{e}" for e in AUDIO_EXTS)),
                       ("All Files", "*.*")])
        if path:
            self.path_var.set(path)
            if not self.name_var.get():
                self.name_var.set(os.path.splitext(os.path.basename(path))[0])

    def _record_hotkey(self):
        self.hk_var.set("Press a key...")
        if self.hk_mgr:
            self.hk_mgr.start_recording(
                lambda k: self.after(0, lambda: self.hk_var.set(k)))

    def _pick_color(self, color):
        self.color_var.set(color)

    def _save(self):
        self.sound["name"]        = self.name_var.get() or "Sound"
        self.sound["path"]        = self.path_var.get()
        self.sound["hotkey"]      = self.hk_var.get()
        self.sound["volume"]      = self.vol_var.get()
        self.sound["color"]       = self.color_var.get()
        self.sound["passthrough"] = self.pt_var.get()
        if self.on_save:
            self.on_save(self.sound)
        self.destroy()

# ── Settings Panel ────────────────────────────────────────────────────────────
class SettingsPanel(tk.Toplevel):
    def __init__(self, parent, engine: AudioEngine, config: dict, on_save=None):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self.engine   = engine
        self.config   = config
        self.on_save  = on_save
        self._build()
        self.geometry("520x420+%d+%d" % (parent.winfo_rootx()+60,
                                           parent.winfo_rooty()+60))

    def _build(self):
        tk.Label(self, text="⚙  Settings", fg=C["text"], bg=C["bg"],
                 font=FONT_TITLE).pack(padx=20, pady=(16,4), anchor="w")

        frm = tk.Frame(self, bg=C["bg"])
        frm.pack(fill="both", expand=True, padx=20)

        devices = self.engine.get_output_devices()
        dev_names = [f"[{i}] {n}" if i>=0 else n for i,n in devices]
        dev_ids   = [i for i,_ in devices]

        def combo(label, key, info=""):
            row = tk.Frame(frm, bg=C["bg"])
            row.pack(fill="x", pady=6)
            tk.Label(row, text=label, fg=C["text"], bg=C["bg"],
                     font=FONT_HEAD, width=20, anchor="w").pack(side="left")
            var = tk.StringVar()
            cur = self.config.get(key)
            if cur in dev_ids:
                idx = dev_ids.index(cur)
                var.set(dev_names[idx])
            else:
                var.set(dev_names[0])
            cb = ttk.Combobox(row, textvariable=var, values=dev_names,
                              width=30, state="readonly")
            cb.pack(side="left")
            if info:
                tk.Label(row, text=info, fg=C["muted"], bg=C["bg"],
                         font=FONT_SMALL).pack(side="left", padx=6)
            return var, dev_ids, dev_names

        self.sp_var,  sp_ids,  sp_names  = combo("Speaker Output",
                                                   "speaker_device")
        self.pt_var2, pt_ids, pt_names  = combo("Passthrough Device",
                                                  "passthrough_device",
                                                  "← VB-CABLE Input")

        tk.Label(frm,
                 text="💡  Install VB-CABLE (free) to enable mic passthrough in Discord/Teams.\n"
                      "    Set 'CABLE Input' as your mic in those apps.",
                 fg=C["muted"], bg=C["bg"], font=FONT_SMALL,
                 justify="left").pack(anchor="w", pady=6)

        # Master volume
        mv_row = tk.Frame(frm, bg=C["bg"])
        mv_row.pack(fill="x", pady=6)
        tk.Label(mv_row, text="Master Volume", fg=C["text"], bg=C["bg"],
                 font=FONT_HEAD, width=20, anchor="w").pack(side="left")
        self.mv_var = tk.IntVar(value=self.config.get("master_volume", 80))
        tk.Scale(mv_row, from_=0, to=100, orient="horizontal",
                 variable=self.mv_var, length=180,
                 bg=C["bg"], fg=C["text"], troughcolor=C["border"],
                 highlightthickness=0, bd=0).pack(side="left")

        # Stop hotkey
        hk_row = tk.Frame(frm, bg=C["bg"])
        hk_row.pack(fill="x", pady=6)
        tk.Label(hk_row, text="Stop-All Hotkey", fg=C["text"], bg=C["bg"],
                 font=FONT_HEAD, width=20, anchor="w").pack(side="left")
        self.stop_hk = tk.StringVar(value=self.config.get("stop_hotkey","f4"))
        tk.Entry(hk_row, textvariable=self.stop_hk, width=10,
                 bg=C["card"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", font=FONT_BODY, bd=4).pack(side="left")

        def save():
            # Resolve device index from name
            def resolve(var, ids, names):
                try: return ids[names.index(var.get())]
                except: return None
            self.config["speaker_device"]      = resolve(self.sp_var,  sp_ids,  sp_names)
            self.config["passthrough_device"]  = resolve(self.pt_var2, pt_ids, pt_names)
            self.config["master_volume"]       = self.mv_var.get()
            self.config["stop_hotkey"]         = self.stop_hk.get()
            if self.on_save: self.on_save()
            self.destroy()

        RoundedButton(self, text="Save Settings", command=save,
                      color=C["green"], width=200, height=38).pack(pady=16)

# ── Main App ──────────────────────────────────────────────────────────────────
class SoundDropApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{VERSION}")
        self.configure(bg=C["bg"])
        self.minsize(780, 520)
        self.geometry("960x640")

        self.config_data = load_config()
        self.engine      = AudioEngine()
        self.hk_mgr      = HotkeyManager(self.engine)
        self._sound_cards= {}   # id -> SoundCard widget
        self._next_id    = 1

        self._apply_audio_config()
        self._build_ui()
        self._load_sounds()
        self._bind_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not PYGAME_OK:
            self.after(500, lambda: messagebox.showwarning(
                "Missing Dependency",
                "pygame not found.\nRun:  pip install pygame\n\nAudio playback disabled."))
        if not PYNPUT_OK:
            print("[Warn] pynput not found — hotkeys disabled. pip install pynput")

    # ── Build UI ───────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Header
        header = tk.Frame(self, bg=C["panel"], height=60)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="🔊", fg=C["accent"], bg=C["panel"],
                 font=("Segoe UI Emoji", 22)).pack(side="left", padx=(16,4))
        tk.Label(header, text=APP_NAME, fg=C["white"], bg=C["panel"],
                 font=FONT_TITLE).pack(side="left")
        tk.Label(header, text=f"v{VERSION}", fg=C["muted"], bg=C["panel"],
                 font=FONT_SMALL).pack(side="left", pady=(10,0))

        # Header buttons
        RoundedButton(header, text="⚙  Settings", command=self._open_settings,
                      color=C["card"], width=110, height=32).pack(
                      side="right", padx=8, pady=14)
        RoundedButton(header, text="📁  Add Folder", command=self._add_folder,
                      color=C["card"], width=110, height=32).pack(
                      side="right", padx=0, pady=14)
        RoundedButton(header, text="+ Add Sound", command=self._add_sound,
                      color=C["accent"], width=110, height=32).pack(
                      side="right", padx=8, pady=14)

        # Stop-all bar
        stop_bar = tk.Frame(self, bg=C["panel"], height=36)
        stop_bar.pack(fill="x")
        stop_bar.pack_propagate(False)

        self.status_lbl = tk.Label(stop_bar, text="Ready", fg=C["muted"],
                                    bg=C["panel"], font=FONT_SMALL)
        self.status_lbl.pack(side="left", padx=12)

        RoundedButton(stop_bar, text="■  Stop All",
                      command=self._stop_all,
                      color=C["red"], width=100, height=26,
                      font=FONT_SMALL).pack(side="right", padx=8, pady=5)

        # Status indicators
        self.pygame_dot = tk.Label(stop_bar,
                                    text="● Audio",
                                    fg=C["green"] if PYGAME_OK else C["red"],
                                    bg=C["panel"], font=FONT_SMALL)
        self.pygame_dot.pack(side="right", padx=8)

        self.pt_dot = tk.Label(stop_bar, text="● Passthrough",
                                fg=C["muted"], bg=C["panel"], font=FONT_SMALL)
        self.pt_dot.pack(side="right", padx=4)

        # Search bar
        search_frm = tk.Frame(self, bg=C["bg"])
        search_frm.pack(fill="x", padx=12, pady=6)
        tk.Label(search_frm, text="🔍", fg=C["muted"], bg=C["bg"],
                 font=FONT_BODY).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter_sounds())
        tk.Entry(search_frm, textvariable=self.search_var, width=30,
                 bg=C["card"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", font=FONT_BODY, bd=6).pack(side="left", padx=4)

        # ── Sound grid (scrollable)
        container = tk.Frame(self, bg=C["bg"])
        container.pack(fill="both", expand=True, padx=12, pady=(0,12))

        canvas = tk.Canvas(container, bg=C["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                  command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.grid_frame = tk.Frame(canvas, bg=C["bg"])
        self.grid_win   = canvas.create_window((0,0), window=self.grid_frame,
                                                anchor="nw")
        self.grid_frame.bind("<Configure>",
                              lambda e: canvas.configure(
                                  scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(self.grid_win, width=e.width))
        canvas.bind_all("<MouseWheel>",
                         lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))
        self._canvas = canvas

        self._empty_lbl = tk.Label(self.grid_frame,
                                    text="No sounds yet.\nClick  + Add Sound  to get started!",
                                    fg=C["muted"], bg=C["bg"],
                                    font=("Segoe UI", 13), justify="center")

    # ── Sound management ────────────────────────────────────────────────────
    def _new_id(self):
        sid = str(self._next_id)
        self._next_id += 1
        return sid

    def _load_sounds(self):
        for s in self.config_data.get("sounds", []):
            if "id" not in s:
                s["id"] = self._new_id()
            else:
                try:
                    self._next_id = max(self._next_id, int(s["id"])+1)
                except Exception:
                    pass
            self._add_card(s)

    def _add_card(self, sound_data: dict):
        card = SoundCard(self.grid_frame, sound_data, self.engine,
                          on_edit=self._edit_sound,
                          on_delete=self._delete_sound)
        card.pack(fill="x", pady=3, padx=2)
        self._sound_cards[sound_data["id"]] = card
        self._update_empty()

    def _add_sound(self):
        path = filedialog.askopenfilename(
            title="Select Sound File",
            filetypes=[("Audio Files", " ".join(f"*{e}" for e in AUDIO_EXTS)),
                       ("All Files", "*.*")])
        if not path:
            return
        sid  = self._new_id()
        name = os.path.splitext(os.path.basename(path))[0]
        color = SoundCard.COLORS[(int(sid)-1) % len(SoundCard.COLORS)]
        sound = {"id": sid, "name": name, "path": path,
                 "hotkey": "", "volume": 100, "color": color,
                 "passthrough": True}
        self.config_data["sounds"].append(sound)
        save_config(self.config_data)
        self._add_card(sound)
        self._edit_sound(sound)   # open edit immediately

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select Folder of Sounds")
        if not folder:
            return
        added = 0
        for fname in sorted(os.listdir(folder)):
            if os.path.splitext(fname)[1].lower() in AUDIO_EXTS:
                path  = os.path.join(folder, fname)
                sid   = self._new_id()
                name  = os.path.splitext(fname)[0]
                color = SoundCard.COLORS[(int(sid)-1) % len(SoundCard.COLORS)]
                sound = {"id": sid, "name": name, "path": path,
                         "hotkey": "", "volume": 100, "color": color,
                         "passthrough": True}
                self.config_data["sounds"].append(sound)
                self._add_card(sound)
                added += 1
        save_config(self.config_data)
        self.status_lbl.config(text=f"Added {added} sounds from folder")

    def _edit_sound(self, sound_data: dict):
        def on_save(updated):
            # Update in config list
            for i, s in enumerate(self.config_data["sounds"]):
                if s["id"] == updated["id"]:
                    self.config_data["sounds"][i] = updated
                    break
            save_config(self.config_data)
            # Refresh card
            card = self._sound_cards.get(updated["id"])
            if card:
                card.sound = updated
                card.refresh()
            self._rebind_hotkeys()

        old_hk = sound_data.get("hotkey","")
        if old_hk:
            self.hk_mgr.unbind(old_hk)
        EditDialog(self, sound_data, self.hk_mgr, on_save=on_save)

    def _delete_sound(self, sound_data: dict):
        if not messagebox.askyesno("Delete", f"Delete '{sound_data['name']}'?"):
            return
        sid = sound_data["id"]
        self.engine.stop(sid)
        hk = sound_data.get("hotkey","")
        if hk: self.hk_mgr.unbind(hk)
        card = self._sound_cards.pop(sid, None)
        if card: card.destroy()
        self.config_data["sounds"] = [
            s for s in self.config_data["sounds"] if s["id"] != sid]
        save_config(self.config_data)
        self._update_empty()

    def _filter_sounds(self):
        q = self.search_var.get().lower()
        for sid, card in self._sound_cards.items():
            name = card.sound.get("name","").lower()
            if q in name:
                card.pack(fill="x", pady=3, padx=2)
            else:
                card.pack_forget()

    def _update_empty(self):
        if self._sound_cards:
            self._empty_lbl.pack_forget()
        else:
            self._empty_lbl.pack(expand=True)

    # ── Hotkeys ─────────────────────────────────────────────────────────────
    def _bind_hotkeys(self):
        self._rebind_hotkeys()
        stop_hk = self.config_data.get("stop_hotkey","f4")
        if stop_hk:
            self.hk_mgr.bind(stop_hk, self._stop_all)

    def _rebind_hotkeys(self):
        # Clear all sound hotkeys first
        for s in self.config_data.get("sounds",[]):
            hk = s.get("hotkey","")
            if hk: self.hk_mgr.unbind(hk)
        # Re-bind
        for s in self.config_data.get("sounds",[]):
            hk = s.get("hotkey","")
            if hk:
                sid = s["id"]
                def make_cb(sound=s):
                    def cb():
                        vol = sound.get("volume",100)/100.0
                        pt  = sound.get("passthrough", True)
                        self.engine.play(sound["path"], vol, sound["id"], passthrough=pt)
                        card = self._sound_cards.get(sound["id"])
                        if card:
                            self.after(0, lambda: card._set_playing(True))
                    return cb
                self.hk_mgr.bind(hk, make_cb())

    # ── Settings ─────────────────────────────────────────────────────────────
    def _open_settings(self):
        def on_save():
            save_config(self.config_data)
            self._apply_audio_config()
            self._rebind_hotkeys()
        SettingsPanel(self, self.engine, self.config_data, on_save=on_save)

    def _apply_audio_config(self):
        pt = self.config_data.get("passthrough_device")
        self.engine.set_passthrough_device(pt)
        sp = self.config_data.get("speaker_device")
        self.engine.set_speaker_device(sp)
        mv = self.config_data.get("master_volume", 80) / 100.0
        # Update passthrough indicator
        if hasattr(self, "pt_dot"):
            if pt is not None and pt >= 0:
                self.pt_dot.config(fg=C["green"], text="● Passthrough ON")
            else:
                self.pt_dot.config(fg=C["muted"], text="● Passthrough OFF")

    # ── Stop all ─────────────────────────────────────────────────────────────
    def _stop_all(self):
        self.engine.stop_all()
        for card in self._sound_cards.values():
            card._set_playing(False)
        self.status_lbl.config(text="Stopped all sounds")

    # ── Close ─────────────────────────────────────────────────────────────────
    def _on_close(self):
        save_config(self.config_data)
        self.engine.shutdown()
        self.hk_mgr.stop()
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = SoundDropApp()
    app.mainloop()
