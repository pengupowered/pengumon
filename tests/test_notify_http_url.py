"""notify.settings.http_url: the checks every webhook URL and public_url go through."""

import pytest

from pengumon.notify.settings import http_url


@pytest.mark.parametrize("url", ["https://hooks.example.com:99999/x", "https://hooks.example.com:0/x",
                                 "http://hooks.example.com:abc", "http://hooks.example.com:-1/", "http://hooks.example.com:65536"])
def test_a_port_that_is_not_a_port_is_refused(url):
    with pytest.raises(ValueError, match="valid port") as e:
        http_url(url, "url", allow_query=True)
    assert "hooks.example.com" not in str(e.value)


@pytest.mark.parametrize("url", ["https://hooks.example.com:8443/x", "http://hooks.example.com:1", "http://hooks.example.com:65535/",
                                 "https://hooks.example.com/x", "http://[2001:db8::1]:8080/x"])
def test_a_real_port_or_none_passes(url):
    assert http_url(f" {url} ", "url", allow_query=True) == url
