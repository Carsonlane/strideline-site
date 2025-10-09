"""
Pace Lights GUI for Raspberry Pi (Tkinter)
- Queue of events (up to ~50)
- Start/Stop controls with emergency halt
- Monotonic timing loop for accurate pacing
- Pluggable LED driver (rpi_ws281x)

Save as: pace_gui.py
Run:    python3 pace_gui.py

If using real LEDs, ensure rpi_ws281x is available.
"""
from __future__ import annotations
import threading
import time
import queue
import math
from dataclasses import dataclass
from typing import Optional

# --- Branding / UI ---
try:
    from PIL import Image, ImageTk  # pip install pillow
except Exception:
    Image = None
    ImageTk = None

NEON_GREEN = "#8CFF00"     # brand accent
DARK_BG    = "#0A0A0A"     # app background
MID_BG     = "#121212"     # panel background
TEXT_FG    = "#E6FFE6"     # high-contrast text
SUBDUED_FG = "#9EDB9E"     # secondary text
FONT_FAMILY = "SF Pro Display"  # fallback handled below
LOGO_PATH_CANDIDATES = [
    "./logo.png",
    "StrideLine.png",
    "/home/pi/ledproj/.venv/logo.png",
]

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:
    print("Tkinter is required to run this GUI.")
    raise

# ==========================
# Configuration
# ==========================
LED_COUNT = 200   # number of LEDs on the rail
GPIO_PIN = 18     # PWM pin (default for ws281x)
FREQ_HZ = 800000
DMA = 10
BRIGHTNESS = 255
INVERT = False
CHANNEL = 0
COLOR_ORDER = "GRB"  # Common for UCS1903/WS281x strips
MAX_FPS = 120  # cap rendering to reduce CPU load on the Pi
START_LED_MILE = 191  # 1609m (mile) start LED index

# ==========================
# LED Driver Abstraction
# ==========================
class BaseStrip:
    def begin(self):
        raise NotImplementedError

    def show(self):
        raise NotImplementedError

    def setPixelColor(self, i: int, color: int):
        raise NotImplementedError

    def numPixels(self) -> int:
        raise NotImplementedError

    def clear(self):
        for i in range(self.numPixels()):
            self.setPixelColor(i, 0)
        self.show()



def pack_color(r: int, g: int, b: int, order: str = COLOR_ORDER) -> int:
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    if order.upper() == "RGB":
        return (r << 16) | (g << 8) | b
    elif order.upper() == "GRB":
        return (g << 16) | (r << 8) | b
    elif order.upper() == "BRG":
        return (b << 16) | (r << 8) | g
    else:  # default to RGB
        return (r << 16) | (g << 8) | b

# --- Helper functions for parallel event blending ---
def clamp_byte(x: int) -> int:
    return 0 if x < 0 else (255 if x > 255 else x)

def blend_additive(c1: tuple[int,int,int], c2: tuple[int,int,int]) -> tuple[int,int,int]:
    r = clamp_byte(c1[0] + c2[0])
    g = clamp_byte(c1[1] + c2[1])
    b = clamp_byte(c1[2] + c2[2])
    return (r, g, b)

def pack_tuple(color_tuple: tuple[int,int,int], order: str = COLOR_ORDER) -> int:
    r, g, b = color_tuple
    return pack_color(r, g, b, order=order)




class WS281xStrip(BaseStrip):
    def __init__(self, n):
        from rpi_ws281x import Adafruit_NeoPixel, ws
        if COLOR_ORDER.upper() == "GRB":
            strip_type = ws.WS2811_STRIP_GRB
        elif COLOR_ORDER.upper() == "RGB":
            strip_type = ws.WS2811_STRIP_RGB
        elif COLOR_ORDER.upper() == "BRG":
            strip_type = ws.WS2811_STRIP_BRG
        else:
            strip_type = ws.WS2811_STRIP_GRB
        self.strip = Adafruit_NeoPixel(
            n, GPIO_PIN, FREQ_HZ, DMA, INVERT, BRIGHTNESS, CHANNEL, strip_type
        )

    def begin(self):
        self.strip.begin()

    def show(self):
        self.strip.show()

    def setPixelColor(self, i, color):
        self.strip.setPixelColor(i, color)

    def numPixels(self):
        return self.strip.numPixels()


