"""Screen sharing over the relay.

Capture goes through the XDG desktop portal (the same monitor/window picker
every Wayland app uses — on Omarchy, xdg-desktop-portal-hyprland's), and
GStreamer pulls frames out of the PipeWire stream the portal hands back.
Frames travel as JPEG stills, encrypted like everything else, so there's no
video codec to negotiate and nothing beyond gst-plugins-base + gdk-pixbuf
to install.

On the wire, under the network's topic namespace:
  screen/<device_id>        retained share state while sharing, empty when not
  screen-watch/<device_id>  viewers' heartbeats to the sharer (QoS 0)
  screen-frame/<share_id>   JPEG frames split into chunk_size pieces (QoS 0)

Frames only go out while at least one viewer's heartbeat is fresh, so an
unwatched share costs nothing beyond its retained state message.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gst  # noqa: E402

Gst.init(None)

WATCH_HEARTBEAT_SECONDS = 3
WATCH_EXPIRE_SECONDS = 10

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST = "org.freedesktop.portal.ScreenCast"
_SOURCE_MONITOR = 1
_SOURCE_WINDOW = 2
_CURSOR_EMBEDDED = 2
_RESPONSE_CANCELLED = 1


class ScreenShareError(RuntimeError):
    pass


def portal_source(fd: int, node_id: int) -> str:
    # keepalive-time repeats the last frame every second while nothing on
    # screen changes (PipeWire only sends frames on damage), so a viewer who
    # opens a share of a static screen still gets a picture promptly.
    return f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 always-copy=true"


class PortalSession:
    """One ScreenCast portal session: CreateSession, SelectSources, Start
    (where the user picks a monitor or window), then OpenPipeWireRemote.
    Each step answers later through a Request.Response signal, so this runs
    on the GLib main loop and reports back through callbacks."""

    def __init__(self, on_ready: Callable[[int, int], None], on_error: Callable[[str], None]) -> None:
        self._on_ready = on_ready  # (PipeWire remote fd, stream node id)
        self._on_error = on_error
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self._session_handle: Optional[str] = None
        self._subscriptions: list[int] = []
        self._closed = False

    def start(self) -> None:
        token = self._token()
        options = {
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", self._token()),
        }
        self._request("CreateSession", GLib.Variant("(a{sv})", (options,)), token, self._on_session_created)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscription in self._subscriptions:
            self._bus.signal_unsubscribe(subscription)
        self._subscriptions.clear()
        if self._session_handle:
            try:
                self._bus.call_sync(
                    _PORTAL_BUS,
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except GLib.Error:
                pass

    def _token(self) -> str:
        return f"omarchy_relay_{uuid.uuid4().hex[:12]}"

    def _request(self, method: str, params: GLib.Variant, token: str, on_response: Callable[[dict], None]) -> None:
        # Subscribed before the call, not after: the portal may answer before
        # call_sync even returns, and a signal nobody listened for is lost.
        path = f"{_PORTAL_PATH}/request/{self._sender}/{token}"
        subscription = 0

        def on_signal(_conn, _sender, _path, _iface, _signal, parameters, *_user_data) -> None:
            self._bus.signal_unsubscribe(subscription)
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)
            if self._closed:
                return
            response, results = parameters.unpack()
            if response == _RESPONSE_CANCELLED:
                self._fail("Screen sharing was cancelled")
            elif response != 0:
                self._fail("The screen sharing portal refused the request")
            else:
                on_response(results)

        subscription = self._bus.signal_subscribe(
            _PORTAL_BUS,
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_signal,
        )
        self._subscriptions.append(subscription)
        try:
            self._bus.call_sync(_PORTAL_BUS, _PORTAL_PATH, _SCREENCAST, method, params, None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            self._fail(f"Couldn't reach the screen sharing portal: {exc.message}")

    def _cursor_modes(self) -> int:
        try:
            reply = self._bus.call_sync(
                _PORTAL_BUS,
                _PORTAL_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (_SCREENCAST, "AvailableCursorModes")),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error:
            return 0
        return reply.unpack()[0]

    def _on_session_created(self, results: dict) -> None:
        self._session_handle = results["session_handle"]
        token = self._token()
        options = {
            "handle_token": GLib.Variant("s", token),
            "types": GLib.Variant("u", _SOURCE_MONITOR | _SOURCE_WINDOW),
            "multiple": GLib.Variant("b", False),
        }
        if self._cursor_modes() & _CURSOR_EMBEDDED:
            options["cursor_mode"] = GLib.Variant("u", _CURSOR_EMBEDDED)
        self._request(
            "SelectSources", GLib.Variant("(oa{sv})", (self._session_handle, options)), token, self._on_sources_selected
        )

    def _on_sources_selected(self, _results: dict) -> None:
        token = self._token()
        options = {"handle_token": GLib.Variant("s", token)}
        self._request("Start", GLib.Variant("(osa{sv})", (self._session_handle, "", options)), token, self._on_started)

    def _on_started(self, results: dict) -> None:
        streams = results.get("streams") or []
        if not streams:
            self._fail("No screen or window was picked")
            return
        node_id = streams[0][0]
        try:
            reply, fd_list = self._bus.call_with_unix_fd_list_sync(
                _PORTAL_BUS,
                _PORTAL_PATH,
                _SCREENCAST,
                "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self._session_handle, {})),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
        except GLib.Error as exc:
            self._fail(f"Couldn't open the screen stream: {exc.message}")
            return
        self._on_ready(fd_list.get(reply.unpack()[0]), node_id)

    def _fail(self, message: str) -> None:
        self.close()
        self._on_error(message)


class FrameCapture:
    """Pulls frames from a GStreamer source (the portal's PipeWire stream, or
    a test pattern), scales them to fit max_width, and JPEG-encodes at most
    `fps` a second on its own thread. Frames arriving while `wanted()` says
    nobody is watching are dropped before any encoding work happens."""

    def __init__(
        self,
        source: str,
        *,
        fps: int,
        max_width: int,
        quality: int,
        wanted: Callable[[], bool],
        on_frame: Callable[[bytes, int, int], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._quality = max(10, min(95, quality))
        self._wanted = wanted
        self._on_frame = on_frame
        self._on_error = on_error
        # The bare video/x-raw caps right after the source keep frames in
        # system memory: PipeWire would otherwise happily offer DMA-BUFs,
        # which videoconvert can't read. Spelled as a capsfilter element
        # because two caps strings in a row (a source ending in its own
        # caps) don't parse.
        description = (
            f"{source} ! capsfilter caps=video/x-raw ! videorate drop-only=true max-rate={max(1, fps)} ! "
            f"videoconvert ! videoscale ! "
            f"video/x-raw,format=RGB,pixel-aspect-ratio=1/1,width=[16,{max_width}],height=[16,{max_width}] ! "
            f"appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )
        try:
            self._pipeline = Gst.parse_launch(description)
        except GLib.Error as exc:
            raise ScreenShareError(f"couldn't set up screen capture: {exc.message}") from exc
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: Optional[tuple[bytes, int, int]] = None
        self._stopped = False
        self._encoder = threading.Thread(target=self._encode_loop, daemon=True)

    def start(self) -> None:
        self._encoder.start()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise ScreenShareError("couldn't start screen capture")

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._wake.set()
        self._pipeline.get_bus().remove_signal_watch()
        self._pipeline.set_state(Gst.State.NULL)

    def _on_bus_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            self._on_error(err.message)
        elif message.type == Gst.MessageType.EOS:
            self._on_error("the shared screen or window went away")

    def _on_sample(self, sink) -> Gst.FlowReturn:
        # Runs on GStreamer's streaming thread: copy the pixels out and hand
        # them to the encoder, replacing any frame it hasn't picked up yet.
        sample = sink.emit("pull-sample")
        if sample is None or not self._wanted():
            return Gst.FlowReturn.OK
        structure = sample.get_caps().get_structure(0)
        width, height = structure.get_value("width"), structure.get_value("height")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)
        with self._lock:
            self._pending = (data, width, height)
            self._wake.set()
        return Gst.FlowReturn.OK

    def _encode_loop(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if self._stopped:
                    return
                pending, self._pending = self._pending, None
                self._wake.clear()
            if pending is None:
                continue
            data, width, height = pending
            # GStreamer pads each RGB row out to a multiple of 4 bytes.
            stride = (width * 3 + 3) & ~3
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                    GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, False, 8, width, height, stride
                )
                ok, jpeg = pixbuf.save_to_bufferv("jpeg", ["quality"], [str(self._quality)])
            except GLib.Error:
                continue
            if ok:
                self._on_frame(bytes(jpeg), width, height)


class ScreenSharer:
    """Everything on the sharing side: the portal session, capture, the
    retained share state, and which viewers are currently watching.

    `on_started()`, `on_watchers_changed(nicks)` and `on_ended(reason)` are
    called on the GLib main loop. Pass `source` to capture a GStreamer
    source directly instead of asking the portal (used for testing)."""

    def __init__(
        self,
        client,
        cfg,
        *,
        on_watchers_changed: Callable[[list[str]], None],
        on_ended: Callable[[str], None],
        on_started: Optional[Callable[[], None]] = None,
        source: Optional[str] = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.share_id = uuid.uuid4().hex[:12]
        self._on_started = on_started
        self._on_watchers_changed = on_watchers_changed
        self._on_ended = on_ended
        self._source = source
        self._portal: Optional[PortalSession] = None
        self._capture: Optional[FrameCapture] = None
        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._watchers: dict[str, tuple[str, float]] = {}  # device_id -> (nick, monotonic last heartbeat)
        self._last_frame: Optional[tuple[bytes, int, int]] = None
        self._inflight: list = []  # MQTTMessageInfo for the last frame's chunks
        self._seq = 0
        self._timer_id: Optional[int] = None
        self._active = False
        self._ended = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._source is not None:
            self._begin(self._source)
            return
        self._portal = PortalSession(on_ready=self._on_portal_ready, on_error=self._end)
        self._portal.start()

    def stop(self, reason: str = "") -> None:
        if self._ended:
            return
        self._ended = True
        was_active = self._active
        self._active = False
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._portal is not None:
            self._portal.close()
            self._portal = None
        if self._fd is not None:
            os.close(self._fd)  # pipewiresrc works on its own duplicate
            self._fd = None
        if was_active:
            self.client.set_screen_state(None)
        self._on_ended(reason)

    def watcher_nicks(self) -> list[str]:
        with self._lock:
            return sorted(nick for nick, _seen in self._watchers.values())

    def handle_watch(self, obj: dict) -> None:
        """A viewer's heartbeat, straight from paho's network thread."""
        device_id = obj.get("from")
        if not self._active or obj.get("share_id") != self.share_id or not device_id:
            return
        watching = obj.get("state") == "watching"
        with self._lock:
            known = device_id in self._watchers
            if watching:
                self._watchers[device_id] = (obj.get("nick", "?"), time.monotonic())
            elif known:
                del self._watchers[device_id]
            last_frame = self._last_frame
        if known == watching:
            return  # a routine heartbeat, or a stop from someone who wasn't watching
        if watching and last_frame is not None:
            # Don't make a new viewer wait for the next change on screen.
            self._send_frame(*last_frame, force=True)
        GLib.idle_add(self._notify_watchers)

    def _on_portal_ready(self, fd: int, node_id: int) -> None:
        self._fd = fd
        self._begin(portal_source(fd, node_id))

    def _begin(self, source: str) -> None:
        if self._ended:
            return
        try:
            self._capture = FrameCapture(
                source,
                fps=self.cfg.screen_share_fps,
                max_width=self.cfg.screen_share_max_width,
                quality=self.cfg.screen_share_quality,
                wanted=self._has_watchers,
                on_frame=self._on_frame,
                on_error=lambda message: self._end(f"Screen sharing stopped: {message}"),
            )
            self._capture.start()
        except ScreenShareError as exc:
            self._capture = None
            self._end(str(exc))
            return
        self._active = True
        self.client.set_screen_state(
            {"share_id": self.share_id, "from": self.cfg.device_id, "nick": self.cfg.nickname, "ts": time.time()}
        )
        self._timer_id = GLib.timeout_add_seconds(1, self._expire_watchers)
        if self._on_started is not None:
            self._on_started()

    def _end(self, reason: str) -> None:
        self.stop(reason)

    def _has_watchers(self) -> bool:
        with self._lock:
            return bool(self._watchers)

    def _expire_watchers(self) -> bool:
        cutoff = time.monotonic() - WATCH_EXPIRE_SECONDS
        with self._lock:
            expired = [device_id for device_id, (_nick, seen) in self._watchers.items() if seen < cutoff]
            for device_id in expired:
                del self._watchers[device_id]
        if expired:
            self._notify_watchers()
        return True

    def _notify_watchers(self) -> bool:
        if not self._ended:
            self._on_watchers_changed(self.watcher_nicks())
        return False

    def _on_frame(self, jpeg: bytes, width: int, height: int) -> None:
        with self._lock:
            self._last_frame = (jpeg, width, height)
        self._send_frame(jpeg, width, height)

    def _send_frame(self, jpeg: bytes, width: int, height: int, force: bool = False) -> None:
        with self._lock:
            # Backpressure: while the previous frame is still queued in paho
            # (a slow broker or uplink), skip this one rather than pile up a
            # backlog that plays out seconds late.
            if not force and any(getattr(info, "rc", 0) == 0 and not info.is_published() for info in self._inflight):
                return
            self._seq += 1
            seq = self._seq
        chunk_size = max(4096, self.cfg.chunk_size)
        total = (len(jpeg) + chunk_size - 1) // chunk_size
        infos = []
        for index in range(total):
            header = {
                "share_id": self.share_id,
                "seq": seq,
                "index": index,
                "total": total,
                "width": width,
                "height": height,
            }
            infos.append(self.client.send_screen_frame(self.share_id, header, jpeg[index * chunk_size : (index + 1) * chunk_size]))
        with self._lock:
            self._inflight = infos


class FrameAssembler:
    """Viewer side: stitches one share's frame chunks back into JPEGs. A
    frame missing a chunk (QoS 0 drops it) is abandoned as soon as a newer
    frame starts arriving."""

    def __init__(self) -> None:
        self._seq = -1
        self._shown = -1
        self._total = 0
        self._parts: dict[int, bytes] = {}

    def add(self, header: dict, chunk: bytes) -> Optional[tuple[bytes, int, int]]:
        seq = header.get("seq", -1)
        if seq <= self._shown or seq < self._seq:
            return None
        if seq != self._seq:
            self._seq, self._total, self._parts = seq, header.get("total", 0), {}
        self._parts[header.get("index", 0)] = chunk
        if len(self._parts) < self._total:
            return None
        self._shown = seq
        data = b"".join(self._parts[i] for i in range(self._total))
        self._parts = {}
        return data, header.get("width", 0), header.get("height", 0)
