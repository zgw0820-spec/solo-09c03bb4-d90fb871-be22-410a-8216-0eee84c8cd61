#!/usr/bin/env python3
"""构造用于演示/测试的单平台 OCI Image Layout tar。

生成一个三层镜像，覆盖发布工程师常见的排障场景：

层 0（底层）
  /etc/app/config.yaml          基础配置
  /etc/app/secret.env           下层密钥（层 1 用 whiteout 删除）
  /etc/app/legacy/old.conf      遗留配置（层 2 用 opaque 整目录遮蔽）
  /etc/app/legacy/keep.txt      同层（层 2）opaque 之后新建，仍应可见
  /opt/app/bin/tool             普通文件
  /data/readme.txt              挂载点目录内的镜像文件（挂载后被遮蔽）
层 1
  whiteout: /etc/app/secret.env
  /etc/app/config.yaml          同层新增覆盖（遮蔽后新增仍可见）
  /var/log/app.log -> /opt/app/bin/tool   绝对符号链接
层 2
  opaque: /etc/app/legacy
  /etc/app/legacy/keep.txt      opaque 同层新增
  /etc/app/current -> config.yaml        相对符号链接
  /etc/app/loop/a -> ../loop/b           与 b 构成环
  /etc/app/loop/b -> ../loop/a

用法:
    python3 scripts/build_sample_oci.py <输出 tar.gz 路径>
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

MEDIA_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_LAYOUT = "application/vnd.oci.image.layout.v1+json"


def _tar_bytes(entries: list[tuple[str, bytes | str, int]]) -> bytes:
    """entries: (arcname, content-bytes-or-symlink-target, 类型)。

    类型: 0=目录, 1=普通文件, 2=符号链接
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        now = 1700000000
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name=name)
            info.mtime = now
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if kind == 0:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == 1:
                assert isinstance(payload, bytes)
                info.type = tarfile.REGTYPE
                info.mode = 0o644
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            else:
                assert isinstance(payload, str)
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.linkname = payload
                tar.addfile(info)
    return buf.getvalue()


def _gzip(data: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(data)
    return out.getvalue()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _add_blob(blobs: dict[str, bytes], data: bytes) -> str:
    digest = _digest(data)
    blobs[digest.split(":", 1)[1]] = data
    return digest


def build(output: Path) -> None:
    blobs: dict[str, bytes] = {}

    layer0_tar = _tar_bytes(
        [
            ("etc/", b"", 0),
            ("etc/app/", b"", 0),
            ("etc/app/config.yaml", b"base: true\nlog_level: info\n", 1),
            ("etc/app/secret.env", b"SECRET_TOKEN=lower-layer-value\n", 1),
            ("etc/app/legacy/", b"", 0),
            ("etc/app/legacy/old.conf", b"old-config-should-be-opaqued\n", 1),
            ("opt/", b"", 0),
            ("opt/app/", b"", 0),
            ("opt/app/bin/", b"", 0),
            ("opt/app/bin/tool", b"binary-placeholder\n", 1),
            ("var/", b"", 0),
            ("var/log/", b"", 0),
            ("data/", b"", 0),
            ("data/readme.txt", b"image-provided-data-file\n", 1),
        ]
    )
    layer1_tar = _tar_bytes(
        [
            ("etc/app/.wh.secret.env", b"", 1),
            ("etc/app/config.yaml", b"base: true\nlog_level: debug\noverride: layer1\n", 1),
            ("var/log/app.log", "/opt/app/bin/tool", 2),
        ]
    )
    layer2_tar = _tar_bytes(
        [
            ("etc/app/legacy/", b"", 0),
            ("etc/app/legacy/.wh..wh..opq", b"", 1),
            ("etc/app/legacy/keep.txt", b"created-in-same-layer-after-opaque\n", 1),
            ("etc/app/current", "config.yaml", 2),
            ("etc/app/loop/", b"", 0),
            ("etc/app/loop/a", "../loop/b", 2),
            ("etc/app/loop/b", "../loop/a", 2),
        ]
    )

    layer_descriptors: list[dict] = []
    diff_ids: list[str] = []
    for idx, tar_data in enumerate((layer0_tar, layer1_tar, layer2_tar)):
        diff_id = _digest(tar_data)
        diff_ids.append(diff_id)
        gz_data = _gzip(tar_data)
        digest = _add_blob(blobs, gz_data)
        layer_descriptors.append(
            {
                "mediaType": MEDIA_LAYER_GZIP,
                "digest": digest,
                "size": len(gz_data),
                "annotations": {"com.example.layer-order": str(idx)},
            }
        )

    config = {
        "created": "2026-11-20T00:00:00Z",
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
            "WorkingDir": "/",
        },
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [
            {"created": "2026-11-20T00:00:00Z", "comment": "base layer"},
            {"created": "2026-11-20T00:01:00Z", "comment": "whiteout + override"},
            {"created": "2026-11-20T00:02:00Z", "comment": "opaque + symlinks"},
        ],
    }
    config_digest = _add_blob(blobs, json.dumps(config, indent=2).encode())

    manifest = {
        "schemaVersion": 2,
        "mediaType": MEDIA_MANIFEST,
        "config": {"mediaType": MEDIA_CONFIG, "digest": config_digest, "size": 0},
        "layers": layer_descriptors,
    }
    # 修正 config size
    manifest["config"]["size"] = len(blobs[config_digest.split(":", 1)[1]])
    manifest_digest = _add_blob(blobs, json.dumps(manifest, indent=2).encode())

    index = {
        "schemaVersion": 2,
        "mediaType": MEDIA_INDEX,
        "manifests": [
            {
                "mediaType": MEDIA_MANIFEST,
                "digest": manifest_digest,
                "size": len(blobs[manifest_digest.split(":", 1)[1]]),
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        ],
    }
    layout = {"imageLayoutVersion": "1.0.0"}

    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, mode="w:gz") as out_tar:
        def _add_text(arcname: str, text: str) -> None:
            data = text.encode()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 1700000000
            out_tar.addfile(info, io.BytesIO(data))

        _add_text("oci-layout", json.dumps(layout, indent=2))
        _add_text("index.json", json.dumps(index, indent=2))
        for hex_digest, data in sorted(blobs.items()):
            info = tarfile.TarInfo(name=f"blobs/sha256/{hex_digest}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 1700000000
            out_tar.addfile(info, io.BytesIO(data))

    print(f"wrote {output} ({output.stat().st_size} bytes, {len(blobs)} blobs)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    build(Path(sys.argv[1]))
