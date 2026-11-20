"""目录挂载（bind/volume）处理。

规则：
* 挂载点必须是镜像合并视图中已存在的目录（词法路径，不跟随末段符号链接做替换）；
* 挂载会用卷内容整棵替换挂载点子树，旧子树保留在 ``replaced`` 上用于解释遮蔽；
* 卷清单只接受普通文件的相对路径，禁止绝对路径与 ``..``；
* 多个挂载点之间不允许互相嵌套覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

from .layers import (
    MountSource,
    Node,
    NodeKind,
)


class MountError(ValueError):
    """挂载参数非法或挂载点不满足要求。"""


@dataclass(frozen=True)
class MountRequest:
    mount_point: str  # 镜像内绝对路径
    volume_files: tuple[str, ...]  # 卷内普通文件相对路径清单


def _split_abs(path: str) -> list[str]:
    if not path.startswith("/"):
        raise MountError(f"挂载点必须是绝对路径: {path!r}")
    parts = [p for p in path.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise MountError(f"挂载点不允许包含 ..: {path!r}")
    return parts


def _lexical_lookup(root: Node, parts: list[str]) -> Node | None:
    """按词法路径查节点（不跟随符号链接）。"""
    cursor = root
    for component in parts:
        if cursor.kind != NodeKind.DIRECTORY:
            return None
        cursor = cursor.children.get(component)
        if cursor is None:
            return None
    return cursor


def _build_volume_tree(files: tuple[str, ...], source: MountSource) -> Node:
    vol_root = Node(kind=NodeKind.DIRECTORY, source=source)
    for rel in files:
        if rel.startswith("/"):
            raise MountError(f"卷内文件必须是相对路径: {rel!r}")
        parts = [p for p in rel.split("/") if p and p != "."]
        if not parts or any(p == ".." for p in parts):
            raise MountError(f"卷内文件相对路径非法: {rel!r}")
        cursor = vol_root
        for component in parts[:-1]:
            nxt = cursor.children.get(component)
            if nxt is None:
                nxt = Node(kind=NodeKind.DIRECTORY, source=source)
                cursor.children[component] = nxt
            elif nxt.kind != NodeKind.DIRECTORY:
                raise MountError(f"卷内路径与文件冲突: {rel!r}")
            cursor = nxt
        name = parts[-1]
        if name in cursor.children:
            raise MountError(f"卷内文件重复: {rel!r}")
        cursor.children[name] = Node(kind=NodeKind.FILE, source=source)
    return vol_root


def apply_mounts(root: Node, requests: list[MountRequest]) -> None:
    """在合并后的镜像树上原地应用所有挂载。"""
    seen: list[list[str]] = []
    for req in requests:
        parts = _split_abs(req.mount_point)
        if not parts:
            raise MountError("不允许挂载到根目录 /")

        for earlier in seen:
            if parts == earlier[: len(parts)] or earlier == parts[: len(earlier)]:
                raise MountError(
                    f"挂载点 {'/'+ '/'.join(parts)} 与已有挂载点 "
                    f"{'/'+ '/'.join(earlier)} 互相嵌套"
                )
        seen.append(parts)

        target = _lexical_lookup(root, parts)
        if target is None:
            raise MountError(f"挂载点在镜像中不存在: {req.mount_point}")
        if target.kind != NodeKind.DIRECTORY:
            raise MountError(
                f"挂载点必须是镜像内已有目录，当前类型为 {target.kind.value}: {req.mount_point}"
            )

        source = MountSource(kind="mount", mount_point=req.mount_point)
        volume_root = _build_volume_tree(req.volume_files, source)
        # 被替换下来的镜像子树保留在 replaced 上，供需要时解释挂载遮蔽；
        # 解析器通过节点的 MountSource 识别挂载来源，无需伪层占位。
        volume_root.replaced = target

        parent = _lexical_lookup(root, parts[:-1])
        assert parent is not None and parent.kind == NodeKind.DIRECTORY
        parent.children[parts[-1]] = volume_root
