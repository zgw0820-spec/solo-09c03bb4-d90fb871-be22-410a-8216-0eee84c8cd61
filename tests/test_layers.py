"""层合并语义测试：whiteout / opaque / 同层新增 / 多层层序。"""

from __future__ import annotations

import gzip
import io
import tarfile

from app.layers import (
    MaskReason,
    NodeKind,
    merge_layers,
)
from app.oci import LayerBlob


def _tar(entries: list[tuple[str, int, str]]) -> bytes:
    """entries: (name, kind 0=dir 1=file 2=symlink, payload)"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name=name)
            info.mtime = 0
            if kind == 0:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == 1:
                info.type = tarfile.REGTYPE
                info.mode = 0o644
                data = payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
    return buf.getvalue()


def _layer(tmp_path, index: int, tar_bytes: bytes, gz: bool = True) -> LayerBlob:
    raw_name = f"layer{index}.tar" + (".gz" if gz else "")
    path = tmp_path / raw_name
    data = gzip.compress(tar_bytes, mtime=0) if gz else tar_bytes
    path.write_bytes(data)
    import hashlib

    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    diff_id = "sha256:" + hashlib.sha256(tar_bytes).hexdigest()
    return LayerBlob(
        digest=digest, diff_id=diff_id, media_type="application/vnd.oci.image.layer.v1.tar+gzip",
        path=path, index=index,
    )


def _get(root, path: str):
    cursor = root
    for part in [p for p in path.split("/") if p]:
        assert cursor.kind == NodeKind.DIRECTORY, path
        cursor = cursor.children[part]
    return cursor


def test_whiteout_masks_lower_but_same_layer_addition_visible(tmp_path):
    l0 = _tar([
        ("etc/", 0, ""),
        ("etc/app/", 0, ""),
        ("etc/app/secret.env", 1, "lower"),
        ("etc/app/config.yaml", 1, "v0"),
    ])
    l1 = _tar([
        ("etc/app/.wh.secret.env", 1, ""),
        ("etc/app/config.yaml", 1, "v1"),
        ("etc/app/new.txt", 1, "new"),
    ])
    tree = merge_layers((_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1)))

    secret = _get(tree, "/etc/app/secret.env")
    assert secret.kind == NodeKind.TOMBSTONE
    assert secret.mask.reason == MaskReason.WHITEOUT
    assert secret.mask.source.layer_index == 1

    assert _get(tree, "/etc/app/config.yaml").source.layer_index == 1
    assert _get(tree, "/etc/app/new.txt").source.layer_index == 1


def test_whiteout_then_recreate_in_same_layer(tmp_path):
    l0 = _tar([("a.txt", 1, "lower")])
    # tar 内顺序：先 whiteout，再同名重建
    l1 = _tar([(".wh.a.txt", 1, ""), ("a.txt", 1, "recreated")])
    tree = merge_layers((_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1)))
    node = _get(tree, "/a.txt")
    assert node.kind == NodeKind.FILE
    assert node.source.layer_index == 1


def test_opaque_masks_children_but_same_layer_additions_survive(tmp_path):
    l0 = _tar([
        ("d/", 0, ""),
        ("d/old1", 1, "x"),
        ("d/old2", 1, "y"),
        ("d/sub/", 0, ""),
        ("d/sub/deep", 1, "z"),
    ])
    l1 = _tar([
        ("d/", 0, ""),
        ("d/.wh..wh..opq", 1, ""),
        ("d/fresh", 1, "ok"),
    ])
    tree = merge_layers((_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1)))

    for name in ("old1", "old2", "sub"):
        node = _get(tree, f"/d/{name}")
        assert node.kind == NodeKind.TOMBSTONE, name
        assert node.mask.reason == MaskReason.OPAQUE
        assert node.mask.opaque_dir == "/d"
        assert node.mask.source.layer_index == 1

    fresh = _get(tree, "/d/fresh")
    assert fresh.kind == NodeKind.FILE
    assert fresh.source.layer_index == 1


def test_opaque_then_whiteout_in_higher_layer(tmp_path):
    l0 = _tar([("d/", 0, ""), ("d/a", 1, "x"), ("d/b", 1, "y")])
    l1 = _tar([("d/", 0, ""), ("d/.wh..wh..opq", 1, ""), ("d/a", 1, "new-a")])
    l2 = _tar([("d/.wh.a", 1, "")])
    tree = merge_layers(
        (_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1), _layer(tmp_path, 2, l2))
    )
    node = _get(tree, "/d/a")
    assert node.kind == NodeKind.TOMBSTONE
    # 最高层的 whiteout 是直接遮蔽原因
    assert node.mask.reason == MaskReason.WHITEOUT
    assert node.mask.source.layer_index == 2


def test_symlink_merged_with_target_metadata(tmp_path):
    l0 = _tar([
        ("etc/", 0, ""),
        ("etc/ln", 2, "../target"),
        ("target", 1, "data"),
    ])
    tree = merge_layers((_layer(tmp_path, 0, l0),))
    ln = _get(tree, "/etc/ln")
    assert ln.kind == NodeKind.SYMLINK
    assert ln.link_target == "../target"


def test_plain_tar_layer_without_gzip(tmp_path):
    l0 = _tar([("f", 1, "plain")])
    tree = merge_layers((_layer(tmp_path, 0, l0, gz=False),))
    assert _get(tree, "/f").kind == NodeKind.FILE


def test_illegal_member_path_rejected(tmp_path):
    l0 = _tar([("../escape", 1, "x")])
    import pytest

    with pytest.raises(ValueError, match="非法"):
        merge_layers((_layer(tmp_path, 0, l0),))


def test_sample_image_full_semantics(merged_tree):
    """样例镜像：whiteout、opaque、同层新增与层序来源一起核对。"""
    secret = _get(merged_tree, "/etc/app/secret.env")
    assert secret.kind == NodeKind.TOMBSTONE
    assert secret.mask.reason == MaskReason.WHITEOUT
    assert secret.mask.source.digest.startswith("sha256:")

    cfg = _get(merged_tree, "/etc/app/config.yaml")
    assert cfg.kind == NodeKind.FILE
    assert cfg.source.layer_index == 1  # 被层 1 覆盖

    old = _get(merged_tree, "/etc/app/legacy/old.conf")
    assert old.kind == NodeKind.TOMBSTONE
    assert old.mask.reason == MaskReason.OPAQUE

    keep = _get(merged_tree, "/etc/app/legacy/keep.txt")
    assert keep.kind == NodeKind.FILE
    assert keep.source.layer_index == 2
