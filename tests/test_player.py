"""Finding a sound player. Nothing here plays a sound or looks at a real machine."""

from speak.player import Player, candidates, resolve_player


def installed(*names: str):
    """A `which` that reports exactly these binaries as present."""
    present = set(names)
    return lambda binary: f"/usr/bin/{binary}" if binary in present else None


def test_macos_uses_afplay():
    assert resolve_player("darwin", installed("afplay", "aplay")) == Player("afplay")


def test_a_mac_without_afplay_still_finds_a_portable_player():
    """Unusual, but trying the rest afterwards costs nothing."""
    player = resolve_player("darwin", installed("ffplay"))
    assert player is not None
    assert player.binary == "ffplay"


def test_linux_prefers_the_sound_server_over_raw_alsa():
    assert resolve_player("linux", installed("paplay", "aplay")).binary == "paplay"
    assert resolve_player("linux", installed("aplay")).binary == "aplay"


def test_an_unknown_platform_falls_back_to_the_portable_list():
    assert resolve_player("freebsd13", installed("mpv")).binary == "mpv"


def test_nothing_installed_is_reported_rather_than_guessed():
    assert resolve_player("linux", installed()) is None


def test_windows_uses_powershell():
    player = resolve_player("win32", installed("powershell"))
    assert player is not None
    assert player.binary == "powershell"
    assert player.script is not None


def test_windows_falls_back_to_pwsh():
    assert resolve_player("win32", installed("pwsh")).binary == "pwsh"


def test_a_plain_player_takes_the_file_as_its_last_argument():
    command, args = Player("aplay", ("-q",)).command_for("/tmp/cue.wav")
    assert command == "aplay"
    assert args == ["-q", "/tmp/cue.wav"]


def test_powershell_puts_the_file_inside_the_script():
    """It has no trailing-file form, which is the only reason `script` exists."""
    player = resolve_player("win32", installed("powershell"))
    _command, args = player.command_for(r"C:\\Sounds\\cue.wav")
    assert args[-1] == r"(New-Object Media.SoundPlayer 'C:\\Sounds\\cue.wav').PlaySync()"
    assert args[:-1] == ["-NoProfile", "-NonInteractive", "-Command"]


def test_a_quote_in_the_path_cannot_end_the_script_early():
    """Single quotes delimit the path, so one in a filename would close the string
    and leave the rest of the name as PowerShell to execute."""
    player = resolve_player("win32", installed("powershell"))
    _command, args = player.command_for("C:\\it's\\cue.wav")
    assert args[-1] == "(New-Object Media.SoundPlayer 'C:\\it''s\\cue.wav').PlaySync()"


def test_every_platform_has_something_to_try():
    for platform in ("darwin", "win32", "linux", "something-else"):
        assert candidates(platform), platform
