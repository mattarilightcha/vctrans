import pytest

from vc import pad, ptt


def ds_usb(b0, b1=0, b2=0):
    r = [0] * 64
    r[0], r[8], r[9], r[10] = 0x01, b0, b1, b2
    return r


def test_dualsense_usb_buttons():
    assert pad.parse(ds_usb(0x08), "ds") == set()                 # 方向キーは 8 で離した状態
    assert pad.parse(ds_usb(0x02), "ds") == {"right"}
    assert pad.parse(ds_usb(0x01), "ds") == {"up", "right"}       # 斜め
    assert pad.parse(ds_usb(0x08 | 0x20, 0x40, 0x10), "ds") == {"cross", "l3", "fn_l"}


def test_dualsense_bluetooth_and_ds4():
    bt = [0] * 78
    bt[0], bt[9] = 0x31, 0x06
    assert pad.parse(bt, "ds") == {"left"}
    ds4 = [0] * 64
    ds4[0], ds4[5], ds4[6] = 0x01, 0x08 | 0x80, 0x01
    assert pad.parse(ds4, "ds4") == {"triangle", "l1"}
    assert pad.parse([0x05, 1, 2], "ds") is None


def test_xinput_maps_to_same_names():
    assert pad.xinput_parse(0x0008, 0, 0) == {"right"}
    assert pad.xinput_parse(0x1000 | 0x2000 | 0x0100, 0, 0) == {"cross", "circle", "l1"}
    assert pad.xinput_parse(0, 200, pad.XI_TRIGGER) == {"l2"}     # しきい値ちょうどは押していない
    assert pad.xinput_parse(0x0010 | 0x0020, 0, 0) == {"options", "create"}


def test_labels():
    assert pad.label("pad:right") == "方向キー右"
    assert pad.label("pad:cross") == "× / A"
    assert pad.label("x2") == "マウス x2"
    assert pad.label("b") == "キー B"
    assert pad.label("") == "（なし）"


def test_reader_merges_sources():
    r = pad.PadReader()
    seen = []
    r.watch("right", lambda down: seen.append(down))
    r.hid_on = {"right"}
    r._update(r.hid_on | r.xi_on)
    r.xi_on = {"right"}
    r._update(r.hid_on | r.xi_on)       # もう片方でも押した: 知らせない
    r.hid_on = set()
    r._update(r.hid_on | r.xi_on)       # 片方を離してもまだ押している
    r.xi_on = set()
    r._update(r.hid_on | r.xi_on)
    assert seen == [True, False]


def test_any_press_grabs_once():
    r = pad.PadReader()
    got = []
    r.any_press = got.append
    r._update({"l1"})
    r._update({"l1", "r1"})
    assert got == ["pad:l1"]


def test_parse_combo_and_vk():
    mods, vk = ptt._parse_combo("ctrl+alt+o")
    assert mods == {"ctrl", "alt"} and vk == ord("O")
    with pytest.raises(ValueError):
        ptt._parse_combo("ctrl+alt")
    assert ptt.key_vk("b") == ord("B")
    assert ptt.key_vk("f8") == 0x77
    assert ptt.key_vk("pad:right") is None and ptt.key_vk("x2") is None and ptt.key_vk("") is None
