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


def test_whiteout_and_rebuild_directory_in_same_layer(tmp_path):
    """同层先 whiteout 整目录、再重建并补回部分文件：补回的可见，其余仍报 whiteout。"""
    l0 = _tar([
        ("d/", 0, ""),
        ("d/keep", 1, "k0"),
        ("d/lost", 1, "l0"),
        ("d/sub/", 0, ""),
        ("d/sub/x", 1, "x0"),
    ])
    l1 = _tar([
        (".wh.d", 1, ""),
        ("d/", 0, ""),
        ("d/keep", 1, "k1"),
        ("d/new", 1, "n1"),
    ])
    tree = merge_layers((_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1)))

    assert _get(tree, "/d/keep").source.layer_index == 1
    assert _get(tree, "/d/new").source.layer_index == 1

    for name in ("lost", "sub"):
        node = _get(tree, f"/d/{name}")
        assert node.kind == NodeKind.TOMBSTONE, name
        assert node.mask.reason == MaskReason.WHITEOUT
        assert node.mask.source.layer_index == 1


def test_whiteout_directory_then_rebuild_keeps_mask_for_unrestored(tmp_path):
    """层 1 whiteout 整目录、层 2 重建目录：没补回的旧条目仍指向层 1。"""
    l0 = _tar([
        ("conf/", 0, ""),
        ("conf/a.conf", 1, "a"),
        ("conf/b.conf", 1, "b"),
        ("conf/nested/", 0, ""),
        ("conf/nested/deep.conf", 1, "d"),
    ])
    l1 = _tar([(".wh.conf", 1, "")])
    l2 = _tar([
        ("conf/", 0, ""),
        ("conf/a.conf", 1, "a-restored"),
        ("conf/new.conf", 1, "n"),
    ])
    tree = merge_layers(
        (_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1), _layer(tmp_path, 2, l2))
    )

    restored = _get(tree, "/conf/a.conf")
    assert restored.kind == NodeKind.FILE
    assert restored.source.layer_index == 2

    for name in ("b.conf", "nested"):
        node = _get(tree, f"/conf/{name}")
        assert node.kind == NodeKind.TOMBSTONE, name
        assert node.mask.reason == MaskReason.WHITEOUT
        # 让它消失的是层 1 的整目录 whiteout，而不是重建它的层 2
        assert node.mask.source.layer_index == 1

    # 未补回目录本身是带最初遮蔽来源的墓碑，其历史快照仍可用于解释更深层文件
    nested = _get(tree, "/conf/nested")
    assert nested.kind == NodeKind.TOMBSTONE
    assert nested.mask.source.layer_index == 1
    assert nested.shadowed is not None
    assert nested.shadowed.children["deep.conf"].kind == NodeKind.FILE


def test_opaque_subdir_rebuilt_keeps_opaque_mask(tmp_path):
    """opaque 遮蔽子目录、后续层原地重建子目录：未补回条目仍报 opaque。"""
    l0 = _tar([("d/", 0, ""), ("d/sub/", 0, ""), ("d/sub/old", 1, "o")])
    l1 = _tar([("d/.wh..wh..opq", 1, "")])
    l2 = _tar([("d/sub/", 0, ""), ("d/sub/new", 1, "n")])
    tree = merge_layers(
        (_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1), _layer(tmp_path, 2, l2))
    )

    assert _get(tree, "/d/sub/new").source.layer_index == 2
    old = _get(tree, "/d/sub/old")
    assert old.kind == NodeKind.TOMBSTONE
    assert old.mask.reason == MaskReason.OPAQUE
    assert old.mask.source.layer_index == 1
    assert old.mask.opaque_dir == "/d"


