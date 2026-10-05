# -*- coding: utf-8 -*-
"""Разведка API расписания api.unifirst.ru/api/v1."""

import json
import re
import sys
import urllib.request

API = "https://api.unifirst.ru/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get(url, headers=None):
    request_headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=40) as response:
        return response.status, dict(response.headers), response.read()


def show_json(url, limit=2500):
    print("=" * 70)
    print("GET", url)
    try:
        status, headers, data = get(url)
    except Exception as error:
        print("  FAIL:", type(error).__name__, error)
        return None
    print("  status:", status, "| type:", headers.get("Content-Type"), "| bytes:", len(data))
    text = data.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError:
        print("  (не JSON) первые символы:", text[:300])
        return None
    print("  JSON:", json.dumps(payload, ensure_ascii=False)[:limit])
    return payload


def context(bundle_path, needle, before=260, after=420, max_hits=6):
    print("=" * 70)
    print("КОНТЕКСТ В БАНДЛЕ:", needle)
    text = open(bundle_path, encoding="utf-8", errors="replace").read()
    hits = 0
    for match in re.finditer(re.escape(needle), text):
        start = max(0, match.start() - before)
        print("---", match.start(), "---")
        print(text[start:match.end() + after].replace("\n", " "))
        hits += 1
        if hits >= max_hits:
            break
    if not hits:
        print("  не найдено")


if __name__ == "__main__":
    bundle = sys.argv[1] if len(sys.argv) > 1 else "data/recon/bundle.js"
    show_json(API + "/groups")
    context(bundle, "/groups?")
    context(bundle, "/timetable?")
