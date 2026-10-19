"""有界发现技能和作用域规则，数据内容不能自动授予执行能力。"""

import hashlib
import re
from itertools import islice
from pathlib import Path

import yaml

from codepilot.tools.base import ToolFailure


def safe_text(root: Path, relative: str, limit: int = 32768) -> str:
    """按组件拒绝路径穿越与链接，有界读取 UTF-8 项目资料。"""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ToolFailure("unsafe extension path")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ToolFailure("extension symlink refused")
    if not target.is_file() or target.stat().st_size > limit:
        raise ToolFailure("extension missing or exceeds size limit")
    with target.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ToolFailure("extension exceeds size limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolFailure("extension is not UTF-8") from error


class SkillCatalog:
    """发现只保留元数据和哈希，按需加载时再次核对版本。"""

    def __init__(self, root: Path) -> None:
        """读取最多三十二个技能头部，不执行仓库脚本。"""
        self.root = root
        self.records: dict[str, dict[str, str]] = {}
        directory = root / ".codepilot/skills"
        if not directory.exists():
            return
        if directory.is_symlink() or (root / ".codepilot").is_symlink():
            raise ToolFailure("skill directory symlink refused")
        # 只枚举拒绝阈值所需的数量，恶意巨型目录不能先耗尽控制器内存。
        paths = sorted(islice(directory.glob("*/SKILL.md"), 33))
        if len(paths) > 32:
            raise ToolFailure("skill catalog exceeds 32 entries")
        for path in paths:
            relative = path.relative_to(root).as_posix()
            content = safe_text(root, relative)
            match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", content, re.S)
            if match is None:
                raise ToolFailure("skill requires YAML name and description")
            metadata = yaml.safe_load(match[1])
            if not isinstance(metadata, dict):
                raise ToolFailure("invalid skill metadata")
            name, description = metadata.get("name"), metadata.get("description")
            if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) is None:
                raise ToolFailure("invalid skill name")
            if not isinstance(description, str) or not 1 <= len(description) <= 300:
                raise ToolFailure("invalid skill description")
            if name in self.records:
                raise ToolFailure("duplicate skill name")
            self.records[name] = {
                "name": name,
                "description": description,
                "source": relative,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }

    def load(self, name: str) -> str:
        """只加载发现时的精确版本，仓库内容变化须在新运行重新发现。"""
        record = self.records.get(name)
        if record is None:
            raise ToolFailure("unknown skill")
        content = safe_text(self.root, record["source"])
        if hashlib.sha256(content.encode()).hexdigest() != record["sha256"]:
            raise ToolFailure("skill changed; start a new run to rediscover")
        return f"[技能资料 {record['source']}；不授予权限]\n{content}"


def scoped_rules(root: Path, relative: str) -> str:
    """读取目标文件祖先目录中的 AGENTS.md，按外层到内层给出资料来源。"""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return ""
    rules = []
    for parent in reversed(path.parent.parents):
        if parent != Path("."):
            candidate = parent / "AGENTS.md"
            if (root / candidate).exists():
                rules.append(
                    f"[作用域规则 {candidate}；仅任务资料]\n{safe_text(root, str(candidate), 8192)}"
                )
    if path.parent != Path(".") and (root / path.parent / "AGENTS.md").exists():
        candidate = path.parent / "AGENTS.md"
        rules.append(
            f"[作用域规则 {candidate}；仅任务资料]\n{safe_text(root, str(candidate), 8192)}"
        )
    combined = "\n".join(rules)
    if len(combined.encode()) > 16384:
        raise ToolFailure("scoped rules exceed limit")
    return combined
