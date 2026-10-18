# 使っている部品とライセンス（THIRD_PARTY_NOTICES）

VC翻訳（vctrans）本体は MIT ライセンスです（`LICENSE`）。
配る exe のフォルダには、下の部品が入っています。ライセンスの全文は exe のフォルダの `licenses/` にあります
（Python で動かす場合は、各部品の配布物に入っています）。

## exe に入っている部品

| 部品 | 使い道 | ライセンス |
|---|---|---|
| Python 3.13（標準ライブラリ・Tcl/Tk を含む） | 動かす土台・画面 | PSF License（Tcl/Tk は BSD 系） |
| PyInstaller のブートローダー | exe にする | GPL-2.0 に例外条項（できた exe は自由に配れる） |
| faster-whisper | 文字起こし | MIT |
| CTranslate2 | 文字起こしの計算 | MIT（同梱の Intel OpenMP `libiomp5md.dll` は Intel の再配布可のライセンス） |
| Silero VAD（faster-whisper に同梱の `silero_vad_v6.onnx`） | 声のある所の切り出し | MIT |
| ONNX Runtime | Silero VAD を動かす | MIT |
| tokenizers | 文字起こしの語の分け方 | Apache-2.0 |
| huggingface_hub・hf-xet | モデルの取得 | Apache-2.0 |
| anthropic（と httpx・httpcore・anyio・pydantic・jiter・distro・sniffio・h11・idna ほか） | Claude で訳す | MIT・BSD-3-Clause ほか |
| certifi | 通信の証明書 | MPL-2.0（手を加えずに同梱。ソースは https://github.com/certifi/python-certifi ） |
| tqdm | faster-whisper が読み込む | MPL-2.0 AND MIT（手を加えずに同梱。ソースは https://github.com/tqdm/tqdm ） |
| NumPy・SciPy（中の OpenBLAS ほか） | 音の計算 | BSD-3-Clause（同梱のランタイムは各ライセンス。`licenses/` を参照） |
| PyAudioWPatch（中の PortAudio） | 音の機器 | Apache-2.0（PortAudio は MIT 系） |
| hidapi（cython-hidapi） | PlayStation のコントローラーを読む | BSD・GPL-3.0・HIDAPI の 3 つから選べる。**BSD を選んで使う** |
| pywin32 | Windows の読み上げ（SAPI） | PSF License |
| six | pynput が使う | MIT |
| PyYAML・requests・urllib3・filelock・fsspec・packaging ほか | huggingface_hub が使う | MIT・Apache-2.0・Unlicense・BSD ほか |

### LGPL の部品

| 部品 | ライセンス | 置き方 |
|---|---|---|
| pynput | LGPL-3.0 | exe に固めず、**ソースのまま** exe の横の `lgpl/pynput/` に置いています。差し替えれば vctrans.exe はそれを読みます。ソース: https://github.com/moses-palmer/pynput |

## exe に入れていない物

| 物 | 扱い |
|---|---|
| NVIDIA cuDNN 9・cuBLAS 12（GPU で文字起こしするための DLL） | 配っていません。初回の起動で、使う人の PC が PyPI の NVIDIA の配布物（`nvidia-cudnn-cu12`・`nvidia-cublas-cu12`）から要る DLL だけを取り、`runtime/cuda/` に置きます。NVIDIA のライセンス（NVIDIA Software License Agreement）に従います。GPU の無い PC でも、CTranslate2 の読み込みに要る `cudnn64_9.dll`（小さい入口）だけは同じように取ります |
| Whisper のモデル（faster-whisper 形式） | 配っていません。初回の起動で Hugging Face から `models/` に取ります。GPU: `mobiuslabsgmbh/faster-whisper-large-v3-turbo`、CPU: `Systran/faster-whisper-small`。元の OpenAI Whisper の重みは MIT |
| PyAV（FFmpeg） | 入れていません（Python の配布物の FFmpeg が GPL の x264/x265 を含むため）。音を配列で渡すので使いません |
| edge-tts | 入れていません。Python で動かすときだけ「試し」で使えます（LGPL-3.0。Microsoft が外のツール向けに開いている方法ではありません） |
| Piper・Azure AI Speech・ElevenLabs | 入れていません。使う人が自分で入れる・自分のキーで使う外の物です |
| VB-CABLE・VB-CABLE A+B（VB-Audio） | 入れていません。使う人が VB-Audio のサイトから入れます（VB-CABLE は寄付制の無料、A+B は寄付で入手） |

## 外のサービス

- 訳: Anthropic の Claude API（使う人のキー）。文字にした会話を送ります
- 読み上げ（選んだときだけ）: Azure AI Speech・ElevenLabs（使う人のキー）。英訳の文を送ります
- 取得: PyPI（NVIDIA の DLL）・Hugging Face（モデル）
