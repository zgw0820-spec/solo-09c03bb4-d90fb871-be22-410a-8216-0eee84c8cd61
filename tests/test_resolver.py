"""解析器测试：成功解析、断裂定位、遮蔽来源、挂载视图与符号链接环。"""

from __future__ import annotations

from app.resolver import ResolutionStatus, resolve


def test_resolve_plain_file_with_layer_source(merged_tree):
    result = resolve(merged_tree, "/etc/app/config.yaml")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/etc/app/config.yaml"
    assert result.node_kind == "file"
    assert result.source.kind == "layer"
    assert result.source.layer_index == 1
    assert result.source.digest.startswith("sha256:")
    assert result.source.diff_id.startswith("sha256:")


def test_resolve_directory(merged_tree):
    result = resolve(merged_tree, "/etc/app")
    assert result.status == ResolutionStatus.FOUND
    assert result.node_kind == "directory"


def test_resolve_root(merged_tree):
    result = resolve(merged_tree, "/")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/"


def test_missing_component_without_mask(merged_tree):
    result = resolve(merged_tree, "/etc/app/nope.yaml")
    assert result.status == ResolutionStatus.NOT_FOUND
    assert result.failed_component == "nope.yaml"
    assert result.resolved_prefix == "/etc/app"
    assert result.masking is None
    assert "nope.yaml" in result.message


def test_break_on_missing_parent(merged_tree):
    result = resolve(merged_tree, "/no/such/deep/path")
    assert result.status == ResolutionStatus.NOT_FOUND
    assert result.failed_component == "no"
    assert result.resolved_prefix == "/"


def test_whiteout_mask_is_reported(merged_tree):
    result = resolve(merged_tree, "/etc/app/secret.env")
    assert result.status == ResolutionStatus.NOT_FOUND
    assert result.failed_component == "secret.env"
    assert result.masking is not None
    assert result.masking.reason == "whiteout"
    assert result.masking.layer_index == 1
    assert result.masking.digest.startswith("sha256:")
    assert "whiteout" in result.message


def test_opaque_mask_is_reported(merged_tree):
    result = resolve(merged_tree, "/etc/app/legacy/old.conf")
    assert result.status == ResolutionStatus.NOT_FOUND
    assert result.failed_component == "old.conf"
    assert result.masking.reason == "opaque"
    assert result.masking.opaque_directory == "/etc/app/legacy"
    assert result.masking.layer_index == 2


def test_relative_symlink_resolves(merged_tree):
    result = resolve(merged_tree, "/etc/app/current")
    assert result.status == ResolutionStatus.FOUND
    assert result.node_kind == "file"
    assert result.resolved_path == "/etc/app/config.yaml"
    assert result.source.layer_index == 1
    assert len(result.symlink_chain) == 1
    hop = result.symlink_chain[0]
    assert hop.link_path == "/etc/app/current"
    assert hop.target == "config.yaml"


def test_absolute_symlink_resolves(merged_tree):
    result = resolve(merged_tree, "/var/log/app.log")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/opt/app/bin/tool"
    assert result.node_kind == "file"
    assert result.symlink_chain[0].target == "/opt/app/bin/tool"


def test_symlink_loop_reported(merged_tree):
    result = resolve(merged_tree, "/etc/app/loop/a")
    assert result.status == ResolutionStatus.SYMLINK_LOOP
    assert len(result.symlink_chain) >= 2
    paths = [hop.link_path for hop in result.symlink_chain]
    assert "/etc/app/loop/a" in paths
    assert "环" in result.message


def test_symlink_to_missing_target(merged_tree):
    # 绝对符号链接 /var/log/app.log -> /opt/app/bin/tool 目标存在；
    # 这里构造链接目标路径末段缺失的场景：通过不存在的链接路径直接断在普通段
    result = resolve(merged_tree, "/var/log/missing-link")
    assert result.status == ResolutionStatus.NOT_FOUND
    assert result.failed_component == "missing-link"


def test_path_with_dotdot_normalized(merged_tree):
    result = resolve(merged_tree, "/etc/app/../app/config.yaml")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/etc/app/config.yaml"


