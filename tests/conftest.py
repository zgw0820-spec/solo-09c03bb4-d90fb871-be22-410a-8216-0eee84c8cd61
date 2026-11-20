"""pytest 夹具：在独立临时目录构造样例 OCI 归档与层合并树。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_sample_oci import build  # noqa: E402

from app.layers import merge_layers  # noqa: E402
from app.mounts import MountRequest, apply_mounts  # noqa: E402
from app.oci import load_image  # noqa: E402


@pytest.fixture()
def sample_archive(tmp_path: Path) -> Path:
    archive = tmp_path / "sample-oci.tar.gz"
    build(archive)
    return archive


@pytest.fixture()
def merged_tree(tmp_path: Path):
    archive = tmp_path / "sample-oci.tar.gz"
    build(archive)
    extract = tmp_path / "layout"
    extract.mkdir()
    import tarfile

    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extract)
    image = load_image(extract)
    return merge_layers(image.layers)


@pytest.fixture()
def make_mount():
    def _apply(tree, mount_point: str, files: list[str]):
        apply_mounts(tree, [MountRequest(mount_point=mount_point, volume_files=tuple(files))])
        return tree

    return _apply