# ==========================
# Domain Model
# ==========================
@dataclass
class PaceEvent:
    name: str
    distance_m: int               # race distance (e.g., 800, 1500)
    rail_leds: int                # number of LEDs on rail (e.g., 200)
    target_time_s: float          # target finish time in seconds
    laps_m: int = 400             # track lap length in meters
    color: tuple = (0, 255, 0)    # default green
    trail_len: int = 5            # trailing lights
    splits_100: Optional[list[float]] = None  # per-100m times (sec), only for 600/800
    start_led: Optional[int] = None           # optional fixed start LED index (e.g., mile start)

    def pace_mps(self) -> float:
        return self.distance_m / self.target_time_s if self.target_time_s > 0 else 0.0

    def leds_per_meter(self) -> float:
        # Rail covers one lap (laps_m); map meters to LEDs
        return self.rail_leds / float(self.laps_m)

    def total_led_traversals(self) -> int:
        # How many times the head should go from 1 -> rail_leds
        return max(1, math.ceil(self.distance_m / self.laps_m))

    def precompute_trail(self) -> list[tuple[int,int,int]]:
        r, g, b = self.color
        levels = []
        for t in range(self.trail_len, -1, -1):
            factor = max(0.1, 1.0 - (t / max(1, self.trail_len)))
            levels.append((int(r * factor), int(g * factor), int(b * factor)))
        return levels


