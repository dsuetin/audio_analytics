"""Audio Analytics status window (GUI).

Shows a small always-on-top window on the store monitor: microphone state,
consultant name, current phrase, and gRPC/Kafka connection status. It reads
the client's live status from a named shared-memory block (RAM, published by
``client.py``) so it is independent of the client: closing it does not stop
the client and vice versa.

The window is intentionally hard to close (system Close button is removed and
WM_DELETE_WINDOW is intercepted) so that an operator cannot accidentally stop
the status display.
"""
import os
import ctypes
import threading
import time
from ctypes import wintypes

from client import SharedStatusReader


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL = 0.16

_shared_reader = SharedStatusReader()


# ---------------------------------------------------------------------------
# Single instance (Windows Mutex)
# ---------------------------------------------------------------------------

import ctypes
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
kernel32 = ctypes.windll.kernel32


class SingleInstanceLock:
    """Single-instance lock using a Windows named mutex.
    No files are created — the OS manages the mutex lifetime."""

    def __init__(self, name="AudioAnalyticsGUI"):
        self._name = name
        self._handle = None
        self.acquired = False

    def acquire(self):
        if self._handle is not None:
            return True
        try:
            self._handle = kernel32.CreateMutexW(None, True, self._name)
            if not self._handle:
                return False
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(self._handle)
                self._handle = None
                return False
            self.acquired = True
            return True
        except Exception:
            self._handle = None
            return False

    def release(self):
        if self._handle is not None:
            try:
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            self.acquired = False


# ---------------------------------------------------------------------------
# Window close prevention (Windows only)
# ---------------------------------------------------------------------------

_scan = None
WM_DELETE_WINDOW = 0x0010


# System menu constants
SC_CLOSE = 0xF060
SC_MINIMIZE = 0xF020
MF_BYCOMMAND = 0x0000
GWL_STYLE = -16
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
GA_ROOT = 2


def _remove_system_close(root):
    """Remove the Close and Maximize buttons from the window, keep Minimize.

    Uses GetWindowLong/SetWindowLong to strip WS_MAXIMIZEBOX and remove
    SC_CLOSE from the system menu. Resolves the real top-level HWND via
    GetAncestor because ``root.winfo_id()`` returns a child window handle
    in Tkinter on Windows."""
    try:
        child = root.winfo_id()
        hwnd = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
        if not hwnd:
            hwnd = ctypes.windll.user32.GetParent(child)

        # 1) Strip maximize style -> maximize button disappears from titlebar.
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~WS_MAXIMIZEBOX
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)

        # 2) Refresh the system menu and remove Close only.
        ctypes.windll.user32.GetSystemMenu(hwnd, True)  # reset menu cache
        hmenu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
        if hmenu:
            ctypes.windll.user32.RemoveMenu(hmenu, SC_CLOSE, MF_BYCOMMAND)

        ctypes.windll.user32.DrawMenuBar(hwnd)
    except Exception:
        pass


def _prevent_close():
    """Intercept close attempts - the window should not be closed by the user."""
    pass


# ---------------------------------------------------------------------------
# Status reader
# ---------------------------------------------------------------------------

def read_status():
    """Fetch the client's status from shared memory (RAM). Returns the latest
    snapshot, or {} if the client has not published anything yet / is down."""
    data = _shared_reader.read()
    if data is None:
        return {}
    return data


def default_status():
    return {
        "mic_connected": False,
        "mic_device": "",
        "consultant_name": "",
        "current_phrase": "",
        "phrase_is_final": False,
        "partial_phrase": "",
        "recognized_phrase": "",
        "grpc_connected": False,
        "kafka_connected": False,
        "purchase_seq": 0,
        "purchase_text": "",
    }


status = default_status()

# Purchase notification state (reset when the window is built).
purchase_notify = {"seq": 0, "after_id": None}
PURCHASE_SHOW_MS = 6000


def poll_loop():
    global status
    while True:
        status = read_status()
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