def test_still_masked_after_repeated_rebuild_keeps_original_source(tmp_path):
    """遮蔽→重建→再遮蔽→再重建：从未恢复的条目一直保留最初的遮蔽层。"""
    l0 = _tar([("conf/", 0, ""), ("conf/never.conf", 1, "x")])
    l1 = _tar([(".wh.conf", 1, "")])
    l2 = _tar([("conf/", 0, ""), ("conf/recreated.conf", 1, "r2")])
    l3 = _tar([(".wh.conf", 1, "")])
    l4 = _tar([("conf/", 0, ""), ("conf/final.conf", 1, "r4")])
    tree = merge_layers(tuple(
        _layer(tmp_path, i, data)
        for i, data in enumerate((l0, l1, l2, l3, l4))
    ))

    never = _get(tree, "/conf/never.conf")
    assert never.kind == NodeKind.TOMBSTONE
    assert never.mask.source.layer_index == 1

    # 层 2 恢复过、层 3 再次遮蔽的条目指向层 3
    recreated = _get(tree, "/conf/recreated.conf")
    assert recreated.kind == NodeKind.TOMBSTONE
    assert recreated.mask.reason == MaskReason.WHITEOUT
    assert recreated.mask.source.layer_index == 3

    assert _get(tree, "/conf/final.conf").source.layer_index == 4


def test_later_opaque_does_not_override_never_restored_tombstone(tmp_path):
    """从未恢复的条目先被 whiteout、后又被 opaque 覆盖：保留最初的遮蔽来源。"""
    l0 = _tar([("d/", 0, ""), ("d/gone", 1, "x"), ("d/other", 1, "y")])
    l1 = _tar([("d/.wh.gone", 1, "")])
    l2 = _tar([("d/.wh..wh..opq", 1, "")])
    tree = merge_layers(
        (_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1), _layer(tmp_path, 2, l2))
    )
    gone = _get(tree, "/d/gone")
    assert gone.mask.reason == MaskReason.WHITEOUT
    assert gone.mask.source.layer_index == 1

    other = _get(tree, "/d/other")
    assert other.kind == NodeKind.TOMBSTONE
    assert other.mask.reason == MaskReason.OPAQUE
    assert other.mask.source.layer_index == 2


def test_file_restored_then_whiteouted_points_to_new_layer(tmp_path):
    l0 = _tar([("f", 1, "v0")])
    l1 = _tar([(".wh.f", 1, "")])
    l2 = _tar([("f", 1, "v2")])
    l3 = _tar([(".wh.f", 1, "")])
    tree = merge_layers(tuple(
        _layer(tmp_path, i, data) for i, data in enumerate((l0, l1, l2, l3))
    ))
    node = _get(tree, "/f")
    assert node.kind == NodeKind.TOMBSTONE
    assert node.mask.source.layer_index == 3


def test_rebuild_without_dir_member_still_inherits(tmp_path):
    """重建层只给子文件、不给目录成员时，也要继承未补回条目的遮蔽来源。"""
    l0 = _tar([("conf/", 0, ""), ("conf/lost.conf", 1, "x")])
    l1 = _tar([(".wh.conf", 1, "")])
    l2 = _tar([("conf/added.conf", 1, "n")])
    tree = merge_layers(
        (_layer(tmp_path, 0, l0), _layer(tmp_path, 1, l1), _layer(tmp_path, 2, l2))
    )
    assert _get(tree, "/conf/added.conf").source.layer_index == 2
    lost = _get(tree, "/conf/lost.conf")
    assert lost.kind == NodeKind.TOMBSTONE
    assert lost.mask.source.layer_index == 1


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

    # 层 1 whiteout 整目录 /etc/app/plugins，层 2 重建后补回 a.conf
    plugin_a = _get(merged_tree, "/etc/app/plugins/a.conf")
    assert plugin_a.kind == NodeKind.FILE
    assert plugin_a.source.layer_index == 2

    # 未补回的条目仍指向让它消失的层 1 whiteout
    for rel in ("/etc/app/plugins/b.conf", "/etc/app/plugins/nested"):
        lost = _get(merged_tree, rel)
        assert lost.kind == NodeKind.TOMBSTONE, rel
        assert lost.mask.reason == MaskReason.WHITEOUT
        assert lost.mask.source.layer_index == 1

    plugin_nested = _get(merged_tree, "/etc/app/plugins/nested")
    assert plugin_nested.shadowed is not None
    assert plugin_nested.shadowed.children["deep.conf"].kind == NodeKind.FILE

    # 层 2 opaque 遮蔽子目录后重建，未补回的 x.conf 仍报层 2 opaque
    x_conf = _get(merged_tree, "/etc/app/legacy/sub/x.conf")
    assert x_conf.kind == NodeKind.TOMBSTONE
    assert x_conf.mask.reason == MaskReason.OPAQUE
    assert x_conf.mask.source.layer_index == 2
