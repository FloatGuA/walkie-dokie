import logging

from walkie_dokie.logging_config import _RedactingFormatter


def test_redacting_formatter_hides_feishu_websocket_credentials():
    formatter = _RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="Lark",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "connected to wss://example.test/ws?fpid=1&access_key=secret-key"
            "&ticket=secret-ticket&service_id=2"
        ),
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert "secret-key" not in rendered
    assert "secret-ticket" not in rendered
    assert "access_key=<redacted>" in rendered
    assert "ticket=<redacted>" in rendered
