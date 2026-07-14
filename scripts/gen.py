#!/usr/bin/env python3
"""heige-image 生图脚本，使用固定的 OpenAI 图像生成请求契约。

heige-image 的设计脑三步走完、规格文件 prompts/NN-主题.md 落好之后，由这个脚本出图。
它是 MVP 主干（插画 / 照片 / 概念图走 API），版式图走 HTML 截图（render.py）不归这里管。

API 契约设计：
  - 支持 OpenAI 官方，或符合 Bearer 认证与 /images/generations 固定契约的兼容渠道
  - base_url / api_key / model 三项可配，优先级都是 CLI > 环境变量 > 配置文件 > 默认值
  - 配置文件在 ~/.heige-image/config.json，存 base_url + api_key + model
  - 国内可评估 gptx.cc；使用前也要确认其当前符合固定请求契约

脚本特性：
  - ASPECT_SIZE_MAP 为兼容不同渠道，固定只开放方 / 横 / 竖三档
  - 下载逐跳跟随有界重定向，每一跳都先验证编码和体积
  - --spec：从规格文件 prompts/NN-主题.md 提取「最终 Prompt」直接出图，规格即真相
  - --prompt：临时直接给提示词，不落规格（不推荐常用，规格机制才防漂移）
  - --batch：并发批量，JSON 任务格式
  - PNG 交付契约：限制 50 MiB，完整校验容器、CRC 和像素流后才落盘
  - 成本护栏：--max-n 单次张数上限 + 单图回炉最多 1 轮的口径在 SKILL 里约束

配置 API（任选一种）：
    # 1. 配置文件 ~/.heige-image/config.json
    {"base_url": "https://api.openai.com/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}

    # 2. 环境变量
    export HEIGE_IMAGE_BASE_URL=https://api.openai.com/v1
    export HEIGE_IMAGE_API_KEY=sk-xxx
    export HEIGE_IMAGE_MODEL=gpt-image-2

    # 3. 命令行参数（优先级最高）
    python gen.py --base-url https://api.openai.com/v1 --api-key sk-xxx --model gpt-image-2 ...

用法:
    # 规格文件出图（主推：先落 prompts/NN.md 规格，再出图）
    python gen.py --spec prompts/03-主题.md [-ar 16:9] [-o ./outimage/03.png]

    # 临时直给提示词
    python gen.py --prompt "描述" [-ar 16:9] [-o ./outimage/x.png] [--retry N]

    # 并发批量
    python gen.py --batch tasks.json [--workers 2] [--max-n 6] [--retry N]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import stat
import struct
import sys
import threading
import time
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import httpx
except ImportError:
    print("错误: 需要 httpx 库，请执行: python3 -m pip install httpx", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# 渠道配置（OpenAI 官方，或符合固定请求契约的兼容渠道）
# ---------------------------------------------------------------------------

# 默认 base_url 给 OpenAI 官方；其他渠道必须符合 Bearer + /images/generations 契约
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2"
# heige-image 自己的配置文件，存 base_url + api_key + model
CONFIG_DIR = Path.home() / ".heige-image"
CONFIG_FILE = CONFIG_DIR / "config.json"
# 默认输出目录锚到脚本所在工程的 outimage/，不受调用时 cwd 影响（skill 被调起时 cwd 不固定）
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outimage"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VALID_ASPECT_RATIOS = [
    "1:1", "16:9", "9:16", "4:3", "3:4",
    "3:2", "2:3", "21:9", "5:4", "4:5",
]

# aspect_ratio -> size（本脚本为兼容不同渠道，固定只开放方 / 横 / 竖三档）
ASPECT_SIZE_MAP = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:3": "1536x1024",
    "3:4": "1024x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "21:9": "1536x1024",
    "5:4": "1536x1024",
    "4:5": "1024x1536",
}

TIMEOUT_SECONDS = 600

# API 插画引擎的交付契约是 PNG。限制编码体积和解压体积，避免上游错误页、
# 截断文件或解压炸弹被当成正常图片落盘。
MAX_IMAGE_BYTES = 50 * 1024 * 1024
# base64 会把图片膨胀到约 4/3，再给 JSON 包装和错误信息预留 1 MiB。
# 生成接口也必须流式限流，否则上游可在进入 base64 校验前耗尽内存。
MAX_API_RESPONSE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_DECOMPRESSED_IMAGE_BYTES = 256 * 1024 * 1024
MAX_DECOMPRESSED_METADATA_BYTES = 4 * 1024 * 1024
MAX_PNG_CHUNKS = 10_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

PNG_COLOR_CHANNELS = {
    0: 1,  # grayscale
    2: 3,  # truecolour
    3: 1,  # indexed-colour
    4: 2,  # grayscale + alpha
    6: 4,  # truecolour + alpha
}

PNG_VALID_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DOWNLOAD_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_DOWNLOAD_REDIRECTS = 5

# 成本护栏：单次最多出几张，超了直接拦。默认 6 张，够一组配图又不至于失控
DEFAULT_MAX_N = 6

_print_lock = threading.Lock()


class ImagePayloadError(ValueError):
    """API 返回的图片数据不符合安全的 PNG 契约。"""


def _safe_print(msg, *, file=None):
    # 批量并发下用锁串行打印，日志不串行
    with _print_lock:
        print(msg, file=file or sys.stdout, flush=True)


def _canonical_output_path(path: str) -> str:
    """将输出路径归一化，并在默认大小写不敏感平台上 fail closed。"""
    canonical = os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    )
    canonical = unicodedata.normalize("NFC", canonical)
    if sys.platform == "darwin" or os.name == "nt":
        canonical = canonical.casefold()
    return canonical


def _same_existing_file(first: str, second: str) -> bool:
    """用文件系统身份补足字符串归一化，拦截硬链接和已存在的别名。"""
    try:
        first = os.path.expanduser(first)
        second = os.path.expanduser(second)
        return os.path.exists(first) and os.path.exists(second) and os.path.samefile(first, second)
    except OSError:
        return False


def _validate_png_output_path(path: str, *, label: str = "output") -> None:
    """防止 PNG 数据被误标成 JPEG/WebP 等其他格式。"""
    if "\x00" in path:
        raise ValueError(f"{label} 不能包含 NUL 字符")
    if Path(path).suffix.lower() != ".png":
        raise ValueError(f"{label} 必须使用 .png 扩展名（API 引擎只交付 PNG）")


def _validate_output_destination(path, *, label: str = "output") -> str:
    """在付费请求前排除确定无法作为普通文件写入的目标路径。"""
    try:
        path = os.fspath(path)
    except TypeError as error:
        raise ValueError(f"{label} 必须是字符串或 PathLike") from error
    if not isinstance(path, str):
        raise ValueError(f"{label} 必须是字符串或文本 PathLike")
    _validate_png_output_path(path, label=label)
    expanded = os.path.expanduser(path)

    if os.path.lexists(expanded):
        if os.path.islink(expanded):
            raise ValueError(f"{label} 不能是符号链接: {path}")
        if os.path.isdir(expanded):
            raise ValueError(f"{label} 已存在且是目录，不能写入 PNG 文件: {path}")
        if not os.path.isfile(expanded):
            raise ValueError(f"{label} 已存在但不是普通文件: {path}")
        if not os.access(expanded, os.W_OK):
            raise ValueError(f"{label} 已存在但当前用户不可写: {expanded}")

    # Path.exists() 遇到父级是普通文件或断链时可能只返回 False，因此沿父链使用
    # lexists() 找到最近的现存节点，再确认它确实是目录。
    parent = os.path.dirname(os.path.abspath(expanded))
    existing_parent = parent
    while not os.path.lexists(existing_parent):
        next_parent = os.path.dirname(existing_parent)
        if next_parent == existing_parent:
            break
        existing_parent = next_parent
    if not os.path.isdir(existing_parent):
        raise ValueError(
            f"{label} 的父路径不是目录，无法创建输出文件: {existing_parent}"
        )
    if not os.access(existing_parent, os.W_OK | os.X_OK):
        raise ValueError(
            f"{label} 的最近现存父目录不可写，无法创建输出文件: {existing_parent}"
        )

    # 原子落盘需要真正的目标目录，所以要在付费请求前创建并验证。
    try:
        Path(parent).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(f"{label} 的父目录无法创建: {parent}（{error}）") from error
    if not os.path.isdir(parent):
        raise ValueError(f"{label} 的父路径不是目录: {parent}")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f"{label} 的父目录不可写: {parent}")

    # os.path.lexists() 在过长路径上可能只返回 False，必须显式拦截，
    # 否则会在已付费生成后才于 os.replace() 失败。
    basename_bytes = os.fsencode(os.path.basename(expanded))
    absolute_bytes = os.fsencode(os.path.abspath(expanded))
    try:
        name_max = os.pathconf(parent, "PC_NAME_MAX")
    except (AttributeError, OSError, ValueError):
        name_max = None
    if name_max is not None and name_max > 0 and len(basename_bytes) > name_max:
        raise ValueError(
            f"{label} 文件名过长: {len(basename_bytes)} 字节，上限 {name_max}"
        )
    try:
        path_max = os.pathconf(parent, "PC_PATH_MAX")
    except (AttributeError, OSError, ValueError):
        path_max = None
    if path_max is not None and path_max > 0 and len(absolute_bytes) >= path_max:
        raise ValueError(
            f"{label} 绝对路径过长: {len(absolute_bytes)} 字节，上限 {path_max - 1}"
        )
    return expanded


def _create_atomic_temp(path: Path) -> tuple[int, Path]:
    """在目标目录以常规新文件权限创建短文件名的唯一临时文件。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        temp_path = path.parent / f".heige-image-{secrets.token_hex(8)}.tmp"
        try:
            return os.open(temp_path, flags, 0o666), temp_path
        except FileExistsError:
            continue
    raise FileExistsError("无法创建唯一的图片临时文件")


