"""ローカル LLM (LM Studio などの OpenAI 互換 API) の呼び出し。翻訳とエンハンサーで共用する。

openai パッケージは使わず urllib で呼ぶ (EXE を軽くするため。comfy.py と同じ流儀)。
"""

import json
import re
import threading
import urllib.error
import urllib.request
from typing import Optional

API_KEY = "lm-studio"
REQUEST_TIMEOUT = 180  # 秒。ローカル LLM は長文だと時間がかかる

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    """利用者に見せるエラー (文は日本語)。"""


def _request(url: str, payload: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Authorization", f"Bearer {API_KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if "No models loaded" in detail:
            # LM Studio は、指定したモデルがサーバーにないときもこう返す
            raise LLMError(
                "指定したモデルを使えません。モデル名がサーバーにあるか確かめてください"
                " (設定の「モデル取得」で一覧から選べます)。"
            ) from exc
        raise LLMError(f"サーバーがエラーを返しました ({exc.code}) {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"サーバーに接続できません: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise LLMError(str(exc)) from exc


def chat(base_url: str, model: str, system: str, user: str, temperature: float) -> str:
    """system と user を 1 往復送り、返事の本文を返す。"""
    messages = [{"role": "system", "content": system}] if system.strip() else []
    messages.append({"role": "user", "content": user})
    body = _request(
        base_url.rstrip("/") + "/chat/completions",
        {"model": model, "messages": messages, "temperature": temperature},
    )
    try:
        content = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("サーバーの応答を読めませんでした。") from exc
    # 考える過程を出すモデル (<think>...</think>) は、その部分を除く
    return _THINK_RE.sub("", content).strip()


def list_models(base_url: str) -> list[str]:
    body = _request(base_url.rstrip("/") + "/models", timeout=15)
    try:
        return sorted(item["id"] for item in body.get("data", []))
    except (KeyError, TypeError) as exc:
        raise LLMError("モデル一覧を読めませんでした。") from exc


def pick_model(current: str, models: list[str]) -> str:
    """モデル一覧を取ったとき欄に出す名前。今の名前がサーバーになければ、一覧の先頭 (埋め込み用は除く)。"""
    if current in models or not models:
        return current
    chat_models = [m for m in models if "embed" not in m.lower()]
    return (chat_models or models)[0]


def run_in_thread(func, done_signal):
    """func() を裏のスレッドで実行し、(結果, エラー文) を done_signal で返す。"""

    def work():
        try:
            result, error = func(), None
        except LLMError as exc:
            result, error = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - 予期しない失敗も画面に出す
            result, error = None, f"{type(exc).__name__}: {exc}"
        done_signal.emit(result, error)

    threading.Thread(target=work, daemon=True).start()
