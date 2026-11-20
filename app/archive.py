"""外层 OCI Image Layout tar 的安全解包与摘要工具。

上传的归档属于不可信数据：
1. 每个请求使用独立的临时目录；
2. 成员名必须是相对路径，禁止绝对路径与 ``..`` 越界；
3. 只接受普通文件、目录、符号链接三类成员，其余类型一律拒绝；
4. 符号链接目标同样不允许逃逸出解包根目录；
5. 全程使用 ``shutil.move`` 风格的边界校验，避免 ``..`` 拼接绕过。
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path

# 兼容老版本 tarfile（Python 3.11 没有 data_filter）
_BLOCKLISTED_MEMBER_TYPES = {
    tarfile.CHRTYPE,
    tarfile.BLKTYPE,
    tarfile.FIFOTYPE,
    tarfile.CONTTYPE,
}


class UnsafeArchiveError(ValueError):
    """归档包含越界或不被允许的成员。"""


class ArchiveFormatError(ValueError):
    """归档本身不是可识别的 tar/gzip tar。"""


def _resolve_within(root: Path, candidate: Path) -> Path:
    """确保 candidate 解析后仍位于 root 之内。"""
    resolved = candidate.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise UnsafeArchiveError(f"路径逃逸出解包目录: {candidate}")
    return resolved


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """把 tar（可 gzip）安全解包到 dest，dest 必须是已存在的空目录。"""
    if any(dest.iterdir()):
        raise UnsafeArchiveError(f"解包目标目录非空: {dest}")
    dest = dest.resolve()

    try:
        opened = tarfile.open(tar_path, mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveFormatError(f"无法作为 tar 归档打开: {exc}") from exc

    with opened as tar:
        for member in tar:
            name = member.name
            if not name or name.startswith("/"):
                raise UnsafeArchiveError(f"不允许的绝对路径成员: {name!r}")
            # 归一化后任何一段为 .. 即视为越界
            parts = Path(name).parts
            if any(part == ".." for part in parts):
                raise UnsafeArchiveError(f"包含父目录跳转的成员: {name!r}")
            if member.type in _BLOCKLISTED_MEMBER_TYPES:
                raise UnsafeArchiveError(f"不支持的成员类型: {name!r}")

            target = _resolve_within(dest, dest.joinpath(*parts))

            if member.type == tarfile.SYMTYPE:
                # 符号链接：链接目标按链接所在目录解析，且不能逃逸
                link_path = target
                link_target = os.path.normpath(
                    os.path.join(os.path.dirname(link_path), member.linkname)
                )
                _resolve_within(dest, Path(link_target))
                os.makedirs(link_path.parent, exist_ok=True)
                if link_path.exists() or link_path.is_symlink():
                    link_path.unlink()
                os.symlink(member.linkname, link_path)
            elif member.type == tarfile.DIRTYPE:
                target.mkdir(parents=True, exist_ok=True)
            elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                with open(target, "wb") as out:
                    if src is not None:
                        out.write(src.read())
            else:
                # 硬链接等其余类型在外层 layout 中没有合法用途
                raise UnsafeArchiveError(f"不支持的成员类型: {name!r}")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 sha256，返回不带算法前缀的十六进制摘要。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