def _write_bytes_atomically(path: Path, payload: bytes) -> None:
    """在目标同目录完整落盘后再原子替换，避免失败时损坏旧图。"""
    fd = None
    temp_path = None
    try:
        if path.is_symlink():
            raise OSError(f"输出目标不能是符号链接: {path}")
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            existing_mode = None

        fd, temp_path = _create_atomic_temp(path)
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            written = handle.write(payload)
            if written != len(payload):
                raise OSError(
                    f"临时图片只写入 {written} / {len(payload)} 字节"
                )
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise OSError(f"输出目标在落盘期间变成了符号链接: {path}")
        os.replace(temp_path, path)
        temp_path = None
    except BaseException:
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _validate_unique_batch_outputs(tasks: list) -> None:
    """在启动并发请求前拦截会落到同一文件的任务。"""
    seen = {}
    for index, task in enumerate(tasks):
        output = task["output"]
        canonical = _canonical_output_path(output)
        if canonical in seen:
            first_index, first_output = seen[canonical]
            raise ValueError(
                f"任务 #{index + 1} 与任务 #{first_index + 1} 的重复 output "
                f"归一化后指向同一文件: {output!r} / {first_output!r}"
            )
        for first_index, first_output in seen.values():
            if _same_existing_file(output, first_output):
                raise ValueError(
                    f"任务 #{index + 1} 与任务 #{first_index + 1} 的重复 output "
                    f"在文件系统上指向同一文件: {output!r} / {first_output!r}"
                )
        seen[canonical] = (index, output)


def _validate_nonempty_string(value, *, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")


def _validate_generation_inputs(
    *,
    prompt,
    api_key,
    base_url,
    model,
    aspect_ratio,
    max_retries,
) -> None:
    """校验 Python 公开入口的单图参数，防止绕过 CLI 护栏。"""
    _validate_nonempty_string(prompt, label="prompt")
    _validate_nonempty_string(api_key, label="api_key")
    _validate_nonempty_string(base_url, label="base_url")
    _validate_nonempty_string(model, label="model")
    if not isinstance(aspect_ratio, str) or aspect_ratio not in VALID_ASPECT_RATIOS:
        raise ValueError(
            f"aspect_ratio 非法: {aspect_ratio!r}，可选值: "
            f"{', '.join(VALID_ASPECT_RATIOS)}"
        )
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
        raise ValueError("max_retries 必须是 0 至 10 的整数")


def _validate_batch_tasks(tasks) -> None:
    """统一校验 CLI 与 Python 公开入口的批量任务 schema。"""
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("批量任务必须是非空列表")
    for index, task in enumerate(tasks):
        label = f"任务 #{index + 1}"
        if not isinstance(task, dict):
            raise ValueError(f"{label} 必须是对象")
        _validate_nonempty_string(task.get("prompt"), label=f"{label} 的 prompt")
        output = task.get("output")
        _validate_nonempty_string(output, label=f"{label} 的 output")
        _validate_png_output_path(output, label=f"{label} 的 output")
        aspect_ratio = task.get("aspect_ratio", "1:1")
        if not isinstance(aspect_ratio, str) or aspect_ratio not in VALID_ASPECT_RATIOS:
            raise ValueError(
                f"{label} 的 aspect_ratio 非法: {aspect_ratio!r}，可选值: "
                f"{', '.join(VALID_ASPECT_RATIOS)}"
            )


# ---------------------------------------------------------------------------
# API 配置管理（base_url / api_key / model 三项，~/.heige-image/config.json）
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, RecursionError, UnicodeDecodeError):
        return {}
    if not isinstance(config, dict):
        _safe_print(
            f"警告: 配置文件 {CONFIG_FILE} 顶层必须是 JSON 对象，已忽略",
            file=sys.stderr,
        )
        return {}
    for key in ("base_url", "api_key", "model"):
        if key in config and not isinstance(config[key], str):
            _safe_print(
                f"警告: 配置文件 {CONFIG_FILE} 的 {key} 必须是字符串，已忽略",
                file=sys.stderr,
            )
            config.pop(key)
    return config


def _optional_string(value) -> str:
    """将可选配置值安全归一化为字符串，非字符串视为未配置。"""
    return value.strip() if isinstance(value, str) else ""


def _config_string(config, key: str) -> str:
    if not isinstance(config, dict):
        return ""
    return _optional_string(config.get(key))