# ==========================
# Runner Thread
# ==========================
class Runner(threading.Thread):
    def __init__(self, strip: BaseStrip, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.strip = strip
        self.queue: "queue.Queue[PaceEvent]" = queue.Queue()
        self.stop_event = stop_event
        self.running_event = threading.Event()  # set while actively running an event

    def enqueue(self, ev: PaceEvent):
        self.queue.put(ev)

    def _push_frame(self, fb: list[tuple[int,int,int]]):
        for i, (r, g, b) in enumerate(fb):
            self.strip.setPixelColor(i, pack_color(r, g, b, order=COLOR_ORDER))
        self.strip.show()

    def run_event_with_splits(self, ev: PaceEvent):
        """
        Variable-pace runner using per-100m splits (sec). Distance-based completion.
        """
        splits = ev.splits_100 or []
        if not splits:
            return self.run_event(ev)
        lpm = ev.leds_per_meter()
        rail_leds = self.strip.numPixels()
        self.running_event.set()
        # Framebuffer approach to keep drawing consistent with parallel runner
        fb = [(0,0,0)] * rail_leds
        # Start at event's start_led if specified
        start_led = (ev.start_led % rail_leds) if (ev.start_led is not None and rail_leds > 0) else 0
        pos_f = float(start_led)
        meters_done = 0.0
        head_rgb = ev.color
        trail_levels = ev.precompute_trail()
        # Measure and compensate startup delay before first movement
        t0 = time.monotonic()
        try:
            self._push_frame(fb)
        except Exception:
            pass
        startup_delay = max(0.0, time.monotonic() - t0)
        # Use the first segment’s speed for compensation
        first_seg_mps = 100.0 / (splits[0] if splits and splits[0] > 0 else 1e-9)
        first_v = first_seg_mps * lpm
        pos_f += first_v * startup_delay
        meters_done += (first_v / lpm) * startup_delay
        frame_dt_target = 1.0 / MAX_FPS
        next_time = time.monotonic() + frame_dt_target
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_time:
                time.sleep(next_time - now)
                continue
            dt = frame_dt_target
            next_time += frame_dt_target
            # choose current segment by meters_done
            seg_idx = int(min(len(splits) - 1, meters_done // 100))
            mps = 100.0 / splits[seg_idx]  # meters per second for this 100m block
            v = mps * lpm                  # leds per second
            # advance
            pos_f += v * dt
            meters_done += (v / lpm) * dt
            # clear framebuffer
            fb = [(0,0,0)] * rail_leds
            # wrap and draw
            head_idx = int(pos_f % rail_leds)
            for t, col in enumerate(trail_levels):
                idx = head_idx - (ev.trail_len - t)
                if idx < 0:
                    continue
                fb[idx] = col
            # push
            for i, (r,g,b) in enumerate(fb):
                self.strip.setPixelColor(i, pack_color(r, g, b, order=COLOR_ORDER))
            try:
                self.strip.show()
            except Exception as e:
                print(f"[WARN] show() throttled: {e}")
            if meters_done >= ev.distance_m:
                break
        try:
            self.strip.clear()
        except Exception:
            pass
        self.running_event.clear()

    def run_event(self, ev: PaceEvent):
        # If custom per-100m splits are provided (for 600/800), use variable-pace path
        if ev.splits_100:
            return self.run_event_with_splits(ev)
        # Constant-pace framebuffer runner with optional start_led
        lpm = ev.leds_per_meter()
        rail_leds = self.strip.numPixels()
        mps = ev.pace_mps()
        leds_per_sec = mps * lpm
        if leds_per_sec <= 0 or rail_leds <= 0:
            return
        self.running_event.set()
        fb = [(0,0,0)] * rail_leds
        start_led = (ev.start_led % rail_leds) if (ev.start_led is not None and rail_leds > 0) else 0
        pos_f = float(start_led)
        meters_done = 0.0
        trail_levels = ev.precompute_trail()
        frame_dt_target = 1.0 / MAX_FPS
        # Measure and compensate startup delay (first .show() / DMA warm-up)
        fb = [(0,0,0)] * rail_leds  # ensure defined prior to push
        t0 = time.monotonic()
        try:
            self._push_frame(fb)
        except Exception:
            pass
        startup_delay = max(0.0, time.monotonic() - t0)
        # Pre-advance position by the measured delay so first lap timing aligns
        pos_f += leds_per_sec * startup_delay
        meters_done += (leds_per_sec / lpm) * startup_delay
        next_time = time.monotonic() + frame_dt_target
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_time:
                time.sleep(next_time - now)
                continue
            dt = frame_dt_target
            next_time += frame_dt_target
            v = leds_per_sec
            pos_f += v * dt
            meters_done += (v / lpm) * dt
            while pos_f >= rail_leds:
                pos_f -= rail_leds
            if meters_done >= ev.distance_m:
                break
            # Clear framebuffer and draw head+trail
            fb = [(0,0,0)] * rail_leds
            head_idx = int(pos_f)
            for t, col in enumerate(trail_levels):
                idx = head_idx - (ev.trail_len - t)
                if idx < 0:
                    continue
                fb[idx] = col
            for i, (r,g,b) in enumerate(fb):
                self.strip.setPixelColor(i, pack_color(r, g, b, order=COLOR_ORDER))
            try:
                self.strip.show()
            except Exception as e:
                print(f"[WARN] show() throttled: {e}")
        try:
            self.strip.clear()
        except Exception:
            pass
        self.running_event.clear()
    def run_events_parallel(self, events: list[PaceEvent]):
        """
        Run up to two PaceEvents simultaneously on the same rail.
        Uses a fixed-timestep compositor so the GUI stays responsive
        and trails from both events blend additively.
        """
        if not events:
            return
        if len(events) > 2:
            events = events[:2]

        # Validate rail configurations match
        rail_leds = self.strip.numPixels()
        for ev in events:
            if ev.rail_leds != rail_leds:
                raise ValueError("All events must use the same Rail LEDs to run in parallel.")
            if ev.laps_m != events[0].laps_m:
                raise ValueError("All events must use the same lap length to run in parallel.")

        # Per-event state
        state = []
        for ev in events:
            mps = ev.pace_mps()
            lpm = ev.leds_per_meter()
            leds_per_sec = mps * lpm
            if leds_per_sec <= 0:
                continue
            use_splits = bool(ev.splits_100)
            start_led = (ev.start_led % rail_leds) if (ev.start_led is not None and rail_leds > 0) else 0
            st = {
                "ev": ev,
                "leds_per_sec": leds_per_sec,
                "pos_f": float(start_led),
                "traversals_needed": ev.total_led_traversals(),
                "traversals_done": 0,
                "meters_done": 0.0,
                "use_splits": use_splits,
                "done": False,
            }
            st["trail_levels"] = ev.precompute_trail()
            state.append(st)

        if not state:
            return

        # Global startup delay compensation for parallel run
        fb = [(0,0,0)] * rail_leds
        t0 = time.monotonic()
        try:
            self._push_frame(fb)
        except Exception:
            pass
        startup_delay = max(0.0, time.monotonic() - t0)
        if startup_delay > 0.0:
            for st in state:
                v0 = st["leds_per_sec"] if not st["use_splits"] else (100.0 / (st["ev"].splits_100[0] if st["ev"].splits_100 and st["ev"].splits_100[0] > 0 else 1e-9)) * st["ev"].leds_per_meter()
                st["pos_f"] += v0 * startup_delay
                st["meters_done"] += (v0 / st["ev"].leds_per_meter()) * startup_delay

        self.running_event.set()
        # working framebuffer as tuples
        frame_dt_target = 1.0 / MAX_FPS
        next_time = time.monotonic() + frame_dt_target

        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_time:
                time.sleep(next_time - now)
                continue
            dt = frame_dt_target
            next_time += frame_dt_target

            # Clear only the framebuffer (avoid calling strip.clear() every frame)
            fb = [(0,0,0)] * rail_leds

            all_done = True
            for st in state:
                if st["done"]:
                    continue
                all_done = False
                ev = st["ev"]
                # pick velocity
                if st["use_splits"]:
                    splits = st["ev"].splits_100
                    lpm = st["ev"].leds_per_meter()
                    # meters/sec from current 100m split
                    seg_idx = int(min(len(splits) - 1, st["meters_done"] // 100))
                    mps = 100.0 / splits[seg_idx]
                    v = mps * lpm
                else:
                    v = st["leds_per_sec"]
                # advance position and meters
                st["pos_f"] += v * dt
                st["meters_done"] += (v / st["ev"].leds_per_meter()) * dt
                # wrap position around rail
                while st["pos_f"] >= rail_leds:
                    st["pos_f"] -= rail_leds
                    if not st["use_splits"]:
                        st["traversals_done"] += 1
                        if st["traversals_done"] >= st["traversals_needed"]:
                            st["done"] = True
                            break
                # completion condition
                if st["use_splits"]:
                    if st["meters_done"] >= st["ev"].distance_m:
                        st["done"] = True
                        continue
                else:
                    if st["done"]:
                        continue

                head_idx = int(st["pos_f"])
                # Draw head + trail onto framebuffer with simple linear fade
                levels = st["trail_levels"]
                for t, col in enumerate(levels):
                    idx = head_idx - (ev.trail_len - t)
                    if idx < 0:
                        continue
                    fb[idx] = blend_additive(fb[idx], col)

            # Push framebuffer to hardware
            for i, (r, g, b) in enumerate(fb):
                self.strip.setPixelColor(i, pack_color(r, g, b, order=COLOR_ORDER))
            try:
                self.strip.show()
            except Exception as e:
                print(f"[WARN] show() throttled: {e}")

            if all_done:
                break

        # cleanup
        try:
            self.strip.clear()
        except Exception:
            pass
        self.running_event.clear()

    def run(self):
        while not self.stop_event.is_set():
            try:
                ev = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.stop_event.is_set():
                break
            try:
                self.run_event(ev)
            except Exception as e:
                print(f"[ERROR] Event failed: {e}")


# ==========================
# GUI
# ==========================
class PaceGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("StrideLine Pace Lights")
        self.root.geometry("1000x600")
        self.root.configure(bg=DARK_BG)
        self._apply_theme()
        # If window focus is grabbed by startup, briefly force-top, then release
        try:
            self.root.attributes("-topmost", True)
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        # --- Header (logo + title) ---
        self._build_header()

        # LED strip (real hardware only)
        try:
            self.strip: BaseStrip = WS281xStrip(LED_COUNT)
            self.strip.begin()
        except Exception as e:
            messagebox.showerror("LED Init Error", f"WS281x initialization failed: {e}")
            raise

        # worker (manual mode)
        self.stop_event = threading.Event()
        self.runner = Runner(self.strip, self.stop_event)
        # self.runner.start()  # manual mode: don't auto-consume

        # manual-queue state
        self.events = []            # type: list[PaceEvent]
        self.current_thread = None  # type: threading.Thread | None
        self.current_idx = None     # type: int | None
        self.current_dual = False  # whether a dual run is active

        # UI elements
        self._build_form()
        self._build_queue()
        self._build_controls()
        self._build_current()   # NEW: shows the currently running event

        # graceful shutdown
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Allow Esc to trigger STOP so you can regain control if mouse is unresponsive
        try:
            self.root.bind("<Escape>", lambda *_: self.stop_all())
        except Exception:
            pass

    def _apply_theme(self):
        # ttk theme colors
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        # Base styles
        style.configure("TFrame", background=DARK_BG)
        style.configure("Card.TLabelframe", background=MID_BG, foreground=TEXT_FG, bordercolor=NEON_GREEN)
        style.configure("Card.TLabelframe.Label", background=MID_BG, foreground=TEXT_FG)
        style.configure("TLabel", background=DARK_BG, foreground=TEXT_FG)
        style.configure("Header.TLabel", background=DARK_BG, foreground=NEON_GREEN, font=(FONT_FAMILY, 22, "bold"))
        style.configure("TButton", background=MID_BG, foreground=TEXT_FG, padding=8)
        style.map("TButton",
                  background=[("active", "#1A1A1A")],
                  foreground=[("disabled", "#777777")])

    def _load_logo_image(self, height=64):
        if Image is None:
            return None
        # Try candidates; fall back to none
        for p in LOGO_PATH_CANDIDATES:
            try:
                img = Image.open(p).convert("RGBA")
                # maintain aspect ratio
                w, h = img.size
                new_w = int(w * (height / float(h)))
                img = img.resize((new_w, height), Image.LANCZOS)
                # subtle glow: compositing an outer neon stroke
                return ImageTk.PhotoImage(img)
            except Exception:
                continue
        return None

    def _build_header(self):
        header = ttk.Frame(self.root, style="TFrame")
        header.pack(side=tk.TOP, fill=tk.X)
        # gradient-like strip (simple Canvas)
        canvas = tk.Canvas(header, height=6, bg=DARK_BG, highlightthickness=0)
        canvas.pack(fill=tk.X)
        # neon line
        canvas.create_rectangle(0, 0, 2000, 6, fill=NEON_GREEN, outline="")

        inner = ttk.Frame(header, style="TFrame")
        inner.pack(side=tk.TOP, fill=tk.X, padx=16, pady=10)
        self.logo_img = self._load_logo_image(56)
        if self.logo_img:
            logo = ttk.Label(inner, image=self.logo_img, style="TLabel")
            logo.pack(side=tk.LEFT, padx=(0,12))
        title = ttk.Label(inner, text="StrideLine", style="Header.TLabel")
        title.pack(side=tk.LEFT)
        subtitle = ttk.Label(inner, text="Futuristic pace light controller", foreground=SUBDUED_FG)
        subtitle.pack(side=tk.LEFT, padx=12)

    # --- UI Builders ---
    def _build_current(self):
        frm = ttk.LabelFrame(self.root, text="Current Event", style="Card.TLabelframe")
        frm.pack(side=tk.TOP, fill=tk.X, padx=10, pady=6)
        self.current_event_var = tk.StringVar(value="None")
        ttk.Label(frm, textvariable=self.current_event_var).pack(side=tk.LEFT, padx=8, pady=6)

    def _build_form(self):
        frm = ttk.LabelFrame(self.root, text="New Event", style="Card.TLabelframe")
        frm.configure(width=960)
        frm.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        # Inputs
        ttk.Label(frm, text="Event name").grid(row=0, column=0, sticky='w', padx=6, pady=6)
        self.name_var = tk.StringVar(value="Race")
        ttk.Entry(frm, textvariable=self.name_var, width=24).grid(row=0, column=1, padx=6, pady=6)

        ttk.Label(frm, text="Distance (m)").grid(row=0, column=2, sticky='w', padx=6, pady=6)
        self.dist_var = tk.StringVar(value="800")
        ttk.Entry(frm, textvariable=self.dist_var, width=10).grid(row=0, column=3, padx=6, pady=6)

        ttk.Label(frm, text="Target time (mm:ss.s)").grid(row=0, column=4, sticky='w', padx=6, pady=6)
        self.time_var = tk.StringVar(value="1:50.0")
        ttk.Entry(frm, textvariable=self.time_var, width=10).grid(row=0, column=5, padx=6, pady=6)

        ttk.Label(frm, text="Rail LEDs").grid(row=1, column=0, sticky='w', padx=6, pady=6)
        self.leds_var = tk.IntVar(value=LED_COUNT)
        ttk.Entry(frm, textvariable=self.leds_var, width=10).grid(row=1, column=1, padx=6, pady=6)

        ttk.Label(frm, text="Lap length (m)").grid(row=1, column=2, sticky='w', padx=6, pady=6)
        self.lap_var = tk.IntVar(value=400)
        ttk.Entry(frm, textvariable=self.lap_var, width=10).grid(row=1, column=3, padx=6, pady=6)

        ttk.Label(frm, text="Trail LEDs").grid(row=1, column=4, sticky='w', padx=6, pady=6)
        self.trail_var = tk.IntVar(value=5)
        ttk.Entry(frm, textvariable=self.trail_var, width=10).grid(row=1, column=5, padx=6, pady=6)

        ttk.Label(frm, text="Color (R,G,B)").grid(row=2, column=0, sticky='w', padx=6, pady=6)
        self.color_var = tk.StringVar(value="0,255,0")
        ttk.Entry(frm, textvariable=self.color_var, width=10).grid(row=2, column=1, padx=6, pady=6)

        self.add_btn = ttk.Button(frm, text="Add to Queue", command=self.add_event)
        self.add_btn.grid(row=2, column=5, padx=6, pady=6, sticky='e')

        # pace preview
        self.preview_var = tk.StringVar(value="Pace: – m/s • –/lap")
        ttk.Label(frm, textvariable=self.preview_var).grid(row=2, column=2, columnspan=3, sticky='w', padx=6)

        # Custom per-100m splits (only used for 600m/800m)
        ttk.Label(frm, text="100m splits (sec, CSV)").grid(row=3, column=0, sticky='w', padx=6, pady=6)
        self.splits_var = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.splits_var, width=30).grid(row=3, column=1, columnspan=2, padx=6, pady=6, sticky='w')
        ttk.Label(frm, text="(Applies when distance is 600 or 800)").grid(row=3, column=3, columnspan=3, sticky='w', padx=6)

        # live update preview when fields change
        for var in (self.dist_var, self.time_var, self.leds_var, self.lap_var):
            var_trace = getattr(var, 'trace_add', None)
            if var_trace:
                var.trace_add('write', lambda *_: self.update_preview())
        self.update_preview()

    def _build_queue(self):
        frm = ttk.LabelFrame(self.root, text="Event Queue", style="Card.TLabelframe")
        frm.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=6)

        self.listbox = tk.Listbox(frm, height=10, selectmode=tk.EXTENDED)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6,0), pady=6)
        self.listbox.configure(bg=MID_BG, fg=TEXT_FG, selectbackground=NEON_GREEN, selectforeground="#000000", highlightthickness=0, relief=tk.FLAT)

        sb = ttk.Scrollbar(frm, orient=tk.VERTICAL, command=self.listbox.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y, padx=0, pady=6)
        self.listbox.config(yscrollcommand=sb.set)

        btns = ttk.Frame(frm)
        btns.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=6)
        ttk.Button(btns, text="Start Selected", command=self.start_selected).pack(fill=tk.X, pady=3)
        ttk.Button(btns, text="Start Two (Dual Pace)", command=self.start_two_selected).pack(fill=tk.X, pady=3)
        ttk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(fill=tk.X, pady=3)
        ttk.Button(btns, text="Clear Queue", command=self.clear_queue).pack(fill=tk.X, pady=3)
    def start_two_selected(self):
        # prevent overlapping runs
        if self.current_thread and self.current_thread.is_alive():
            messagebox.showinfo("Busy", "An event is already running.")
            return

        idxs = list(self.listbox.curselection())
        if len(idxs) != 2:
            messagebox.showinfo("Select Two", "Select exactly two events to run together.")
            return

        idxs.sort()
        ev1 = self.events[idxs[0]] if idxs[0] < len(self.events) else None
        ev2 = self.events[idxs[1]] if idxs[1] < len(self.events) else None
        if not ev1 or not ev2:
            messagebox.showerror("Out of range", "Selected items not found.")
            return

        # Validate matching rail and lap settings
        if ev1.rail_leds != ev2.rail_leds:
            messagebox.showerror("Mismatch", "Both events must have the same Rail LEDs to run together.")
            return
        if ev1.laps_m != ev2.laps_m:
            messagebox.showerror("Mismatch", "Both events must have the same lap length to run together.")
            return

        self.current_dual = True
        self.current_idx = None
        self.current_event_var.set(f"Dual: {ev1.name} ({ev1.distance_m}m @ {ev1.target_time_s:.1f}s)  +  {ev2.name} ({ev2.distance_m}m @ {ev2.target_time_s:.1f}s)")
        self.stop_event.clear()

        def _run_and_finalize_dual():
            try:
                self.runner.run_events_parallel([ev1, ev2])
            finally:
                def _cleanup():
                    # Remove the higher index first
                    for i in sorted(idxs, reverse=True):
                        if 0 <= i < self.listbox.size():
                            self.listbox.delete(i)
                        if 0 <= i < len(self.events):
                            del self.events[i]
                    self.current_event_var.set("None")
                    self.current_dual = False
                self.root.after(0, _cleanup)

        self.current_thread = threading.Thread(target=_run_and_finalize_dual, daemon=True)
        self.current_thread.start()

    def _build_controls(self):
        frm = ttk.Frame(self.root, style="TFrame")
        frm.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)

        self.start_all_btn = ttk.Button(frm, text="Start All", command=self.start_all)
        self.start_all_btn.pack(side=tk.LEFT, padx=6)
        self.stop_btn = ttk.Button(frm, text="STOP (Emergency)", command=self.stop_all)
        self.stop_btn.pack(side=tk.LEFT, padx=12)
        self.status_var = tk.StringVar(value="Idle")
        status = ttk.Label(frm, textvariable=self.status_var)
        status.pack(side=tk.RIGHT)
        self._status_updater()

    # --- Helpers ---
    def _parse_time(self, s: str) -> float:
        s = s.strip()
        if ':' in s:
            mm, ss = s.split(':', 1)
            return int(mm) * 60 + float(ss)
        return float(s)

    def _parse_color(self, s: str):
        try:
            parts = [int(x) for x in s.split(',')]
            if len(parts) != 3:
                raise ValueError
            return tuple(max(0, min(255, v)) for v in parts)
        except Exception:
            return (0, 255, 0)

    def update_preview(self):
        try:
            dist = int(self.dist_var.get())
            tsec = self._parse_time(self.time_var.get())
            lap = int(self.lap_var.get())
            # If valid 100m splits provided for 600/800, override tsec with their sum
            use_splits = False
            splits_text = getattr(self, "splits_var", None).get() if hasattr(self, "splits_var") else ""
            splits = []
            if splits_text.strip():
                splits = [float(x.strip()) for x in splits_text.split(",") if x.strip()]
                if dist in (600, 800) and len(splits) == dist // 100 and all(s > 0 for s in splits):
                    tsec = sum(splits)
                    use_splits = True
            mps = dist / tsec if tsec > 0 else 0
            per_lap = tsec * (lap / dist) if dist > 0 else 0
            suffix = " • custom splits" if use_splits else ""
            self.preview_var.set(f"Pace: {mps:.2f} m/s • {per_lap:.2f}s per lap{suffix}")
        except Exception:
            self.preview_var.set("Pace: – m/s • –/lap")

    def add_event(self):
        # Parse optional per-100m splits
        splits_list = None
        try:
            stext = self.splits_var.get().strip() if hasattr(self, "splits_var") else ""
        except Exception:
            stext = ""
        if stext:
            try:
                raw = [x.strip() for x in stext.split(",") if x.strip()]
                splits = [float(x) for x in raw]
            except Exception:
                messagebox.showerror("Invalid Splits", "Splits must be comma-separated seconds, e.g., 14.5, 14.8, ...")
                return
            d = int(self.dist_var.get())
            if d not in (600, 800):
                messagebox.showerror("Distance Mismatch", "Custom 100m splits are only supported for 600m or 800m.")
                return
            if len(splits) != d // 100 or any(s <= 0 for s in splits):
                messagebox.showerror("Invalid Splits", f"Provide exactly {d//100} positive values (one per 100m).")
                return
            splits_list = splits
        try:
            ev = PaceEvent(
                name=self.name_var.get().strip() or "Race",
                distance_m=int(self.dist_var.get()),
                rail_leds=int(self.leds_var.get()),
                target_time_s=(sum(splits_list) if splits_list else self._parse_time(self.time_var.get())),
                laps_m=int(self.lap_var.get()),
                color=self._parse_color(self.color_var.get()),
                trail_len=int(self.trail_var.get()),
                splits_100=splits_list,
                start_led=(START_LED_MILE if int(self.dist_var.get()) == 1609 else None),
            )
        except Exception as e:
            messagebox.showerror("Invalid Input", f"Please check your inputs.\n{e}")
            return

        # If rail size changed, rebuild strip and attach to runner
        if ev.rail_leds != self.strip.numPixels():
            global LED_COUNT
            LED_COUNT = ev.rail_leds
            try:
                new_strip = WS281xStrip(LED_COUNT)
                new_strip.begin()
            except Exception as e:
                messagebox.showerror("LED Reinit Error", f"WS281x reinit failed: {e}")
                return
            self.strip = new_strip
            self.runner.strip = self.strip

        # Store + display only — DO NOT start automatically
        self.events.append(ev)
        self.listbox.insert(
            tk.END,
            f"{ev.name} — {ev.distance_m}m in {ev.target_time_s:.1f}s • LEDs:{ev.rail_leds} • trail:{ev.trail_len}"
            + (" • custom splits" if ev.splits_100 else "")
            + (f" • start@{START_LED_MILE}" if ev.start_led == START_LED_MILE else ""))

    def start_selected(self):
        # prevent overlapping runs
        if self.current_thread and self.current_thread.is_alive():
            messagebox.showinfo("Busy", "An event is already running.")
            return

        idxs = self.listbox.curselection()
        if not idxs:
            messagebox.showinfo("Select Event", "Choose an event in the queue.")
            return

        idx = idxs[0]
        if idx >= len(self.events):
            messagebox.showerror("Out of range", "Selected item not found.")
            return

        ev = self.events[idx]
        self.current_idx = idx
        self.current_event_var.set(f"{ev.name} — {ev.distance_m}m in {ev.target_time_s:.1f}s")
        self.stop_event.clear()

        def _run_and_finalize():
            try:
                self.runner.run_event(ev)
            finally:
                # remove the completed event from the queue on the UI thread
                def _cleanup():
                    if 0 <= idx < self.listbox.size():
                        self.listbox.delete(idx)
                    if 0 <= idx < len(self.events):
                        del self.events[idx]
                    self.current_event_var.set("None")
                    self.current_idx = None
                self.root.after(0, _cleanup)

        self.current_thread = threading.Thread(target=_run_and_finalize, daemon=True)
        self.current_thread.start()

    def remove_selected(self):
        idxs = list(self.listbox.curselection())
        if not idxs:
            return
        # Don’t remove items that are currently running
        if self.current_dual:
            messagebox.showinfo("Busy", "Cannot modify the queue while a dual run is active.")
            return
        if self.current_idx is not None and self.current_idx in idxs:
            messagebox.showinfo("Busy", "Cannot remove the currently running event.")
            return
        for i in reversed(idxs):
            if 0 <= i < self.listbox.size():
                self.listbox.delete(i)
            if 0 <= i < len(self.events):
                del self.events[i]

    def clear_queue(self):
        if self.current_thread and self.current_thread.is_alive():
            messagebox.showinfo("Busy", "Stop the current event before clearing the queue.")
            return
        self.listbox.delete(0, tk.END)
        self.events.clear()

    def start_all(self):
        messagebox.showinfo("Manual Mode", "Use Start Selected to run one event at a time.")

    def stop_all(self):
        self.stop_event.set()
        try:
            self.strip.clear()
        except Exception:
            pass
        self.status_var.set("Stopped")
        # Prepare a fresh stop_event for the next Start
        self.stop_event = threading.Event()
        self.runner.stop_event = self.stop_event
        self.current_dual = False
        self.current_event_var.set("None")

    def _status_updater(self):
        if self.stop_event.is_set():
            self.status_var.set("Stopped")
        else:
            running = (self.current_thread is not None and self.current_thread.is_alive()) or self.runner.running_event.is_set()
            self.status_var.set("Running" if running else "Idle")
        self.root.after(200, self._status_updater)

    def on_close(self):
        if messagebox.askokcancel("Quit", "Exit and turn off lights?"):
            self.stop_event.set()
            try:
                self.strip.clear()
            except Exception:
                pass
            self.root.destroy()


def main():
    root = tk.Tk()
    root.configure(bg=DARK_BG)
    PaceGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
