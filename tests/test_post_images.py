"""Картинки постов LinkedIn на сервере платформы: что скачивается, что хранится и что отдаётся без входа.

Скачивание подменено: сеть не нужна. Проверяется защита: только https с доменов картинок LinkedIn (и после
переадресаций), только изображение до 5 МБ, имя по хэшу содержимого, раздача только своих файлов.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
import pytest

from oiltech_digest import api, config
from oiltech_digest.ingestion import core_page, post_images

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SOURCE = "https://media.licdn.com/dms/image/v2/abc/feedshare/0/1?e=1&v=beta"


@pytest.fixture(autouse=True)
def _storage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://platform.example")


def _download(content: bytes, final_url: str = SOURCE, ok: bool = True):
    calls = []

    def download(url, **kwargs):
        calls.append((url, kwargs))
        return {"ok": ok, "content": content, "final_url": final_url, "error": None if ok else "http_404"}

    download.calls = calls
    return download


@pytest.mark.parametrize("url, allowed", [
    (SOURCE, True),
    ("https://dms.licdn.com/playlist/vid/x.jpg", True),
    ("http://media.licdn.com/x.jpg", False),
    ("https://media.licdn.com.evil.example/x.jpg", False),
    ("https://evil.example/media.licdn.com/x.jpg", False),
    ("https://user:pass@media.licdn.com/x.jpg", False),
    ("https://media.licdn.com:8443/x.jpg", False),
    ("https://127.0.0.1/x.jpg", False),
    ("", False),
])
def test_only_linkedin_image_hosts_over_https_are_downloaded(url, allowed):
    assert post_images.allowed_source(url) is allowed


def test_image_kind_by_first_bytes():
    assert post_images.image_kind(JPEG) == "jpg"
    assert post_images.image_kind(PNG) == "png"
    assert post_images.image_kind(b"GIF89a" + b"\x00" * 8) == "gif"
    assert post_images.image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert post_images.image_kind(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>") is None
    assert post_images.image_kind(b"<html>") is None


def test_saved_image_gets_a_hash_name_and_a_platform_address(tmp_path):
    url = post_images.save(SOURCE, download=_download(JPEG))
    name = f"{hashlib.sha256(JPEG).hexdigest()}.jpg"
    assert url == f"https://platform.example/media/post-images/{name}"
    assert (tmp_path / "post_images" / name).read_bytes() == JPEG
    assert post_images.save(SOURCE, download=_download(JPEG)) == url  # то же содержимое: тот же файл
    assert len(list((tmp_path / "post_images").iterdir())) == 1


@pytest.mark.parametrize("download", [
    _download(b"<html>not an image</html>"),
    _download(b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024)),
    _download(b""),
    _download(JPEG, final_url="https://evil.example/x.jpg"),
    _download(JPEG, ok=False),
])
def test_refused_downloads_store_nothing(download, tmp_path):
    assert post_images.save(SOURCE, download=download) is None
    assert not (tmp_path / "post_images").exists()


def test_without_public_address_or_with_a_foreign_host_nothing_is_downloaded(monkeypatch):
    download = _download(JPEG)
    assert post_images.save("https://evil.example/x.jpg", download=download) is None
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert post_images.save(SOURCE, download=download) is None
    assert download.calls == []


def test_route_serves_only_stored_images_without_a_session(tmp_path):
    url = post_images.save(SOURCE, download=_download(PNG))
    client = TestClient(api.app)
    response = client.get(url.replace("https://platform.example", ""))
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/media/post-images/" + "0" * 64 + ".png", "/media/post-images/..%2F..%2Fetc%2Fpasswd",
                 "/media/post-images/" + "a" * 64 + ".svg", "/media/post-images/x.png"):
        assert client.get(path).status_code == 404, path


def test_save_asks_for_allowlist_size_limit_and_checked_certificate():
    download = _download(JPEG)
    post_images.save(SOURCE, download=download)
    _url, kwargs = download.calls[0]
    assert kwargs == {"allowed": post_images.allowed_source, "max_bytes": post_images.MAX_BYTES, "verified_tls": True}


class _Response:
    def __init__(self, status, headers=None, chunks=()):
        self.status, self.headers, self._chunks = status, headers or {}, list(chunks)

    def release_conn(self):
        pass

    def shutdown(self):
        pass

    def stream(self, size, decode_content=True):
        yield from self._chunks


def _fake_network(monkeypatch, responses):
    """Без сети: адрес считается публичным, ответ берётся по адресу; посещённые адреса записываются."""
    visited = []
    monkeypatch.setattr(core_page, "_public_target", lambda url: url)

    def get(target, *, deadline, verified_tls=False):
        visited.append((target, verified_tls))
        return responses[target]

    monkeypatch.setattr(core_page, "_get", get)
    return visited


def test_download_stops_a_redirect_off_the_allowlist_before_connecting(monkeypatch):
    visited = _fake_network(monkeypatch, {
        SOURCE: _Response(302, {"Location": "http://10.0.0.5/internal.jpg"}),
    })
    result = core_page.download(SOURCE, allowed=post_images.allowed_source, verified_tls=True)
    assert not result["ok"] and "разрешённых" in result["error"]
    assert visited == [(SOURCE, True)]  # внутренний адрес переадресации даже не открывался


def test_download_cuts_the_body_at_its_own_limit(monkeypatch):
    _fake_network(monkeypatch, {SOURCE: _Response(200, chunks=[b"x" * 4096] * 3)})
    result = core_page.download(SOURCE, max_bytes=8192)
    assert not result["ok"] and "больше" in result["error"]


def test_download_without_a_checked_certificate_is_refused(monkeypatch):
    import ssl
    import urllib3

    class _Pool:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def urlopen(self, *args, **kwargs):
            if self.kwargs.get("cert_reqs") == "CERT_NONE":
                raise AssertionError("без проверки сертификата не ходим")
            raise ssl.SSLError("certificate verify failed")

    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", _Pool)
    target = core_page._Target("https", "media.licdn.com", 443, "203.0.113.7", "/x.jpg", "media.licdn.com")
    with pytest.raises(core_page.PageRefused):
        core_page._get(target, deadline=__import__("time").monotonic() + 10, verified_tls=True)
