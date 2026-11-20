"""OCI Image Layout 解析。

只支持单平台镜像：index.json 里取唯一的 image manifest 描述符（或唯一平台条目）。
所有按摘要寻址的 blob 都做 sha256 校验，防止归档被篡改后定位到错误的层。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import sha256_file


class OCIParseError(ValueError):
    """OCI 布局或清单不符合预期时抛出。"""


@dataclass(frozen=True)
class LayerBlob:
    """一个可按 diff 顺序应用的镜像层。"""

    digest: str  # manifest 描述符摘要（压缩态），用于来源标注
    diff_id: str  # config 里记录的解压态摘要
    media_type: str
    path: Path  # 归档内 blob 实际路径
    index: int  # 从底到顶的层序号（0 = 最底层）


@dataclass(frozen=True)
class ImageModel:
    layers: tuple[LayerBlob, ...]
    platform: dict[str, Any]
    config_history_size: int


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise OCIParseError(f"缺少必需文件: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise OCIParseError(f"JSON 解析失败 {path.name}: {exc}") from exc


def _blob_path(root: Path, digest: str) -> Path:
    algo, _, encoded = digest.partition(":")
    if algo != "sha256" or not encoded or "/" in encoded:
        raise OCIParseError(f"只支持 sha256 摘要，收到: {digest!r}")
    path = root / "blobs" / algo / encoded
    if not path.is_file():
        raise OCIParseError(f"清单引用的 blob 不存在: {digest}")
    return path


def _verify_digest(path: Path, digest: str) -> None:
    actual = "sha256:" + sha256_file(path)
    if actual != digest:
        raise OCIParseError(
            f"blob 摘要校验失败: 期望 {digest}，实际 {actual}（文件 {path.name}）"
        )


def _select_manifest_descriptor(index_doc: dict[str, Any]) -> dict[str, Any]:
    manifests = index_doc.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise OCIParseError("index.json 中没有 manifests 条目")

    image_manifests = [
        m
        for m in manifests
        if isinstance(m, dict)
        and m.get("mediaType", "application/vnd.oci.image.manifest.v1+json")
        in (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    ]
    if not image_manifests:
        raise OCIParseError("index.json 中没有 image manifest（本服务不支持 fat manifest 多平台选择）")
    if len(image_manifests) > 1:
        raise OCIParseError(
            f"检测到 {len(image_manifests)} 个平台清单，本服务仅接受单平台 OCI Image Layout"
        )
    return image_manifests[0]


def load_image(root: Path) -> ImageModel:
    """读取解包后的 OCI 布局根目录，返回按序排列的层描述。"""
    root = root.resolve()

    layout_doc = _read_json(root / "oci-layout")
    if layout_doc.get("imageLayoutVersion") != "1.0.0":
        raise OCIParseError(
            f"imageLayoutVersion 不支持: {layout_doc.get('imageLayoutVersion')!r}"
        )

    index_doc = _read_json(root / "index.json")
    descriptor = _select_manifest_descriptor(index_doc)
    manifest_digest = descriptor.get("digest")
    if not manifest_digest:
        raise OCIParseError("manifest 描述符缺少 digest")
    manifest_path = _blob_path(root, manifest_digest)
    _verify_digest(manifest_path, manifest_digest)
    manifest_doc = _read_json(manifest_path)

    if manifest_doc.get("schemaVersion") != 2:
        raise OCIParseError("manifest schemaVersion 必须为 2")

    config_desc = manifest_doc.get("config")
    if not isinstance(config_desc, dict) or "digest" not in config_desc:
        raise OCIParseError("manifest 缺少 config 描述符")
    config_path = _blob_path(root, config_desc["digest"])
    _verify_digest(config_path, config_desc["digest"])
    config_doc = _read_json(config_path)

    layer_descriptors = manifest_doc.get("layers")
    if not isinstance(layer_descriptors, list) or not layer_descriptors:
        raise OCIParseError("manifest 中没有任何层")

    diff_ids = config_doc.get("rootfs", {}).get("diff_ids", [])
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layer_descriptors):
        raise OCIParseError(
            "config.rootfs.diff_ids 与 manifest.layers 数量不一致，无法建立层对应关系"
        )

    layers: list[LayerBlob] = []
    for idx, desc in enumerate(layer_descriptors):
        digest = desc.get("digest")
        if not digest:
            raise OCIParseError(f"第 {idx} 层缺少 digest")
        path = _blob_path(root, digest)
        _verify_digest(path, digest)
        diff_id = diff_ids[idx]
        if not isinstance(diff_id, str) or not diff_id.startswith("sha256:"):
            raise OCIParseError(f"第 {idx} 层 diff_id 非法: {diff_id!r}")
        layers.append(
            LayerBlob(
                digest=digest,
                diff_id=diff_id,
                media_type=str(desc.get("mediaType", "")),
                path=path,
                index=idx,
            )
        )

    platform = descriptor.get("platform") or config_doc.get("os", {}) or {}
    history = config_doc.get("history", [])
    return ImageModel(
        layers=tuple(layers),
        platform=platform,
        config_history_size=len(history) if isinstance(history, list) else 0,
    )
