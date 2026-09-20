import importlib.util
import sys
import types
from pathlib import Path


def load_pyupdate_module(monkeypatch):
    tkinter = types.ModuleType("tkinter")

    class DummyWidget:
        pass

    tkinter.Tk = DummyWidget
    tkinter.Frame = DummyWidget
    tkinter.Button = DummyWidget
    tkinter.Label = DummyWidget

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.askyesno = lambda *args, **kwargs: True

    scrolledtext = types.ModuleType("tkinter.scrolledtext")
    scrolledtext.ScrolledText = DummyWidget

    ttk = types.ModuleType("tkinter.ttk")
    ttk.Progressbar = DummyWidget

    tkinter.messagebox = messagebox
    tkinter.scrolledtext = scrolledtext
    tkinter.ttk = ttk

    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    monkeypatch.setitem(sys.modules, "tkinter.scrolledtext", scrolledtext)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)

    module_path = Path(__file__).resolve().parents[1] / "pyupdate.py"
    spec = importlib.util.spec_from_file_location("pyupdate", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_version_comparison_prefers_newer_release(monkeypatch):
    pyupdate = load_pyupdate_module(monkeypatch)

    assert pyupdate.Version("v1.4.3") > pyupdate.Version("1.4.2")
    assert pyupdate.Version("1.4.2-beta") < pyupdate.Version("1.4.2")
    assert pyupdate.is_newer("v1.5.0", "1.4.2")


def test_release_asset_selection_uses_platform_hints_and_checksums(monkeypatch):
    pyupdate = load_pyupdate_module(monkeypatch)

    assets = [
        pyupdate.ReleaseAsset("pyupdate-macos.zip", "https://example.invalid/mac", 10),
        pyupdate.ReleaseAsset("checksums.txt", "https://example.invalid/sums", 20),
        pyupdate.ReleaseAsset("pyupdate-linux.tar.gz", "https://example.invalid/linux", 30),
    ]

    selected = pyupdate.GitHubReleaseChecker.pick_asset(assets, pyupdate.ASSET_HINTS["linux"])

    assert selected.name == "pyupdate-linux.tar.gz"
    assert pyupdate.GitHubReleaseChecker.find_checksums_asset(assets).name == "checksums.txt"
