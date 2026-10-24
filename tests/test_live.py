import os

import pytest


@pytest.mark.live
def test_live_agent(sip_caller):
    uri = os.environ.get("SIPLING_TEST_URI")
    if not uri:
        pytest.skip("SIPLING_TEST_URI is not set")
    result = sip_caller(uri, transport="tcp", timeout=15)
    assert result.answered, result.ladder
    assert result.heard_audio(within=5)
    result.hangup()
    assert result.clean_bye, result.ladder
