from edupagetasks import notify


def test_empty_url_noop(mocker):
    get = mocker.patch.object(notify.requests, "get")
    post = mocker.patch.object(notify.requests, "post")
    notify.notify_failure("", "subject", "body")
    notify.notify_failure(None, "subject", "body")
    get.assert_not_called()
    post.assert_not_called()


def test_healthchecks_get_fail_suffix(mocker):
    get = mocker.patch.object(notify.requests, "get")
    notify.notify_failure("https://hc-ping.com/uuid-abc123", "S", "B")
    get.assert_called_once_with(
        "https://hc-ping.com/uuid-abc123/fail", timeout=notify._TIMEOUT_S
    )


def test_healthchecks_io_ping_fail_suffix(mocker):
    get = mocker.patch.object(notify.requests, "get")
    notify.notify_failure("https://healthchecks.io/ping/uuid-xyz", "S", "B")
    get.assert_called_once_with(
        "https://healthchecks.io/ping/uuid-xyz/fail", timeout=notify._TIMEOUT_S
    )


def test_healthchecks_trailing_slash_handled(mocker):
    get = mocker.patch.object(notify.requests, "get")
    notify.notify_failure("https://hc-ping.com/uuid-abc/", "S", "B")
    get.assert_called_once_with(
        "https://hc-ping.com/uuid-abc/fail", timeout=notify._TIMEOUT_S
    )


def test_ntfy_post_text_plain_with_title(mocker):
    post = mocker.patch.object(notify.requests, "post")
    notify.notify_failure("https://ntfy.sh/mytopic", "Hw sync down", "details here")
    post.assert_called_once_with(
        "https://ntfy.sh/mytopic",
        data="details here",
        headers={"X-Title": "Hw sync down"},
        timeout=notify._TIMEOUT_S,
    )


def test_generic_webhook_post_json(mocker):
    post = mocker.patch.object(notify.requests, "post")
    notify.notify_failure("https://example.com/hook", "S", "B")
    post.assert_called_once_with(
        "https://example.com/hook",
        json={"subject": "S", "body": "B", "message": "S: B"},
        timeout=notify._TIMEOUT_S,
    )


def test_http_webhook_supported(mocker):
    post = mocker.patch.object(notify.requests, "post")
    notify.notify_failure("http://localhost:9999/hook", "S", "B")
    post.assert_called_once()


def test_exceptions_swallowed(mocker):
    post = mocker.patch.object(
        notify.requests, "post", side_effect=RuntimeError("boom")
    )
    notify.notify_failure("https://example.com/hook", "S", "B")
    post.assert_called_once()

    get = mocker.patch.object(notify.requests, "get", side_effect=RuntimeError("boom"))
    notify.notify_failure("https://hc-ping.com/uuid", "S", "B")
    get.assert_called_once()
