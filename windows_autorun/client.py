"""Audio Analytics Client (Windows store PC).

Streams microphone audio to the Audio Analytics server over gRPC and consumes
ASR / classification / session / alert / purchase / salesperson events from
Kafka.

Design goals:
  * start automatically via Windows Task Scheduler (see install.*);
  * never lose Kafka messages  -> manual commit, at-least-once semantics;
  * survive network / audio / Kafka outages with backoff reconnect;
  * allow an external watchdog to detect a hung process (heartbeat file);
  * only one client instance per machine (pid lock file).
"""

import sys
import os
import io
import json
import struct
import signal
import queue
import asyncio
import logging
import threading
import time
import subprocess
from logging.handlers import RotatingFileHandler
from multiprocessing import shared_memory
from queue import Empty

import grpc
import sounddevice as sd
from dotenv import load_dotenv
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import (
    KafkaError,
    UnknownMemberIdError,
    RebalanceInProgressError,
    CommitFailedError,
)

import bridge_pb2
import bridge_pb2_grpc


# ============================================================
# SHARED-MEMORY STATUS CHANNEL (client -> GUI)
# Instead of writing a status file to disk for the GUI to poll, the client
# publishes its live status into a small named shared-memory block (RAM) that
# gui.py attaches to and reads. This removes disk I/O from the hot status path
# and gives the GUI a consistent snapshot.
#
# Layout (little-endian), all within one fixed-size shared memory block:
#     [0:4]   magic         b"AAST"
#     [4:8]   payload_len   uint32 - byte length of the JSON payload
#     [8:12]  generation    uint32 - bumped on every publish
#     [12:]   payload       UTF-8 JSON bytes of the status dict
#
# The writer always writes the payload *before* bumping the generation, and the
# reader double-checks the header before/after copying the payload, so a
# mid-write collision is detected and the previous good snapshot is returned.
# ============================================================

SHM_NAME = "audio_analytics_status"
MAGIC = b"AAST"
_HEADER = struct.Struct("<4sII")
HEADER_SIZE = _HEADER.size  # 12
PAYLOAD_SIZE = 32 * 1024     # roomy; status JSON is only a few hundred bytes
BLOCK_SIZE = HEADER_SIZE + PAYLOAD_SIZE