def build_window():
    import tkinter as tk

    global root
    root = tk.Tk()
    root.title("Audio Analytics")
    root.geometry("400x300")
    root.resizable(False, False)
    root.configure(bg="#0f1420")
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", _prevent_close)
    root.after(300, _remove_system_close, root)

    # Color palette (dark modern theme)
    BG = "#0f1420"
    CARD = "#1a2233"
    BORDER = "#2a3550"
    MIC_ON = "#34d399"
    MIC_OFF = "#f87171"
    CONN_ON = "#34d399"
    CONN_OFF = "#64748b"
    TEXT = "#e2e8f0"
    MUTED = "#8b96a8"
    ACCENT = "#38bdf8"

    def make_card(container, pady_top=8):
        card = tk.Frame(container, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=10, pady=(pady_top, 0))
        return card

    # --- Card 1: microphone / consultant ---
    card1 = make_card(root, 8)
    mic_label = tk.Label(card1, text="", font=("Consolas", 11),
                         justify="left", anchor="w", bg=CARD, fg=TEXT)
    mic_label.pack(fill="x", padx=10, pady=(6, 0))

    cons_label = tk.Label(card1, text="", font=("Segoe UI", 11),
                          justify="left", anchor="w", bg=CARD, fg=MUTED)
    cons_label.pack(fill="x", padx=10, pady=(0, 6))

    # --- Card 2: recognition (intermediate vs final, different colour) ---
    card2 = make_card(root)
    recog_label = tk.Label(card2, text="Ожидание речи...", font=("Segoe UI", 14, "bold"),
                           justify="left", anchor="w", bg=CARD, fg=MUTED,
                           wraplength=360)
    recog_label.pack(fill="x", padx=10, pady=10)

    # --- Footer: connections ---
    footer = tk.Frame(root, bg=BG)
    footer.pack(fill="x", padx=10, pady=(8, 0))
    conn_label = tk.Label(footer, text="", font=("Consolas", 10),
                          justify="left", anchor="w", bg=BG, fg=TEXT)
    conn_label.pack(fill="x")

    # --- Purchase notification overlay (big dollars emoji) ---
    purchase_frame = tk.Frame(root, bg="#0d1520",
                              highlightbackground="#f59e0b", highlightthickness=3)
    purchase_emoji = tk.Label(
        purchase_frame, text="\U0001F4B0", font=("Segoe UI Emoji", 64),
        bg="#0d1520", fg="#fbbf24")
    purchase_emoji.pack(padx=24, pady=(24, 2))
    purchase_title = tk.Label(
        purchase_frame, text="Покупка!", font=("Segoe UI", 15, "bold"),
        bg="#0d1520", fg="#fbbf24")
    purchase_title.pack(pady=(0, 4))
    purchase_text_label = tk.Label(
        purchase_frame, text="", font=("Segoe UI", 12),
        bg="#0d1520", fg="#e2e8f0", wraplength=340, justify="center")
    purchase_text_label.pack(padx=16, pady=(0, 22))

    def show_purchase(text):
        if PURCHASE_SHOW_MS <= 0:
            return
        if purchase_notify["after_id"] is not None:
            try:
                root.after_cancel(purchase_notify["after_id"])
            except Exception:
                pass
        purchase_text_label.config(text=text or "")
        purchase_frame.place(relx=0.5, rely=0.5, anchor="center",
                             relwidth=1, relheight=1)
        purchase_frame.lift()
        purchase_notify["after_id"] = root.after(PURCHASE_SHOW_MS, hide_purchase)

    def hide_purchase():
        purchase_notify["after_id"] = None
        purchase_frame.place_forget()

    def refresh():
        # Keep the window always on top (other apps may try to override it).
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

        s = status
        mic_connected = bool(s.get("mic_connected"))
        mic_text = (s.get("mic_device") or "?") if mic_connected else "микрофон не подключён"
        mic_label.config(
            text="\u25cf Микрофон: %s\n    %s" % (
                "подключён" if mic_connected else "отключён",
                mic_text,
            ),
            fg=MIC_ON if mic_connected else MIC_OFF,
        )

        cons = s.get("consultant_name") or ""
        cons_label.config(text="\U0001F464 Консультант: %s" % cons)

        # Recognition text: intermediate (partial) vs final - different colours
        phrase = s.get("current_phrase") or ""
        is_final = bool(s.get("phrase_is_final"))
        if phrase:
            if is_final:
                recog_label.config(text=phrase, fg=MIC_ON)  # final = green
            else:
                recog_label.config(text="\u25cf " + phrase, fg=ACCENT)  # partial = blue
        else:
            recog_label.config(text="Ожидание речи...", fg=MUTED)

        grpc_ok = bool(s.get("grpc_connected"))
        kafka_ok = bool(s.get("kafka_connected"))
        conn_label.config(
            text="gRPC: %s     Kafka: %s" % (
                ("\u25cf подключён" if grpc_ok else "\u25cb отключён"),
                ("\u25cf подключён" if kafka_ok else "\u25cb отключён"),
            ),
            fg=CONN_ON if (grpc_ok and kafka_ok) else CONN_OFF,
        )

        # New purchase -> show the big dollars-emoji overlay.
        try:
            seq = int(s.get("purchase_seq", 0))
            if seq and seq != purchase_notify["seq"]:
                purchase_notify["seq"] = seq
                show_purchase(s.get("purchase_text", ""))
        except Exception:
            pass

        root.after(int(POLL_INTERVAL * 1000), refresh)

    refresh()
    return root


def main():
    lock = SingleInstanceLock()
    if not lock.acquire():
        return 0

    try:
        import tkinter as tk
    except Exception:
        lock.release()
        return 1

    threading.Thread(target=poll_loop, name="status-poller", daemon=True).start()

    root = build_window()
    try:
        root.mainloop()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
