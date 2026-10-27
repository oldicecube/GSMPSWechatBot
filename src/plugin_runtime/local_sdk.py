"""本地插件 SDK 开发模式支持。"""

from pathlib import Path
from typing import Mapping, Optional

import os
import sys
import tomllib

from src.plugin_runtime import ENV_LOCAL_PLUGIN_SDK_PATH

_SDK_PACKAGE_NAME = "maibot-plugin-sdk"
_SDK_IMPORT_DIR = "maibot_sdk"


def resolve_local_sdk_path(
    environ: Optional[Mapping[str, str]] = None,
    project_root: Optional[Path] = None,
) -> Optional[Path]:
    """解析并校验环境变量指定的本地 SDK 路径。

    Args:
        environ: 环境变量映射；留空时读取 ``os.environ``。
        project_root: 相对路径解析基准；留空时使用当前工作目录。

    Returns:
        Optional[Path]: 已校验的 SDK 仓库路径；未启用时返回 ``None``。

    Raises:
        ValueError: 环境变量存在但路径不是有效的 maibot-plugin-sdk 仓库。
    """

    env = environ or os.environ
    raw_path = str(env.get(ENV_LOCAL_PLUGIN_SDK_PATH, "") or "").strip()
    if not raw_path:
        return None

    base_dir = project_root or Path.cwd()
    sdk_path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not sdk_path.is_absolute():
        sdk_path = base_dir / sdk_path
    sdk_path = sdk_path.resolve()

    _validate_local_sdk_path(sdk_path)
    return sdk_path


def activate_local_sdk_import_path(
    environ: Optional[Mapping[str, str]] = None,
    project_root: Optional[Path] = None,
) -> Optional[Path]:
    """把本地 SDK 仓库加入 ``sys.path`` 的最前面。

    Args:
        environ: 环境变量映射；留空时读取 ``os.environ``。
        project_root: 相对路径解析基准；留空时使用当前工作目录。

    Returns:
        Optional[Path]: 已启用的 SDK 路径；未启用时返回 ``None``。
    """

    sdk_path = resolve_local_sdk_path(environ=environ, project_root=project_root)
    if sdk_path is None:
        return None

    sdk_path_text = str(sdk_path)
    sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != sdk_path]
    sys.path.insert(0, sdk_path_text)
    return sdk_path


def read_local_sdk_version(
    environ: Optional[Mapping[str, str]] = None,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """读取本地 SDK 的项目版本号。

    Args:
        environ: 环境变量映射；留空时读取 ``os.environ``。
        project_root: 相对路径解析基准；留空时使用当前工作目录。

    Returns:
        Optional[str]: 本地 SDK 版本；未启用时返回 ``None``。
    """

    sdk_path = resolve_local_sdk_path(environ=environ, project_root=project_root)
    if sdk_path is None:
        return None
    return _read_sdk_version_from_path(sdk_path)


def build_pythonpath_with_local_sdk(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """构造包含本地 SDK 的 ``PYTHONPATH``。

    Args:
        environ: 环境变量映射；留空时读取 ``os.environ``。

    Returns:
        Optional[str]: 启用本地 SDK 后的新 ``PYTHONPATH``；未启用时返回 ``None``。
    """

    env = environ or os.environ
    sdk_path = resolve_local_sdk_path(environ=env)
    if sdk_path is None:
        return None

    existing_entries = [
        entry
        for entry in str(env.get("PYTHONPATH", "") or "").split(os.pathsep)
        if entry and Path(entry).resolve() != sdk_path
    ]
    return os.pathsep.join([str(sdk_path), *existing_entries])


def _validate_local_sdk_path(sdk_path: Path) -> None:
    if not sdk_path.is_dir():
        raise ValueError(f"{ENV_LOCAL_PLUGIN_SDK_PATH} 指向的目录不存在: {sdk_path}")
    if not (sdk_path / _SDK_IMPORT_DIR).is_dir():
        raise ValueError(f"{ENV_LOCAL_PLUGIN_SDK_PATH} 必须指向包含 {_SDK_IMPORT_DIR}/ 的 SDK 仓库: {sdk_path}")

    project_data = _read_project_data(sdk_path)
    package_name = str(project_data.get("name", "") or "").strip()
    if package_name != _SDK_PACKAGE_NAME:
        raise ValueError(f"{ENV_LOCAL_PLUGIN_SDK_PATH} 指向的项目不是 {_SDK_PACKAGE_NAME}: {sdk_path}")

    version = str(project_data.get("version", "") or "").strip()
    if not version:
        raise ValueError(f"{ENV_LOCAL_PLUGIN_SDK_PATH} 指向的 SDK 缺少 project.version: {sdk_path}")


def _read_sdk_version_from_path(sdk_path: Path) -> str:
    project_data = _read_project_data(sdk_path)
    return str(project_data.get("version", "") or "").strip()


def _read_project_data(sdk_path: Path) -> Mapping[str, object]:
    pyproject_path = sdk_path / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as pyproject_file:
            pyproject_data = tomllib.load(pyproject_file)
    except Exception as exc:
        raise ValueError(f"读取本地 SDK pyproject.toml 失败: {pyproject_path}: {exc}") from exc

    project_data = pyproject_data.get("project", {})
    if not isinstance(project_data, dict):
        raise ValueError(f"本地 SDK pyproject.toml 缺少 [project]: {pyproject_path}")
    return project_data
