"""Smoke tests for athenaeum package."""


def test_import():
    import athenaeum

    assert hasattr(athenaeum, "__version__")


def test_version_format():
    from athenaeum import __version__

    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_py_typed_marker():
    import importlib.resources

    marker = importlib.resources.files("athenaeum") / "py.typed"
    assert marker.is_file()