class SharedStatusWriter:
    """Single-writer publisher used by the client's StatusWriter."""

    def __init__(self, name=SHM_NAME):
        self._name = name
        self._shm = None
        self._attached = False

    def _attach(self):
        if self._attached:
            return True
        try:
            self._shm = shared_memory.SharedMemory(
                name=self._name, create=True, size=BLOCK_SIZE
            )
            # fresh block: wipe the header, continue generation from 0
            self._shm.buf[0:HEADER_SIZE] = MAGIC + struct.pack("<II", 0, 0)
        except FileExistsError:
            try:
                self._shm = shared_memory.SharedMemory(name=self._name, create=False)
            except FileNotFoundError:
                return False
        self._attached = True
        return True

    def write(self, data):
        try:
            if not self._attach():
                return
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            if not payload or len(payload) > PAYLOAD_SIZE:
                return  # oversized; drop rather than corrupt the block
            buf = self._shm.buf
            # 1) publish the payload bytes
            buf[HEADER_SIZE:HEADER_SIZE + len(payload)] = payload
            # 2) compute the next generation from the current header
            gen = struct.unpack("<I", bytes(buf[8:12]))[0] + 1
            # 3) publish length + generation last so the reader's double-check
            #    can detect a half-written update.
            buf[0:HEADER_SIZE] = MAGIC + struct.pack("<II", len(payload), gen)
        except Exception:
            pass

    def close(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
            self._attached = False


class SharedStatusReader:
    """Single-reader consumer used by gui.py. Returns the last good snapshot,
    or None if the client has not (yet) published anything."""

    def __init__(self, name=SHM_NAME):
        self._name = name
        self._shm = None
        self._attached = False
        self._last = None
        self._gen = -1

    def read(self):
        try:
            if not self._attach():
                return None
            buf = self._shm.buf
            head = bytes(buf[0:HEADER_SIZE])
            magic, length, gen = _HEADER.unpack(head)
            if magic != MAGIC or length > PAYLOAD_SIZE:
                return self._last
            payload = bytes(buf[HEADER_SIZE:HEADER_SIZE + length])
            head2 = bytes(buf[0:HEADER_SIZE])
            if head2 != head:  # a write landed mid-read: retry next tick
                return self._last
            data = json.loads(payload.decode("utf-8"))
            self._gen = gen
            self._last = data
            return data
        except Exception:
            return self._last

    def _attach(self):
        if self._attached:
            return True
        try:
            self._shm = shared_memory.SharedMemory(name=self._name, create=False)
            self._attached = True
            return True
        except FileNotFoundError:
            return False

    def close(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
            self._attached = False
            self._last = None
            self._gen = -1



# ============================================================
# PATHS / CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "client.log")

load_dotenv(os.path.join(BASE_DIR, ".env"))

SERVER_IP = os.getenv("SERVER_IP", "172.16.20.111")
STORE_ID = os.getenv("STORE_ID", "main_store")
WORKER_NAME = os.getenv("WORKER_NAME", "unknown")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "").strip()
# Stable hardware identifier (USB VID/PID) of the EXTERNAL microphone to use.
# Unlike the device name/index, this does not change when the mic is plugged
# back in, so the client always selects exactly this microphone - even if
# Windows renames it (e.g. "... (2)"). See list_devices.py to find the ID.
AUDIO_DEVICE_ID = os.getenv("AUDIO_DEVICE_ID", "").strip()

KAFKA_BOOTSTRAP = f"{SERVER_IP}:19092"
GRPC_ADDR = f"{SERVER_IP}:6000"

# Independent per-store consumer group (critical for ~20 stores).
KAFKA_GROUP_ID = f"mic-client-{STORE_ID}"

KAFKA_TOPICS = (
    "asr_transcripts",
    "classified_events",
    "new_client_session",
    "alerts",
    "purchases",
    "salesperson_changes",
)

SAMPLE_RATE = 16000
CHUNK_MS = 150

# How long to wait for the microphone to (re)appear.
AUDIO_RECONNECT_DELAY = 2.0
# How long without an audio callback before we consider the stream dead.
AUDIO_CALLBACK_TIMEOUT = 3.0

# Heartbeat is updated every N seconds while the process is alive.
HEARTBEAT_INTERVAL = 30.0

CLASS_ICONS = {
    "buy": "\U0001F6D2",
    "service": "\U0001F527",
    "complaint": "\u26A0\ufe0f",
    "corporate": "\U0001F3E2",
    "working_hours": "\U0001F552",
    "vacancy": "\U0001F4BC",
    "help": "\u2753",
    "lost": "\U0001F9ED",
    "other": "\U0001F4AC",
}


# ============================================================
# SAFE OUTPUT (Task Scheduler has no interactive console)
# ============================================================

class _SafeStream:
    """A writeable stream that never raises even when the underlying
    stream is closed, missing or is /dev/null. Used so that a console-less
    Task Scheduler launch cannot crash the client."""

    def __init__(self, stream, name):
        self._stream = stream
        self._name = name

    def write(self, s):
        try:
            n = self._stream.write(s)
            return n if isinstance(n, int) else len(s)
        except Exception:
            return 0

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False


def _resolve_console():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            return _SafeStream(stream, "console")
    return _SafeStream(io.StringIO(), "null")


console = _resolve_console()
sys.stdout = console
sys.stderr = console


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("audio-analytics-client")
logger.setLevel(logging.INFO)
logger.propagate = False

formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(console)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


# ============================================================
# SINGLE-INSTANCE LOCK (Windows Mutex)
# ============================================================

import ctypes
from ctypes import wintypes

# Windows API constants
ERROR_ALREADY_EXISTS = 183

kernel32 = ctypes.windll.kernel32


class SingleInstanceLock:
    """Single-instance lock using a Windows named mutex.
    No files are created — the OS manages the mutex lifetime."""

    def __init__(self, name="AudioAnalyticsClient"):
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


# ============================================================
# HEARTBEAT / LIVENESS
# ============================================================

class AppClock:
    """Monotonic timestamp touched by the main / Kafka / audio loops on every
    progress step. The heartbeat writer uses it to decide whether the
    application is actually making progress; if the main loop is wedged
    (async dead-lock, blocked await) the clock stops moving."""

    def __init__(self):
        self.last_tick = time.monotonic()
        self._lock = threading.Lock()

    def touch(self):
        with self._lock:
            self.last_tick = time.monotonic()

    def age_seconds(self):
        with self._lock:
            return time.monotonic() - self.last_tick


app_clock = AppClock()

# The Kafka consumer runs in its own thread with its own event loop. Keep a
# separate progress clock for it so a dead/dead-locked Kafka thread is also
# detected (a dying Kafka loop must never hide behind a healthy audio path).
kafka_clock = AppClock()


class SessionMonitor:
    """Tracks the active microphone session's chunk queue.

    If the gRPC send path stops draining the queue (consumer thread wedged),
    the queue stays full with no drain progress - that is a hang, and the
    heartbeat must go stale so the external watchdog can restart the client.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.queue = None
        self.last_drain = time.monotonic()

    def set_queue(self, q):
        with self._lock:
            self.queue = q
            self.last_drain = time.monotonic()

    def clear(self):
        with self._lock:
            self.queue = None

    def touch_drain(self):
        with self._lock:
            self.last_drain = time.monotonic()

    def snapshot(self):
        with self._lock:
            return self.queue, self.last_drain


session_monitor = SessionMonitor()


class Heartbeat:
    """Monitors liveness of main and Kafka loops.

    If the application loops stop progressing for longer than
    ``stall_threshold`` seconds, a CRITICAL log is emitted so operators
    can detect the hang (e.g. via external monitoring).
    """

    def __init__(
        self,
        interval=HEARTBEAT_INTERVAL,
        stall_threshold=90.0,
    ):
        self.interval = interval
        self.stall_threshold = stall_threshold

    def start(self):
        app_clock.touch()

        def _loop():
            while True:
                time.sleep(self.interval)
                if shutdown_event.is_set():
                    break

                # --- liveness signals (ALL must be fresh) ---
                app_age = app_clock.age_seconds()
                kafka_age = kafka_clock.age_seconds()

                stale = None
                if app_age > self.stall_threshold:
                    stale = "main loop stalled %.0fs" % app_age
                elif kafka_age > self.stall_threshold:
                    stale = "kafka loop stalled %.0fs" % kafka_age

                # If the audio queue is full and not being drained, the gRPC
                # send path is wedged (consumer thread stuck).
                q, last_drain = session_monitor.snapshot()
                if q is not None and not q.empty() and stale is None:
                    drain_age = time.monotonic() - last_drain
                    if drain_age > self.stall_threshold:
                        stale = "audio queue full & undrained for %.0fs" % drain_age

                if stale:
                    logger.critical(
                        "Liveness check failed: %s",
                        stale,
                    )
                else:
                    logger.debug("Liveness check passed (all loops advancing)")

        thread = threading.Thread(target=_loop, name="heartbeat", daemon=True)
        thread.start()


# ============================================================
# STATE
# ============================================================

worker_lock = threading.Lock()

current_stop_event_lock = threading.Lock()
current_stop_event = None  # type: threading.Event | None

shutdown_event = threading.Event()

# Bounded: only the current session's label is needed (no unbounded history).
session_lock = threading.Lock()
current_session_label = None  # type: str | None

live_buffer_lock = threading.Lock()


# ============================================================
# STATUS WRITER (publishes client state to the GUI via shared memory)
# ============================================================

# Shared status state, written atomically every STATUS_INTERVAL seconds.
_status_lock = threading.Lock()
_status = {
    "mic_connected": False,
    "mic_device": "",
    "consultant_name": WORKER_NAME,
    "current_phrase": "",
    "phrase_is_final": False,
    "partial_phrase": "",
    "recognized_phrase": "",
    "grpc_connected": False,
    "kafka_connected": False,
    # Purchase notification (incremented so the GUI can spot a NEW purchase).
    "purchase_seq": 0,
    "purchase_text": "",
}

STATUS_INTERVAL = 0.15


class StatusWriter:
    """Publishes the client's live status so the GUI (gui.py) can read it.

    The status is pushed into a named shared-memory block (RAM - read by the
    GUI). Never raises: a failure here must not take the client down."""

    def __init__(self):
        self._shared = SharedStatusWriter()

    def update(self, **kw):
        with _status_lock:
            _status.update(kw)

    def publish_purchase(self, text):
        """Increment the purchase counter and set the purchase text, then
        publish immediately so the GUI shows the notification right away."""
        with _status_lock:
            _status["purchase_seq"] = int(_status.get("purchase_seq", 0)) + 1
            _status["purchase_text"] = text or ""
        self.flush()

    def flush(self):
        """Immediately publish the current status. Used for time-critical
        updates (e.g. partial recognition) so the GUI sees them without
        waiting for the periodic STATUS_INTERVAL tick."""
        data = self._snapshot()
        self._publish(data)

    def _publish(self, data):
        self._shared.write(data)

    def _snapshot(self):
        with _status_lock:
            return dict(_status)

    def start(self):
        self.update(consultant_name=WORKER_NAME, current_phrase="", phrase_is_final=False)

        def _loop():
            while True:
                time.sleep(STATUS_INTERVAL)
                if shutdown_event.is_set():
                    break
                self._publish(self._snapshot())

        thread = threading.Thread(target=_loop, name="status-writer", daemon=True)
        thread.start()


status_writer = StatusWriter()


# ============================================================
# SHUTDOWN
# ============================================================

def handle_shutdown(signum, frame):
    logger.info("Received stop signal %s", signum)
    shutdown_event.set()
    stop_current_session()


def set_current_stop_event(ev):
    global current_stop_event
    with current_stop_event_lock:
        current_stop_event = ev


def stop_current_session():
    with current_stop_event_lock:
        ev = current_stop_event
    if ev is not None:
        ev.set()


# ============================================================
# AUDIBLE / LIVE OUTPUT
# ============================================================

def print_live(text):
    """Render a live (replaced) console line without ever crashing."""
    with live_buffer_lock:
        console.write("\r\033[2K")
        console.write(text)
        console.flush()


def newline():
    console.write("\n")
    console.flush()


# ============================================================
# AUDIO DEVICE
# ============================================================

def _usb_media_devices():
    """Return [(name, pnp_id)] of present USB audio (MEDIA) devices.

    Queried through WMI (subprocess PowerShell) because sounddevice only
    exposes the audio endpoint name, not the stable USB VID/PID. Every match
    of this function is a best-effort; on any failure it returns [] so the
    caller falls back to the normal name/index resolution.
    """
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPDeviceID -match '^USB\\\\VID_' -and "
        "$_.PNPClass -eq 'MEDIA' } | "
        "ForEach-Object { \"{0}|{1}\" -f $_.Name, $_.PNPDeviceID }"
    )
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", script],
                capture_output=True, text=True, timeout=10,
            )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    result = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, pid = line.split("|", 1)
        result.append((name.strip(), pid.strip()))
    return result


def _audio_device_names_for_id(device_id):
    """Return the base device names that belong to the given USB hardware id.

    ``device_id`` (from AUDIO_DEVICE_ID) is matched as a substring of each USB
    MEDIA device's PnP id (e.g. "USB\\VID_046D&PID_081B"), returning the
    device name (e.g. "Logi C310 HD WebCam") used to find the matching audio
    endpoint later. Normalised comparison.
    """
    key = device_id.replace("\\", "\\").lower()
    names = []
    for name, pid in _usb_media_devices():
        if key in pid.lower():
            n = name.strip()
            if n and n not in names:
                names.append(n)
    return names


def _match_by_device_id(device_id):
    """Resolve an audio input index from a stable USB hardware id.

    Returns an int device index or None. The endpoint is located by name: the
    USB MEDIA device that matches ``device_id`` provides the base name (e.g.
    "Logi C310 HD WebCam"), which is then matched (substring) against the
    input devices visible to sounddevice. If the microphone is unplugged we
    return None and the caller keeps waiting for exactly this device.
    """
    base_names = _audio_device_names_for_id(device_id)
    if not base_names:
        return None
    inputs = list_input_devices()
    if not inputs:
        return None
    # Prefer an exact full-name match first, then a substring match.
    for base in base_names:
        bl = base.lower()
        for idx, dev in inputs:
            if dev.get("name", "").strip().lower() == bl:
                return idx
    for base in base_names:
        bl = base.lower()
        for idx, dev in inputs:
            try:
                if bl in dev.get("name", "").lower():
                    return idx
            except Exception:
                continue
    return None


def list_input_devices():
    """Return [(index, device_dict)] for devices that can be used as input.
    Never raises; returns [] if PortAudio/capture is unavailable."""
    try:
        devices = sd.query_devices()
    except Exception as exc:
        logger.warning("Could not query audio devices: %s", exc)
        return []

    result = []
    for index, device in enumerate(devices):
        try:
            if device["max_input_channels"] > 0:
                result.append((index, device))
        except Exception:
            continue
    return result


def log_available_input_devices():
    devices = list_input_devices()
    if not devices:
        logger.warning("No input audio devices are available right now")
        return
    logger.info("Available input audio devices:")
    for index, device in devices:
        try:
            default = sd.default.device[0] if isinstance(sd.default.device, (tuple, list)) else sd.default.device
        except Exception:
            default = None
        marker = " (default)" if default is not None and default == index else ""
        logger.info("  #%d: %s%s", index, device.get("name", "?"), marker)


def get_audio_device():
    """Resolve the configured audio device to an input index.

    Returns an int device index or None. Never raises.

    Resolution priority:
      1. ``AUDIO_DEVICE_ID`` (stable USB hardware id) if set - always picks
         exactly the intended physical microphone, even if Windows renames it;
      2. ``AUDIO_DEVICE``:
         * empty  -> Windows default input device (or None);
         * numeric -> exact index (if it is an input device);
         * name    -> exact match first, then substring match.

    IMPORTANT: never silently falls back to a *different* named microphone.
    If the configured device is missing it returns None and logs the list of
    devices currently visible to PortAudio.
    """
    if AUDIO_DEVICE_ID:
        idx = _match_by_device_id(AUDIO_DEVICE_ID)
        if idx is not None:
            return idx
        logger.warning(
            "Configured USB microphone id '%s' is not available right now "
            "(waiting for exactly that microphone)",
            AUDIO_DEVICE_ID,
        )
        return None  # keep waiting for THIS mic; never switch to another

    if not AUDIO_DEVICE:
        devices = list_input_devices()
        if not devices:
            logger.warning("Default input device is unavailable; no input devices visible")
            return None
        try:
            default = sd.default.device[0] if isinstance(sd.default.device, (tuple, list)) else sd.default.device
        except Exception:
            default = None
        if default is None or default < 0:
            if len(devices) == 1:
                default = devices[0][0]
            else:
                logger.warning("No Windows default input device set")
                return None
        if any(idx == default for idx, _ in devices):
            return default
        return None

    devices = list_input_devices()
    if not devices:
        logger.warning(
            "Configured microphone '%s' is not available (no input devices visible)",
            AUDIO_DEVICE,
        )
        log_available_input_devices()
        return None

    # Numeric exact index.
    try:
        requested = int(AUDIO_DEVICE)
        for idx, _ in devices:
            if idx == requested:
                return idx
        logger.warning(
            "Configured microphone index %d is not a visible input device",
            requested,
        )
        log_available_input_devices()
        return None
    except ValueError:
        pass

    # Name: exact first, then substring.
    requested = AUDIO_DEVICE.strip().lower()
    for idx, device in devices:
        if device.get("name", "").lower() == requested:
            return idx
    for idx, device in devices:
        if requested in device.get("name", "").lower():
            return idx

    logger.warning(
        "Configured microphone '%s' could not be matched to a visible input device",
        AUDIO_DEVICE,
    )
    log_available_input_devices()
    return None


def resolve_audio_device():
    """Convenience wrapper returning (index_or_None, is_configured)."""
    return get_audio_device()


def check_audio():
    """Startup diagnostic. Never fatal: a missing mic is recovered later."""
    logger.info("Checking audio devices...")
    devices = list_input_devices()
    if not devices:
        logger.warning("No input audio devices visible at startup")
        return False
    for idx, device in devices:
        logger.info("Input device #%d: %s", idx, device.get("name", "?"))
    selected = get_audio_device()
    if selected is None:
        logger.warning(
            "Configured microphone is currently unavailable; "
            "client will keep waiting for it"
        )
        return False
    try:
        name = sd.query_devices(selected).get("name", "?")
    except Exception:
        name = "?"
    logger.info("Selected audio device #%d: %s", selected, name)
    return True


# ============================================================
# AUDIO CALLBACK
# ============================================================

def make_audio_callback(audio_queue, audio_state):
    def callback(indata, frames, time_info, status):
        if status:
            logger.warning("Audio stream status=%s", status)
        audio_state["last_callback"] = time.monotonic()
        # Liveness proof #1: the microphone callback is still firing, so the
        # audio capture pipeline is alive. (If the *process* dead-locked the
        # heartbeat would go stale too; a wedged gRPC path is covered by log
        # + manual restart / Task Scheduler "End task".)
        app_clock.touch()
        try:
            audio_queue.put_nowait(indata.copy())
            session_monitor.touch_drain()
        except queue.Full:
            logger.warning("Audio queue full; dropping one chunk")
        except Exception:
            # A callback must never raise (PortAudio thread would die).
            logger.exception("Audio callback error")

    return callback


# ============================================================
# SESSION
# ============================================================

def make_session_id():
    with worker_lock:
        worker = WORKER_NAME
    return f"{STORE_ID}-{worker}"


def get_current_session_label():
    with session_lock:
        return current_session_label


def set_session_label(label):
    global current_session_label
    with session_lock:
        current_session_label = label


# ============================================================
# MICROPHONE STREAM
# ============================================================

def mic_stream(session_id, stop_event):
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)
    logger.info("Microphone session started: %s", session_id)

    first_chunk = True
    logged_missing = False

    while not stop_event.is_set() and not shutdown_event.is_set():
        device = get_audio_device()

        if device is None:
            if not logged_missing:
                logger.warning(
                    "Microphone is unavailable. Waiting for device to appear..."
                )
                status_writer.update(mic_connected=False, mic_device="")
                log_available_input_devices()
                logged_missing = True
            else:
                logger.debug("Microphone still unavailable; retrying...")
            # Waiting for the microphone is a *normal* state, not a hang:
            # keep the application clock advancing so the watchdog does not
            # restart a healthy client that is simply waiting for audio.
            app_clock.touch()
            stop_event.wait(AUDIO_RECONNECT_DELAY)
            continue

        logged_missing = False

        try:
            device_name = sd.query_devices(device).get("name", "?")
        except Exception:
            device_name = "?"
        status_writer.update(mic_connected=True, mic_device=device_name)
        logger.info("Opening microphone #%d: %s", device, device_name)
        logger.info("Using microphone device for session %s", device)

        audio_queue = queue.Queue(maxsize=50)
        audio_state = {"last_callback": time.monotonic()}
        callback = make_audio_callback(audio_queue, audio_state)
        session_monitor.set_queue(audio_queue)
        stream = None

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                device=device,
                callback=callback,
            )
            stream.start()
            logger.info("Microphone stream started")

            while not stop_event.is_set() and not shutdown_event.is_set():
                # --- watchdog: callbacks must keep arriving ---
                age = time.monotonic() - audio_state["last_callback"]
                if age > AUDIO_CALLBACK_TIMEOUT:
                    logger.error(
                        "Audio watchdog: no callback for %.1fs; assuming device lost",
                        age,
                    )
                    break

                try:
                    if not stream.active:
                        logger.warning("Audio stream is no longer active")
                        break
                except Exception as exc:
                    logger.warning("Audio stream check failed: %s", exc)
                    break

                try:
                    audio = audio_queue.get(timeout=0.2)
                except Empty:
                    continue
                except Exception as exc:
                    logger.warning("Failed reading audio chunk: %s", exc)
                    break

                yield bridge_pb2.MicChunk(
                    session_id=session_id,
                    audio=audio.tobytes(),
                    sample_rate=SAMPLE_RATE,
                    is_begin=first_chunk,
                    is_end=False,
                )
                first_chunk = False

        except Exception as exc:
            logger.error("Microphone stream error: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            session_monitor.clear()
            # Only report mic as disconnected if it actually failed,
            # not when gRPC stopped us (stop_event set by main loop).
            if not stop_event.is_set():
                status_writer.update(mic_connected=False, mic_device=device_name)
            logger.info("Microphone stream closed")

        if not stop_event.is_set() and not shutdown_event.is_set():
            logger.warning(
                "Microphone disconnected; retrying in %.1fs...",
                AUDIO_RECONNECT_DELAY,
            )
            stop_event.wait(AUDIO_RECONNECT_DELAY)

    logger.info("Microphone session stopped: %s", session_id)


# ============================================================
# EVENT HELPERS
# ============================================================

def event_store_id(event):
    if event.get("store_id") is not None:
        return str(event["store_id"])
    session_id = event.get("session_id", "")
    parts = session_id.split("-")
    if len(parts) >= 8:
        return parts[2]
    return parts[0] if parts else ""


def event_belongs_to_current_store(event):
    return event_store_id(event) == str(STORE_ID)


# ============================================================
# KAFKA (manual commit, at-least-once)
# ============================================================

def _handle_message(msg):
    """Process a single Kafka message.

    Returns True if the message was handled (even if it was a no-op, e.g.
    wrong store or bad JSON) so the offset may advance. Raises on unexpected
    errors so the caller can decide not to commit (at-least-once -> redeliver).
    """
    raw = msg.value
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    try:
        event = json.loads(raw)
    except Exception:
        logger.exception("Failed to parse Kafka message on %s: %r", msg.topic, raw)
        return True  # unparseable garbage: reprocessing won't help

    if not event_belongs_to_current_store(event):
        return True

    topic = msg.topic

    # ---- classification ----
    if topic == "classified_events":
        set_session_label(event.get("label"))
        matched = event.get("matched_words", {})
        parts = []
        if isinstance(matched, dict):
            for category, words in matched.items():
                if words:
                    parts.append(
                        f"{category}: "
                        + ", ".join(
                            f"{w}({c})"
                            for w, c in sorted(words.items(), key=lambda x: x[1], reverse=True)
                        )
                    )
        logger.info("CLASSIFICATION label=%s %s", event.get("label"), " | ".join(parts))
        return True

    # ---- new client session ----
    if topic == "new_client_session":
        set_session_label(None)
        logger.info("New client session started (label reset)")
        return True

    # ---- alerts / purchases / salesperson ----
    if topic in ("alerts", "purchases", "salesperson_changes"):
        if topic == "alerts":
            icon = "\U0001F6A8"
        elif topic == "purchases":
            icon = "\U0001F4B0"
            # Show a big purchase notification in the GUI window.
            status_writer.publish_purchase(event.get("text", ""))
        else:
            global WORKER_NAME
            with worker_lock:
                WORKER_NAME = event.get("new_salesperson", WORKER_NAME)
            status_writer.update(consultant_name=WORKER_NAME)
            icon = "\U0001F464"
            # Restart the microphone session with the new worker name.
            stop_current_session()
        logger.info("%s store=%s session=%s text=%s",
                    icon, event.get("store_id"), event.get("session_id", ""), event.get("text", ""))
        return True

    # ---- asr transcripts ----
    if topic == "asr_transcripts":
        label = get_current_session_label()
        icon = CLASS_ICONS.get(label) if label else None
        prefix = f"{icon} " if icon else ""
        text = event.get("text", "")
        is_final = bool(event.get("is_final"))
        status_writer.update(current_phrase=text, phrase_is_final=is_final)
        if is_final:
            # A finalised phrase becomes the "recognized" result; clear the
            # in-progress (partial) recognition for the next utterance.
            status_writer.update(partial_phrase="", recognized_phrase=text)
            status_writer.flush()
            print_live(f"{prefix}\U0001F3C1 {text}")
            newline()
        else:
            # Intermediate / partial recognition - shown live while speaking.
            status_writer.update(partial_phrase=text)
            status_writer.flush()
            print_live(f"{prefix}\U0001F5A8\ufe0f {text}")
        return True

    # Unknown topic: ignore but still allow the offset to advance.
    logger.debug("Ignoring message on unknown topic %s", topic)
    return True


class BatchProcessingError(Exception):
    """Raised when a message in a batch could not be processed. The caller
    must NOT commit the offset for that partition (redelivery expected)."""

    def __init__(self, message, tp=None, offset=None):
        super().__init__(message)
        self.tp = tp
        self.offset = offset


def process_message(msg):
    """Process one message. Returns True if handled (offset may advance).
    Raises BatchProcessingError if handling failed (offset must NOT advance).
    """
    try:
        handled = _handle_message(msg)
    except Exception as exc:
        raise BatchProcessingError(
            f"{exc} (topic={msg.topic} offset={msg.offset})"
        ) from exc
    if not handled:
        raise BatchProcessingError(f"not handled (topic={msg.topic} offset={msg.offset})")


async def poll_once(consumer):
    """Fetch one batch from the consumer, process every message and commit
    each partition's offset only if ALL of its messages were processed
    successfully.

    This implements at-least-once: a processing failure means no commit for
    that partition, so Kafka will redeliver the message later (duplicates are
    acceptable, message loss is not).
    """
    batches = await consumer.getmany(timeout_ms=500)
    app_clock.touch()
    kafka_clock.touch()

    for tp, messages in batches.items():
        if not messages:
            continue
        for msg in messages:
            process_message(msg)
        commit_to = messages[-1].offset + 1
        await consumer.commit({tp: commit_to})


async def kafka_listener():
    """Kafka consumer with reconnect, manual commit and at-least-once
    semantics. Survives Kafka/network outages: on any error it stops the
    consumer, waits with exponential backoff (bounded to 30s, so no busy
    loop), and starts a fresh consumer that continues from the last
    committed offset."""
    backoff = 1
    # Escalating delay for a persistently-failing (poison) message. We never
    # commit an offset whose message failed (at-least-once => no loss), but we
    # also must not busy-loop re-delivering it every 0.5s forever.
    poison_sleep = 0.5

    while not shutdown_event.is_set():
        # We are actively trying to (re)connect: the Kafka thread is alive.
        kafka_clock.touch()
        consumer = AIOKafkaConsumer(
            *KAFKA_TOPICS,
            bootstrap_servers=KAFKA_BOOTSTRAP,
            group_id=KAFKA_GROUP_ID,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            max_poll_records=300,
            session_timeout_ms=30000,
            heartbeat_interval_ms=10000,
            request_timeout_ms=30000,
            auto_commit_interval_ms=0,
        )

        try:
            await consumer.start()
            status_writer.update(kafka_connected=True)
            logger.info(
                "Kafka consumer started group=%s topics=%s",
                KAFKA_GROUP_ID, list(KAFKA_TOPICS),
            )

            poison_sleep = 0.5
            while not shutdown_event.is_set():
                try:
                    await poll_once(consumer)
                    poison_sleep = 0.5  # a batch was fully handled: reset
                except (UnknownMemberIdError, RebalanceInProgressError) as exc:
                    logger.warning(
                        "Kafka rebalance/reset (%s); positions will be "
                        "re-fetched from committed offsets, continuing",
                        exc,
                    )
                    await asyncio.sleep(0.2)
                    continue
                except BatchProcessingError as exc:
                    logger.error(
                        "Message processing failed: %s; offset NOT committed "
                        "(will be redelivered after %.1fs)",
                        exc,
                        poison_sleep,
                    )
                    await asyncio.sleep(poison_sleep)
                    poison_sleep = min(poison_sleep * 2, 30.0)
                    continue
                except CommitFailedError as exc:
                    logger.warning("Kafka commit failed: %s; redelivery expected", exc)
                    await asyncio.sleep(min(poison_sleep, 2.0))
                    poison_sleep = min(poison_sleep * 2, 30.0)
                    continue
                except KafkaError as exc:
                    logger.warning("Kafka connection/coordinator error: %s; reconnecting", exc)
                    break
                except Exception:
                    logger.exception("Unexpected Kafka listener error; reconnecting")
                    break

        except asyncio.CancelledError:
            logger.info("Kafka listener cancelled")
            raise
        except Exception as exc:
            logger.exception("Failed to start Kafka consumer: %s", exc)
        finally:
            try:
                await consumer.stop()
            except Exception:
                pass
            status_writer.update(kafka_connected=False)
            logger.info("Kafka consumer stopped")

        if not shutdown_event.is_set():
            kafka_clock.touch()
            logger.info(
                "Reconnecting Kafka in %ss... (committed offsets preserved)",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(int(backoff * 2), 30)


def start_kafka():
    """Run the Kafka listener in a dedicated thread. ``kafka_listener``
    already reconnects internally with backoff; the outer loop below is a
    defensive fallback in the unlikely event the listener coroutine returns
    without being asked to (e.g. a bug), so the client never silently loses
    its Kafka consumer."""
    while not shutdown_event.is_set():
        try:
            asyncio.run(kafka_listener())
        except Exception:
            logger.exception("Kafka listener exited unexpectedly")
        if not shutdown_event.is_set():
            logger.info("Restarting Kafka listener in 3s...")
            shutdown_event.wait(3)


# ============================================================
# MAIN
# ============================================================

async def main():
    kafka_thread = threading.Thread(
        target=start_kafka,
        name="kafka-manager",
        daemon=True,
    )
    kafka_thread.start()

    channel = grpc.insecure_channel(GRPC_ADDR)
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)
    loop = asyncio.get_running_loop()

    logger.info("Connecting to gRPC server %s", GRPC_ADDR)
    app_clock.touch()

    try:
        while not shutdown_event.is_set():
            app_clock.touch()
            session_id = make_session_id()
            stop_event = threading.Event()
            set_current_stop_event(stop_event)
            logger.info("Starting microphone session: %s", session_id)

            try:
                stream = await loop.run_in_executor(
                    None,
                    lambda: stub.StreamMic(
                        mic_stream(session_id, stop_event)
                    ),
                )
                status_writer.update(grpc_connected=True)

                for msg in stream:
                    if msg.is_begin:
                        logger.info("SPEECH START session=%s", msg.session_id)
                    if msg.is_end:
                        logger.info("SPEECH END session=%s", msg.session_id)

            except grpc.RpcError as exc:
                status_writer.update(grpc_connected=False)
                logger.error("gRPC error: %s", exc)
            except Exception:
                status_writer.update(grpc_connected=False)
                logger.exception("Unexpected client error")

            finally:
                stop_event.set()
                set_current_stop_event(None)

            if not shutdown_event.is_set():
                logger.info("Restarting microphone session...")
                await asyncio.sleep(1)
    finally:
        try:
            channel.close()
        except Exception:
            pass

    logger.info("Main loop stopped")


# ============================================================
# ENTRYPOINT
# ============================================================

def install_signal_handlers():
    if hasattr(signal, "SIGINT"):
        try:
            signal.signal(signal.SIGINT, handle_shutdown)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handle_shutdown)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handle_shutdown)
        except (ValueError, OSError):
            pass


def run():
    lock = SingleInstanceLock()
    if not lock.acquire():
        logger.error(
            "Another Audio Analytics client is already running. Exiting.",
        )
        return 0

    heartbeat = Heartbeat()
    heartbeat.start()

    status_writer.start()

    install_signal_handlers()

    logger.info("========================================")
    logger.info("Audio Analytics Client starting")
    logger.info("BASE_DIR=%s", BASE_DIR)
    logger.info("SERVER_IP=%s", SERVER_IP)
    logger.info("STORE_ID=%s", STORE_ID)
    logger.info("WORKER_NAME=%s", WORKER_NAME)
    logger.info("AUDIO_DEVICE=%s", AUDIO_DEVICE or "<default>")
    logger.info("AUDIO_DEVICE_ID=%s", AUDIO_DEVICE_ID or "<none>")
    logger.info("GRPC_ADDR=%s", GRPC_ADDR)
    logger.info("KAFKA_BOOTSTRAP=%s", KAFKA_BOOTSTRAP)
    logger.info("KAFKA_GROUP_ID=%s", KAFKA_GROUP_ID)
    logger.info("========================================")

    try:
        check_audio()
    except Exception:
        logger.exception("Audio self-check raised (non-fatal)")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    except Exception:
        logger.exception("Fatal error")
    finally:
        logger.info("Audio Analytics Client stopped")
        lock.release()

    return 0


if __name__ == "__main__":
    sys.exit(run())
