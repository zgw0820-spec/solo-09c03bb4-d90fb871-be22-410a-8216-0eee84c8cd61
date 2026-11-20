"""镜像层合并引擎。

语义（与 OCI/Docker 分层规则一致）：

* 层按 manifest 中的顺序从底到顶应用；
* ``.wh.`` 前缀是 whiteout，标记同层该路径上的下层条目为墓碑；
* ``.wh..wh..opq`` 是 opaque 目录标记，遮蔽该目录的全部下层子项；
* whiteout/opaque 只遮下层，同一层里在遮蔽之后新建的条目照常可见；
* 墓碑节点保留遮蔽来源（哪个层、哪种原因、opaque 时的父目录），
  供诊断 API 在路径断裂时给出可定位的信息；
* 全程在内存里操作，归档内容不会落到宿主机文件系统上被执行。
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

from .oci import LayerBlob

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"
MAX_SYMLINK_TARGET_BYTES = 4096


class NodeKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    TOMBSTONE = "tombstone"


class MaskReason(str, Enum):
    WHITEOUT = "whiteout"
    OPAQUE = "opaque"
    MOUNT = "mount"  # 目录挂载整体替换子树


@dataclass(frozen=True)
class LayerSource:
    kind: str = "layer"
    layer_index: int = 0
    digest: str = ""
    diff_id: str = ""

    def describe(self) -> str:
        return f"layer[{self.layer_index}] {self.digest}"


@dataclass(frozen=True)
class MountSource:
    kind: str = "mount"
    mount_point: str = ""

    def describe(self) -> str:
        return f"mount:{self.mount_point}"


Source = Union[LayerSource, MountSource]


@dataclass(frozen=True)
class Mask:
    """一次遮蔽的可定位信息。"""

    reason: MaskReason
    source: LayerSource  # 产生遮蔽的层（挂载场景由调用方补 MOUNT 类型）
    opaque_dir: Optional[str] = None  # opaque 时记录被标记的目录绝对路径


@dataclass
class Node:
    kind: NodeKind
    source: Source
    children: dict[str, "Node"] = field(default_factory=dict)
    link_target: Optional[str] = None  # 符号链接目标（镜像内原始字符串）
    size: int = 0
    mask: Optional[Mask] = None  # 仅墓碑使用
    # 挂载点：被替换下来的旧子树根，用于解释“为什么挂载点下面的镜像文件没了”
    replaced: Optional["Node"] = None


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _split_member_path(name: str) -> list[str]:
    parts = [p for p in name.split("/") if p and p != "."]
    return parts


def _is_safe_relpath(parts: list[str]) -> bool:
    return all(p != ".." and p != "/" for p in parts)


def _ensure_child_dir(parent: Node, name: str, source: Source) -> Node:
    """在 parent 下取到一个可用的目录节点；遇到墓碑或类型冲突则用新目录替换。"""
    existing = parent.children.get(name)
    if existing is None or existing.kind in (NodeKind.TOMBSTONE, NodeKind.FILE, NodeKind.SYMLINK):
        fresh = Node(kind=NodeKind.DIRECTORY, source=source)
        parent.children[name] = fresh
        return fresh
    return existing


def _new_dir(source: Source) -> Node:
    return Node(kind=NodeKind.DIRECTORY, source=source)


# --------------------------------------------------------------------------- #
# 单层处理
# --------------------------------------------------------------------------- #


@dataclass
class _PendingEntry:
    parts: list[str]
    member: tarfile.TarInfo


def _apply_whiteout(root: Node, parts: list[str], source: LayerSource) -> None:
    """在合并树上放置/更新墓碑。中间目录缺失说明下层本就没有，无需动作。"""
    *lead, final = parts
    cursor: Node = root
    for component in lead:
        nxt = cursor.children.get(component)
        if nxt is None or nxt.kind != NodeKind.DIRECTORY:
            return
        cursor = nxt

    existing = cursor.children.get(final)
    if existing is None:
        cursor.children[final] = Node(
            kind=NodeKind.TOMBSTONE,
            source=source,
            mask=Mask(reason=MaskReason.WHITEOUT, source=source),
        )
    else:
        # 同层可能先建后删，也可能删下层项：统一用带遮蔽来源的墓碑替换，
        # 墓碑会被同层后续新增项再覆盖。
        cursor.children[final] = Node(
            kind=NodeKind.TOMBSTONE,
            source=source,
            mask=Mask(reason=MaskReason.WHITEOUT, source=source),
        )


def _apply_opaque(root: Node, dir_parts: list[str], source: LayerSource) -> None:
    """opaque：目录本身保留，其全部子项替换为墓碑。"""
    cursor: Node = root
    for component in dir_parts:
        nxt = cursor.children.get(component)
        if nxt is None or nxt.kind != NodeKind.DIRECTORY:
            return
        cursor = nxt

    opaque_path = "/" + "/".join(dir_parts) if dir_parts else "/"
    for name, child in list(cursor.children.items()):
        cursor.children[name] = Node(
            kind=NodeKind.TOMBSTONE,
            source=source,
            mask=Mask(reason=MaskReason.OPAQUE, source=source, opaque_dir=opaque_path),
        )


def _apply_entry(root: Node, entry: _PendingEntry, source: LayerSource) -> None:
    parts = entry.parts
    member = entry.member

    # 逐层走/建中间目录（同层新增的目录会顶替墓碑与类型冲突节点）
    cursor: Node = root
    for component in parts[:-1]:
        cursor = _ensure_child_dir(cursor, component, source)

    name = parts[-1]

    if member.isdir():
        existing = cursor.children.get(name)
        if existing is not None and existing.kind == NodeKind.DIRECTORY:
            return  # 目录合并且保留下层已有子项
        cursor.children[name] = _new_dir(source)
        return

    if member.issym():
        target = member.linkname or ""
        if len(target.encode("utf-8", "replace")) > MAX_SYMLINK_TARGET_BYTES:
            raise ValueError(f"符号链接目标过长: {member.name}")
        cursor.children[name] = Node(
            kind=NodeKind.SYMLINK, source=source, link_target=target
        )
        return

    if member.isreg():
        cursor.children[name] = Node(
            kind=NodeKind.FILE, source=source, size=member.size
        )
        return

    if member.islnk():
        # 硬链接：按 tar 内链接目标解析到本层/下层已存在的普通文件，复用其来源类型
        link_parts = _split_member_path(member.linkname)
        if not _is_safe_relpath(link_parts):
            raise ValueError(f"硬链接目标非法: {member.name} -> {member.linkname}")
        target_node = _walk(root, link_parts)
        if target_node is None or target_node.kind != NodeKind.FILE:
            raise ValueError(
                f"硬链接目标在归档中不存在或不是普通文件: {member.name} -> {member.linkname}"
            )
        cursor.children[name] = Node(
            kind=NodeKind.FILE, source=target_node.source, size=target_node.size
        )
        return

    # 字符/块设备、fifo 等：诊断场景无意义，明确拒绝而不是静默忽略
    raise ValueError(f"层内存在不支持的成员类型 ({member.type}): {member.name}")


def _walk(root: Node, parts: list[str]) -> Optional[Node]:
    cursor: Node = root
    for component in parts:
        if cursor.kind != NodeKind.DIRECTORY:
            return None
        nxt = cursor.children.get(component)
        if nxt is None:
            return None
        cursor = nxt
    return cursor


# --------------------------------------------------------------------------- #
# 层 tar 读取与合并
# --------------------------------------------------------------------------- #


def _open_layer_tar(blob_path: str) -> tarfile.TarFile:
    """层 blob 可能是 gzip tar 或裸 tar，按内容嗅探。

    blob 是磁盘上的真实文件（可 seek），使用随机访问模式，
    以便硬链接引用同层稍后出现的条目时也能解析。
    """
    try:
        return tarfile.open(blob_path, mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"层 blob 无法作为 tar 读取: {exc}") from exc


def merge_layers(layers: tuple[LayerBlob, ...]) -> Node:
    """把所有层合并成一棵以根目录节点表示的文件树。"""
    root = Node(kind=NodeKind.DIRECTORY, source=LayerSource(kind="layer", layer_index=-1, digest="<root>"))

    for layer in layers:
        source = LayerSource(
            kind="layer",
            layer_index=layer.index,
            digest=layer.digest,
            diff_id=layer.diff_id,
        )
        pending: list[_PendingEntry] = []
        deferred_links: list[_PendingEntry] = []
        # 随机访问 tar：whiteout/opaque 先应用，普通条目按归档内原始顺序随后应用，
        # 从而保证“同层遮蔽后新增仍可见”；硬链接若暂时找不到目标则延后一轮。
        with _open_layer_tar(str(layer.path)) as tar:
            members = tar.getmembers()
            for member in members:
                if member.isdir() and not member.name:
                    continue
                parts = _split_member_path(member.name)
                if not parts:
                    continue
                if not _is_safe_relpath(parts):
                    raise ValueError(f"层内成员路径非法: {member.name}")

                last = parts[-1]
                if last == OPAQUE_MARKER:
                    _apply_opaque(root, parts[:-1], source)
                    continue
                if last.startswith(WHITEOUT_PREFIX):
                    wh_parts = parts[:-1] + [last[len(WHITEOUT_PREFIX):]]
                    if not wh_parts[-1]:
                        raise ValueError(f"非法 whiteout 名称: {member.name}")
                    _apply_whiteout(root, wh_parts, source)
                    continue
                pending.append(_PendingEntry(parts=parts, member=member))

            for entry in pending:
                if entry.member.islnk():
                    link_parts = _split_member_path(entry.member.linkname)
                    target_node = _walk(root, link_parts) if _is_safe_relpath(link_parts) else None
                    if target_node is None or target_node.kind != NodeKind.FILE:
                        deferred_links.append(entry)
                        continue
                _apply_entry(root, entry, source)

            # 第二轮：处理目标在同层稍后才出现的硬链接
            for entry in deferred_links:
                link_parts = _split_member_path(entry.member.linkname)
                if not _is_safe_relpath(link_parts):
                    raise ValueError(
                        f"硬链接目标非法: {entry.member.name} -> {entry.member.linkname}"
                    )
                target_node = _walk(root, link_parts)
                if target_node is None or target_node.kind != NodeKind.FILE:
                    raise ValueError(
                        f"硬链接目标在归档中不存在或不是普通文件: "
                        f"{entry.member.name} -> {entry.member.linkname}"
                    )
                _apply_entry(root, entry, source)

    return root
