from app.services import waha


def test_waha_media_url_uses_only_configured_host_path():
    assert waha._media_request_path("http://evil.example/api/files/image.png?token=abc") == "/api/files/image.png?token=abc"
    assert waha._media_request_path("/api/files/image.png") == "/api/files/image.png"


def test_waha_media_url_rejects_non_api_paths():
    import pytest

    with pytest.raises(waha.WahaError):
        waha._media_request_path("http://127.0.0.1:8000/admin")
