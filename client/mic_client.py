"""
Mic client with separated capture and send threads for reliable USB unplug/replug recovery.

Architecture:
- capture_loop(): Independent thread that owns PortAudio stream lifecycle
  - Opens stream, monitors health, handles recovery
  - Puts audio into shared queue (non-blocking)
  - Completely independent of gRPC backpressure
  
- send_generator(): Generator that sends audio to gRPC
  - Reads from shared queue
  - Yields to gRPC (may block on backpressure)
  - Does NOT control stream lifecycle

This separation ensures capture monitoring continues even if send is blocked.
"""
import sys
import queue
import threading
import time
import gc
from queue import Empty

import grpc
import sounddevice as sd
import numpy

import bridge_pb2
import bridge_pb2_grpc

import logging
import asyncio
import json

from aiokafka import AIOKafkaConsumer

import signal
import os

# Load AUDIO_DEVICE_ID from .env if exists
AUDIO_DEVICE_ID = os.getenv("AUDIO_DEVICE_ID", "").strip() or "Wireless Microphone RX"
AUDIO_RECONNECT_DELAY = 2.0
AUDIO_CALLBACK_TIMEOUT = 3.0
EXTENDED_SILECE_THRESHOLD = 5.0  # seconds of continuous silence before recovery

# SERVER_IP = "10.201.0.9"
# SERVER_IP = "192.168.0.10"
SERVER_IP = "localhost"

KAFKA_BOOTSTRAP = f"{SERVER_IP}:19092"
GRPC_ADDR = f"{SERVER_IP}:6000"

CLASS_ICONS = {
    "buy": "🛒",
    "service": "🔧",
    "complaint": "⚠️",
    "corporate": "🏢",
    "working_hours": "🕒",
    "vacancy": "💼",
    "help": "❓",
    "lost": "🧭",
    "other": "💬",
}


# ---------------- LOGGING ----------------
logger = logging.getLogger(__name__)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.flush = sys.stdout.flush  # Force flush on every log
    logger_instance = logging.getLogger()
    logger_instance.addHandler(handler)
    logger_instance.setLevel(logging.INFO)


configure_logging()


# ---------------- CONFIG ----------------
SAMPLE_RATE = 16000
CHUNK_MS = 150
STORE_ID = "main_store"
WORKER_NAME = "иванов_иван"

worker_lock = threading.Lock()
current_stop_event_lock = threading.Lock()
current_stop_event = None
shutdown_event = threading.Event()


audio_queue = queue.Queue()

# Capture thread state
capture_state = {
    "running": False,
    "stream": None,
    "last_callback": time.time(),
    "silent_frames": 0,
    "extended_silence_start": None,
    "buffers_sent": 0,
    "buffers_dropped": 0,
}
capture_state_lock = threading.Lock()


def handle_shutdown(signum, frame):
    logger.info("Received signal %s", signum)
    shutdown_event.set()
    stop_capture_loop()
    stop_current_session()


# ---------------- DEVICE MANAGEMENT ----------------

def list_input_devices():
    """Return list of input devices with their indices.
    
    Resets PortAudio to ensure fresh device list after hotplug.
    """
    try:
        # Reset PortAudio to refresh device list
        sd._terminate()
        sd._initialize()
        
        devices = sd.query_devices()
        return [(idx, dev) for idx, dev in enumerate(devices) if dev.get("max_input_channels", 0) > 0]
    except Exception as e:
        logger.error("Failed to query devices: %s", e)
        return []


def find_audio_device():
    """Find device by AUDIO_DEVICE_ID (name match). Returns (index, device_info) or (None, None)."""
    devices = list_input_devices()
    target = AUDIO_DEVICE_ID.lower()
    
    for idx, dev in devices:
        name = dev.get("name", "").lower()
        if target in name or name in target:
            logger.info("FOUND device: #%d %s", idx, dev.get("name"))
            return idx, dev
    
    logger.error("Device '%s' NOT FOUND. Available: %s", 
                 AUDIO_DEVICE_ID, [d.get("name") for _, d in devices])
    return None, None


def close_stream(stream):
    """Properly close a PortAudio stream using abort() + close()."""
    if stream is None:
        return
    
    try:
        stream.abort()
        logger.info("Stream aborted")
    except Exception as e:
        logger.error("Stream abort failed: %s", e)
    
    try:
        stream.close()
        logger.info("Stream closed")
    except Exception as e:
        logger.error("Stream close failed: %s", e)


