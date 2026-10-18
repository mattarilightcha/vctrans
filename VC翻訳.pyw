# ダブルクリックで起動（窓なしの pythonw で動く）。終了は字幕の窓を右クリック →「終了」。
import runpy
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.argv = [str(here / "vctrans.py")]
runpy.run_path(str(here / "vctrans.py"), run_name="__main__")
