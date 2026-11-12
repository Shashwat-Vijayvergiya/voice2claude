"""Paste into the window you were using (e.g. Claude's prompt box) on Wayland.

Wayland doesn't let apps type into other apps. The sanctioned way is the
xdg-desktop-portal RemoteDesktop API: GNOME asks once ("Allow remote
interaction?"), and with ``persist_mode=2`` hands back a restore token so it
doesn't ask again. We open a short session, send Ctrl+V, and close it.

The text itself goes through the clipboard (set by the caller first), so if
anything here fails, the user can still press Ctrl+V themselves. Enter is
never pressed.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2

XK_CONTROL_L = 0xFFE3
XK_V = 0x0076

REQUEST_TIMEOUT = 15.0
CONSENT_TIMEOUT = 120.0  # first time, the user has to answer GNOME's dialog


class InsertError(Exception):
    pass


def _variant_value(v):
    """jeepney gives variants as (signature, value)."""
    return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) else v


class PortalPaster:
    """Sends Ctrl+V through the RemoteDesktop portal. Blocking; run off the UI thread."""

    def __init__(self, token_file: Path):
        self.token_file = token_file

    # ---- restore token (so GNOME only asks once) -------------------------------

    def _load_token(self) -> str | None:
        try:
            return self.token_file.read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(token)
            os.chmod(self.token_file, 0o600)
        except OSError as exc:
            log.warning("Could not save portal token: %s", exc)

    # ---- portal plumbing ---------------------------------------------------------

    def _connect(self):
        try:
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:  # pragma: no cover
            raise InsertError("The jeepney package is missing (pip install jeepney).") from exc
        try:
            return open_dbus_connection(bus="SESSION")
        except Exception as exc:
            raise InsertError(f"Could not reach the desktop session bus: {exc}") from exc

    def _request(self, conn, method: str, signature: str, args: tuple, timeout: float) -> dict:
        """Call a portal method that answers later with a Request.Response signal."""
        from jeepney import DBusAddress, MatchRule, MessageType, message_bus, new_method_call
        from jeepney.io.blocking import Proxy

        token = "v2c" + secrets.token_hex(6)
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        handle = f"{PORTAL_PATH}/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=handle)
        Proxy(message_bus, conn).AddMatch(rule)
        options = dict(args[-1])
        options["handle_token"] = ("s", token)
        args = (*args[:-1], options)
        addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=RD_IFACE)
        with conn.filter(rule) as queue:
            reply = conn.send_and_get_reply(new_method_call(addr, method, signature, args), timeout=REQUEST_TIMEOUT)
            if reply.header.message_type == MessageType.error:
                raise InsertError(f"Portal {method} failed: {reply.body}")
            try:
                signal = conn.recv_until_filtered(queue, timeout=timeout)
            except TimeoutError as exc:
                raise InsertError(f"No answer from the desktop for {method}.") from exc
        response, results = signal.body
        if response == 1:
            raise InsertError("Permission to type into other windows was declined.")
        if response != 0:
            raise InsertError(f"The desktop refused {method} (code {response}).")
        return {k: _variant_value(v) for k, v in results.items()}

    def _call(self, conn, path: str, iface: str, method: str, signature: str = "", args: tuple = ()):
        from jeepney import DBusAddress, MessageType, new_method_call

        reply = conn.send_and_get_reply(
            new_method_call(DBusAddress(path, bus_name=PORTAL_BUS, interface=iface), method, signature, args),
            timeout=REQUEST_TIMEOUT,
        )
        if reply.header.message_type == MessageType.error:
            raise InsertError(f"Portal {method} failed: {reply.body}")
        return reply

    def open_session(self, conn) -> str:
        session = self._request(conn, "CreateSession", "a{sv}",
                                ({"session_handle_token": ("s", "v2c" + secrets.token_hex(6))},), REQUEST_TIMEOUT)
        handle = str(session["session_handle"])
        select = {"types": ("u", KEYBOARD), "persist_mode": ("u", PERSIST_UNTIL_REVOKED)}
        saved = self._load_token()
        if saved:
            select["restore_token"] = ("s", saved)
        self._request(conn, "SelectDevices", "oa{sv}", (handle, select), REQUEST_TIMEOUT)
        return handle

    def close_session(self, conn, handle: str) -> None:
        try:
            self._call(conn, handle, "org.freedesktop.portal.Session", "Close")
        except Exception as exc:
            log.debug("Closing portal session: %s", exc)

    # ---- public --------------------------------------------------------------------

    def paste(self, before_keys: Callable[[], None] = lambda: None) -> None:
        """Start a session (GNOME asks the first time), run ``before_keys`` (e.g. hide our
        window so focus returns to Claude), then press and release Ctrl+V."""
        conn = self._connect()
        handle = None
        try:
            handle = self.open_session(conn)
            started = self._request(conn, "Start", "osa{sv}", (handle, "", {}), CONSENT_TIMEOUT)
            if not int(started.get("devices", 0)) & KEYBOARD:
                raise InsertError("Keyboard access wasn't granted.")
            if started.get("restore_token"):
                self._save_token(str(started["restore_token"]))
            before_keys()
            for keysym, state in ((XK_CONTROL_L, 1), (XK_V, 1), (XK_V, 0), (XK_CONTROL_L, 0)):
                self._call(conn, PORTAL_PATH, RD_IFACE, "NotifyKeyboardKeysym", "oa{sv}iu",
                           (handle, {}, keysym, state))
                time.sleep(0.02)
        finally:
            if handle:
                self.close_session(conn, handle)
            conn.close()

    def probe(self) -> str:
        """Create and close a session without starting it: no dialog, no keys."""
        conn = self._connect()
        try:
            handle = self.open_session(conn)
            self.close_session(conn, handle)
            return handle
        finally:
            conn.close()
