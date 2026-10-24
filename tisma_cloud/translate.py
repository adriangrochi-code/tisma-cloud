"""Automatic translation of the problem reports with LibreTranslate on our own servers (free software, AGPL-3.0,
https://docs.libretranslate.com): the reports in English or Portuguese are read and searched in the language of the
team. Nothing leaves the servers. Optional: TISMA_TRANSLATE_URL (the libretranslate service of compose.master.yaml).

API used (docs.libretranslate.com, "API usage"): POST /translate {q, source: "auto", target, format: "text"} answers
{translatedText, detectedLanguage: {language, confidence}}.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TranslationError(Exception):
    """The translator did not answer, or answered something else."""


class Translator:
    def __init__(self, url: str, target: str = "es", timeout: float = 120.) -> None:
        self.url = url.rstrip("/")
        self.target = target
        self.timeout = timeout

    def translate(self, text: str) -> tuple[str, str]:
        """The text in the target language and the language it was written in (the text itself when it already was in
        the target language)."""
        body = json.dumps({"q": text, "source": "auto", "target": self.target, "format": "text"}).encode()
        request = urllib.request.Request(self.url + "/translate", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        # The service is on the local network of the master: never through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout) as answer:
                data = json.loads(answer.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise TranslationError(str(ex)) from None
        if not isinstance(data, dict) or not isinstance(data.get("translatedText"), str):
            raise TranslationError("Unexpected answer of the translator")
        detected = data.get("detectedLanguage") or {}
        language = detected.get("language", "") if isinstance(detected, dict) else ""
        return data["translatedText"], str(language)[:8]
