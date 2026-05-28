import os
import tempfile
from pathlib import Path

import pytest
from app.core.config import settings
from app.services.session_workspace import validate_session_id


class TestValidateSessionId:

    def test_valid_ids(self):
        for sid in ["abc123", "test-session", "session_01", "x" * 64]:
            assert validate_session_id(sid) == sid

    def test_invalid_empty(self):
        with pytest.raises(ValueError):
            validate_session_id("")

    def test_invalid_slash(self):
        with pytest.raises(ValueError):
            validate_session_id("abc/../def")

    def test_invalid_too_long(self):
        with pytest.raises(ValueError):
            validate_session_id("x" * 65)

    def test_invalid_special_chars(self):
        for sid in ["abc def", "abc.def", "abc\tdef"]:
            with pytest.raises(ValueError):
                validate_session_id(sid)