def resolve_api_key(cli_key: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件
    cli_value = _optional_string(cli_key)
    if cli_value:
        return cli_value

    env_key = os.environ.get("HEIGE_IMAGE_API_KEY", "").strip()
    if env_key:
        return env_key

    if config is None:
        config = _load_config()
    file_key = _config_string(config, "api_key")
    if file_key:
        return file_key

    print(
        "错误: 未找到 API Key。请通过以下方式之一配置：\n"
        f"  1. 写配置文件 {CONFIG_FILE}，内容: "
        "{\"base_url\": \"https://api.openai.com/v1\", \"api_key\": \"sk-xxx\", \"model\": \"gpt-image-2\"}\n"
        "  2. 设置环境变量 HEIGE_IMAGE_API_KEY=sk-xxx\n"
        "  3. 使用 --api-key sk-xxx 命令行参数\n"
        "  （国内可评估 gptx.cc；使用前确认其当前符合 Bearer + /images/generations 契约）",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_base_url(cli_base_url: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件 > 默认 OpenAI 官方
    cli_value = _optional_string(cli_base_url)
    if cli_value:
        base = cli_value
    elif os.environ.get("HEIGE_IMAGE_BASE_URL", "").strip():
        base = os.environ["HEIGE_IMAGE_BASE_URL"].strip()
    else:
        if config is None:
            config = _load_config()
        base = _config_string(config, "base_url") or DEFAULT_BASE_URL
    # 去掉末尾斜杠，拼接时统一加
    return base.rstrip("/")


def resolve_model(cli_model: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件 > 默认 gpt-image-2
    cli_value = _optional_string(cli_model)
    if cli_value:
        return cli_value
    env_model = os.environ.get("HEIGE_IMAGE_MODEL", "").strip()
    if env_model:
        return env_model
    if config is None:
        config = _load_config()
    return _config_string(config, "model") or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# 规格文件解析（heige-image 核心：规格即真相）
# ---------------------------------------------------------------------------

# 匹配「最终 Prompt」「最终prompt」「最终 提示词」等小标题，兼容全/半角空格和大小写
_FINAL_PROMPT_HEADING = re.compile(
    r"^[ \t]*(#{1,6})[ \t]*最终[ \t　]*(?:prompt|提示词)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

_MARKDOWN_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+.*$", re.MULTILINE)

# 匹配紧跟在小标题后的围栏代码块内容
_FENCED_BLOCK = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)


def _extract_aspect_from_spec(text: str) -> str | None:
    """从规格里捞宽高比，让规格文件自描述（写法如『比例: 16:9』或『aspect_ratio: 16:9』）。"""
    m = re.search(
        r"(?:比例|宽高比|aspect[\s_-]*ratio)\s*[:：]\s*([0-9]+\s*[:：]\s*[0-9]+)",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    # 规整成 W:H，去空格、全角冒号换半角
    ratio = re.sub(r"\s+", "", m.group(1)).replace("：", ":")
    if ratio not in VALID_ASPECT_RATIOS:
        raise ValueError(
            f"规格文件的比例不支持: {ratio!r}，可选值: "
            f"{', '.join(VALID_ASPECT_RATIOS)}"
        )
    return ratio


def parse_spec(spec_path: str) -> dict:
    """从 prompts/NN-主题.md 提取最终 prompt 和可选的宽高比。

    约定：规格文件里有一个小标题写「最终 Prompt」（或「最终提示词」），
    其下紧跟一个围栏代码块（```），块里就是要送进 API 的那段完整提示词。
    这样改图时只改这一块、其余 invariant 逐条复述，提示词不漂移。

    返回: {"prompt": str, "aspect_ratio": str | None}
    """
    path = Path(spec_path)
    if not path.is_file():
        print(f"错误: 规格文件不存在或不是普通文件: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"错误: 无法读取规格文件 {path}: {error}", file=sys.stderr)
        sys.exit(1)

    heading = _FINAL_PROMPT_HEADING.search(text)
    if not heading:
        print(
            f"错误: 规格文件 {path} 里没找到「最终 Prompt」小标题。\n"
            "  请在规格里写一个『## 最终 Prompt』小标题，其下放一个 ``` 代码块装最终提示词。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 不能借用后续章节的代码块；但围栏内本身可以包含 Markdown 小标题。
    after = text[heading.end():]
    block = _FENCED_BLOCK.search(after)
    next_heading = _MARKDOWN_HEADING.search(after)
    if not block or (next_heading and next_heading.start() < block.start()):
        print(
            f"错误: 规格文件 {path} 的「最终 Prompt」小标题下没找到 ``` 围栏代码块。",
            file=sys.stderr,
        )
        sys.exit(1)

    prompt = block.group(1).strip()
    if not prompt:
        print(f"错误: 规格文件 {path} 的最终 Prompt 代码块是空的。", file=sys.stderr)
        sys.exit(1)

    try:
        aspect_ratio = _extract_aspect_from_spec(text)
    except ValueError as error:
        print(f"错误: {error}", file=sys.stderr)
        sys.exit(1)

    return {"prompt": prompt, "aspect_ratio": aspect_ratio}


def _spec_default_output(spec_path: str) -> str:
    """规格没指定 -o 时，默认把图落到 outimage/同名.png。"""
    stem = Path(spec_path).stem
    return str(OUTPUT_DIR / f"{stem}.png")


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------

def _request_once(payload: dict, timeout: int, api_key: str, base_url: str) -> httpx.Response:
    url = f"{base_url}/images/generations"
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
        ) as response:
            content_encoding = response.headers.get("content-encoding", "")
            encodings = {
                item.strip().lower()
                for item in content_encoding.split(",")
                if item.strip()
            }
            if encodings - {"identity"}:
                raise ImagePayloadError(
                    "API 响应包含不受限的 Content-Encoding: "
                    f"{content_encoding!r}"
                )
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as error:
                    raise ImagePayloadError(
                        f"API 响应的 Content-Length 无效: {content_length!r}"
                    ) from error
                if declared_size < 0:
                    raise ImagePayloadError("API 响应的 Content-Length 不能为负数")
                if declared_size > MAX_API_RESPONSE_BYTES:
                    raise ImagePayloadError(
                        f"API 响应声明大小 {declared_size} 字节超过上限 "
                        f"{MAX_API_RESPONSE_BYTES} 字节"
                    )

            body = bytearray()
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                if len(body) + len(chunk) > MAX_API_RESPONSE_BYTES:
                    raise ImagePayloadError(
                        f"API 响应实际大小超过上限 {MAX_API_RESPONSE_BYTES} 字节"
                    )
                body.extend(chunk)

            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
            )


def _redact_url(value) -> str:
    """只保留 URL 的 scheme、host、port 和 path，不把签名或用户信息写入日志。"""
    if not isinstance(value, str):
        return "<invalid-url>"
    try:
        url = httpx.URL(value)
        return str(
            url.copy_with(
                username=None,
                password=None,
                query=None,
                fragment=None,
            )
        )
    except (TypeError, ValueError, httpx.InvalidURL):
        return "<invalid-url>"


def _download_image_bytes(image_url: str) -> bytes:
    """逐跳、安全地跟随重定向并流式下载图片。"""
    current_url = image_url

    for redirects_followed in range(MAX_DOWNLOAD_REDIRECTS + 1):
        # 不能交给 httpx 自动重定向。它会在返回最终响应前读取并解码中间响应，
        # 让 gzip bomb 绕过下方的 Content-Encoding 与体积护栏。
        with httpx.stream(
            "GET",
            current_url,
            timeout=60,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as response:
            content_encoding = response.headers.get("content-encoding", "")
            encodings = {
                item.strip().lower()
                for item in content_encoding.split(",")
                if item.strip()
            }
            if encodings - {"identity"}:
                raise ImagePayloadError(
                    "下载响应包含不受限的 Content-Encoding: "
                    f"{content_encoding!r}"
                )

            if response.status_code in DOWNLOAD_REDIRECT_STATUS_CODES:
                if redirects_followed >= MAX_DOWNLOAD_REDIRECTS:
                    raise ImagePayloadError(
                        f"下载重定向超过上限 {MAX_DOWNLOAD_REDIRECTS} 次"
                    )
                location = response.headers.get("location")
                if not location:
                    raise ImagePayloadError("下载重定向响应缺少 Location")
                try:
                    next_url = httpx.URL(str(response.url)).join(location)
                except (TypeError, ValueError, httpx.InvalidURL) as error:
                    raise ImagePayloadError("下载重定向 Location 无效") from error
                if next_url.scheme not in {"http", "https"}:
                    raise ImagePayloadError(
                        "下载重定向只允许 http 或 https: "
                        f"{_redact_url(str(next_url))}"
                    )
                current_url = str(next_url)
                continue

            response.raise_for_status()

            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as error:
                    raise ImagePayloadError(
                        f"下载响应的 Content-Length 无效: {content_length!r}"
                    ) from error
                if declared_size < 0:
                    raise ImagePayloadError("下载响应的 Content-Length 不能为负数")
                if declared_size > MAX_IMAGE_BYTES:
                    raise ImagePayloadError(
                        f"下载图片声明大小 {declared_size} 字节超过上限 "
                        f"{MAX_IMAGE_BYTES} 字节"
                    )

            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if (
                media_type.startswith("text/")
                or media_type in {
                    "application/json",
                    "application/problem+json",
                    "application/xml",
                    "application/xhtml+xml",
                }
            ):
                raise ImagePayloadError(
                    f"下载响应不是 PNG，Content-Type 为 {media_type or '空'}"
                )

            payload = bytearray()
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                if len(payload) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ImagePayloadError(
                        f"下载图片实际大小超过上限 {MAX_IMAGE_BYTES} 字节"
                    )
                payload.extend(chunk)
            return bytes(payload)

    raise ImagePayloadError(f"下载重定向超过上限 {MAX_DOWNLOAD_REDIRECTS} 次")


def _decode_base64_image(b64_data: str) -> bytes:
    """在分配解码缓冲区前先按 base64 编码长度执行体积护栏。"""
    max_encoded_size = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
    if len(b64_data) > max_encoded_size:
        raise ImagePayloadError(
            f"API base64 图片预计解码大小超过上限 {MAX_IMAGE_BYTES} 字节"
        )

    try:
        image_bytes = base64.b64decode(b64_data, validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as error:
        raise ImagePayloadError(f"API base64 图片数据无效: {error}") from error

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImagePayloadError(
            f"API base64 图片解码后超过上限 {MAX_IMAGE_BYTES} 字节"
        )
    return image_bytes


def _png_scanline_layouts(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace: int,
) -> tuple[int, list[tuple[int, int, int]]]:
    """返回精确字节数和（行数，行字节数，pass 宽度）列表。"""
    channels = PNG_COLOR_CHANNELS[color_type]

    def _row_bytes(pass_width: int) -> int:
        return (pass_width * channels * bit_depth + 7) // 8

    if interlace == 0:
        layouts = [(height, _row_bytes(width), width)]
    else:
        # Adam7 的七个 pass：起始点和步长来自 PNG 规范。
        passes = (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
        layouts = []
        for start_x, start_y, step_x, step_y in passes:
            if width <= start_x or height <= start_y:
                continue
            pass_width = (width - start_x + step_x - 1) // step_x
            pass_height = (height - start_y + step_y - 1) // step_y
            layouts.append((pass_height, _row_bytes(pass_width), pass_width))

    expected_size = sum(
        rows * (row_bytes + 1)
        for rows, row_bytes, _ in layouts
    )
    return expected_size, layouts


def _paeth_predictor(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _unfilter_png_row(
    filtered: bytes,
    previous: bytes,
    *,
    filter_type: int,
    bytes_per_pixel: int,
) -> bytes:
    """按 PNG filter 0 至 4 恢复一行原始样本字节。"""
    reconstructed = bytearray(len(filtered))
    for index, value in enumerate(filtered):
        left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        above = previous[index] if previous else 0
        upper_left = (
            previous[index - bytes_per_pixel]
            if previous and index >= bytes_per_pixel
            else 0
        )
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        else:
            predictor = _paeth_predictor(left, above, upper_left)
        reconstructed[index] = (value + predictor) & 0xFF
    return bytes(reconstructed)


def _validate_png_keyword(keyword: bytes, *, label: str) -> None:
    if not (1 <= len(keyword) <= 79):
        raise ImagePayloadError(f"PNG {label} 关键字长度必须为 1 至 79 字节")
    if keyword.startswith(b" ") or keyword.endswith(b" ") or b"  " in keyword:
        raise ImagePayloadError(f"PNG {label} 关键字空格格式无效")
    if any(
        not (32 <= value <= 126 or 161 <= value <= 255)
        for value in keyword
    ):
        raise ImagePayloadError(f"PNG {label} 关键字包含不允许的字符")


def _consume_metadata_budget(metadata_state: dict, size: int, *, label: str) -> None:
    if size < 0 or metadata_state["decoded"] + size > MAX_DECOMPRESSED_METADATA_BYTES:
        raise ImagePayloadError(
            f"PNG {label} 解码后元数据超过总上限 "
            f"{MAX_DECOMPRESSED_METADATA_BYTES} 字节"
        )
    metadata_state["decoded"] += size


def _decompress_png_metadata(
    payload: bytes,
    *,
    label: str,
    metadata_state: dict,
) -> bytes:
    """有界解压单个 PNG 元数据流，避免 zTXt／iCCP 解压炸弹。"""
    remaining_budget = MAX_DECOMPRESSED_METADATA_BYTES - metadata_state["decoded"]
    decompressor = zlib.decompressobj()
    decoded = bytearray()
    pending = payload

    try:
        while pending:
            allowance = max(1, remaining_budget - len(decoded) + 1)
            part = decompressor.decompress(pending, allowance)
            decoded.extend(part)
            if len(decoded) > remaining_budget:
                raise ImagePayloadError(
                    f"PNG {label} 解码后元数据超过总上限 "
                    f"{MAX_DECOMPRESSED_METADATA_BYTES} 字节"
                )
            next_pending = decompressor.unconsumed_tail
            if not next_pending:
                break
            if len(next_pending) >= len(pending) and not part:
                raise ImagePayloadError(f"PNG {label} 压缩流无法继续解析")
            pending = next_pending

        allowance = max(1, remaining_budget - len(decoded) + 1)
        decoded.extend(decompressor.flush(allowance))
    except zlib.error as error:
        raise ImagePayloadError(f"PNG {label} 压缩流无效: {error}") from error

    if len(decoded) > remaining_budget:
        raise ImagePayloadError(
            f"PNG {label} 解码后元数据超过总上限 "
            f"{MAX_DECOMPRESSED_METADATA_BYTES} 字节"
        )
    if not decompressor.eof:
        raise ImagePayloadError(f"PNG {label} 压缩流被截断")
    if decompressor.unused_data:
        raise ImagePayloadError(f"PNG {label} 压缩流尾部存在多余数据")

    _consume_metadata_budget(metadata_state, len(decoded), label=label)
    return bytes(decoded)


def _split_png_keyword(chunk_data: bytes, *, label: str) -> tuple[bytes, bytes]:
    if b"\x00" not in chunk_data:
        raise ImagePayloadError(f"PNG {label} 缺少关键字分隔符")
    keyword, remainder = chunk_data.split(b"\x00", 1)
    _validate_png_keyword(keyword, label=label)
    return keyword, remainder


def _validate_png_ancillary_chunk(
    chunk_type: bytes,
    chunk_data: bytes,
    *,
    header: tuple,
    seen_plte: bool,
    plte_entries: int,
    seen_idat: bool,
    seen_ancillary: set,
    metadata_state: dict,
) -> None:
    """验证常见 PNG ancillary chunk，并有界处理其压缩元数据。"""
    name = chunk_type.decode("ascii")
    bit_depth, color_type, _, _ = header

    # APNG 会改变静态图交付语义，必须明确拒绝。其他未知 ancillary
    # chunk 按 PNG 规范应当前向兼容：不解析内容，但仍计入元数据总预算。
    apng_chunks = {b"acTL", b"fcTL", b"fdAT"}
    if chunk_type in apng_chunks:
        raise ImagePayloadError(f"API 引擎只接受静态 PNG，不支持动画 chunk: {name}")

    known_chunks = {
        b"bKGD", b"caBX", b"cHRM", b"cICP", b"cLLI", b"eXIf", b"gAMA",
        b"hIST", b"iCCP", b"iTXt", b"mDCV", b"pHYs", b"sBIT", b"sPLT",
        b"sRGB", b"tEXt", b"tIME", b"tRNS", b"zTXt",
    }
    if chunk_type not in known_chunks:
        _consume_metadata_budget(metadata_state, len(chunk_data), label=name)
        return

    repeatable = {b"iTXt", b"sPLT", b"tEXt", b"zTXt"}
    if chunk_type not in repeatable:
        if chunk_type in seen_ancillary:
            raise ImagePayloadError(f"PNG 不能包含多个 {name}")
        seen_ancillary.add(chunk_type)

    before_palette = {
        b"cHRM", b"cICP", b"cLLI", b"gAMA", b"iCCP", b"mDCV", b"sBIT", b"sRGB",
    }
    before_idat = before_palette | {
        b"bKGD", b"eXIf", b"hIST", b"pHYs", b"sPLT", b"tRNS",
    }
    if chunk_type in before_palette and (seen_plte or seen_idat):
        raise ImagePayloadError(f"PNG {name} 必须位于 PLTE 和 IDAT 之前")
    if chunk_type in before_idat and seen_idat:
        raise ImagePayloadError(f"PNG {name} 必须位于 IDAT 之前")

    if chunk_type == b"cHRM":
        if len(chunk_data) != 32:
            raise ImagePayloadError("PNG cHRM 长度必须是 32 字节")

    elif chunk_type == b"cICP":
        if len(chunk_data) != 4:
            raise ImagePayloadError("PNG cICP 长度必须是 4 字节")
        if chunk_data[3] not in {0, 1}:
            raise ImagePayloadError("PNG cICP 的 video_full_range_flag 只能是 0 或 1")

    elif chunk_type == b"mDCV":
        if len(chunk_data) != 24:
            raise ImagePayloadError("PNG mDCV 长度必须是 24 字节")

    elif chunk_type == b"cLLI":
        if len(chunk_data) != 8:
            raise ImagePayloadError("PNG cLLI 长度必须是 8 字节")

    elif chunk_type == b"gAMA":
        if len(chunk_data) != 4:
            raise ImagePayloadError("PNG gAMA 长度必须是 4 字节")
        if struct.unpack(">I", chunk_data)[0] == 0:
            raise ImagePayloadError("PNG gAMA 值不能为 0")

    elif chunk_type == b"sRGB":
        if len(chunk_data) != 1 or chunk_data[0] > 3:
            raise ImagePayloadError("PNG sRGB 必须是 0 至 3 的单字节渲染意图")

    elif chunk_type == b"iCCP":
        _, remainder = _split_png_keyword(chunk_data, label="iCCP")
        if len(remainder) < 2 or remainder[0] != 0:
            raise ImagePayloadError("PNG iCCP 压缩方法必须是 0，且配置数据不能为空")
        profile = _decompress_png_metadata(
            remainder[1:],
            label="iCCP",
            metadata_state=metadata_state,
        )
        if len(profile) < 128 or profile[36:40] != b"acsp":
            raise ImagePayloadError("PNG iCCP 不是有效的 ICC 配置文件")
        declared_size = struct.unpack(">I", profile[:4])[0]
        if declared_size < 128 or declared_size > len(profile):
            raise ImagePayloadError("PNG iCCP 声明的 ICC 配置长度无效")
        expected_color_space = b"GRAY" if color_type in {0, 4} else b"RGB "
        if profile[16:20] != expected_color_space:
            raise ImagePayloadError("PNG iCCP 的 ICC 色彩空间与 PNG 颜色类型不匹配")

    elif chunk_type == b"pHYs":
        if len(chunk_data) != 9:
            raise ImagePayloadError("PNG pHYs 长度必须是 9 字节")
        if chunk_data[8] not in {0, 1}:
            raise ImagePayloadError("PNG pHYs 的单位字段只能是 0 或 1")

    elif chunk_type == b"sPLT":
        palette_name, remainder = _split_png_keyword(chunk_data, label="sPLT")
        palette_names = metadata_state.setdefault("splt_names", set())
        if palette_name in palette_names:
            raise ImagePayloadError("PNG sPLT 调色板名称不能重复")
        palette_names.add(palette_name)
        if not remainder or remainder[0] not in {8, 16}:
            raise ImagePayloadError("PNG sPLT 的 sample depth 只能是 8 或 16")
        entry_size = 6 if remainder[0] == 8 else 10
        entries = remainder[1:]
        if not entries or len(entries) % entry_size != 0:
            raise ImagePayloadError("PNG sPLT 条目长度与 sample depth 不匹配")
        _consume_metadata_budget(metadata_state, len(entries), label="sPLT")

    elif chunk_type == b"sBIT":
        expected_lengths = {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}
        if len(chunk_data) != expected_lengths[color_type]:
            raise ImagePayloadError("PNG sBIT 长度与颜色类型不匹配")
        max_depth = 8 if color_type == 3 else bit_depth
        if any(value == 0 or value > max_depth for value in chunk_data):
            raise ImagePayloadError("PNG sBIT 的有效位数无效")

    elif chunk_type == b"bKGD":
        expected_lengths = {0: 2, 2: 6, 3: 1, 4: 2, 6: 6}
        if len(chunk_data) != expected_lengths[color_type]:
            raise ImagePayloadError("PNG bKGD 长度与颜色类型不匹配")
        if color_type == 3:
            if not seen_plte or chunk_data[0] >= plte_entries:
                raise ImagePayloadError("索引色 PNG bKGD 必须引用有效的 PLTE 项")
        else:
            max_sample = (1 << bit_depth) - 1
            samples = struct.unpack(">" + "H" * (len(chunk_data) // 2), chunk_data)
            if any(sample > max_sample for sample in samples):
                raise ImagePayloadError("PNG bKGD 样本值超过当前位深")

    elif chunk_type == b"hIST":
        if not seen_plte:
            raise ImagePayloadError("PNG hIST 之前必须有 PLTE")
        if len(chunk_data) != plte_entries * 2:
            raise ImagePayloadError("PNG hIST 长度必须与 PLTE 项数匹配")

    elif chunk_type == b"tRNS":
        if color_type in {4, 6}:
            raise ImagePayloadError("带 alpha 的 PNG 不能包含 tRNS")
        expected_lengths = {0: 2, 2: 6}
        if color_type == 3:
            if not seen_plte or not (1 <= len(chunk_data) <= plte_entries):
                raise ImagePayloadError("索引色 PNG tRNS 长度必须与 PLTE 匹配")
        elif len(chunk_data) != expected_lengths[color_type]:
            raise ImagePayloadError("PNG tRNS 长度与颜色类型不匹配")
        else:
            max_sample = (1 << bit_depth) - 1
            samples = struct.unpack(">" + "H" * (len(chunk_data) // 2), chunk_data)
            if any(sample > max_sample for sample in samples):
                raise ImagePayloadError("PNG tRNS 样本值超过当前位深")

    elif chunk_type == b"tIME":
        if len(chunk_data) != 7:
            raise ImagePayloadError("PNG tIME 长度必须是 7 字节")
        _, month, day, hour, minute, second = struct.unpack(">HBBBBB", chunk_data)
        if not (
            1 <= month <= 12
            and 1 <= day <= 31
            and hour <= 23
            and minute <= 59
            and second <= 60
        ):
            raise ImagePayloadError("PNG tIME 日期或时间字段无效")

    elif chunk_type == b"tEXt":
        _, text = _split_png_keyword(chunk_data, label="tEXt")
        if b"\x00" in text:
            raise ImagePayloadError("PNG tEXt 文本字段不能包含 NUL")
        _consume_metadata_budget(metadata_state, len(text), label="tEXt")

    elif chunk_type == b"zTXt":
        _, remainder = _split_png_keyword(chunk_data, label="zTXt")
        if len(remainder) < 2 or remainder[0] != 0:
            raise ImagePayloadError("PNG zTXt 压缩方法必须是 0，且文本数据不能为空")
        text = _decompress_png_metadata(
            remainder[1:],
            label="zTXt",
            metadata_state=metadata_state,
        )
        if b"\x00" in text:
            raise ImagePayloadError("PNG zTXt 文本字段不能包含 NUL")

    elif chunk_type == b"iTXt":
        _, remainder = _split_png_keyword(chunk_data, label="iTXt")
        if len(remainder) < 2:
            raise ImagePayloadError("PNG iTXt 缺少压缩标志或压缩方法")
        compression_flag, compression_method = remainder[:2]
        if compression_flag not in {0, 1} or compression_method != 0:
            raise ImagePayloadError("PNG iTXt 的压缩标志或压缩方法无效")
        try:
            language_tag, remainder = remainder[2:].split(b"\x00", 1)
            translated_keyword, text_payload = remainder.split(b"\x00", 1)
            language_tag.decode("ascii")
            translated_keyword.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ImagePayloadError("PNG iTXt 的语言或翻译关键字无效") from error
        if compression_flag == 1:
            text = _decompress_png_metadata(
                text_payload,
                label="iTXt",
                metadata_state=metadata_state,
            )
        else:
            _consume_metadata_budget(metadata_state, len(text_payload), label="iTXt")
            text = text_payload
        try:
            text.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ImagePayloadError("PNG iTXt 文本不是有效 UTF-8") from error
        if b"\x00" in text:
            raise ImagePayloadError("PNG iTXt 文本字段不能包含 NUL")

    elif chunk_type == b"eXIf":
        if len(chunk_data) < 8 or chunk_data[:2] not in {b"II", b"MM"}:
            raise ImagePayloadError("PNG eXIf 缺少完整的 TIFF 头")
        endian = "<" if chunk_data[:2] == b"II" else ">"
        magic, first_ifd_offset = struct.unpack(
            f"{endian}HI",
            chunk_data[2:8],
        )
        if magic != 42:
            raise ImagePayloadError("PNG eXIf 的 TIFF magic 无效")
        if first_ifd_offset:
            if first_ifd_offset < 8 or first_ifd_offset + 2 > len(chunk_data):
                raise ImagePayloadError("PNG eXIf 的首个 IFD 偏移越界")
            entry_count = struct.unpack(
                f"{endian}H",
                chunk_data[first_ifd_offset:first_ifd_offset + 2],
            )[0]
            if first_ifd_offset + 2 + entry_count * 12 + 4 > len(chunk_data):
                raise ImagePayloadError("PNG eXIf 的首个 IFD 数据被截断")
        _consume_metadata_budget(metadata_state, len(chunk_data), label="eXIf")

    elif chunk_type == b"caBX":
        if len(chunk_data) < 8:
            raise ImagePayloadError("PNG caBX 缺少完整的 JUMBF box 头")
        box_size, box_type = struct.unpack(">I4s", chunk_data[:8])
        if box_type != b"jumb":
            raise ImagePayloadError("PNG caBX 的 JUMBF box 类型无效")
        if box_size == 1:
            if len(chunk_data) < 16:
                raise ImagePayloadError("PNG caBX 缺少扩展 JUMBF box 长度")
            extended_size = struct.unpack(">Q", chunk_data[8:16])[0]
            if extended_size != len(chunk_data) or extended_size < 16:
                raise ImagePayloadError("PNG caBX 的扩展 JUMBF box 长度无效")
        elif box_size != 0 and (box_size < 8 or box_size != len(chunk_data)):
            raise ImagePayloadError("PNG caBX 的 JUMBF box 长度无效")
        _consume_metadata_budget(metadata_state, len(chunk_data), label="caBX")

def _validate_png(image_bytes: bytes) -> tuple[int, int]:
    """校验 PNG 容器、像素流，并返回 IHDR 声明的宽高。"""
    if not isinstance(image_bytes, bytes):
        raise ImagePayloadError("PNG 数据必须是字节串")
    if not image_bytes:
        raise ImagePayloadError("PNG 内容为空")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImagePayloadError(
            f"PNG 大小超过上限 {MAX_IMAGE_BYTES} 字节"
        )
    if not image_bytes.startswith(PNG_SIGNATURE):
        raise ImagePayloadError("PNG 签名无效，上游可能返回了 HTML 或其他格式")

    offset = len(PNG_SIGNATURE)
    chunk_count = 0
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    idat_ended = False
    seen_iend = False
    idat_parts = []
    header = None
    plte_entries = 0
    seen_ancillary = set()
    metadata_state = {"decoded": 0}

    while offset < len(image_bytes):
        chunk_count += 1
        if chunk_count > MAX_PNG_CHUNKS:
            raise ImagePayloadError(f"PNG chunk 数量超过上限 {MAX_PNG_CHUNKS}")
        if len(image_bytes) - offset < 12:
            raise ImagePayloadError("PNG 在 chunk 头部或 CRC 处被截断")

        chunk_length = struct.unpack(">I", image_bytes[offset:offset + 4])[0]
        chunk_type = image_bytes[offset + 4:offset + 8]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(image_bytes):
            name = chunk_type.decode("ascii", errors="replace")
            raise ImagePayloadError(f"PNG chunk {name} 数据被截断")

        if len(chunk_type) != 4 or any(
            not (65 <= value <= 90 or 97 <= value <= 122)
            for value in chunk_type
        ):
            raise ImagePayloadError("PNG chunk 类型必须由四个 ASCII 字母组成")
        # PNG chunk 类型的第三个字母是保留位，规范要求大写。
        if chunk_type[2] & 0x20:
            raise ImagePayloadError("PNG chunk 类型的保留位无效")

        chunk_data = image_bytes[offset + 8:offset + 8 + chunk_length]
        stored_crc = struct.unpack(">I", image_bytes[offset + 8 + chunk_length:chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            name = chunk_type.decode("ascii")
            raise ImagePayloadError(f"PNG chunk {name} 的 CRC 校验失败")

        if chunk_count == 1 and chunk_type != b"IHDR":
            raise ImagePayloadError("PNG 的第一个 chunk 必须是 IHDR")

        if chunk_type == b"IHDR":
            if seen_ihdr:
                raise ImagePayloadError("PNG 不能包含多个 IHDR")
            if chunk_length != 13:
                raise ImagePayloadError("PNG IHDR 长度必须是 13 字节")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            if width == 0 or height == 0 or width > 0x7FFFFFFF or height > 0x7FFFFFFF:
                raise ImagePayloadError("PNG IHDR 的宽高无效")
            if width * height > MAX_IMAGE_PIXELS:
                raise ImagePayloadError(
                    f"PNG 像素数超过上限 {MAX_IMAGE_PIXELS}"
                )
            if color_type not in PNG_VALID_BIT_DEPTHS:
                raise ImagePayloadError(f"PNG 颜色类型无效: {color_type}")
            if bit_depth not in PNG_VALID_BIT_DEPTHS[color_type]:
                raise ImagePayloadError(
                    f"PNG 位深 {bit_depth} 不适用于颜色类型 {color_type}"
                )
            if compression != 0 or filtering != 0 or interlace not in {0, 1}:
                raise ImagePayloadError("PNG IHDR 的压缩、过滤或隔行参数无效")

            expected_size, layouts = _png_scanline_layouts(
                width,
                height,
                bit_depth,
                color_type,
                interlace,
            )
            if expected_size > MAX_DECOMPRESSED_IMAGE_BYTES:
                raise ImagePayloadError(
                    "PNG 解压后的像素数据超过安全上限 "
                    f"{MAX_DECOMPRESSED_IMAGE_BYTES} 字节"
                )
            header = (bit_depth, color_type, expected_size, layouts)
            seen_ihdr = True

        elif chunk_type == b"PLTE":
            if not seen_ihdr or seen_idat:
                raise ImagePayloadError("PNG PLTE 必须位于 IHDR 之后、IDAT 之前")
            if seen_plte:
                raise ImagePayloadError("PNG 不能包含多个 PLTE")
            if chunk_length == 0 or chunk_length > 768 or chunk_length % 3 != 0:
                raise ImagePayloadError("PNG PLTE 长度无效")
            bit_depth, color_type, _, _ = header
            if color_type in {0, 4}:
                raise ImagePayloadError("灰度 PNG 不能包含 PLTE")
            if color_type == 3 and chunk_length // 3 > 2 ** bit_depth:
                raise ImagePayloadError("PNG PLTE 颜色数超过当前位深上限")
            plte_entries = chunk_length // 3
            seen_plte = True

        elif chunk_type == b"IDAT":
            if not seen_ihdr:
                raise ImagePayloadError("PNG IDAT 之前缺少 IHDR")
            if idat_ended:
                raise ImagePayloadError("PNG 的 IDAT chunk 必须连续出现")
            bit_depth, color_type, _, _ = header
            if color_type == 3 and not seen_plte:
                raise ImagePayloadError("索引色 PNG 的 IDAT 之前必须有 PLTE")
            idat_parts.append(chunk_data)
            seen_idat = True

        elif chunk_type == b"IEND":
            if chunk_length != 0:
                raise ImagePayloadError("PNG IEND 长度必须为 0")
            if not seen_idat:
                raise ImagePayloadError("PNG IEND 之前缺少 IDAT")
            if chunk_end != len(image_bytes):
                raise ImagePayloadError("PNG IEND 之后存在多余数据")
            seen_iend = True

        elif chunk_type[0] & 0x20:
            _validate_png_ancillary_chunk(
                chunk_type,
                chunk_data,
                header=header,
                seen_plte=seen_plte,
                plte_entries=plte_entries,
                seen_idat=seen_idat,
                seen_ancillary=seen_ancillary,
                metadata_state=metadata_state,
            )

        else:
            name = chunk_type.decode("ascii")
            raise ImagePayloadError(f"PNG 包含不支持的关键 chunk: {name}")

        if seen_idat and chunk_type not in {b"IDAT", b"IEND"}:
            idat_ended = True

        offset = chunk_end
        if seen_iend:
            break

    if not seen_iend:
        raise ImagePayloadError("PNG 数据被截断，缺少 IEND")

    bit_depth, color_type, expected_size, layouts = header
    compressed = b"".join(idat_parts)
    if not compressed:
        raise ImagePayloadError("PNG IDAT 数据为空")

    decompressor = zlib.decompressobj()
    try:
        decoded = decompressor.decompress(compressed, expected_size + 1)
        remaining = expected_size + 1 - len(decoded)
        if remaining > 0:
            decoded += decompressor.flush(remaining)
    except zlib.error as error:
        raise ImagePayloadError(f"PNG IDAT zlib 数据无效: {error}") from error

    if len(decoded) > expected_size or decompressor.unconsumed_tail:
        raise ImagePayloadError("PNG IDAT 解压数据超过 IHDR 声明大小")
    if not decompressor.eof:
        raise ImagePayloadError("PNG IDAT zlib 数据被截断")
    # PNG 3 允许最后一个 IDAT 在 zlib 流结束后含未使用尾字节，
    # 解码器应忽略它们。整个 PNG 编码体积仍受 MAX_IMAGE_BYTES 限制。
    if len(decoded) != expected_size:
        raise ImagePayloadError(
            f"PNG IDAT 解压长度无效: 应为 {expected_size}，实为 {len(decoded)}"
        )

    position = 0
    bytes_per_pixel = max(
        1,
        (PNG_COLOR_CHANNELS[color_type] * bit_depth + 7) // 8,
    )
    for rows, row_bytes, pass_width in layouts:
        previous = b""
        for _ in range(rows):
            filter_type = decoded[position]
            if filter_type > 4:
                raise ImagePayloadError(
                    f"PNG 扫描行过滤器类型无效: {filter_type}"
                )
            filtered = decoded[position + 1:position + 1 + row_bytes]
            if color_type == 3:
                reconstructed = _unfilter_png_row(
                    filtered,
                    previous,
                    filter_type=filter_type,
                    bytes_per_pixel=bytes_per_pixel,
                )
                mask = (1 << bit_depth) - 1
                for pixel in range(pass_width):
                    bit_offset = pixel * bit_depth
                    shift = 8 - bit_depth - (bit_offset % 8)
                    palette_index = (
                        reconstructed[bit_offset // 8] >> shift
                    ) & mask
                    if palette_index >= plte_entries:
                        raise ImagePayloadError(
                            "索引色 PNG 像素引用了超出 PLTE 范围的调色板索引: "
                            f"{palette_index} >= {plte_entries}"
                        )
                previous = reconstructed
            position += row_bytes + 1

    return width, height


# ---------------------------------------------------------------------------
# 核心生成逻辑
# ---------------------------------------------------------------------------

def _bounded_json_summary(value, *, max_chars: int = 500) -> str:
    """有界摘要 API JSON，避免把 base64 图片或巨型字段写进错误日志。"""
    sensitive_keys = {"api_key", "authorization", "b64_json"}

    def _sanitize(item, depth: int = 0):
        if depth >= 3:
            return f"<{type(item).__name__}>"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            if len(item) <= 160:
                return item
            return f"{item[:64]}... <{len(item)} chars>"
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 10:
                    result["<more>"] = f"{len(item) - 10} more keys"
                    break
                safe_key = str(key)[:80]
                if safe_key.lower() in sensitive_keys:
                    length = len(child) if hasattr(child, "__len__") else "unknown"
                    result[safe_key] = f"<omitted {length} chars>"
                elif safe_key.lower() in {"image_url", "url"}:
                    result[safe_key] = _redact_url(child)
                else:
                    result[safe_key] = _sanitize(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = [_sanitize(child, depth + 1) for child in item[:10]]
            if len(item) > 10:
                result.append(f"<{len(item) - 10} more items>")
            return result
        return f"<{type(item).__name__}>"

    try:
        summary = json.dumps(_sanitize(value), ensure_ascii=False)
    except (TypeError, ValueError, RecursionError):
        summary = f"<{type(value).__name__}>"
    return summary[:max_chars]


def _response_text_prefix(response, *, max_chars: int = 500) -> str:
    """从响应中只解码有界前缀，避免为错误摘要构造整个大字符串。"""
    try:
        content = response.content
    except AttributeError:
        return str(getattr(response, "text", ""))[:max_chars]
    if isinstance(content, bytes):
        return content[: max_chars * 4].decode("utf-8", errors="replace")[:max_chars]
    return str(getattr(response, "text", ""))[:max_chars]


def _api_error_message(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
    else:
        message = error
    if isinstance(message, str) and message.strip():
        return message.strip()[:500]
    return None

def _generate_core(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    max_retries: int = 0,
    task_label: str = "",
) -> dict:
    tag = f"[heige-image{' ' + task_label if task_label else ''}]"
    try:
        _validate_generation_inputs(
            prompt=prompt,
            api_key=api_key,
            base_url=base_url,
            model=model,
            aspect_ratio=aspect_ratio,
            max_retries=max_retries,
        )
    except ValueError as error:
        return {"success": False, "error": str(error)}
    if output_path is None:
        output_path = str(OUTPUT_DIR / "output.png")
    try:
        output_path = _validate_output_destination(output_path)
    except ValueError as error:
        return {"success": False, "error": str(error)}
    resolved_size = ASPECT_SIZE_MAP[aspect_ratio]

    payload = {
        "model": model,
        "prompt": prompt,
        "size": resolved_size,
        "n": 1,
    }

    _safe_print(f"{tag} 正在生成图片...")
    _safe_print(f"{tag}   宽高比: {aspect_ratio} -> {resolved_size} | 超时: {TIMEOUT_SECONDS}s")

    resp = None
    last_error = None
    elapsed = 0.0

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # 指数退避，封顶 60s
            delay = min(2 ** attempt, 60)
            _safe_print(f"{tag} 第 {attempt}/{max_retries} 次重试，等待 {delay}s ...")
            time.sleep(delay)

        _safe_print(f"{tag} 发送请求 (attempt {attempt + 1})")

        t0 = time.time()
        try:
            resp = _request_once(payload, TIMEOUT_SECONDS, api_key, base_url)
        except httpx.InvalidURL:
            return {
                "success": False,
                "error": f"API URL 无效: {_redact_url(base_url)}",
            }
        except ImagePayloadError as e:
            return {"success": False, "error": str(e)}
        except httpx.TimeoutException:
            last_error = "请求超时"
            _safe_print(f"{tag} 请求超时", file=sys.stderr)
            continue
        except httpx.HTTPError as e:
            last_error = (
                f"请求失败 {type(e).__name__}: {_redact_url(base_url)}"
            )
            _safe_print(f"{tag} {last_error}", file=sys.stderr)
            continue

        elapsed = time.time() - t0

        if resp.status_code == 200:
            _safe_print(f"{tag} API 响应成功，耗时 {elapsed:.1f}s")
            break

        # 5xx / 429 可重试，其余直接失败返回
        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            try:
                err_body = resp.json()
                err_msg = _api_error_message(err_body) or _bounded_json_summary(
                    err_body,
                    max_chars=200,
                )
            except (ValueError, RecursionError):
                err_msg = _response_text_prefix(resp, max_chars=200)
            last_error = f"HTTP {resp.status_code}: {err_msg}"
            _safe_print(f"{tag} 收到 {resp.status_code}，将重试", file=sys.stderr)
            continue

        try:
            err_body = resp.json()
            err_detail = _api_error_message(err_body) or _bounded_json_summary(err_body)
        except (ValueError, RecursionError):
            err_detail = _response_text_prefix(resp)
        return {"success": False, "error": f"HTTP {resp.status_code}: {err_detail}"}
    else:
        return {"success": False, "error": f"重试 {max_retries} 次仍然失败。最后错误: {last_error}"}

    # 解析响应：优先 url，其次 b64_json。兼容中转服务返回 HTML、空数组等异常响应，
    # 这些都应成为任务级失败，不能用 traceback 打断整个批次。
    try:
        data = resp.json()
    except (ValueError, RecursionError) as e:
        return {"success": False, "error": f"API 成功响应不是有效 JSON: {e}"}

    if not isinstance(data, dict):
        return {"success": False, "error": "API 成功响应的 JSON 顶层必须是对象"}
    records = data.get("data")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return {
            "success": False,
            "error": "API 响应中未找到有效图片记录: "
            f"{_bounded_json_summary(data)}",
        }

    image_url = records[0].get("url")
    b64_data = records[0].get("b64_json")

    if image_url:
        if not isinstance(image_url, str):
            return {"success": False, "error": "API 图片 URL 必须是字符串"}
        safe_image_url = _redact_url(image_url)
        _safe_print(f"{tag} 下载图片 from: {safe_image_url}")
        try:
            image_bytes = _download_image_bytes(image_url)
        except httpx.InvalidURL:
            return {"success": False, "error": f"下载图片 URL 无效: {safe_image_url}"}
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "error": f"下载图片失败: HTTP {e.response.status_code}，{safe_image_url}",
            }
        except httpx.HTTPError as e:
            return {
                "success": False,
                "error": f"下载图片失败: {type(e).__name__}，{safe_image_url}",
            }
        except ImagePayloadError as e:
            return {"success": False, "error": f"下载图片失败: {e}"}
    elif b64_data:
        if not isinstance(b64_data, str):
            return {"success": False, "error": "API base64 图片数据必须是字符串"}
        _safe_print(f"{tag} 解码 base64 图片...")
        try:
            image_bytes = _decode_base64_image(b64_data)
        except ImagePayloadError as e:
            return {"success": False, "error": str(e)}
    else:
        return {
            "success": False,
            "error": "API 响应中未找到图片数据: "
            f"{_bounded_json_summary(data)}",
        }

    try:
        actual_dimensions = _validate_png(image_bytes)
    except ImagePayloadError as e:
        return {"success": False, "error": f"API 返回的 PNG 数据无效: {e}"}
    expected_dimensions = tuple(int(value) for value in resolved_size.split("x"))
    if actual_dimensions != expected_dimensions:
        actual_size = f"{actual_dimensions[0]}x{actual_dimensions[1]}"
        return {
            "success": False,
            "error": (
                f"API 返回的 PNG 尺寸与请求不一致: "
                f"请求 {resolved_size}，实际 {actual_size}"
            ),
        }

    out = Path(output_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomically(out, image_bytes)
    except OSError as e:
        return {"success": False, "error": f"写入图片失败: {out}（{e}）"}

    size_kb = len(image_bytes) / 1024
    _safe_print(f"{tag} 生成完成，大小 {size_kb:.0f}KB -> {out}")

    return {
        "success": True,
        "path": str(out),
        "size_kb": round(size_kb, 1),
        "elapsed": round(elapsed, 1),
    }


def generate(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    max_retries: int = 0,
) -> str:
    result = _generate_core(
        prompt=prompt,
        api_key=api_key,
        base_url=base_url,
        model=model,
        aspect_ratio=aspect_ratio,
        output_path=output_path,
        max_retries=max_retries,
    )
    if not result["success"]:
        print(f"错误: {result['error']}", file=sys.stderr)
        sys.exit(1)
    return result["path"]


# ---------------------------------------------------------------------------
# 并发批量
# ---------------------------------------------------------------------------

def generate_batch(
    tasks: list,
    api_key: str,
    base_url: str,
    model: str,
    workers: int = 0,
    max_retries: int = 0,
    max_n: int = DEFAULT_MAX_N,
) -> list:
    _validate_batch_tasks(tasks)
    _validate_nonempty_string(api_key, label="api_key")
    _validate_nonempty_string(base_url, label="base_url")
    _validate_nonempty_string(model, label="model")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 0:
        raise ValueError("workers 必须是非负整数")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
        raise ValueError("max_retries 必须是 0 至 10 的整数")
    if isinstance(max_n, bool) or not isinstance(max_n, int) or max_n <= 0:
        raise ValueError("max_n 必须是正整数")
    if len(tasks) > max_n:
        raise ValueError(
            f"批量任务 {len(tasks)} 张超过成本护栏 max_n={max_n}"
        )
    num_tasks = len(tasks)

    # generate_batch 也是可直接调用的 Python 入口，不能只依赖 CLI 层校验。
    _validate_unique_batch_outputs(tasks)
    for index, task in enumerate(tasks):
        _validate_output_destination(
            task["output"],
            label=f"任务 #{index + 1} 的 output",
        )

    if workers <= 0:
        workers = min(num_tasks, 2)
    workers = max(1, min(workers, num_tasks))

    print(f"[heige-image 批量] 共 {num_tasks} 个任务，并发数: {workers}")

    t_start = time.time()
    results = [None] * num_tasks

    def _run_task(index: int, task: dict) -> tuple:
        try:
            result = _generate_core(
                prompt=task["prompt"],
                api_key=api_key,
                base_url=base_url,
                model=model,
                aspect_ratio=task.get("aspect_ratio", "1:1"),
                output_path=task["output"],
                max_retries=max_retries,
                task_label=f"#{index + 1}",
            )
        except Exception as error:
            detail = str(error).replace("\n", " ")[:500]
            result = {
                "success": False,
                "error": f"任务发生未预期错误 {type(error).__name__}: {detail}",
            }
        result["index"] = index
        return index, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_task, i, t): i
            for i, t in enumerate(tasks)
        }
        for future in as_completed(futures):
            try:
                idx, result = future.result()
            except Exception as error:
                idx = futures[future]
                detail = str(error).replace("\n", " ")[:500]
                result = {
                    "success": False,
                    "error": f"任务收集失败 {type(error).__name__}: {detail}",
                    "index": idx,
                }
            results[idx] = result
            status = "OK" if result["success"] else "FAIL"
            _safe_print(f"[heige-image 批量] 任务 #{idx + 1} {status}")

    t_total = time.time() - t_start
    ok = sum(1 for r in results if r and r["success"])
    print(f"\n[heige-image 批量] 全部完成: {ok}/{num_tasks} 成功，总耗时 {t_total:.1f}s")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="heige-image - 使用固定 OpenAI 图像生成请求契约的生图脚本（规格驱动）",
    )

    parser.add_argument(
        "--base-url", default=None,
        help="API 基址（优先级 CLI > 环境变量 HEIGE_IMAGE_BASE_URL > 配置文件 > 默认 https://api.openai.com/v1）",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="API Key（优先级 CLI > 环境变量 HEIGE_IMAGE_API_KEY > 配置文件 ~/.heige-image/config.json）",
    )
    parser.add_argument(
        "--model", default=None,
        help="模型名（优先级 CLI > 环境变量 HEIGE_IMAGE_MODEL > 配置文件 > 默认 gpt-image-2）",
    )

    # 三选一输入：规格文件 / 临时提示词 / 批量
    parser.add_argument(
        "--spec", default=None, metavar="MD_FILE",
        help="规格文件路径 prompts/NN-主题.md，从中提取「最终 Prompt」出图（主推）",
    )
    parser.add_argument(
        "--prompt", "-p", default=None,
        help="临时直接给提示词出图（不落规格，不推荐常用）",
    )
    parser.add_argument(
        "--batch", "-b", default=None, metavar="JSON_FILE",
        help="批量任务 JSON 文件路径（与 --spec / --prompt 互斥）",
    )

    parser.add_argument(
        "--aspect-ratio", "-ar", default=None,
        choices=VALID_ASPECT_RATIOS,
        help="宽高比（默认 1:1；--spec 模式下若规格里写了比例则以命令行为优先覆盖）",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出 PNG 文件路径（--spec 默认 outimage/同名.png，--prompt 默认 outimage/output.png）",
    )

    parser.add_argument(
        "--workers", "-w", type=int, default=0,
        help="批量并发 worker 数（默认: 自动）",
    )

    parser.add_argument(
        "--retry", "-r", type=int, default=0,
        choices=range(0, 11), metavar="0-10",
        help="每个任务的最大重试次数（默认: 0；显式重试可能重复计费）",
    )

    parser.add_argument(
        "--max-n", type=int, default=DEFAULT_MAX_N, metavar="N",
        help=f"成本护栏：单次最多交付几张（默认: {DEFAULT_MAX_N}；不含显式重试）",
    )

    args = parser.parse_args()

    # 输入模式三选一校验
    modes = [bool(args.spec), bool(args.prompt), bool(args.batch)]
    if sum(modes) > 1:
        parser.error("--spec / --prompt / --batch 三者只能选一个")
    if sum(modes) == 0:
        parser.error("必须指定 --spec（规格文件）、--prompt（临时提示词）或 --batch（批量）之一")

    if args.max_n <= 0:
        parser.error("--max-n 必须是正整数")
    if args.retry > 0:
        print(
            "警告: 图像生成 POST 没有幂等性保证，--retry 可能对同一任务重复计费。",
            file=sys.stderr,
        )

    # 配置文件读一次复用，省得三个 resolver 各读一遍
    config = _load_config()
    base_url = resolve_base_url(args.base_url, config)
    api_key = resolve_api_key(args.api_key, config)
    model = resolve_model(args.model, config)

    # ---- 批量模式 ----
    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.is_file():
            print(
                f"错误: 批量任务文件不存在或不是普通文件: {batch_path}",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            tasks = json.loads(batch_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, RecursionError, UnicodeDecodeError) as e:
            print(f"错误: 解析批量任务文件失败: {e}", file=sys.stderr)
            sys.exit(1)

        if not isinstance(tasks, list) or not tasks:
            print("错误: 批量任务文件必须是非空 JSON 数组", file=sys.stderr)
            sys.exit(1)

        # 成本护栏：批量张数不能超 --max-n
        if len(tasks) > args.max_n:
            print(
                f"错误: 批量任务 {len(tasks)} 张超过成本护栏上限 --max-n={args.max_n}。\n"
                "  要么拆批，要么显式调高 --max-n（明确知道在烧多少钱再调）。",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            _validate_batch_tasks(tasks)
            _validate_unique_batch_outputs(tasks)
        except ValueError as error:
            print(f"错误: {error}", file=sys.stderr)
            sys.exit(1)

        try:
            results = generate_batch(
                tasks=tasks,
                api_key=api_key,
                base_url=base_url,
                model=model,
                workers=args.workers,
                max_retries=args.retry,
                max_n=args.max_n,
            )
        except ValueError as error:
            print(f"错误: {error}", file=sys.stderr)
            sys.exit(1)

        print("\n" + json.dumps(results, indent=2, ensure_ascii=False))

        if any(not r["success"] for r in results if r):
            sys.exit(1)
        return

    # ---- 单图：规格文件模式 ----
    if args.spec:
        spec = parse_spec(args.spec)
        prompt = spec["prompt"]
        # 宽高比优先级：命令行 > 规格自描述 > 默认 1:1
        aspect_ratio = args.aspect_ratio or spec["aspect_ratio"] or "1:1"
        output_path = args.output or _spec_default_output(args.spec)
        print(f"[heige-image] 规格驱动: {args.spec} | 宽高比 {aspect_ratio} | 输出 {output_path}")
    # ---- 单图：临时提示词模式 ----
    else:
        prompt = args.prompt
        if not prompt.strip():
            parser.error("--prompt 必须是非空字符串")
        aspect_ratio = args.aspect_ratio or "1:1"
        output_path = args.output or str(OUTPUT_DIR / "output.png")

    generate(
        prompt=prompt,
        api_key=api_key,
        base_url=base_url,
        model=model,
        aspect_ratio=aspect_ratio,
        output_path=output_path,
        max_retries=args.retry,
    )


if __name__ == "__main__":
    main()
