import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "image"))
import image_digest  # noqa: E402


def make_tree(root: Path):
    (root / "bin").mkdir()
    (root / "bin/tool").write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(root / "bin/tool", 0o755)
    os.symlink("bin/tool", root / "entry")
    (root / "etc").mkdir()
    (root / "etc/empty").write_bytes(b"")
    (root / "etc/z-last").write_bytes(b"z")
    (root / "etc/A-first").write_bytes(b"A")


def test_rootfs_and_tar_digests_agree_and_are_tamper_sensitive(tmp_path):
    a = tmp_path / "a"; a.mkdir(); make_tree(a)
    b = tmp_path / "b"; b.mkdir(); make_tree(b)
    da = image_digest.digest_rootfs(str(a))
    assert da == image_digest.digest_rootfs(str(b))
    tar = tmp_path / "a.tar"
    with tarfile.open(tar, "w") as tf:
        for name in sorted(os.listdir(a)):
            tf.add(a / name, arcname=name)
    assert image_digest.digest_tar(str(tar)) == da
    (b / "bin/tool").write_bytes(b"#!/bin/sh\necho pwned\n")
    assert image_digest.digest_rootfs(str(b)) != da
    c = tmp_path / "c"; c.mkdir(); make_tree(c)
    os.chmod(c / "bin/tool", 0o777)
    assert image_digest.digest_rootfs(str(c)) != da


def test_tar_hardlinks_and_docker_style_paths(tmp_path):
    tar = tmp_path / "x.tar"
    with tarfile.open(tar, "w") as tf:
        d = tarfile.TarInfo("./usr/"); d.type = tarfile.DIRTYPE; d.mode = 0o755; tf.addfile(d)
        f = tarfile.TarInfo("./usr/one"); f.size = 3; f.mode = 0o644; tf.addfile(f, __import__("io").BytesIO(b"abc"))
        l = tarfile.TarInfo("./usr/two"); l.type = tarfile.LNKTYPE; l.linkname = "./usr/one"; l.mode = 0o644; tf.addfile(l)
    root = tmp_path / "r"; root.mkdir(); (root / "usr").mkdir(); os.chmod(root / "usr", 0o755)
    (root / "usr/one").write_bytes(b"abc"); (root / "usr/two").write_bytes(b"abc")
    os.chmod(root / "usr/one", 0o644); os.chmod(root / "usr/two", 0o644)
    assert image_digest.digest_tar(str(tar)) == image_digest.digest_rootfs(str(root))


RUNNER_NODE = os.environ.get("RUNNER_NODE_BIN")


@pytest.mark.skipif(not RUNNER_NODE, reason="RUNNER_NODE_BIN not set")
def test_matches_runner_node_image_digest(tmp_path):
    root = tmp_path / "r"; root.mkdir(); make_tree(root)
    out = subprocess.run([RUNNER_NODE, "image-digest", str(root)], check=True, capture_output=True, text=True).stdout.strip()
    assert out.split()[-1].lower().lstrip("0x") == image_digest.digest_rootfs(str(root)).hex()
