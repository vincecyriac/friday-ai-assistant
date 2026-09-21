import re


def test_version_is_semver():
    import friday

    assert re.fullmatch(r"\d+\.\d+\.\d+", friday.__version__)
