import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MAX_QUESTION_CHARS", "501", "MAX_QUESTION_CHARS must be at most 500"),
        ("CHUNK_SIZE", "1501", "CHUNK_SIZE must be at most 1500"),
    ],
)
def test_environment_limits_match_response_schema(monkeypatch, name, value, message) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()
