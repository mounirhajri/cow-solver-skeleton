import io
import json

from src.log import configure_logging, get_logger


def test_logger_emits_structured_json(monkeypatch) -> None:
    buf = io.StringIO()
    configure_logging(level="DEBUG", stream=buf)
    log = get_logger("test")
    log.info("hello", auction_id="42")
    output = buf.getvalue().strip()
    payload = json.loads(output)
    assert payload["event"] == "hello"
    assert payload["auction_id"] == "42"
    assert payload["level"] == "info"
