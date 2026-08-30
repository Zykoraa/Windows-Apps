"""Global media-key handling via RegisterHotKey.

The previous implementation installed a pynput keyboard Listener, which is a
system-wide low-level hook: it sees *every* keystroke you type in any
application, just to notice three media keys. That is far more access than
the feature needs, and such hooks routinely trip antivirus heuristics.

RegisterHotKey asks Windows for exactly the three virtual keys we care
about. Nothing else is ever delivered to us. It needs its own thread with a
message pump, because hotkey messages arrive on the thread that registered
them.
"""

import ctypes
import ctypes.wintypes as wintypes
import threading

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000

_ACTIONS = {
    1: ("play_pause", VK_MEDIA_PLAY_PAUSE),
    2: ("next", VK_MEDIA_NEXT_TRACK),
    3: ("prev", VK_MEDIA_PREV_TRACK),
    4: ("stop", VK_MEDIA_STOP),
}


class MediaKeys:
    """Dispatches "play_pause" / "next" / "prev" / "stop" to a callback.

    The callback runs on the hotkey thread, so hand back to the UI thread
    yourself. Falls back to doing nothing on non-Windows or if registration
    fails (another app may already own a key) -- never fatal.
    """

    def __init__(self, on_action):
        self.on_action = on_action
        self._thread = None
        self._thread_id = None
        self._registered = []
        self._ready = threading.Event()

    @property
    def active(self):
        return bool(self._registered)

    def start(self):
        try:
            ctypes.windll  # noqa: B018  -- Windows-only guard
        except AttributeError:
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.active

    def stop(self):
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, WM_QUIT, 0, 0
                )
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None

    def _pump(self):
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        for hotkey_id, (_, vk) in _ACTIONS.items():
            if user32.RegisterHotKey(None, hotkey_id, MOD_NOREPEAT, vk):
                self._registered.append(hotkey_id)
        self._ready.set()

        if not self._registered:
            return

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):  # WM_QUIT, or an error
                    break
                if msg.message == WM_HOTKEY:
                    action = _ACTIONS.get(msg.wParam)
                    if action:
                        try:
                            self.on_action(action[0])
                        except Exception:
                            pass
        finally:
            for hotkey_id in self._registered:
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
            self._registered = []
