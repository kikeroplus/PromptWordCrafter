import sys
from pathlib import Path

# 生成と読み取りの共通部品 (GenCommon) は、同じ AI_projects フォルダに並べて置いている
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "GenCommon"))

from promptwordcrafter.main import main  # noqa: E402

if __name__ == "__main__":
    main()