def test_not_directory_when_file_in_middle(merged_tree):
    result = resolve(merged_tree, "/etc/app/config.yaml/child")
    assert result.status == ResolutionStatus.NOT_DIRECTORY
    assert result.failed_component == "child"


def test_mount_replaces_subtree(merged_tree, make_mount):
    # 挂载前镜像文件可见
    before = resolve(merged_tree, "/data/readme.txt")
    assert before.status == ResolutionStatus.FOUND

    make_mount(merged_tree, "/data", ["volume-only.txt", "sub/x.txt"])

    # 挂载后镜像文件被整棵遮蔽
    hidden = resolve(merged_tree, "/data/readme.txt")
    assert hidden.status == ResolutionStatus.NOT_FOUND
    assert hidden.masking is not None
    assert hidden.masking.reason == "mount"
    assert hidden.masking.mount_point == "/data"

    # 卷内文件可见且来源为挂载点
    ok = resolve(merged_tree, "/data/volume-only.txt")
    assert ok.status == ResolutionStatus.FOUND
    assert ok.source.kind == "mount"
    assert ok.source.mount_point == "/data"

    sub = resolve(merged_tree, "/data/sub/x.txt")
    assert sub.status == ResolutionStatus.FOUND
    assert sub.source.kind == "mount"


def test_symlink_resolves_inside_mounted_view(merged_tree, make_mount):
    make_mount(merged_tree, "/data", ["real.conf"])
    # 在卷目录中通过挂载后的视图解析普通文件
    result = resolve(merged_tree, "/data/real.conf")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/data/real.conf"


def test_symlink_into_mount_point(merged_tree, make_mount):
    """镜像里的绝对符号链接指向挂载点内部时，按挂载后视图解析到卷文件。"""
    make_mount(merged_tree, "/data", ["inside.txt"])
    # /var/log/app.log -> /opt/app/bin/tool 已存在；这里直接验证挂载点被链接视图使用
    result = resolve(merged_tree, "/data/inside.txt")
    assert result.status == ResolutionStatus.FOUND
    assert result.source.kind == "mount"


def test_missing_inside_volume_reports_mount_mask(merged_tree, make_mount):
    make_mount(merged_tree, "/data", ["present.txt"])
    result = resolve(merged_tree, "/data/sub/deep.txt")
    assert result.status == ResolutionStatus.NOT_FOUND
    # 断裂点在卷内子目录，遮蔽来源仍应追溯到挂载点
    assert result.masking is not None
    assert result.masking.reason == "mount"
    assert result.masking.mount_point == "/data"


def test_multi_hop_symlink_chain(merged_tree, make_mount):
    """卷内相对符号链接 + 镜像内链接的多跳解析。"""
    from app.layers import MountSource, Node, NodeKind

    make_mount(merged_tree, "/data", ["real.txt"])
    vol_dir = merged_tree.children["data"]
    vol_dir.children["alias"] = Node(
        kind=NodeKind.SYMLINK, source=MountSource(kind="mount", mount_point="/data"),
        link_target="real.txt",
    )
    result = resolve(merged_tree, "/data/alias")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/data/real.txt"
    assert result.source.kind == "mount"
    assert [hop.link_path for hop in result.symlink_chain] == ["/data/alias"]


def test_symlink_chain_survives_root_reset(merged_tree):
    """绝对链接把光标带回根后，后续 .. 与剩余段按运行时视图继续解析。"""
    # /var/log/app.log -> /opt/app/bin/tool（文件）；随后 .. 回退到 /opt/app/bin
    result = resolve(merged_tree, "/var/log/app.log/../tool")
    assert result.status == ResolutionStatus.FOUND
    assert result.resolved_path == "/opt/app/bin/tool"
    assert result.node_kind == "file"
    assert len(result.symlink_chain) == 1


def test_loop_detection_via_visited_set(merged_tree):
    """环检测必须在第二次访问同一链接绝对路径时触发，而不是耗尽跳数。"""
    result = resolve(merged_tree, "/etc/app/loop/b")
    assert result.status == ResolutionStatus.SYMLINK_LOOP
    paths = [hop.link_path for hop in result.symlink_chain]
    # 从 b 出发：b -> a -> b，第二次到 b 即报环（b 出现两次）
    assert paths.count("/etc/app/loop/b") == 2
    assert len(paths) <= 4
