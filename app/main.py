"""FastAPI 诊断服务入口。

工作流程（每个请求互相隔离，临时目录用后即焚）：

1. 接收上传的单平台 OCI Image Layout tar（层 blob 可为 gzip）；
2. 在独立临时目录中安全解包外层归档；
3. 校验 OCI 布局、manifest 与全部 blob 摘要；
4. 逐层合并内存文件树（whiteout / opaque 语义）；
5. 应用目录挂载（整棵子树替换）；
6. 在挂载后的容器视图里解析目标绝对路径，返回来源或断裂定位。
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .archive import ArchiveFormatError, UnsafeArchiveError, safe_extract_tar
from .layers import merge_layers
from .models import DiagnosisResponse, HealthResponse
from .mounts import MountError, MountRequest, apply_mounts
from .oci import OCIParseError, load_image
from .resolver import resolve

logger = logging.getLogger("oci-path-diag")

app = FastAPI(
    title="OCI 镜像容器路径诊断 API",
    version=__version__,
    description=(
        "上传单平台 OCI Image Layout tar 与容器内绝对路径，"
        "返回该路径在分层合并 + 目录挂载后的容器视图中的解析结果与来源；"
        "找不到时指出断裂路径段以及 whiteout / opaque / 挂载等遮蔽来源，"
        "符号链接成环时明确报错。"
    ),
)

_MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB 保护上限


def _parse_mounts(raw: str | None) -> list[MountRequest]:
    if raw is None or not raw.strip():
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"mounts 不是合法 JSON: {exc}") from exc
    if not isinstance(doc, list):
        raise HTTPException(status_code=400, detail="mounts 必须是数组")
    requests: list[MountRequest] = []
    for idx, item in enumerate(doc):
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail=f"mounts[{idx}] 必须是对象")
        mount_point = item.get("mount_point")
        volume_files = item.get("volume_files", [])
        if not isinstance(mount_point, str) or not mount_point:
            raise HTTPException(status_code=400, detail=f"mounts[{idx}].mount_point 非法")
        if not isinstance(volume_files, list) or not all(
            isinstance(f, str) for f in volume_files
        ):
            raise HTTPException(
                status_code=400, detail=f"mounts[{idx}].volume_files 必须是字符串数组"
            )
        requests.append(
            MountRequest(mount_point=mount_point, volume_files=tuple(volume_files))
        )
    return requests


def _spool_upload(upload: UploadFile, work_dir: Path) -> Path:
    archive_path = work_dir / "upload.tar"
    total = 0
    with open(archive_path, "wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"上传归档超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限",
                )
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="上传的归档为空")
    return archive_path


@app.get("/health", response_model=HealthResponse, tags=["诊断"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/diagnose",
    response_model=DiagnosisResponse,
    tags=["诊断"],
    responses={
        200: {"description": "解析完成（found / not_found / symlink_loop / not_directory 均在 body 中区分）"},
        400: {"description": "归档、OCI 布局或挂载参数非法"},
        413: {"description": "归档超过大小上限"},
        422: {"description": "表单字段校验失败"},
    },
)
async def diagnose(
    archive: UploadFile = File(..., description="单平台 OCI Image Layout tar（层可为 gzip）"),
    target_path: str = Form(
        ...,
        description="待查的容器内绝对路径，例如 /etc/app/config.yaml",
    ),
    mounts: str | None = Form(
        default=None,
        description=(
            "可选 JSON 数组，每项 {\"mount_point\": 镜像内已有目录绝对路径, "
            "\"volume_files\": [卷内普通文件相对路径...]}"
        ),
    ),
) -> JSONResponse:
    if not target_path.startswith("/"):
        raise HTTPException(status_code=400, detail="target_path 必须是以 / 开头的绝对路径")

    mount_requests = _parse_mounts(mounts)

    work_dir = Path(tempfile.mkdtemp(prefix="oci-diag-"))
    try:
        extract_dir = work_dir / "layout"
        extract_dir.mkdir()
        try:
            archive_path = _spool_upload(archive, work_dir)
            safe_extract_tar(archive_path, extract_dir)
        except (UnsafeArchiveError, ArchiveFormatError) as exc:
            raise HTTPException(status_code=400, detail=f"归档解析失败: {exc}") from exc

        # OCI 布局允许根目录即布局根，也允许单层包裹目录
        layout_root = _find_layout_root(extract_dir)

        try:
            image = load_image(layout_root)
            tree = merge_layers(image.layers)
            apply_mounts(tree, mount_requests)
        except (OCIParseError, MountError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            # 层合并中的结构性错误（非法成员、硬链接缺失等）
            raise HTTPException(status_code=400, detail=f"镜像层处理失败: {exc}") from exc

        result = resolve(tree, target_path)
        payload = result.to_dict()
        payload["image"] = {
            "layer_count": len(image.layers),
            "layers": [
                {
                    "layer_index": layer.index,
                    "digest": layer.digest,
                    "diff_id": layer.diff_id,
                    "media_type": layer.media_type,
                }
                for layer in image.layers
            ],
            "platform": image.platform,
        }
        return JSONResponse(payload)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _find_layout_root(extract_dir: Path) -> Path:
    """兼容两种打包方式：布局文件在根目录，或被包在唯一一层子目录里。"""
    if (extract_dir / "oci-layout").is_file() and (extract_dir / "index.json").is_file():
        return extract_dir
    entries = [p for p in extract_dir.iterdir() if p.is_dir()]
    for candidate in entries:
        if (candidate / "oci-layout").is_file() and (candidate / "index.json").is_file():
            return candidate
    raise OCIParseError("解包后找不到 oci-layout / index.json，不是合法的 OCI Image Layout")
