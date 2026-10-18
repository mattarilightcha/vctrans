# PyInstaller の設定（フォルダ形式・窓なし）。build_tools/build.py から使う。
# 入れない物:
#   - NVIDIA の cuDNN（ctranslate2 の wheel に入っている cudnn64_9.dll も）→ 初回に PyPI から取る（vc/prepare.py）
#   - PyAV（中の FFmpeg が GPL の x264/x265 を含む）→ 使わない。faster-whisper の読み込みは空の部品で通す
#   - edge-tts（公式に開かれた方法ではない）
#   - pynput（LGPL-3.0）→ ソースのまま exe の横の lgpl/ に置く（差し替えられるように）
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "vctrans.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=collect_data_files("faster_whisper"),
    hiddenimports=[
        "win32com.client", "pythoncom", "pywintypes",
        # pynput（lgpl/ のソース）が使う物
        "six", "ctypes.wintypes", "enum", "contextlib", "unicodedata", "inspect", "functools", "operator",
        "queue", "importlib", "itertools",
        "vc.panel", "vc.overlay", "vc.vad", "vc.asr", "vc.audio", "vc.translate", "vc.tts", "vc.prepare",
    ] + collect_submodules("scipy._external.array_api_compat.numpy"),  # scipy.signal が読み込みの中で使う
    excludes=["av", "edge_tts", "aiohttp", "pynput", "torch", "matplotlib", "pandas", "IPython", "jupyter",
              "PIL", "pytest", "transformers", "kokoro_onnx", "rapidocr_onnxruntime",
              "cv2", "sympy",
              # 使わない物（scipy・httpx などの「あれば使う」読み込みで入ってくる）
              "cupy", "cupy_backends", "cupyx", "botocore", "boto3", "s3transfer", "grpc", "cryptography",
              "clr", "pythonnet", "clr_loader", "win32ui", "pywin", "Pythonwin", "zstandard", "dask", "numba",
              "jax", "torch", "tensorflow", "sparse", "array_api_strict"],
    noarchive=False,
)
# cuDNN の入口の DLL は配らない（初回に取る）
a.binaries = [b for b in a.binaries if not Path(b[0]).name.lower().startswith(("cudnn", "cublas", "cudart", "nvrtc"))]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vctrans",
    console=False,
    icon=str(ROOT / "assets" / "vctrans.ico"),
    version=str(ROOT / "build_tools" / "version.txt"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="vctrans")
