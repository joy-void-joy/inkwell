"""Inkwell's own ``profiles/`` directory, as the origin a launch selects from.

An inkwell profile is a directory, not a registry entry: its env file, the
Google credentials it earned, and the Claude configuration home the writing
agent runs under all sit under ``profiles/<name>/``. Making that directory the
origin is what keeps one name meaning one account whether it is spelled to
``inkwell --profile``, to the setup wizard, or to a native launch — there is
no second list to register a profile in, and so none to fall out of step.
"""

from pathlib import Path

from lup.runtime.profiles import ProfileDirectory, ProfileStore

import inkwell.agent.config as config
from inkwell.agent.client import PROVIDER_LOGIN
from inkwell.devtools.setup import (
    PROFILES_DIR,
    claude_config_dir_for_profile,
    env_file_for_profile,
    list_profiles,
)


class InkwellProfileStore(ProfileStore):
    """The project's profile directories, answered as named accounts."""

    def known(self, name: str) -> str:
        """Return name, or raise for one no profile directory answers to."""
        if name not in list_profiles():
            raise KeyError(f"unknown inkwell profile {name!r}")
        return name

    def names(self) -> list[str]:
        return list_profiles()

    def config_dir_for(self, name: str) -> Path:
        return claude_config_dir_for_profile(self.known(name))

    def active_profile(self) -> str | None:
        return config.active_profile()

    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        """Start a profile directory, for the setup wizard to fill in.

        Where its configuration home sits is derived from the name, so a
        caller asking for one elsewhere is asking for something an inkwell
        profile cannot be, rather than for a variation on one.
        """
        home = claude_config_dir_for_profile(name)
        if config_dir is not None and config_dir != home:
            raise ValueError(
                f"an inkwell profile keeps its configuration home at {home}, "
                f"which is derived from the name — {config_dir} cannot be one"
            )
        env_file = env_file_for_profile(name)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch()
        home.mkdir(parents=True, exist_ok=True)
        return home

    def set_active(self, name: str) -> None:
        config.ACTIVE_PROFILE_FILE.write_text(f"{self.known(name)}\n", "utf-8")

    def remove_profile(self, name: str) -> None:
        """Refuse, because forgetting one here would mean deleting it.

        Nothing registers an inkwell profile, so there is no registration to
        drop: the directory is the profile, and it holds the credentials and
        the login that profile earned.
        """
        raise ValueError(
            f"an inkwell profile is the directory {PROFILES_DIR / self.known(name)}, "
            "which holds its credentials — remove that directory to remove it"
        )


def inkwell_profile_directory() -> ProfileDirectory:
    """The profile surface every inkwell entry point selects through."""
    return ProfileDirectory(InkwellProfileStore(), PROVIDER_LOGIN)
