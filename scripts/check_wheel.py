"""在发布/安装前核对 wheel 中每个 Python 文件与当前源码一致，拒绝陈旧产物。"""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile


def check(wheel: Path, source: Path) -> dict[str, object]:
    """按内容哈希及路径集合比较，防止最后编辑晚于构建而误验收旧包。"""
    expected = {
        "codepilot/" + path.relative_to(source).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in source.rglob("*.py")
    }
    with ZipFile(wheel) as archive:
        actual = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("codepilot/") and name.endswith(".py")
        }
    # 多文件、少文件或任意正文差异都必须停止，不能只验证版本字符串。
    if actual != expected:
        changed = sorted(
            name
            for name in actual.keys() | expected.keys()
            if actual.get(name) != expected.get(name)
        )
        raise ValueError("wheel differs from source: " + ", ".join(changed))
    return {
        "status": "passed",
        "python_files": len(expected),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }


def main() -> None:
    """解析一个待安装 wheel，默认以当前仓库源码为唯一内容判据。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src/codepilot"
    )
    args = parser.parse_args()
    print(json.dumps(check(args.wheel, args.source)))


if __name__ == "__main__":
    main()
