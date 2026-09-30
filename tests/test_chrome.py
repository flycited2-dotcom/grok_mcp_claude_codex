import asyncio

import pytest

from grok_imagine_mcp.chrome import build_command, ensure_chrome, find_chrome, profile_owns_port
from grok_imagine_mcp.drivers import DriverError

OURS = lambda profile, port: True       # noqa: E731  окно на порту принадлежит профилю
FOREIGN = lambda profile, port: False   # noqa: E731  на порту отвечает чужой браузер


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
    assert find_chrome({"ProgramFiles": str(tmp_path)}, fallbacks=()) is None


def test_find_chrome_falls_back_to_default_install_dirs_when_the_env_is_stripped(tmp_path):
    """Клиент MCP может запустить сервер без ProgramFiles в окружении: Chrome всё равно установлен там, где всегда."""
    installed = _exe(tmp_path, "pf", "Google", "Chrome", "Application", "chrome.exe")
    assert find_chrome({}, fallbacks=(tmp_path / "нет" / "chrome.exe", installed)) == installed
    assert find_chrome({}, fallbacks=(tmp_path / "нет" / "chrome.exe",)) is None


def test_command_has_port_profile_app_window_and_no_headless(tmp_path):
    cmd = build_command(tmp_path / "chrome.exe", 9340, tmp_path / "prof")
    assert cmd[0] == str(tmp_path / "chrome.exe")
    assert "--remote-debugging-port=9340" in cmd
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert f"--user-data-dir={tmp_path / 'prof'}" in cmd
    assert "--app=https://grok.com/imagine" in cmd
    assert "--disable-background-timer-throttling" in cmd
    # без этого окно, закрытое другим окном, считается скрытым и Grok не рисует в нём меню
    assert "--disable-features=CalculateNativeWinOcclusion" in cmd
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

    reused = asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=spawned.append,
                                       owns=OURS))
    assert reused is True and spawned == []


def test_ownership_is_verified_once_not_on_every_generation(tmp_path):
    """Проверка владельца порта стоит секунд (запуск PowerShell): подтверждённое окно не перепроверяем 10 минут."""
    asked, clock = [], Clock()

    async def probe(port):
        return {"Browser": "Chrome/154"}

    def owns(profile, port):
        asked.append(port)
        return True

    for _ in range(3):
        assert asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, now=clock.now,
                                         owns=owns)) is True
    assert asked == [9340]
    clock.t += 601                                                      # срок вышел: проверяем снова
    asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, now=clock.now, owns=owns))
    assert asked == [9340, 9340]


def test_a_failed_ownership_check_is_never_cached(tmp_path):
    asked = []

    async def probe(port):
        return {"Browser": "Edge/154"}

    def owns(profile, port):
        asked.append(port)
        return False

    for _ in range(2):
        with pytest.raises(DriverError):
            asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, owns=owns))
    assert len(asked) == 2


def test_a_foreign_browser_on_the_port_is_refused_and_no_second_chrome_is_started(tmp_path):
    spawned = []

    async def probe(port):
        return {"Browser": "Edge/154"}

    with pytest.raises(DriverError, match="не относится к профилю"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=spawned.append,
                                  owns=FOREIGN))
    assert spawned == []


def test_profile_owns_the_port_only_when_the_listener_runs_with_that_user_data_dir(tmp_path):
    """Chrome 154 при фиксированном порте не пишет DevToolsActivePort (проверено живьём): опознаём по командной
    строке процесса, слушающего порт."""
    prof = tmp_path / "chrome-profile"
    own = f'"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9340 --user-data-dir="{prof}" --app=https://grok.com/imagine'
    other = '"chrome.exe" --remote-debugging-port=9340 --user-data-dir="D:\\other\\profile"'
    no_dir = '"chrome.exe" --remote-debugging-port=9340'
    unquoted = f"chrome.exe --remote-debugging-port=9340 --user-data-dir={prof}"

    def owns(cmd):
        return profile_owns_port(prof, 9340, command_line=lambda port: cmd)

    assert owns(own) is True and owns(unquoted) is True
    assert owns(other) is False                                         # чужой профиль на нашем порту
    assert owns(no_dir) is False                                        # профиль по умолчанию: точно не наш
    assert owns(None) is True                                           # узнать не удалось (не Windows): не мешаем


def test_missing_window_is_launched_and_waited_for(tmp_path):
    spawned, answers = [], [None, None, {"Browser": "Chrome/154"}]
    clock = Clock()

    async def probe(port):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    chrome = _exe(tmp_path, "chrome.exe")
    reused = asyncio.run(ensure_chrome(9340, tmp_path / "prof", chrome, probe=probe, spawn=spawned.append,
                                       sleep=clock.sleep, now=clock.now, owns=OURS))
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
                                  sleep=clock.sleep, now=clock.now, timeout_s=5, owns=OURS))


def test_port_taken_by_a_foreign_browser_after_launch_is_reported_as_foreign_not_as_ours(tmp_path):
    clock, state = Clock(), {"spawned": False}

    async def probe(port):                 # наш Chrome не смог занять порт: после запуска отвечает чужой
        return {"Browser": "Edge/154"} if state["spawned"] else None

    with pytest.raises(DriverError, match="не относится к профилю"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe,
                                  spawn=lambda c: state.update(spawned=True), sleep=clock.sleep, now=clock.now,
                                  timeout_s=5, owns=FOREIGN))
