"""Opt-in real OpenSSH check; only inside the disposable network-isolated fixture."""

import os

import pytest

from tools.check_real_transport import available, check_transport


@pytest.mark.skipif(os.environ.get("CCFLEET_REAL_SSH_TEST") != "1" or not available(),
                    reason="requires the explicitly marked disposable OpenSSH fixture container")
def test_real_openssh_master_and_framed_channels():
    report = check_transport(samples=3)
    assert report["master_did_not_invoke_forced_command"] is True
    assert report["two_channels_one_connection"] is True
    assert report["host_pin_failure_refused"] is True
    assert report["closed_master_no_fallback"] is True
    assert report["owned_master_and_proxy_cleanup"] is True
    assert report["authenticated_connections"] == 4
    assert report["model_requests"] == 0