# ---------------- CAPTURE THREAD ----------------

def audio_callback(indata, frames, time_info, status):
    """Callback called by PortAudio for each audio block."""
    global capture_state
    
    if status:
        logger.warning("Audio status=%s", status)
    
    with capture_state_lock:
        capture_state["last_callback"] = time.time()
        
        # Track silent frames
        is_silent = numpy.all(indata == 0)
        if is_silent:
            capture_state["silent_frames"] += 1
            if capture_state["silent_frames"] >= 10 and capture_state["extended_silence_start"] is None:
                capture_state["extended_silence_start"] = time.time()
                logger.info("Extended silence started (silent_frames=%d)", capture_state["silent_frames"])
        else:
            if capture_state["silent_frames"] > 0:
                logger.info("Silence ended after %d frames", capture_state["silent_frames"])
            capture_state["silent_frames"] = 0
            capture_state["extended_silence_start"] = None
    
    # Put audio in queue (non-blocking, drop if full)
    try:
        audio_queue.put_nowait(indata.copy())
        with capture_state_lock:
            capture_state["buffers_sent"] += 1
    except queue.Full:
        with capture_state_lock:
            capture_state["buffers_dropped"] += 1
        if capture_state["buffers_dropped"] <= 10:  # Log first 10 drops
            logger.warning("Audio queue full, dropping frame (total dropped: %d)", 
                         capture_state["buffers_dropped"])


def capture_loop():
    """
    Independent capture loop with recovery.
    
    This thread owns the PortAudio stream lifecycle and is completely
    independent of gRPC backpressure. It monitors stream health and
    recovers from failures automatically.
    """
    global capture_state
    
    blocksize = int(SAMPLE_RATE * CHUNK_MS / 1000)
    retry_count = 0
    max_retries = 5
    
    logger.info("Capture loop started, looking for device: %s", AUDIO_DEVICE_ID)
    
    with capture_state_lock:
        capture_state["running"] = True
    
    try:
        while not shutdown_event.is_set():
            # Find device (re-query each time, index may change)
            device, dev_info = find_audio_device()
            
            if device is None:
                logger.warning("Device %s not available, waiting %.1fs...", 
                             AUDIO_DEVICE_ID, AUDIO_RECONNECT_DELAY)
                time.sleep(AUDIO_RECONNECT_DELAY)
                continue
            
            # Verify device parameters
            if dev_info.get("max_input_channels", 0) == 0:
                logger.warning("Device #%d has no input channels, skipping...", device)
                time.sleep(AUDIO_RECONNECT_DELAY)
                continue
            
            stream = None
            
            try:
                logger.info("Opening stream on device #%d: %s", device, dev_info.get("name", "?"))
                
                # Check settings compatibility
                try:
                    sd.check_input_settings(device=device, samplerate=SAMPLE_RATE, 
                                          channels=1, dtype="int16")
                except Exception as e:
                    logger.warning("Settings check failed: %s, trying anyway...", e)
                
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=blocksize,
                    device=device,
                    callback=audio_callback,
                )
                stream.start()
                logger.info("Stream started on device #%d", device)
                
                # Store stream reference for monitoring
                with capture_state_lock:
                    capture_state["stream"] = stream
                
                # Reset retry count on successful start
                retry_count = 0
                
                # Wait for first buffer to verify stream works
                logger.info("Waiting for first audio buffer...")
                first_buffer_received = False
                start_wait = time.time()
                while time.time() - start_wait < 3.0:
                    if not audio_queue.empty():
                        first_buffer_received = True
                        logger.info("First audio buffer received")
                        break
                    time.sleep(0.1)
                
                if not first_buffer_received:
                    logger.warning("No audio received in 3s, stream may not be working")
                
                # Reset silence tracking
                with capture_state_lock:
                    capture_state["last_callback"] = time.time()
                    capture_state["silent_frames"] = 0
                    capture_state["extended_silence_start"] = None
                
                # Main monitoring loop
                monitor_counter = 0
                while not shutdown_event.is_set():
                    time.sleep(0.1)  # Small sleep to avoid tight loop
                    monitor_counter += 1
                    
                    with capture_state_lock:
                        stream_ref = capture_state.get("stream")
                        last_callback = capture_state["last_callback"]
                        silent_frames = capture_state["silent_frames"]
                        extended_silence_start = capture_state["extended_silence_start"]
                    
                    # Check if stream is still the one we expect
                    if stream_ref != stream:
                        logger.info("Stream changed, exiting monitoring loop")
                        break
                    
                    # Log monitoring status every 50 iterations (~5 seconds)
                    if monitor_counter % 50 == 0:
                        callback_age = time.time() - last_callback
                        logger.info("Monitoring: callback_age=%.1fs, silent_frames=%d, stream.active=%s",
                                   callback_age, silent_frames, stream.active)
                    
                    # Check callback timeout
                    callback_age = time.time() - last_callback
                    if callback_age > AUDIO_CALLBACK_TIMEOUT:
                        logger.warning("Callback timeout (%.1fs), reconnecting...", callback_age)
                        break
                    
                    # Check if stream is still active
                    if not stream.active:
                        logger.warning("Stream became inactive, reconnecting...")
                        break
                    
                    # Check for extended silence (device disconnected but callback fires with zeros)
                    if silent_frames > 0 and extended_silence_start is not None:
                        silence_duration = time.time() - extended_silence_start
                        logger.info("Extended silence check: duration=%.1fs, threshold=%.1fs, silent_frames=%d",
                                   silence_duration, EXTENDED_SILECE_THRESHOLD, silent_frames)
                        if silence_duration > EXTENDED_SILECE_THRESHOLD:
                            logger.warning("Extended silence for %.1fs (%d frames), "
                                         "device likely disconnected, reconnecting...",
                                         silence_duration, silent_frames)
                            break
                
            except Exception as e:
                error_msg = str(e)
                is_portaudio_error = ("-9986" in error_msg or 
                                     "Device disconnected" in error_msg or 
                                     "paInternalError" in error_msg)
                
                if is_portaudio_error:
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error("PortAudio error after %d attempts, giving up", retry_count)
                        break
                    
                    wait_time = min(20, 2 ** retry_count)
                    logger.warning("PortAudio error (%s), attempt %d/%d, waiting %ds...",
                                 error_msg, retry_count, max_retries, wait_time)
                    
                    gc.collect()
                    time.sleep(wait_time)
                else:
                    logger.error("Stream error: %s, reconnecting...", e)
            
            finally:
                close_stream(stream)
                stream = None
                with capture_state_lock:
                    capture_state["stream"] = None
            
            if not shutdown_event.is_set():
                logger.info("Stream closed, waiting %.1fs before reconnect...", AUDIO_RECONNECT_DELAY)
                time.sleep(AUDIO_RECONNECT_DELAY)
                # Clear queue
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except Empty:
                        break
    
    finally:
        with capture_state_lock:
            capture_state["running"] = False
        logger.info("Capture loop stopped")


