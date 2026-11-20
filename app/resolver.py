"""容器视图下的绝对路径解析。

解析方式与 Linux ``path_lookup`` 对齐：从根开始逐段推进，每遇到符号链接就把
链接目标展开到剩余路径前面重新解析。挂载点在树上已经是替换后的目录节点，
因此链接解析天然落在挂载后的视图里。

诊断增强：
* 任意一段断裂都记录“断在哪一段、已解析到哪、为何不可见（含遮蔽来源）”；
* 符号链接成环 / 超过最大链跳数时明确报错，并回传完整链接链；
* 所有路径字符串均为归一化后的绝对路径，方便发布工程师逐层核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .layers import (
    LayerSource,
    MaskReason,
    MountSource,
    Node,
    NodeKind,
)

MAX_SYMLINK_HOPS = 40  # 与 Linux MAXSYMLINKS 一致


class ResolutionStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    SYMLINK_LOOP = "symlink_loop"
    NOT_DIRECTORY = "not_directory"


@dataclass(frozen=True)
class SourceView:
    """对外输出的来源描述。"""

    kind: str  # layer | mount
    layer_index: Optional[int]
    digest: Optional[str]
    diff_id: Optional[str]
    mount_point: Optional[str]

    @classmethod
    def from_node(cls, node: Node) -> "SourceView":
        src = node.source
        if isinstance(src, MountSource):
            return cls(
                kind="mount",
                layer_index=None,
                digest=None,
                diff_id=None,
                mount_point=src.mount_point,
            )
        assert isinstance(src, LayerSource)
        return cls(
            kind="layer",
            layer_index=src.layer_index,
            digest=src.digest,
            diff_id=src.diff_id,
            mount_point=None,
        )


@dataclass(frozen=True)
class MaskView:
    reason: str  # whiteout | opaque | mount
    layer_index: Optional[int]
    digest: Optional[str]
    diff_id: Optional[str]
    mount_point: Optional[str]
    opaque_directory: Optional[str]


@dataclass
class SymlinkHop:
    link_path: str
    target: str
    layer_index: Optional[int]
    digest: Optional[str]
    mount_point: Optional[str]


@dataclass
class ResolutionResult:
    status: ResolutionStatus
    requested_path: str
    resolved_path: Optional[str] = None
    node_kind: Optional[str] = None
    source: Optional[SourceView] = None
    size: Optional[int] = None
    link_target: Optional[str] = None
    symlink_chain: list[SymlinkHop] = field(default_factory=list)
    failed_component: Optional[str] = None
    resolved_prefix: Optional[str] = None
    masking: Optional[MaskView] = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "requested_path": self.requested_path,
            "resolved_path": self.resolved_path,
            "node_kind": self.node_kind,
            "source": None if self.source is None else self._source_dict(self.source),
            "size": self.size,
            "link_target": self.link_target,
            "symlink_chain": [vars(hop) for hop in self.symlink_chain],
            "failure": None
            if self.status == ResolutionStatus.FOUND
            else {
                "failed_component": self.failed_component,
                "resolved_prefix": self.resolved_prefix,
                "masking": None if self.masking is None else vars(self.masking),
                "message": self.message,
            },
        }

    @staticmethod
    def _source_dict(source: SourceView) -> dict:
        if source.kind == "mount":
            return {"kind": "mount", "mount_point": source.mount_point}
        return {
            "kind": "layer",
            "layer_index": source.layer_index,
            "digest": source.digest,
            "diff_id": source.diff_id,
        }


def _normalize_abs(parts: list[str]) -> str:
    return "/" + "/".join(parts)


def _mask_view(node: Node) -> Optional[MaskView]:
    """墓碑节点只可能由 whiteout/opaque 产生；挂载遮蔽走 _mount_mask_view。"""
    if node.kind != NodeKind.TOMBSTONE or node.mask is None:
        return None
    mask = node.mask
    src = mask.source
    return MaskView(
        reason=mask.reason.value,
        layer_index=src.layer_index,
        digest=src.digest,
        diff_id=src.diff_id,
        mount_point=None,
        opaque_directory=mask.opaque_dir,
    )


def _mount_mask_view(node: Node) -> MaskView:
    assert isinstance(node.source, MountSource)
    return MaskView(
        reason=MaskReason.MOUNT.value,
        layer_index=None,
        digest=None,
        diff_id=None,
        mount_point=node.source.mount_point,
        opaque_directory=None,
    )


def resolve(root: Node, requested: str) -> ResolutionResult:
    """解析容器内绝对路径。"""
    if not requested.startswith("/"):
        return ResolutionResult(
            status=ResolutionStatus.NOT_FOUND,
            requested_path=requested,
            message="待查路径必须是容器内绝对路径（以 / 开头）",
        )
    initial = [p for p in requested.split("/") if p and p != "."]

    remaining: list[str] = list(initial)
    current_parts: list[str] = []
    cursor: Node = root
    chain: list[SymlinkHop] = []
    # 用于环检测：(当前已解析目录部分, 符号链接绝对路径)
    visited_links: set[str] = set()

    while remaining:
        component = remaining.pop(0)

        # 运行时语义的 ..：在当前已解析视图中回退一段（符号链接之后的 .. 也正确）
        if component == "..":
            if current_parts:
                current_parts.pop()
                cursor = _walk_parts(root, current_parts)
            continue

        if cursor.kind != NodeKind.DIRECTORY:
            return _fail_not_directory(requested, current_parts, component, cursor, chain)

        nxt = cursor.children.get(component)
        if nxt is None:
            mount_owner = _find_mount_ancestor(root, current_parts)
            masking = _mount_mask_view(mount_owner) if mount_owner is not None else None
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                requested_path=requested,
                resolved_path=_normalize_abs(current_parts + [component]),
                resolved_prefix=_normalize_abs(current_parts) if current_parts else "/",
                failed_component=component,
                masking=masking,
                symlink_chain=chain,
                message=_missing_message(current_parts, component, masking),
            )

        if nxt.kind == NodeKind.TOMBSTONE:
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                requested_path=requested,
                resolved_path=_normalize_abs(current_parts + [component]),
                resolved_prefix=_normalize_abs(current_parts) if current_parts else "/",
                failed_component=component,
                masking=_mask_view(nxt),
                symlink_chain=chain,
                message=_tombstone_message(current_parts, component, nxt),
            )

        # 挂载点：节点本身是卷目录，replaced 记录被替换的镜像子树
        if nxt.kind == NodeKind.DIRECTORY and isinstance(nxt.source, MountSource):
            cursor = nxt
            current_parts.append(component)
            continue

        if nxt.kind == NodeKind.SYMLINK:
            link_abs = _normalize_abs(current_parts + [component])
            hop = SymlinkHop(
                link_path=link_abs,
                target=nxt.link_target or "",
                layer_index=nxt.source.layer_index if isinstance(nxt.source, LayerSource) else None,
                digest=nxt.source.digest if isinstance(nxt.source, LayerSource) else None,
                mount_point=nxt.source.mount_point if isinstance(nxt.source, MountSource) else None,
            )
            if link_abs in visited_links:
                chain.append(hop)
                return ResolutionResult(
                    status=ResolutionStatus.SYMLINK_LOOP,
                    requested_path=requested,
                    resolved_path=link_abs,
                    symlink_chain=chain,
                    failed_component=component,
                    message=(
                        f"符号链接成环：{link_abs} 再次被解析，链接链为 "
                        + " -> ".join(h.link_path for h in chain)
                    ),
                )
            visited_links.add(link_abs)
            chain.append(hop)

            if len(chain) > MAX_SYMLINK_HOPS:
                return ResolutionResult(
                    status=ResolutionStatus.SYMLINK_LOOP,
                    requested_path=requested,
                    resolved_path=link_abs,
                    symlink_chain=chain,
                    failed_component=component,
                    message=(
                        f"符号链接跳转超过上限 {MAX_SYMLINK_HOPS} 次，判定为链接环/过深链"
                    ),
                )

            # 链接目标统一展开成“从根开始的绝对段”，随后光标重置回根重新解析，
            # 这样绝对/相对链接、含 .. 的目标都走同一条可靠路径。
            target_parts = _expand_link_absolute(nxt.link_target or "", current_parts)
            remaining = target_parts + remaining
            cursor = root
            current_parts = []
            continue

        # 普通文件或目录，成功推进一段
        cursor = nxt
        current_parts.append(component)

    # 全部段消费完：cursor 即目标节点
    if cursor is root:
        return ResolutionResult(
            status=ResolutionStatus.FOUND,
            requested_path=requested,
            resolved_path="/",
            node_kind=NodeKind.DIRECTORY.value,
            source=None,
            symlink_chain=chain,
            message="",
        )

    return ResolutionResult(
        status=ResolutionStatus.FOUND,
        requested_path=requested,
        resolved_path=_normalize_abs(current_parts),
        node_kind=cursor.kind.value,
        source=SourceView.from_node(cursor),
        size=cursor.size if cursor.kind == NodeKind.FILE else None,
        link_target=cursor.link_target if cursor.kind == NodeKind.SYMLINK else None,
        symlink_chain=chain,
    )


def _lexical_collapse(parts: list[str]) -> list[str]:
    """仅用于符号链接目标自身的词法归一化（目标字符串在被展开时求值）。"""
    out: list[str] = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
        else:
            out.append(part)
    return out


def _walk_parts(root: Node, parts: list[str]) -> Node:
    cursor = root
    for component in parts:
        if cursor.kind != NodeKind.DIRECTORY:
            return cursor
        nxt = cursor.children.get(component)
        if nxt is None:
            return cursor
        cursor = nxt
    return cursor


def _expand_link_absolute(target: str, link_parent_parts: list[str]) -> list[str]:
    """把符号链接目标展开为从根开始的绝对路径段（词法归一化，不做存在性检查）。"""
    if target.startswith("/"):
        base = [p for p in target.split("/") if p and p != "."]
    else:
        base = link_parent_parts + [p for p in target.split("/") if p and p != "."]
    return _lexical_collapse(base)


def _find_mount_ancestor(root: Node, parts: list[str]) -> Optional[Node]:
    """沿已解析路径找最深的挂载点目录节点（卷内缺失项的遮蔽来源）。"""
    mount_owner: Optional[Node] = None
    cursor = root
    for component in parts:
        if cursor.kind != NodeKind.DIRECTORY:
            break
        nxt = cursor.children.get(component)
        if nxt is None:
            break
        if isinstance(nxt.source, MountSource):
            mount_owner = nxt
        cursor = nxt
    return mount_owner


def _missing_message(
    current_parts: list[str], component: str, masking: Optional[MaskView]
) -> str:
    prefix = _normalize_abs(current_parts) if current_parts else "/"
    if masking is not None and masking.reason == MaskReason.MOUNT.value:
        return (
            f"路径在段 {component!r} 处断裂：卷挂载点 {masking.mount_point} "
            f"已整棵替换镜像子树，卷内 {prefix}/{component} 不存在；"
            f"镜像原子树中的同名条目在容器视图里被该挂载遮蔽"
        )
    return f"路径在段 {component!r} 处断裂：目录 {prefix} 下不存在该条目"


def _fail_not_directory(
    requested: str,
    current_parts: list[str],
    component: str,
    cursor: Node,
    chain: list[SymlinkHop],
) -> ResolutionResult:
    masking = None
    if isinstance(cursor.source, MountSource):
        masking = _mount_mask_view(cursor)
    return ResolutionResult(
        status=ResolutionStatus.NOT_DIRECTORY,
        requested_path=requested,
        resolved_path=_normalize_abs(current_parts + [component]),
        resolved_prefix=_normalize_abs(current_parts),
        failed_component=component,
        masking=masking,
        symlink_chain=chain,
        message=(
            f"路径在段 {component!r} 处无法继续：{_normalize_abs(current_parts)} "
            f"是 {cursor.kind.value} 而非目录"
        ),
    )


def _tombstone_message(current_parts: list[str], component: str, node: Node) -> str:
    mask = node.mask
    assert mask is not None
    full = _normalize_abs(current_parts + [component])
    if mask.reason == MaskReason.OPAQUE:
        return (
            f"路径在段 {component!r} 处不可见：{full} 被层 "
            f"[{mask.source.layer_index}] ({mask.source.digest}) 的 opaque 标记遮蔽"
            f"（opaque 目录 {mask.opaque_dir}），下层同名条目不会出现在容器视图中"
        )
    return (
        f"路径在段 {component!r} 处不可见：{full} 被层 "
        f"[{mask.source.layer_index}] ({mask.source.digest}) 的 whiteout 标记遮蔽，"
        f"下层同名条目不会出现在容器视图中"
    )
