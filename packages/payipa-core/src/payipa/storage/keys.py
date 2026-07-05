"""对象存储 key 方案 + URL 指纹（服务端统一计算，agent 只上传字节）。

URL 指纹 = hash(方法 + 规范化 URL + 排序请求体)：查询参数排序、去 fragment（w3lib 同构，02 §2.2）。
raw 路径：``{source_uuid}/raw/{batch}/{urlfp}.zst``；多媒体：``{source_uuid}/files/{fileid}``（02 定案）。
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonicalize_url(url: str) -> str:
    """规范化：scheme/host 小写、查询参数排序、去 fragment、空路径补 /。"""
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


def url_fingerprint(url: str, method: str = "GET", body: bytes | None = None) -> str:
    h = hashlib.sha1()  # noqa: S324  指纹用途非安全
    h.update(method.upper().encode())
    h.update(b"\n")
    h.update(canonicalize_url(url).encode("utf-8"))
    if body:
        h.update(b"\n")
        h.update(body)
    return h.hexdigest()


def raw_object_key(source_uuid: str, batch_id: int | str, url: str) -> str:
    """raw（网页/JSON）归档 key，压缩后落此路径。"""
    return f"{source_uuid}/raw/{batch_id}/{url_fingerprint(url)}.zst"


def file_object_key(source_uuid: str, file_id: str) -> str:
    """多媒体文件 key（不入 raw、不过控制面）。"""
    return f"{source_uuid}/files/{file_id}"
