"""配る exe のフォルダと zip を作る。

  python build_tools/build.py            dist/vctrans/ と dist/vctrans-<版>-win64.zip
  python build_tools/build.py --no-zip   フォルダだけ

要る物: この PC の Python に requirements.txt の物と pyinstaller。
exe のフォルダには、設定の手本・ゲームのプロファイル・説明・ライセンス・LGPL の部品のソースを横に置く。
使う人の物（logs・.env・models・runtime）は入れない。
"""
import argparse
import importlib.metadata as md
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build_tools"
DIST = ROOT / "dist"
APP = DIST / "vctrans"
NO_WINDOW = 0x08000000

sys.path.insert(0, str(ROOT))
from vc import __version__  # noqa: E402

VERSION_TXT = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}, 0), prodvers=({v}, 0), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('041104b0', [
      StringStruct('CompanyName', 'nettoge.com'),
      StringStruct('FileDescription', 'VC翻訳（ゲームの VC の英語を字幕に、日本語を英語の声に）'),
      StringStruct('FileVersion', '{s}'),
      StringStruct('InternalName', 'vctrans'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 nettoge.com. MIT License'),
      StringStruct('OriginalFilename', 'vctrans.exe'),
      StringStruct('ProductName', 'VC翻訳 vctrans'),
      StringStruct('ProductVersion', '{s}')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])]
)
"""

# exe の横に置く物（元 → 置き先）
BESIDE = [
    ("config.toml", "config.toml"),
    ("glossary.txt", "glossary.txt"),
    ("README.md", "README.md"),
    ("LICENSE", "LICENSE.txt"),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    ("assets/vctrans.ico", "assets/vctrans.ico"),
]


def site(dist_name):
    """入っている配布物のフォルダ（site-packages）。"""
    return Path(md.distribution(dist_name).locate_file(""))


def copy_lgpl():
    """pynput（LGPL-3.0）をソースのまま lgpl/ へ。差し替え方は lgpl/README.txt。"""
    dest = APP / "lgpl"
    shutil.rmtree(dest, ignore_errors=True)
    src = site("pynput") / "pynput"
    shutil.copytree(src, dest / "pynput", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in md.distribution("pynput").files:
        if f.name.upper().startswith(("COPYING", "LICENSE")):
            shutil.copy(f.locate(), dest / f"pynput-{f.name}")
    ver = md.version("pynput")
    (dest / "README.txt").write_text(
        f"pynput {ver}（LGPL-3.0）のソースをそのまま置いています。\n"
        "vctrans.exe はこのフォルダを読み込みの先頭に足して pynput を読みます。\n"
        "別の版や手を入れた pynput に差し替えるときは、このフォルダの pynput/ を入れ替えてください。\n"
        "ソースの配布元: https://github.com/moses-palmer/pynput （PyPI: https://pypi.org/project/pynput/）\n",
        encoding="utf-8")


def copy_licenses():
    """同梱した部品のライセンスの全文を licenses/ へ（入っている配布物の dist-info から）。"""
    dest = APP / "licenses"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    names = ["faster-whisper", "ctranslate2", "onnxruntime", "anthropic", "numpy", "scipy", "PyAudioWPatch",
             "tokenizers", "huggingface_hub", "hidapi", "pywin32", "six", "tqdm", "httpx", "httpcore", "anyio",
             "certifi", "idna", "sniffio", "h11", "pydantic", "pydantic_core", "typing_extensions", "jiter",
             "distro", "annotated-types", "filelock", "fsspec", "packaging", "PyYAML", "requests", "urllib3",
             "charset-normalizer", "docstring_parser", "typing-inspection", "hf-xet", "pyinstaller"]
    for n in names:
        try:
            d = md.distribution(n)
        except md.PackageNotFoundError:
            continue
        got = [f for f in (d.files or []) if "licen" in f.name.lower() or "copying" in f.name.lower()
               or "notice" in f.name.lower() or (f.parent and "licenses" in str(f.parent).lower())]
        for f in got:
            p = Path(f.locate())
            if p.is_file():
                shutil.copy(p, dest / f"{d.metadata['Name']}-{d.version}-{p.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true")
    a = ap.parse_args()
    major, minor, patch = (__version__.split(".") + ["0", "0"])[:3]
    (BUILD / "version.txt").write_text(
        VERSION_TXT.replace("{v}", f"{major}, {minor}, {patch}").replace("{s}", __version__), encoding="utf-8")
    shutil.rmtree(APP, ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                        "--distpath", str(DIST), "--workpath", str(ROOT / "build"), str(BUILD / "vctrans.spec")],
                       cwd=str(ROOT), creationflags=NO_WINDOW)
    if r.returncode:
        sys.exit("PyInstaller が失敗した")
    for src, dst in BESIDE:
        (APP / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / src, APP / dst)
    shutil.copytree(ROOT / "profiles", APP / "profiles", ignore=shutil.ignore_patterns("*.json", "*.local.*"))
    copy_lgpl()
    copy_licenses()
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"フォルダ {APP}  {size / 1e6:.0f} MB")
    if not a.no_zip:
        z = DIST / f"vctrans-{__version__}-win64.zip"
        z.unlink(missing_ok=True)
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in sorted(APP.rglob("*")):
                if f.is_file():
                    zf.write(f, Path("vctrans") / f.relative_to(APP))
        print(f"zip {z}  {z.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
