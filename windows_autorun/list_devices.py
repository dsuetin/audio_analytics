"""List microphone / input devices visible to this install, together with
their stable USB hardware id (VID/PID) so the client can be pinned to exactly
one physical microphone regardless of Windows renaming it.

Usage:
    .venv\Scripts\python.exe list_devices.py

The hardware id shown here is what you put into .env as ``AUDIO_DEVICE_ID``.
The client will then always use that microphone and, if it is unavailable,
will WAIT for it instead of switching to another one.
"""
import sys
import os
import subprocess


def _usb_media_devices():
    """Return {name_lower: (name, pnp_id)} of present USB audio (MEDIA) devices."""
    if os.name != "nt":
        return {}
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
        return {}
    result = {}
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, pid = line.split("|", 1)
        result[name.strip().lower()] = (name.strip(), pid.strip())
    return result


def main():
    try:
        import sounddevice as sd
    except Exception as exc:
        print("ERROR: sounddevice could not be imported: %s" % (exc,))
        return 1

    try:
        devices = sd.query_devices()
    except Exception as exc:
        print("ERROR: could not query audio devices: %s" % (exc,))
        return 1

    default_in = None
    try:
        d = sd.default.device
        default_in = d[0] if isinstance(d, (tuple, list)) else d
    except Exception:
        default_in = None

    usb_by_name = _usb_media_devices()
    inputs = [(idx, dev) for idx, dev in enumerate(devices)
              if dev.get("max_input_channels", 0) > 0]

    if not inputs:
        print("No input (microphone) devices found.")
        print("Hint: plug in / enable the microphone, then re-run this script.")
        return 0

    print("Available input (microphone) devices:")
    for idx, dev in inputs:
        name = dev.get("name", "?")
        marker = "   <-- default" if idx == default_in else ""
        print("  #%d: %s%s" % (idx, name, marker))

        # try to attach the corresponding USB hardware id
        for k in (name.lower(),):
            if k in usb_by_name:
                uname, pid = usb_by_name[k]
                print("        hardware id: %s" % pid)
                break
        else:
            # fall back to a substring search on the device name
            hits = [(n, p) for nl, (n, p) in usb_by_name.items() if nl in name.lower()]
            for uname, pid in hits:
                print("        hardware id: %s" % pid)

    print()
    print("To use a specific EXTERNAL microphone no matter how Windows renames it,")
    print("copy its 'hardware id' above into .env, for example:")
    print("    AUDIO_DEVICE_ID=USB\\VID_046D&PID_081B&MI_02")
    print("The client will then use exactly that microphone and wait for it if")
    print("it is unplugged (it will NOT switch to another microphone).")
    print()
    print("Alternative (by name / index), if you prefer:")
    print("    AUDIO_DEVICE=<name>    (exact or partial device name)")
    print("    AUDIO_DEVICE=<index>   (one of the numbers above)")
    print("Leave both empty to use the Windows default device.")
    return 0


if __name__ == "__main__":
    rc = main()
    input("Press Enter to close...")
    sys.exit(rc)
