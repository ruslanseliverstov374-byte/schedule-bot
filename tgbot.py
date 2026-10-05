# -*- coding: utf-8 -*-
"""Минимальный клиент Telegram Bot API на стандартной библиотеке (без зависимостей)."""

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid

API_BASE = "https://api.telegram.org"


class TgError(Exception):
    def __init__(self, description, code=None, parameters=None, method=None):
        super().__init__("Telegram API error %s: %s" % (code, description))
        self.description = description
        self.code = code
        self.parameters = parameters or {}
        self.method = method


class Telegram:
    def __init__(self, token, api_base=API_BASE, timeout=35):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.url_base = "%s/bot%s" % (self.api_base, token)
        self.me = None

    def call(self, method, params=None, timeout=None, retries=2):
        url = "%s/%s" % (self.url_base, method)
        data = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        last_error = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("ok"):
                    return payload.get("result")
                raise TgError(
                    payload.get("description", "unknown"),
                    payload.get("error_code"),
                    payload.get("parameters"),
                    method,
                )
            except urllib.error.HTTPError as err:
                body = err.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = {"description": body}
                description = payload.get("description", "http %s" % err.code)
                parameters = payload.get("parameters", {}) or {}
                if err.code == 429 and attempt < retries:
                    wait = min(30, int(parameters.get("retry_after", 3)) + 1)
                    time.sleep(wait)
                    last_error = TgError(description, err.code, parameters, method)
                    continue
                raise TgError(description, err.code, parameters, method)
            except TgError:
                raise
            except Exception as err:  # сеть, таймаут, DNS
                last_error = err
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_error

    # ---------------- методы API ----------------

    def get_me(self):
        self.me = self.call("getMe")
        return self.me

    def get_updates(self, offset=None, timeout=25, allowed_updates=None):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        return self.call("getUpdates", params, timeout=timeout + 15)

    def send_message(self, chat_id, text, reply_markup=None, parse_mode="HTML",
                     disable_notification=False):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params)

    def edit_message(self, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("editMessageText", params)

    def answer_callback(self, callback_id, text=None, show_alert=False):
        params = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            params["text"] = text[:190]
        return self.call("answerCallbackQuery", params)

    def send_chat_action(self, chat_id, action="typing"):
        return self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)

    def set_my_commands(self, commands):
        return self.call("setMyCommands", {"commands": commands})

    def set_webhook(self, url, secret_token=None, drop_pending_updates=True):
        params = {"url": url, "drop_pending_updates": drop_pending_updates,
                  "allowed_updates": ["message", "callback_query"]}
        if secret_token:
            params["secret_token"] = secret_token
        return self.call("setWebhook", params)

    def delete_webhook(self, drop_pending_updates=False):
        return self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def get_webhook_info(self):
        return self.call("getWebhookInfo")

    # ---------------- файлы ----------------

    def send_document(self, chat_id, file_path, caption=None, filename=None,
                      disable_notification=False):
        """Отправляет файл (multipart/form-data) - например, резервную копию базы."""
        path = file_path
        filename = filename or os.path.basename(path)
        boundary = "----EnglishStarter%s" % uuid.uuid4().hex
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            payload = handle.read()

        parts = []

        def field(name, value):
            parts.append(("--%s\r\n" % boundary).encode("utf-8"))
            parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode("utf-8"))
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")

        field("chat_id", chat_id)
        if disable_notification:
            field("disable_notification", "true")
        if caption:
            field("caption", caption)
            field("parse_mode", "HTML")
        parts.append(("--%s\r\n" % boundary).encode("utf-8"))
        parts.append((
            'Content-Disposition: form-data; name="document"; filename="%s"\r\n' % filename
        ).encode("utf-8"))
        parts.append(("Content-Type: %s\r\n\r\n" % mime).encode("utf-8"))
        parts.append(payload)
        parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
        body = b"".join(parts)

        request = urllib.request.Request(
            "%s/sendDocument" % self.url_base,
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise TgError(err.read().decode("utf-8", "replace"), err.code, method="sendDocument")
        if not result.get("ok"):
            raise TgError(result.get("description", "unknown"), result.get("error_code"),
                          result.get("parameters"), "sendDocument")
        return result.get("result")

    def get_file(self, file_id):
        return self.call("getFile", {"file_id": file_id})

    def download_file(self, file_id, destination):
        """Скачивает файл из Telegram в локальный путь."""
        info = self.get_file(file_id)
        file_path = info.get("file_path")
        if not file_path:
            raise TgError("file_path отсутствует", method="getFile")
        url = "%s/file/bot%s/%s" % (self.api_base, self.token, file_path)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        with open(destination, "wb") as handle:
            handle.write(data)
        return destination, len(data)


# ---------------- конструкторы клавиатур ----------------

def inline(rows):
    return {"inline_keyboard": rows}


def btn(text, data):
    return {"text": text, "callback_data": data}


def url_btn(text, url):
    return {"text": text, "url": url}


def reply_keyboard(rows, resize=True):
    return {
        "keyboard": rows,
        "resize_keyboard": resize,
        "input_field_placeholder": "Напишите ответ или выберите пункт",
    }


def remove_keyboard():
    return {"remove_keyboard": True}
