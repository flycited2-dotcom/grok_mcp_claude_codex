import asyncio

import pytest

from grok_imagine_mcp.chrome import build_command, ensure_chrome, find_chrome
from grok_imagine_mcp.drivers import DriverError


def _exe(root, *parts):
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_find_chrome_prefers_explicit_path(tmp_path):
    explicit = _exe(tmp_path, "custom", "chrome.exe")
    assert find_chrome({"GROK_CHROME_PATH": str(explicit)}) == explicit


def test_find_chrome_explicit_but_missing_is_none_not_a_guess(tmp_path):
    _exe(tmp_path, "pf", "Google", "Chrome", "Application", "chrome.exe")
    assert find_chrome({"GROK_CHROME_PATH": str(tmp_path / "нет.exe"), "ProgramFiles": str(tmp_path / "pf")}) is None


def test_find_chrome_looks_in_standard_places(tmp_path):
    found = _exe(tmp_path, "pf86", "Google", "Chrome", "Application", "chrome.exe")
    assert find_chrome({"ProgramFiles": str(tmp_path / "pf"), "ProgramFiles(x86)": str(tmp_path / "pf86")}) == found


def test_find_chrome_none_when_not_installed(tmp_path):
    assert find_chrome({"ProgramFiles": str(tmp_path)}) is None


def test_command_has_port_profile_app_window_and_no_headless(tmp_path):
    cmd = build_command(tmp_path / "chrome.exe", 9340, tmp_path / "prof")
    assert cmd[0] == str(tmp_path / "chrome.exe")
    assert "--remote-debugging-port=9340" in cmd
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert f"--user-data-dir={tmp_path / 'prof'}" in cmd
    assert "--app=https://grok.com/imagine" in cmd
    assert "--disable-background-timer-throttling" in cmd
    assert not any("headless" in a for a in cmd)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def test_existing_window_is_reused_and_nothing_is_spawned(tmp_path):
    spawned = []

    async def probe(port):
        return {"Browser": "Chrome/154"}

    reused = asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=spawned.append))
    assert reused is True and spawned == []


def test_missing_window_is_launched_and_waited_for(tmp_path):
    spawned, answers = [], [None, None, {"Browser": "Chrome/154"}]
    clock = Clock()

    async def probe(port):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    chrome = _exe(tmp_path, "chrome.exe")
    reused = asyncio.run(ensure_chrome(9340, tmp_path / "prof", chrome, probe=probe, spawn=spawned.append,
                                       sleep=clock.sleep, now=clock.now))
    assert reused is False
    assert len(spawned) == 1 and "--remote-debugging-port=9340" in spawned[0]
    assert (tmp_path / "prof").is_dir()


def test_chrome_not_installed_is_a_clear_error(tmp_path):
    async def probe(port):
        return None

    with pytest.raises(DriverError, match="GROK_CHROME_PATH"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", None, probe=probe, spawn=lambda c: None))


def test_port_that_never_answers_points_to_port_setting(tmp_path):
    clock = Clock()

    async def probe(port):
        return None

    with pytest.raises(DriverError, match="GROK_WEB_PORT"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=lambda c: None,
                                  sleep=clock.sleep, now=clock.now, timeout_s=5))
