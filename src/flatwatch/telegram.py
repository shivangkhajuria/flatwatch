"""Minimal Telegram Bot API client over httpx (HTML parse mode).

The bot token is part of the request URL, so never log URLs or raw httpx errors.
"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, msg: str, *, network: bool = False):
        super().__init__(msg)
        self.network = network   # True: transient, worth retrying later


class Telegram:
    def __init__(self, token: str, chat_id: int, *, client: httpx.AsyncClient | None = None):
        self._base = f"{API_BASE}/bot{token}"
        self.chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Telegram":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            r = await self._client.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as e:
            # str(e) may include the URL (and so the token); report only the type.
            raise TelegramError(f"{method}: network error ({type(e).__name__})", network=True) from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}, non-JSON response", network=r.status_code >= 500) from None
        if not data.get("ok"):
            code = data.get("error_code")
            raise TelegramError(f"{method}: {code} {data.get('description')}",
                                network=code == 429 or (isinstance(code, int) and code >= 500))
        return data["result"]

    async def send_message(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        reply_markup: dict | None = None,
        disable_preview: bool = True,
        silent: bool = False,
    ) -> dict:
        return await self._call(
            "sendMessage",
            chat_id=chat_id or self.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": disable_preview},
            disable_notification=silent or None,
        )

    async def edit_message_text(
        self,
        message_id: int,
        text: str,
        *,
        chat_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._call(
            "editMessageText",
            chat_id=chat_id or self.chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True},
        )

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        return await self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    async def edit_message_reply_markup(self, message_id: int, reply_markup: dict | None,
                                        chat_id: int | None = None) -> dict:
        return await self._call("editMessageReplyMarkup", chat_id=chat_id or self.chat_id,
                                message_id=message_id, reply_markup=reply_markup or {"inline_keyboard": []})

    async def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict]:
        return await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )
