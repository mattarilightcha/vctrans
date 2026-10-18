"""初回の準備。文字起こしのモデルと、NVIDIA の GPU で動かす部品（cuDNN・cuBLAS）をこの PC に取ってくる。

- NVIDIA の部品はこのツールに入れて配らない。使う人の PC が PyPI（NVIDIA が出している nvidia-cudnn-cu12 など）から
  直に取る。wheel（zip）を丸ごとは落とさず、要る DLL だけを HTTP の範囲指定で読む
- 文字起こしのモデルは Hugging Face から models/ へ
- GPU が無い PC は CPU で動く小さいモデルにする。そのときも CTranslate2 が読み込みで探す cudnn64_9.dll
  （小さい入口の DLL）だけは取る
"""
import ctypes
import io
import json
import os
import shutil
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .config import FROZEN, ROOT

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # 窓なしの exe では出す先が無い。進み具合は自前で見る

RUNTIME = ROOT / "runtime" / "cuda"
MODELS = ROOT / "models"
UA = {"User-Agent": "vctrans (+https://github.com/)"}

# CTranslate2 4.6 が使う版にそろえる
CUDNN = ("nvidia-cudnn-cu12", "9.16.0.29", "nvidia/cudnn/bin/",
         ["cudnn64_9.dll", "cudnn_graph64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll",
          "cudnn_heuristic64_9.dll", "cudnn_engines_precompiled64_9.dll", "cudnn_engines_runtime_compiled64_9.dll"])
CUBLAS = ("nvidia-cublas-cu12", "12.9.2.10", "nvidia/cublas/bin/", ["cublas64_12.dll", "cublasLt64_12.dll"])
SHIM = "cudnn64_9.dll"

# faster-whisper が使うモデル（Hugging Face のリポジトリ）
REPOS = {
    "tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
MODEL_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.json",
               "vocabulary.txt")


class PrepError(Exception):
    pass


# ---- GPU があるか
def gpu_name():
    """NVIDIA の GPU があれば名前、無ければ ""。ドライバー（nvcuda.dll）に聞くだけで、CUDA の部品は要らない。"""
    try:
        cu = ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return ""
    try:
        if cu.cuInit(0) != 0:
            return ""
        n = ctypes.c_int()
        if cu.cuDeviceGetCount(ctypes.byref(n)) != 0 or n.value < 1:
            return ""
        dev = ctypes.c_int()
        cu.cuDeviceGet(ctypes.byref(dev), 0)
        buf = ctypes.create_string_buffer(256)
        cu.cuDeviceGetName(buf, 256, dev)
        return buf.value.decode("utf-8", "replace") or "NVIDIA GPU"
    except Exception:
        return ""


# ---- HTTP の範囲指定で読む zip
class RemoteFile(io.RawIOBase):
    """URL の先のファイルを、読む所だけ Range で取る（zipfile に渡せる）。"""

    BLOCK = 1 << 20

    def __init__(self, url, on_bytes=None):
        self.url, self.pos, self.on_bytes = url, 0, on_bytes
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            self.size = int(r.headers["Content-Length"])
        self.buf_start, self.buf = 0, b""

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def _fetch(self, start, n):
        end = min(self.size, start + n) - 1
        req = urllib.request.Request(self.url, headers=dict(UA, Range=f"bytes={start}-{end}"))
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                if r.status != 206 and start:
                    raise PrepError("取得元が範囲指定に応じない")
                if self.on_bytes:
                    self.on_bytes(len(data))
                return data
            except (OSError, urllib.error.URLError):
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        off = self.pos - self.buf_start
        if not (0 <= off and off + n <= len(self.buf)):
            self.buf_start = self.pos
            self.buf = self._fetch(self.pos, max(n, self.BLOCK))
            off = 0
        out = self.buf[off:off + n]
        self.pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def wheel_url(package, version):
    """PyPI の JSON から Windows 64 bit の wheel の URL。"""
    with urllib.request.urlopen(urllib.request.Request(f"https://pypi.org/pypi/{package}/{version}/json",
                                                       headers=UA), timeout=30) as r:
        data = json.load(r)
    return pick_wheel(data["urls"])


def pick_wheel(urls):
    for u in urls:
        if u["filename"].endswith("win_amd64.whl"):
            return u["url"], u.get("size", 0)
    raise PrepError("Windows 用の wheel が見つからない")


def fetch_dlls(spec, names, dest, progress=None):
    """wheel の中の DLL のうち names だけを dest へ。progress(取った, 全部) を呼ぶ。"""
    package, version, prefix, _ = spec
    url, _ = wheel_url(package, version)
    got = [0]
    total = [0]   # zip の目次を読み終えるまでは 0（進み具合を出さない）

    def on_bytes(n):
        got[0] += n
        if progress and total[0]:
            progress(min(got[0], total[0]), total[0])

    rf = RemoteFile(url, on_bytes)
    with zipfile.ZipFile(rf) as z:
        infos = [z.getinfo(prefix + n) for n in names]
        total[0] = sum(i.compress_size for i in infos) or 1
        got[0] = 0
        dest.mkdir(parents=True, exist_ok=True)
        for info in infos:
            tmp = dest / (Path(info.filename).name + ".part")
            with z.open(info) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(tmp, dest / Path(info.filename).name)


