# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for rclone binary resolution

# %%
#|default_exp unit._utils.test_rclone_resolution

# %%
#|export
import stat
import pytest
from pathlib import Path
from unittest.mock import patch, Mock

from boxyard import const
import boxyard._utils.rclone as rclone_mod
from boxyard._utils.rclone import _resolve_rclone_binary, get_rclone_binary


# %%
#|export
def _make_fake_rclone(dir_path: Path) -> Path:
    """Create an executable fake `rclone` binary inside dir_path and return its path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    binpath = dir_path / "rclone"
    binpath.write_text("#!/bin/sh\necho fake\n")
    binpath.chmod(binpath.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binpath


def _no_config(monkeypatch, tmp_path):
    """Point BOXYARD_CONFIG_PATH at a nonexistent file so config resolution returns None."""
    monkeypatch.setenv(const.ENV_VAR_BOXYARD_CONFIG_PATH, str(tmp_path / "nonexistent.toml"))


# %%
#|export
class TestResolveRcloneBinary:
    """Tests for the (uncached) rclone binary resolver."""

    def test_env_var_honored(self, tmp_path, monkeypatch):
        """BOXYARD_RCLONE, when it points at an executable, wins over everything else."""
        fake = _make_fake_rclone(tmp_path / "custom")
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_RCLONE, str(fake))
        # Even with a (would-be) PATH hit, the env var takes priority.
        with patch("boxyard._utils.rclone.shutil.which", return_value="/usr/bin/rclone"):
            assert _resolve_rclone_binary() == str(fake)

    def test_env_var_invalid_raises(self, tmp_path, monkeypatch):
        """BOXYARD_RCLONE pointing at a non-executable raises a loud, named error."""
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_RCLONE, str(tmp_path / "does_not_exist"))
        with pytest.raises(RuntimeError, match=const.ENV_VAR_BOXYARD_RCLONE):
            _resolve_rclone_binary()

    def test_config_rclone_path_honored(self, tmp_path, monkeypatch):
        """A `rclone_path` key in config.toml is used when no env var is set."""
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_RCLONE, raising=False)
        fake = _make_fake_rclone(tmp_path / "from_config")
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'rclone_path = "{fake}"\n')
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_CONFIG_PATH, str(config_file))
        with patch("boxyard._utils.rclone.shutil.which", return_value="/usr/bin/rclone"):
            assert _resolve_rclone_binary() == str(fake)

    def test_config_rclone_path_invalid_raises(self, tmp_path, monkeypatch):
        """A `rclone_path` pointing at a non-executable raises a loud, named error."""
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_RCLONE, raising=False)
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'rclone_path = "{tmp_path / "missing"}"\n')
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_CONFIG_PATH, str(config_file))
        with pytest.raises(RuntimeError, match="rclone_path"):
            _resolve_rclone_binary()

    def test_which_used(self, tmp_path, monkeypatch):
        """Falls through to shutil.which (the caller's PATH) when env + config are absent."""
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_RCLONE, raising=False)
        _no_config(monkeypatch, tmp_path)
        with patch("boxyard._utils.rclone.shutil.which", return_value="/path/from/which/rclone"):
            assert _resolve_rclone_binary() == "/path/from/which/rclone"

    def test_fallback_dirs_used(self, tmp_path, monkeypatch):
        """Falls through to the known install dirs when not on PATH."""
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_RCLONE, raising=False)
        _no_config(monkeypatch, tmp_path)
        fake = _make_fake_rclone(tmp_path / "fallback")
        with patch("boxyard._utils.rclone.shutil.which", return_value=None):
            monkeypatch.setattr(rclone_mod, "_RCLONE_FALLBACK_DIRS", [str(tmp_path / "fallback")])
            assert _resolve_rclone_binary() == str(fake)

    def test_not_found_raises_loud_naming_locations(self, tmp_path, monkeypatch):
        """When rclone is nowhere, the error names every place searched and how to fix it."""
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_RCLONE, raising=False)
        _no_config(monkeypatch, tmp_path)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with patch("boxyard._utils.rclone.shutil.which", return_value=None):
            monkeypatch.setattr(rclone_mod, "_RCLONE_FALLBACK_DIRS", [str(empty_dir)])
            with pytest.raises(RuntimeError) as excinfo:
                _resolve_rclone_binary()
        msg = str(excinfo.value)
        # Names every searched location + actionable remediation.
        assert const.ENV_VAR_BOXYARD_RCLONE in msg
        assert "config.toml" in msg
        assert "PATH" in msg
        assert str(empty_dir / "rclone") in msg
        assert "install" in msg.lower()


# %%
#|export
class TestGetRcloneBinaryCaching:
    """Tests for the cached accessor."""

    def test_caches_resolution(self, monkeypatch):
        """get_rclone_binary resolves once and caches the result."""
        monkeypatch.setattr(rclone_mod, "_rclone_binary", None)
        resolver = Mock(return_value="/cached/rclone")
        monkeypatch.setattr(rclone_mod, "_resolve_rclone_binary", resolver)

        first = get_rclone_binary()
        second = get_rclone_binary()

        assert first == "/cached/rclone"
        assert second == "/cached/rclone"
        resolver.assert_called_once()
