"""端到端 API 测试：FastAPI TestClient + 真实样例 OCI tar.gz。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _upload(client: TestClient, archive: Path, target: str, mounts=None):
    data = {"target_path": target}
    if mounts is not None:
        data["mounts"] = json.dumps(mounts)
    with open(archive, "rb") as handle:
        return client.post(
            "/diagnose",
            files={"archive": ("sample.tar.gz", handle, "application/gzip")},
            data=data,
        )


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_diagnose_found_file(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/config.yaml")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "found"
    assert body["resolved_path"] == "/etc/app/config.yaml"
    assert body["source"]["kind"] == "layer"
    assert body["source"]["layer_index"] == 1
    assert body["source"]["digest"].startswith("sha256:")
    assert body["image"]["layer_count"] == 3
    assert body["image"]["platform"]["os"] == "linux"


def test_diagnose_whiteout_not_found(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/secret.env")
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["failure"]["failed_component"] == "secret.env"
    assert body["failure"]["masking"]["reason"] == "whiteout"
    assert body["failure"]["masking"]["layer_index"] == 1


def test_diagnose_opaque_not_found(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/legacy/old.conf")
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["failure"]["masking"]["reason"] == "opaque"
    assert body["failure"]["masking"]["opaque_directory"] == "/etc/app/legacy"


def test_diagnose_same_layer_addition_after_opaque(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/legacy/keep.txt")
    body = resp.json()
    assert body["status"] == "found"
    assert body["source"]["layer_index"] == 2


def test_diagnose_symlink_chain(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/current")
    body = resp.json()
    assert body["status"] == "found"
    assert body["resolved_path"] == "/etc/app/config.yaml"
    assert len(body["symlink_chain"]) == 1


def test_diagnose_symlink_loop(client, sample_archive):
    resp = _upload(client, sample_archive, "/etc/app/loop/a")
    body = resp.json()
    assert body["status"] == "symlink_loop"
    assert body["failure"]["masking"] is None
    assert len(body["symlink_chain"]) >= 2
    assert "环" in body["failure"]["message"]


def test_diagnose_with_mount(client, sample_archive):
    mounts = [
        {
            "mount_point": "/data",
            "volume_files": ["config.yaml", "tls/cert.pem"],
        }
    ]
    # 镜像原文件被挂载遮蔽
    resp = _upload(client, sample_archive, "/data/readme.txt", mounts)
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["failure"]["masking"]["reason"] == "mount"
    assert body["failure"]["masking"]["mount_point"] == "/data"

    # 卷内文件命中挂载来源
    resp2 = _upload(client, sample_archive, "/data/tls/cert.pem", mounts)
    body2 = resp2.json()
    assert body2["status"] == "found"
    assert body2["source"] == {"kind": "mount", "mount_point": "/data"}


def test_mount_point_must_be_existing_directory(client, sample_archive):
    mounts = [{"mount_point": "/no/such", "volume_files": []}]
    resp = _upload(client, sample_archive, "/x", mounts)
    assert resp.status_code == 400
    assert "挂载点" in resp.json()["detail"]


def test_mount_volume_rejects_traversal(client, sample_archive):
    mounts = [{"mount_point": "/data", "volume_files": ["../escape.txt"]}]
    resp = _upload(client, sample_archive, "/data/x", mounts)
    assert resp.status_code == 400


def test_relative_target_path_rejected(client, sample_archive):
    resp = _upload(client, sample_archive, "etc/app/config.yaml")
    assert resp.status_code == 400


def test_broken_archive_rejected(client, tmp_path):
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"not a tar at all" * 10)
    with open(junk, "rb") as handle:
        resp = client.post(
            "/diagnose",
            files={"archive": ("junk.tar", handle, "application/x-tar")},
            data={"target_path": "/x"},
        )
    assert resp.status_code == 400
    assert "归档解析失败" in resp.json()["detail"]


def test_path_traversal_archive_rejected(client, tmp_path, sample_archive):
    """构造含 ../ 成员的恶意 tar，安全解包必须拒绝。"""
    import io
    import tarfile

    evil = tmp_path / "evil.tar"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    evil.write_bytes(buf.getvalue())
    with open(evil, "rb") as handle:
        resp = client.post(
            "/diagnose",
            files={"archive": ("evil.tar", handle, "application/x-tar")},
            data={"target_path": "/x"},
        )
    assert resp.status_code == 400
    assert not (tmp_path.parent / "escape.txt").exists()
