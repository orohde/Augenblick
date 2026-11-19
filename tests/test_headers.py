"""Every answer carries the headers that keep the browser from guessing, framing and telling."""


def test_security_headers_on_every_answer(anonymous):
    for path in ("/", "/api/version", "/api/photos"):
        res = anonymous.get(path)
        assert res.headers["x-content-type-options"] == "nosniff", path
        assert res.headers["x-frame-options"] == "SAMEORIGIN", path
        assert res.headers["referrer-policy"] == "same-origin", path
        assert "frame-ancestors 'self'" in res.headers["content-security-policy"], path
