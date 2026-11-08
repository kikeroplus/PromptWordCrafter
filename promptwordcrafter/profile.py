"""PromptWordCrafter の、共通部品 (gencommon) に渡すアプリごとの決まり。

テキストファイル 1 つが 1 本のプロンプト。出来たファイルはテキストのフォルダの中の「生成結果」フォルダに、
動画なら「(元の名前)_video.mp4」、画像なら「(元の名前)_t2i.png」で保存する
(プロンプトのフォルダには手で作ったテキストだけを置くため。結果の画像を ImageMimicArt などで元画像にすると、
その隣に説明の .txt ができる)。
"""

from pathlib import Path

from gencommon import app, comfy, store

from . import settings as settings_module
from .text_io import RESULTS_DIR

APP_NAME = "PromptWordCrafter"
SUFFIXES = ("_t2i", "_video")  # 出来たファイルの名前に付けるもの (画像 / 動画)


def results_dir(source: Path) -> Path:
    """そのテキストから生成したファイルの置き場所。"""
    return source.parent / RESULTS_DIR


def _output_path(source: Path, request, is_video: bool, ext: str) -> Path:
    return results_dir(source) / (source.stem + ("_video" if is_video else "_t2i") + ext)


PROFILE = app.Profile(
    name=APP_NAME,
    reads=False,
    source_word="テキストファイル",
    unit="件",
    prompt_word="本文",
    prompt_where="編集欄",
    column="ファイル",
    output_path=_output_path,
    workflow_hint=(
        "右クリックしたテキストファイルの本文が、そのままワークフローのプロンプトに入ります "
        f"(文字欄に「{comfy.PROMPT_MARKER}」があればそこ。なければ prompt 入力、CLIPTextEncode の順。"
        "複数あるときはタイトルに Positive とあるもの)。"
        "プロンプトを入れる場所のないワークフローは登録できません。seed は毎回ランダムになります。"
        f"出来たファイルはテキストファイルのフォルダの中の「{RESULTS_DIR}」フォルダに、動画なら「(元の名前)_video.mp4」、"
        "画像なら「(元の名前)_t2i.png」で保存します。"
    ),
    open_on_done=True,
    skip_empty=True,
)


def setup() -> list[str]:
    """起動したときに呼ぶ。戻り値は、共有の設定に移したときの知らせ。

    共有する前は、LLM の接続を promptwordcrafter_settings.json (translate_url / enhance_model) に、
    編集欄の「盛る」で選んでいたエンハンサーの番号を同じファイルの enhance_preset (0 始まり) に置いていた。
    """
    data = settings_module.load_settings()
    extra = {
        "llm_url.txt": str(data.get("translate_url") or "").strip().rstrip("/"),
        "enhance_model.txt": str(data.get("enhance_model") or data.get("translate_model") or "").strip(),
    }
    # 番号は共有に移す前に書いておく (移すときに、移したあとの位置へ付け替わる)
    store.configure(APP_NAME)
    if store.load("enhance_preset.txt") is None and isinstance(data.get("enhance_preset"), int):
        store.save("enhance_preset.txt", str(data["enhance_preset"] + 1))
    return app.setup(PROFILE, extra)
