from fps_booster import icons


def test_letter_icon_is_square_rgba_and_stable():
    a = icons.letter_icon("Discord.exe")
    assert a.mode == "RGBA"
    assert a.size == (icons.ICON_PX, icons.ICON_PX)
    assert a.tobytes() == icons.letter_icon("discord.exe").tobytes()


def test_letter_icon_handles_odd_names():
    for name in ("", "___", "7zFM.exe", "日本.exe"):
        assert icons.letter_icon(name).size == (icons.ICON_PX, icons.ICON_PX)


def test_get_icon_caches_and_falls_back(tmp_path):
    missing = str(tmp_path / "nope.exe")
    first = icons.get_icon("nope.exe", missing)
    assert first is icons.get_icon("NOPE.exe", missing)
    assert first.size == (icons.ICON_PX, icons.ICON_PX)
