from cos2pag.http_client import ApiError, redact_secrets, request_json


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = text.encode() if json_body is None and text else b"{}" if json_body is not None else b""

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def test_request_json_returns_parsed_body():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "GET", "https://example.com/x")
    assert result == {"ok": True}
    assert len(session.calls) == 1


def test_request_json_raises_api_error_on_4xx():
    session = FakeSession(FakeResponse(404, text="not found"))
    try:
        request_json(session, "GET", "https://example.com/x")
        assert False, "expected ApiError"
    except ApiError as exc:
        assert exc.status_code == 404


def test_request_json_dry_run_skips_mutating_request():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "PATCH", "https://example.com/x", json={"a": 1}, dry_run=True)
    assert result is None
    assert session.calls == []


def test_request_json_dry_run_still_performs_get():
    session = FakeSession(FakeResponse(200, json_body={"ok": True}))
    result = request_json(session, "GET", "https://example.com/x", dry_run=True)
    assert result == {"ok": True}
    assert len(session.calls) == 1


def test_redact_secrets_masks_known_sensitive_keys():
    body = {
        "alias": "my-bucket",
        "source": {
            "s3": {
                "bucketName": "my-bucket",
                "accessKey": "AKIA-real-value",
                "secretKey": "shh-do-not-log-me",
            }
        },
        "auth": {"password": "hunter2", "token": "tok-123"},
    }

    redacted = redact_secrets(body)

    assert redacted["source"]["s3"]["bucketName"] == "my-bucket"
    assert redacted["source"]["s3"]["accessKey"] == "***REDACTED***"
    assert redacted["source"]["s3"]["secretKey"] == "***REDACTED***"
    assert redacted["auth"]["password"] == "***REDACTED***"
    assert redacted["auth"]["token"] == "***REDACTED***"
    # original untouched
    assert body["source"]["s3"]["secretKey"] == "shh-do-not-log-me"


def test_redact_secrets_handles_lists_and_none():
    assert redact_secrets(None) is None
    assert redact_secrets([{"secretKey": "x"}, {"bucketName": "y"}]) == [
        {"secretKey": "***REDACTED***"},
        {"bucketName": "y"},
    ]
