"""``HomeAssistantView`` stand-in.

The auth/api views only need a subclassable base at import time (the push
dispatcher's lazy ``from .auth_api import get_engine`` imports the whole
module, class bodies included, without instantiating a view). ``json`` /
``json_message`` mirror the real helpers — aiohttp imported lazily so
merely installing the stubs never requires it.
"""

from __future__ import annotations


class HomeAssistantView:
    url = None
    extra_urls: list = []
    name = None
    requires_auth = True

    @staticmethod
    def json(result, status_code=200, headers=None):
        import json as _json

        from aiohttp import web

        return web.Response(
            body=_json.dumps(result).encode("utf-8"),
            content_type="application/json",
            status=int(status_code),
            headers=headers,
        )

    def json_message(self, message, status_code=200, message_code=None, headers=None):
        data = {"message": message}
        if message_code is not None:
            data["code"] = message_code
        return self.json(data, status_code, headers)