def start_capture_loop():
    """Start the capture loop in a background thread."""
    if capture_state.get("running"):
        logger.info("Capture loop already running")
        return
    
    thread = threading.Thread(target=capture_loop, daemon=True, name="capture_loop")
    thread.start()
    logger.info("Capture loop thread started")


def stop_capture_loop():
    """Stop the capture loop."""
    if not capture_state.get("running"):
        return
    
    logger.info("Stopping capture loop...")
    shutdown_event.set()
    time.sleep(1)  # Give it time to stop cleanly


# ---------------- SEND GENERATOR ----------------

def send_generator(session_id: str, stop_event: threading.Event):
    """
    Generator that sends audio to gRPC.
    
    This generator reads from the shared queue and yields to gRPC.
    It may block on backpressure but does NOT control stream lifecycle.
    The capture thread handles all monitoring and recovery.
    """
    logger.info("Send generator started for session_id=%s", session_id)
    
    first_chunk = True
    
    try:
        while not stop_event.is_set() and not shutdown_event.is_set():
            try:
                audio = audio_queue.get(timeout=0.5)
                
                # Log around yield for debugging
                # logger.debug("Yielding chunk %d bytes", len(audio.tobytes()))
                
                yield bridge_pb2.MicChunk(
                    session_id=session_id,
                    audio=audio.tobytes(),
                    sample_rate=SAMPLE_RATE,
                    is_begin=first_chunk,
                    is_end=False,
                )
                
                first_chunk = False
                
                # logger.debug("Yield completed")
                
            except Empty:
                continue
    
    except Exception as e:
        logger.error("Send generator error: %s", e)
    
    finally:
        logger.info("Send generator stopped for session_id=%s", session_id)


# ---------------- KAFKA AND MAIN ----------------

def make_session_id() -> str:
    with worker_lock:
        worker = WORKER_NAME
    return f"{STORE_ID}-{worker}"


def event_store_id(event: dict) -> str:
    if event.get("store_id") is not None:
        return str(event["store_id"])

    session_id = event.get("session_id", "")
    parts = session_id.split("-")

    if len(parts) >= 8:
        return parts[2]

    return parts[0] if parts else ""


def event_belongs_to_current_store(event: dict) -> bool:
    return event_store_id(event) == str(STORE_ID)


def set_current_stop_event(ev):
    global current_stop_event
    with current_stop_event_lock:
        current_stop_event = ev


def stop_current_session():
    with current_stop_event_lock:
        ev = current_stop_event
    if ev is not None:
        ev.set()


def log_event(message: str, session_id: str):
    sys.stdout.write("\n")
    sys.stdout.flush()
    logger.info("%s %s", message, session_id)


def print_live(text: str):
    sys.stdout.write("\r\033[2K")
    sys.stdout.write(text)
    sys.stdout.flush()


client_sessions = []


async def kafka_listener():
    consumer = AIOKafkaConsumer(
        "asr_transcripts",
        "classified_events",
        "new_client_session",
        "alerts",
        "purchases",
        "salesperson_changes",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="mic-client",
        auto_offset_reset="latest",
    )

    await consumer.start()
    logger.info("Kafka consumer started")

    try:
        while not shutdown_event.is_set():
            batches = await consumer.getmany(timeout_ms=50)

            for _, messages in batches.items():
                for msg in messages:
                    raw = msg.value
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")

                    event = json.loads(raw)

                    if not event_belongs_to_current_store(event):
                        continue

                    if msg.topic == "classified_events":
                        if client_sessions:
                            client_sessions[-1] = event["label"]

                        matched = event.get("matched_words", {})
                        parts = []
                        for category, words in matched.items():
                            if words:
                                parts.append(
                                    f"{category}: "
                                    + ", ".join(
                                        f"{w}({c})"
                                        for w, c in sorted(
                                            words.items(),
                                            key=lambda x: x[1],
                                            reverse=True,
                                        )
                                    )
                                )
                        print(
                            "%s | %s",
                            event["label"],
                            " | ".join(parts),
                        )
                        continue

                    if msg.topic == "new_client_session":
                        print()
                        client_sessions.append(None)
                        print(
                            "👤 Client changed -> ",
                            len(client_sessions),
                        )
                        continue

                    if msg.topic in ("alerts", "purchases", "salesperson_changes"):
                        print()
                        if msg.topic == "alerts":
                            icon = "🚨"
                        elif msg.topic == "purchases":
                            icon = "💰"
                        else:
                            icon = "👷"

                        print("%s %s", icon, event.get("message", ""))
                        continue

                    if msg.topic == "asr_transcripts":
                        text = event.get("text", "")
                        print_live(f"🖨️ {text}")
                        continue

    finally:
        logger.info("Stopping Kafka consumer...")
        await consumer.stop()
        logger.info("Kafka consumer stopped")


def start_kafka():
    asyncio.run(kafka_listener())


async def main():
    # Start capture loop (independent of gRPC)
    start_capture_loop()
    
    # Give capture loop time to start
    time.sleep(1)
    
    # Start Kafka
    threading.Thread(target=start_kafka, daemon=True).start()

    channel = grpc.insecure_channel(GRPC_ADDR)
    stub = bridge_pb2_grpc.AudioBridgeStub(channel)

    loop = asyncio.get_running_loop()

    while not shutdown_event.is_set():
        session_id = make_session_id()
        stop_event = threading.Event()
        set_current_stop_event(stop_event)

        try:
            stream = await loop.run_in_executor(
                None,
                lambda: stub.StreamMic(send_generator(session_id, stop_event))
            )

            for msg in stream:
                if msg.is_begin:
                    print("🟢 SPEECH START", msg.session_id)

                if msg.is_end:
                    print()
                    print("🔴 SPEECH END  ", msg.session_id)

        except grpc.RpcError as e:
            logger.error("gRPC error: %s", e)

        finally:
            stop_event.set()
            set_current_stop_event(None)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    asyncio.run(main())
