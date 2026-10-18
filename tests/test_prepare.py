import io
import random
import re
import zipfile

import pytest

from vc import prepare


class FakeResp(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_wheel():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("nvidia/cudnn/bin/cudnn64_9.dll", b"SHIM" * 1000)
        rnd = random.Random(0)   # 縮まない中身（本物の DLL と同じく、丸ごと落とすと大きい）
        z.writestr("nvidia/cudnn/bin/cudnn_ops64_9.dll", rnd.randbytes(900000))
        z.writestr("nvidia/cudnn/include/cudnn.h", rnd.randbytes(500000))   # 取らない物
    return buf.getvalue()


@pytest.fixture
def fake_net(monkeypatch):
    blob = make_wheel()
    log = {"ranges": [], "bytes": 0}

    def urlopen(req, timeout=None):
        url = req.full_url
        if url.startswith("https://pypi.org/pypi/"):
            body = ('{"urls": [{"filename": "x-py3-none-manylinux.whl", "url": "https://files/linux.whl"},'
                    ' {"filename": "x-py3-none-win_amd64.whl", "url": "https://files/win.whl", "size": %d}]}' % len(blob))
            return FakeResp(body.encode())
        assert url == "https://files/win.whl"
        if req.get_method() == "HEAD":
            return FakeResp(b"", headers={"Content-Length": str(len(blob))})
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", req.get_header("Range"))
        a, b = int(m.group(1)), int(m.group(2))
        log["ranges"].append((a, b))
        log["bytes"] += b - a + 1
        return FakeResp(blob[a:b + 1], status=206)

    monkeypatch.setattr(prepare.urllib.request, "urlopen", urlopen)
    return blob, log


def test_pick_wheel():
    url, size = prepare.pick_wheel([{"filename": "a-manylinux.whl", "url": "l"},
                                    {"filename": "a-win_amd64.whl", "url": "w", "size": 9}])
    assert (url, size) == ("w", 9)
    with pytest.raises(prepare.PrepError):
        prepare.pick_wheel([{"filename": "a.tar.gz", "url": "s"}])


def test_fetch_only_named_dlls(tmp_path, fake_net, monkeypatch):
    blob, log = fake_net
    monkeypatch.setattr(prepare.RemoteFile, "BLOCK", 64 * 1024)
    seen = []
    prepare.fetch_dlls(prepare.CUDNN, ["cudnn64_9.dll"], tmp_path, lambda d, t: seen.append((d, t)))
    assert (tmp_path / "cudnn64_9.dll").read_bytes() == b"SHIM" * 1000
    assert not (tmp_path / "cudnn_ops64_9.dll").exists()
    assert log["bytes"] < len(blob) / 4          # 丸ごとは落としていない
    assert not list(tmp_path.glob("*.part"))


def test_cuda_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "RUNTIME", tmp_path)
    cpu = prepare.cuda_missing(False)
    assert cpu == [(prepare.CUDNN, ["cudnn64_9.dll"])]
    gpu = prepare.cuda_missing(True)
    assert [len(n) for _, n in gpu] == [len(prepare.CUDNN[3]), len(prepare.CUBLAS[3])]
    (tmp_path / "cudnn64_9.dll").write_bytes(b"x")
    assert prepare.cuda_missing(False) == []


def test_plan(monkeypatch):
    cfg = {"device": "auto", "model": "large-v3-turbo", "compute_type": "int8_float16",
           "cpu_model": "small", "cpu_compute_type": "int8"}
    monkeypatch.setattr(prepare, "gpu_name", lambda: "RTX")
    assert prepare.plan(cfg) == ("cuda", "large-v3-turbo", "int8_float16", "RTX")
    assert prepare.plan(dict(cfg, device="cpu"))[:3] == ("cpu", "small", "int8")
    monkeypatch.setattr(prepare, "gpu_name", lambda: "")
    assert prepare.plan(cfg)[:3] == ("cpu", "small", "int8")


def test_find_model_local(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "MODELS", tmp_path)
    d = tmp_path / "tiny"
    d.mkdir()
    (d / "model.bin").write_bytes(b"0")
    (d / "config.json").write_text("{}")
    assert prepare.find_model("tiny") == str(d)


def test_cables():
    devs = {"output": ["CABLE Input (VB-Audio Virtual Cable)", "スピーカー"],
            "input": ["CABLE Output (VB-Audio Virtual Cable)"]}
    assert prepare.cables(devs) == {"cable": True, "cable_a": False}
    devs["output"].append("CABLE-A Input (VB-Audio Cable A)")
    devs["input"].append("CABLE-A Output (VB-Audio Cable A)")
    assert prepare.cables(devs) == {"cable": True, "cable_a": True}
    assert prepare.cables({"output": ["CABLE-A Input"], "input": ["CABLE-A Output"]})["cable"] is False
