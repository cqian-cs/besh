# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""工具函数与目标解析。"""

import pytest

from besh.target import parse_target
from besh.util import (
    BeshError,
    human_size,
    human_time,
    parse_beacon_url,
    safe_id,
    safe_window,
    to_bool,
    to_int,
)


def test_parse_beacon_url_with_secret():
    base, secret = parse_beacon_url("http://1.2.3.4:12242?secret=pw")
    assert base == "http://1.2.3.4:12242"
    assert secret == "pw"


def test_parse_beacon_url_without_scheme():
    base, secret = parse_beacon_url("host:12242")
    assert base == "http://host:12242"
    assert secret == ""


def test_parse_beacon_url_with_path_and_slash():
    base, secret = parse_beacon_url("https://example.com/besh/?secret=s3cret")
    assert base == "https://example.com/besh"
    assert secret == "s3cret"


def test_parse_beacon_url_default():
    base, secret = parse_beacon_url(None)
    assert base.endswith(":12242")
    assert secret == ""


def test_safe_id_rejects_injection():
    for bad in ["", "a b", "a;rm", "../x", "1/2", "-x"]:
        with pytest.raises(BeshError):
            safe_id(bad)
    assert safe_id("phone-1") == "phone-1"
    assert safe_id(" W_1.2 ") == "W_1.2"


def test_safe_window():
    assert safe_window("") == ""
    assert safe_window("gpu") == "gpu"
    with pytest.raises(BeshError):
        safe_window("a:b")


def test_to_bool_and_int():
    assert to_bool(True) is True
    assert to_bool("1") is True
    assert to_bool("false") is False
    assert to_bool(None, default=True) is True
    assert to_int("50") == 50
    assert to_int("x", default=7) == 7


def test_parse_target():
    assert parse_target("w1") == ("w1", "")
    assert parse_target("w1:main") == ("w1", "main")
    assert parse_target("w1:") == ("w1", "")
    assert parse_target("w1", default_window="main") == ("w1", "main")
    with pytest.raises(BeshError):
        parse_target("")


def test_human_helpers():
    assert human_size(1536) == "1.5KB"
    assert human_time("") == "-"


def test_target_str():
    assert str(parse_target("w1:main")) == "w1:main"
    assert str(parse_target("w1")) == "w1"
