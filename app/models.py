"""请求/响应 Pydantic 模型（同时驱动 Swagger 文档与示例）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class MountSpec(BaseModel):
    mount_point: str = Field(
        ...,
        description="镜像内已存在的目录绝对路径，容器启动时该目录被卷整棵替换",
        examples=["/etc/app"],
    )
    volume_files: list[str] = Field(
        default_factory=list,
        description="卷内普通文件的相对路径清单（不含目录，目录由路径自动推导）",
        examples=[["config.yaml", "tls/cert.pem"]],
    )


class SourceInfo(BaseModel):
    kind: Literal["layer", "mount"]
    layer_index: Optional[int] = Field(None, description="层序号，0 为最底层；挂载来源时为 null")
    digest: Optional[str] = Field(None, description="层描述符摘要（压缩态 sha256）")
    diff_id: Optional[str] = Field(None, description="层解压态 sha256（config.rootfs.diff_ids）")
    mount_point: Optional[str] = Field(None, description="挂载来源时的挂载点绝对路径")


class MaskingInfo(BaseModel):
    reason: Literal["whiteout", "opaque", "mount"]
    layer_index: Optional[int] = None
    digest: Optional[str] = None
    diff_id: Optional[str] = None
    mount_point: Optional[str] = None
    opaque_directory: Optional[str] = Field(
        None, description="opaque 标记所在的目录（reason=opaque 时有值）"
    )


class SymlinkHopInfo(BaseModel):
    link_path: str
    target: str
    layer_index: Optional[int] = None
    digest: Optional[str] = None
    mount_point: Optional[str] = None


class FailureInfo(BaseModel):
    failed_component: Optional[str] = Field(
        None, description="第一个无法解析的路径段；符号链接环时为触发环的链接段"
    )
    resolved_prefix: Optional[str] = Field(
        None, description="断裂前已成功解析的前缀绝对路径"
    )
    masking: Optional[MaskingInfo] = Field(
        None, description="若该段被遮蔽，给出可定位的遮蔽来源；普通缺失时为 null"
    )
    message: str


class DiagnosisResponse(BaseModel):
    status: Literal["found", "not_found", "symlink_loop", "not_directory"]
    requested_path: str
    resolved_path: Optional[str] = Field(
        None, description="符号链接全部展开并归一化后的最终容器内绝对路径"
    )
    node_kind: Optional[Literal["directory", "file", "symlink", "tombstone"]] = None
    source: Optional[SourceInfo] = Field(
        None, description="解析结果的来源：某个镜像层（含摘要）或某个挂载点"
    )
    size: Optional[int] = Field(None, description="普通文件字节数")
    link_target: Optional[str] = Field(None, description="最终结果本身是符号链接时的原始目标串")
    symlink_chain: list[SymlinkHopInfo] = Field(default_factory=list)
    failure: Optional[FailureInfo] = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
