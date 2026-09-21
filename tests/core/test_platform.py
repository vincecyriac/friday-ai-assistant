import json

import pytest

from friday.core.platform import PlatformInfo, detect, normalise_arch


@pytest.mark.parametrize(
    "machine,expected",
    [("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64"), ("armv7l", "other")],
)
def test_normalise_arch(machine, expected):
    assert normalise_arch(machine) == expected


def test_detect_on_this_host():
    info = detect()
    assert isinstance(info, PlatformInfo)
    assert info.system
    assert info.hostname
    assert info.python
    assert info.arch_family in ("arm64", "x86_64", "other")
    assert info.summary == f"{info.system}/{info.arch_family}"
    json.dumps(info.to_dict())


def test_raspberry_pi_detected_from_device_tree(tmp_path):
    model = tmp_path / "proc" / "device-tree" / "model"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"Raspberry Pi 4 Model B Rev 1.4\x00")
    assert detect(sys_root=tmp_path).is_raspberry_pi is True


def test_not_pi_when_model_absent(tmp_path):
    assert detect(sys_root=tmp_path).is_raspberry_pi is False


def test_distro_from_os_release(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "os-release").write_text(
        'NAME="Fedora Linux"\nPRETTY_NAME="Fedora Linux 42 (Server Edition)"\nID=fedora\n'
    )
    assert detect(sys_root=tmp_path).distro == "Fedora Linux 42 (Server Edition)"


def test_distro_none_when_os_release_absent(tmp_path):
    assert detect(sys_root=tmp_path).distro is None
