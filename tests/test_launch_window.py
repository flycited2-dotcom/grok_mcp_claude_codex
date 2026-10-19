from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.launch_window import main


def test_uses_port_and_profile_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_STATE_DIR", str(tmp_path))
    seen = {}

    async def ensure(port, profile, chrome):
        seen.update(port=port, profile=profile)
        return False

    assert main({"GROK_WEB_PORT": "9555"}, ensure=ensure) == 0
    assert seen == {"port": 9555, "profile": tmp_path / "chrome-profile"}


def test_failure_is_reported_with_exit_code_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GROK_STATE_DIR", str(tmp_path))

    async def ensure(port, profile, chrome):
        raise DriverError("Chrome не найден")

    assert main({}, ensure=ensure) == 1
    assert "Chrome не найден" in capsys.readouterr().err