def cuda_missing(gpu):
    """取ってくる物 [(spec, [名前])]。GPU が無ければ入口の cudnn64_9.dll だけ。"""
    out = []
    for spec in (CUDNN, CUBLAS):
        names = spec[3] if gpu else [n for n in spec[3] if n == SHIM]
        miss = [n for n in names if not (RUNTIME / n).exists()]
        if miss:
            out.append((spec, miss))
    return out


def ensure_cuda(gpu, progress=None):
    """exe で動くときだけ。NVIDIA の部品を runtime/cuda へ取り、DLL の探し先に足す。"""
    if not FROZEN:
        return
    todo = cuda_missing(gpu)
    for spec, names in todo:
        label = "cuDNN" if spec is CUDNN else "cuBLAS"
        fetch_dlls(spec, names, RUNTIME,
                   (lambda d, t, label=label: progress(f"GPU の部品（{label}）", d, t)) if progress else None)
    add_dll_dir(RUNTIME)


def add_dll_dir(d):
    d = str(d)
    if os.path.isdir(d):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def pip_cuda_dirs():
    """Python で動かすとき: pip の nvidia-cudnn-cu12・nvidia-cublas-cu12 の DLL を探し先へ。"""
    for mod in ("cudnn", "cublas", "cuda_runtime", "cuda_nvrtc"):
        try:
            pkg = __import__(f"nvidia.{mod}", fromlist=["x"])
        except ImportError:
            continue
        for base in getattr(pkg, "__path__", []):
            add_dll_dir(os.path.join(base, "bin"))


def stub_av():
    """exe には PyAV（FFmpeg）を入れていない。faster-whisper は読み込みで av を import するが、
    音を配列で渡す限り使わないので、空の部品で済ませる。"""
    try:
        import av  # noqa: F401
    except ImportError:
        sys.modules["av"] = types.ModuleType("av")


# ---- 文字起こしのモデル
def model_dir(name):
    return MODELS / name


def _has_model(d):
    return (Path(d) / "model.bin").exists() and (Path(d) / "config.json").exists()


def find_model(name):
    """手元にあるモデルの場所。models/<名前> → Hugging Face の置き場（前に Python で落とした物）。無ければ None。"""
    d = model_dir(name)
    if _has_model(d):
        return str(d)
    repo = REPOS.get(name)
    if repo:
        try:
            from huggingface_hub import snapshot_download
            p = snapshot_download(repo, local_files_only=True, allow_patterns=list(MODEL_FILES))
            if _has_model(p):
                return p
        except Exception:
            pass
    if Path(name).is_dir() and _has_model(name):
        return name
    return None


def ensure_model(name, progress=None):
    """モデルの場所を返す。無ければ models/<名前> へ取る（progress(見出し, 取った, 全部)）。"""
    have = find_model(name)
    if have:
        return have
    repo = REPOS.get(name)
    if not repo:
        raise PrepError(f"モデルの名前がわからない: {name}")
    from huggingface_hub import HfApi, snapshot_download
    info = HfApi().model_info(repo, files_metadata=True)
    sizes = {s.rfilename: (s.size or 0) for s in info.siblings if s.rfilename in MODEL_FILES}
    total = sum(sizes.values()) or 1
    dest = model_dir(name)
    dest.mkdir(parents=True, exist_ok=True)
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            got = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
            if progress:
                progress(f"文字起こしのモデル（{name}）", min(got, total), total)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        snapshot_download(repo, local_dir=str(dest), allow_patterns=list(sizes))
    finally:
        done.set()
    shutil.rmtree(dest / ".cache", ignore_errors=True)
    if not _has_model(dest):
        raise PrepError("モデルを取りきれなかった")
    if progress:
        progress(f"文字起こしのモデル（{name}）", total, total)
    return str(dest)


def plan(asr_cfg):
    """(device, モデル名, compute_type, GPU の名前)。device が auto なら GPU の有無で決める。"""
    want = (asr_cfg.get("device") or "auto").lower()
    gpu = gpu_name() if want in ("auto", "cuda") else ""
    if want == "cuda" or (want == "auto" and gpu):
        return "cuda", asr_cfg["model"], asr_cfg["compute_type"], gpu
    return "cpu", asr_cfg.get("cpu_model") or "small", asr_cfg.get("cpu_compute_type") or "int8", gpu


# ---- 仮想ケーブル
def cables(device_names=None):
    """{"cable": VB-CABLE があるか, "cable_a": CABLE-A があるか}。"""
    if device_names is None:
        from .audio import device_names as dn
        devs, _, _ = dn()
    else:
        devs = device_names
    outs = " ".join(devs.get("output", [])).upper()
    ins = " ".join(devs.get("input", [])).upper()
    return {"cable": "CABLE INPUT" in outs and "CABLE OUTPUT" in ins,
            "cable_a": "CABLE-A INPUT" in outs and "CABLE-A OUTPUT" in ins}
