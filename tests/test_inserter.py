from types import SimpleNamespace

import pytest

from app import inserter
from app.inserter import KEYBOARD, InsertError, PortalPaster, XK_CONTROL_L, XK_V


class FakeConn:
    def close(self):
        self.closed = True


class Scripted(PortalPaster):
    """PortalPaster with the D-Bus layer replaced by a script."""

    def __init__(self, tmp_path, start_result=None, start_error=None):
        super().__init__(tmp_path / "token")
        self.start_result = start_result if start_result is not None else {"devices": KEYBOARD, "restore_token": "tok1"}
        self.start_error = start_error
        self.requests, self.calls = [], []

    def _connect(self):
        return FakeConn()

    def _request(self, conn, method, signature, args, timeout):
        self.requests.append((method, args))
        if method == "CreateSession":
            return {"session_handle": "/session/1"}
        if method == "Start":
            if self.start_error:
                raise InsertError(self.start_error)
            return self.start_result
        return {}

    def _call(self, conn, path, iface, method, signature="", args=()):
        self.calls.append((method, args))


def test_paste_sends_ctrl_v_after_before_keys(tmp_path):
    p = Scripted(tmp_path)
    order = []
    p.paste(lambda: order.append("before"))
    keys = [(a[2], a[3]) for m, a in p.calls if m == "NotifyKeyboardKeysym"]
    assert keys == [(XK_CONTROL_L, 1), (XK_V, 1), (XK_V, 0), (XK_CONTROL_L, 0)]
    assert order == ["before"]
    assert ("Close", ()) in p.calls  # session always closed
    assert (tmp_path / "token").read_text() == "tok1"


def test_no_enter_key_ever(tmp_path):
    p = Scripted(tmp_path)
    p.paste()
    sent = {a[2] for m, a in p.calls if m == "NotifyKeyboardKeysym"}
    assert sent == {XK_CONTROL_L, XK_V}  # never Return (0xff0d)


def test_restore_token_reused(tmp_path):
    (tmp_path / "token").write_text("saved")
    p = Scripted(tmp_path)
    p.paste()
    select = next(args for m, args in p.requests if m == "SelectDevices")[1]
    assert select["restore_token"] == ("s", "saved")
    assert select["persist_mode"] == ("u", 2) and select["types"] == ("u", KEYBOARD)


def test_declined_closes_session_and_sends_nothing(tmp_path):
    p = Scripted(tmp_path, start_error="Permission to type into other windows was declined.")
    with pytest.raises(InsertError, match="declined"):
        p.paste()
    assert not any(m == "NotifyKeyboardKeysym" for m, _ in p.calls)
    assert ("Close", ()) in p.calls


def test_keyboard_not_granted(tmp_path):
    p = Scripted(tmp_path, start_result={"devices": 2})
    with pytest.raises(InsertError, match="Keyboard"):
        p.paste()


def test_variant_unwrap():
    assert inserter._variant_value(("u", 1)) == 1
    assert inserter._variant_value("plain") == "plain"
