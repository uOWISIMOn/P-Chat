from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .config import ConfigManager
from .utils import html_unescape


class TranslationError(RuntimeError):
    pass


class TranslatorClient:
    def __init__(self, config: ConfigManager) -> None:
        self.config = config

    def configured(self) -> bool:
        return bool(self.api_url)

    @property
    def api_key(self) -> str:
        return os.environ.get("PCHAT_LIBRETRANSLATE_API_KEY", "").strip() or self.config.translation_api_key

    @property
    def api_url(self) -> str:
        return os.environ.get("PCHAT_LIBRETRANSLATE_API_URL", "").strip() or self.config.translation_api_url

    def translate_zh_to_ja(self, text: str) -> str:
        api_url = self.api_url
        if not api_url:
            raise TranslationError("LibreTranslate API URL is not configured.")
        payload = {
            "q": text,
            "source": "auto",
            "target": "ja",
            "format": "text",
        }
        api_key = self.api_key
        if api_key:
            payload["api_key"] = api_key
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            api_url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise TranslationError(f"LibreTranslate API error: {exc.code} {details}") from exc
        except urllib.error.URLError as exc:
            raise TranslationError(f"LibreTranslate request failed: {exc.reason}") from exc
        translated = str(payload.get("translatedText", "")).strip()
        if not translated:
            raise TranslationError("LibreTranslate returned an empty translation.")
        return html_unescape(translated)
