"""CIP-10 rootfs digest: the runner's `rootfs_digest` (runner-container/src/image.rs)
in Python, so an image can be digested where it is built and verified where it
is deployed without the runner binary.

blake3 over a sorted walk; per entry
    rel_path ‖ NUL ‖ st_mode (u32 LE) ‖ NUL ‖ (file content | symlink target | recurse) ‖ NUL

    python3 image_digest.py --tar rootfs.tar[.gz]   # what the box will extract
    python3 image_digest.py --rootfs /var/lib/cowboy/images/<digest>/rootfs
"""

import argparse
import os
import posixpath
import stat
import sys
import tarfile

import blake3


class _Node:
    __slots__ = ("mode", "kind", "content", "target", "children")

    def __init__(self, mode, kind, content=b"", target="", children=None):
        self.mode, self.kind, self.content, self.target = mode, kind, content, target
        self.children = children if children is not None else {}


def _walk(h, base, entries):
    """entries: list of (name, node); sorted by name bytes like the runner's walk."""
    for name, node in sorted(entries, key=lambda kv: kv[0].encode()):
        rel = posixpath.join(base, name) if base else name
        h.update(rel.encode())
        h.update(b"\0")
        h.update((node.mode & 0xFFFFFFFF).to_bytes(4, "little"))
        h.update(b"\0")
        if node.kind == "symlink":
            h.update(node.target.encode())
        elif node.kind == "file":
            h.update(node.content)
        elif node.kind == "dir":
            _walk(h, rel, list(node.children.items()))
        h.update(b"\0")


def digest_rootfs(root: str) -> bytes:
    def load(path):
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return _Node(st.st_mode, "symlink", target=os.readlink(path))
        if stat.S_ISREG(st.st_mode):
            with open(path, "rb") as f:
                return _Node(st.st_mode, "file", content=f.read())
        if stat.S_ISDIR(st.st_mode):
            return _Node(st.st_mode, "dir", children={n: load(os.path.join(path, n)) for n in os.listdir(path)})
        return _Node(st.st_mode, "other")

    h = blake3.blake3()
    _walk(h, "", list(load(root).children.items()))
    return h.digest()


def digest_tar(path: str) -> bytes:
    """Digest of the tree a root extraction of the archive produces (modes as
    stored; hard links resolve to their target's content)."""
    root = _Node(0o40755, "dir")
    files = {}

    def place(name):
        parts = [p for p in name.split("/") if p not in ("", ".")]
        node = root
        for part in parts[:-1]:
            node = node.children.setdefault(part, _Node(0o40755, "dir"))
        return node, parts[-1] if parts else None

    with tarfile.open(path, "r:*") as tf:
        for m in tf:
            parent, leaf = place(m.name)
            if leaf is None:
                root.mode = 0o40000 | (m.mode & 0o7777)
                continue
            perm = m.mode & 0o7777
            if m.isdir():
                node = parent.children.get(leaf)
                if node is None:
                    node = parent.children[leaf] = _Node(0, "dir")
                node.mode = stat.S_IFDIR | perm
            elif m.issym():
                parent.children[leaf] = _Node(stat.S_IFLNK | perm, "symlink", target=m.linkname)
            elif m.isfile():
                content = tf.extractfile(m).read()
                files[m.name] = content
                parent.children[leaf] = _Node(stat.S_IFREG | perm, "file", content=content)
            elif m.islnk():
                parent.children[leaf] = _Node(stat.S_IFREG | perm, "file", content=files[m.linkname])
            elif m.ischr():
                parent.children[leaf] = _Node(stat.S_IFCHR | perm, "other")
            elif m.isblk():
                parent.children[leaf] = _Node(stat.S_IFBLK | perm, "other")
            elif m.isfifo():
                parent.children[leaf] = _Node(stat.S_IFIFO | perm, "other")
            else:
                raise ValueError(f"unsupported tar entry {m.name!r} type {m.type!r}")
    h = blake3.blake3()
    _walk(h, "", list(root.children.items()))
    return h.digest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--tar")
    src.add_argument("--rootfs")
    args = parser.parse_args(argv)
    print((digest_tar(args.tar) if args.tar else digest_rootfs(args.rootfs)).hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
