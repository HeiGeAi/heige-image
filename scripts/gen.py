#!/usr/bin/env python3
"""heige-image 生图脚本 - 走任意 OpenAI 兼容图像生成 API。

heige-image 的设计脑三步走完、规格文件 prompts/NN-主题.md 落好之后，由这个脚本出图。
它是 MVP 主干（插画 / 照片 / 概念图走 API），版式图走 HTML 截图（render.py）不归这里管。

API 通用化设计：
  - 对接任意 OpenAI 兼容的图像生成接口（官方 https://api.openai.com/v1 或任意中转）
  - base_url / api_key / model 三项可配，优先级都是 CLI > 环境变量 > 配置文件 > 默认值
  - 配置文件在 ~/.heige-image/config.json，存 base_url + api_key + model
  - 国内想直接生成可以用 gptx.cc 这个渠道（OpenAI 兼容，base_url 填 https://api.gptx.cc/v1）

脚本特性：
  - ASPECT_SIZE_MAP 三尺寸映射（方 / 横 / 竖），扩散模型实际只出这三种
  - 下载使用有界流式读取并跟随重定向，避免 301 掉图和超大响应耗尽内存
  - --spec：从规格文件 prompts/NN-主题.md 提取「最终 Prompt」直接出图，规格即真相
  - --prompt：临时直接给提示词，不落规格（不推荐常用，规格机制才防漂移）
  - --batch：并发批量，JSON 任务格式
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
    python gen.py --prompt "描述" [-ar 16:9] [-o ./outimage/x.png] [--retry 3]

    # 并发批量
    python gen.py --batch tasks.json [--workers 2] [--max-n 6] [--retry 3]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import httpx
except ImportError:
    print("错误: 需要 httpx 库，请执行: pip install httpx", file=sys.stderr)
    sys.exit(1)

try:
    from .image_output import (
        ImageResponseError,
        OutputPathError,
        atomic_write_image,
        validate_image_bytes,
        validate_image_response,
        validate_image_url,
        validate_output_path,
    )
except ImportError:
    from image_output import (
        ImageResponseError,
        OutputPathError,
        atomic_write_image,
        validate_image_bytes,
        validate_image_response,
        validate_image_url,
        validate_output_path,
    )

# ---------------------------------------------------------------------------
# 渠道配置（通用：对接任意 OpenAI 兼容图像生成 API）
# ---------------------------------------------------------------------------

# 默认 base_url 给 OpenAI 官方，用户可换成任意兼容中转（如 gptx.cc）
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

# aspect_ratio -> size（gpt-image-2 实际只出方 / 横 / 竖三种，按最接近比例映射）
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

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# 成本护栏：单次最多出几张，超了直接拦。默认 6 张，够一组配图又不至于失控
DEFAULT_MAX_N = 6

_print_lock = threading.Lock()


def _safe_print(msg, *, file=None):
    # 批量并发下用锁串行打印，日志不串行
    with _print_lock:
        print(msg, file=file or sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# API 配置管理（base_url / api_key / model 三项，~/.heige-image/config.json）
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_api_key(cli_key: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件
    if cli_key:
        return cli_key.strip()

    env_key = os.environ.get("HEIGE_IMAGE_API_KEY", "").strip()
    if env_key:
        return env_key

    if config is None:
        config = _load_config()
    file_key = (config.get("api_key") or "").strip()
    if file_key:
        return file_key

    print(
        "错误: 未找到 API Key。请通过以下方式之一配置：\n"
        f"  1. 写配置文件 {CONFIG_FILE}，内容: "
        "{\"base_url\": \"https://api.openai.com/v1\", \"api_key\": \"sk-xxx\", \"model\": \"gpt-image-2\"}\n"
        "  2. 设置环境变量 HEIGE_IMAGE_API_KEY=sk-xxx\n"
        "  3. 使用 --api-key sk-xxx 命令行参数\n"
        "  （国内想直接生成可以用 gptx.cc 渠道，base_url 填 https://api.gptx.cc/v1）",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_base_url(cli_base_url: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件 > 默认 OpenAI 官方
    if cli_base_url:
        base = cli_base_url.strip()
    elif os.environ.get("HEIGE_IMAGE_BASE_URL", "").strip():
        base = os.environ["HEIGE_IMAGE_BASE_URL"].strip()
    else:
        if config is None:
            config = _load_config()
        base = (config.get("base_url") or "").strip() or DEFAULT_BASE_URL
    # 去掉末尾斜杠，拼接时统一加
    return base.rstrip("/")


def resolve_model(cli_model: str | None = None, config: dict | None = None) -> str:
    # 优先级：命令行 > 环境变量 > 配置文件 > 默认 gpt-image-2
    if cli_model:
        return cli_model.strip()
    env_model = os.environ.get("HEIGE_IMAGE_MODEL", "").strip()
    if env_model:
        return env_model
    if config is None:
        config = _load_config()
    return (config.get("model") or "").strip() or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# 规格文件解析（heige-image 核心：规格即真相）
# ---------------------------------------------------------------------------

# 匹配「最终 Prompt」「最终prompt」「最终 提示词」等小标题，兼容全/半角空格和大小写
_FINAL_PROMPT_HEADING = re.compile(
    r"^\s*#{1,6}\s*最终[\s　]*(?:prompt|提示词)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

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
    return ratio if ratio in VALID_ASPECT_RATIOS else None


def parse_spec(spec_path: str) -> dict:
    """从 prompts/NN-主题.md 提取最终 prompt 和可选的宽高比。

    约定：规格文件里有一个小标题写「最终 Prompt」（或「最终提示词」），
    其下紧跟一个围栏代码块（```），块里就是要送进 API 的那段完整提示词。
    这样改图时只改这一块、其余 invariant 逐条复述，提示词不漂移。

    返回: {"prompt": str, "aspect_ratio": str | None}
    """
    path = Path(spec_path)
    if not path.exists():
        print(f"错误: 规格文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text(encoding="utf-8")

    heading = _FINAL_PROMPT_HEADING.search(text)
    if not heading:
        print(
            f"错误: 规格文件 {path} 里没找到「最终 Prompt」小标题。\n"
            "  请在规格里写一个『## 最终 Prompt』小标题，其下放一个 ``` 代码块装最终提示词。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 从小标题之后开始找第一个围栏代码块
    after = text[heading.end():]
    block = _FENCED_BLOCK.search(after)
    if not block:
        print(
            f"错误: 规格文件 {path} 的「最终 Prompt」小标题下没找到 ``` 围栏代码块。",
            file=sys.stderr,
        )
        sys.exit(1)

    prompt = block.group(1).strip()
    if not prompt:
        print(f"错误: 规格文件 {path} 的最终 Prompt 代码块是空的。", file=sys.stderr)
        sys.exit(1)

    return {"prompt": prompt, "aspect_ratio": _extract_aspect_from_spec(text)}


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
        return client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )


# ---------------------------------------------------------------------------
# 核心生成逻辑
# ---------------------------------------------------------------------------

def _generate_core(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    max_retries: int = 3,
    task_label: str = "",
) -> dict:
    tag = f"[heige-image{' ' + task_label if task_label else ''}]"
    if output_path is None:
        output_path = str(OUTPUT_DIR / "output.png")
    try:
        output_path = str(validate_output_path(output_path))
    except (OSError, OutputPathError) as e:
        return {"success": False, "error": f"输出路径校验失败: {e}"}
    resolved_size = ASPECT_SIZE_MAP.get(aspect_ratio, "1024x1024")

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
        except httpx.TimeoutException:
            last_error = "请求超时"
            _safe_print(f"{tag} 请求超时", file=sys.stderr)
            continue
        except httpx.ConnectError as e:
            last_error = f"连接失败: {e}"
            _safe_print(f"{tag} 连接失败: {e}", file=sys.stderr)
            continue
        except httpx.HTTPError as e:
            # ReadError / RemoteProtocolError / DecodeError 等其余网络异常同样重试，
            # 避免批量模式下单任务连接重置中断整批。
            last_error = f"网络异常: {e}"
            _safe_print(f"{tag} 网络异常: {e}", file=sys.stderr)
            continue

        elapsed = time.time() - t0

        if resp.status_code == 200:
            _safe_print(f"{tag} API 响应成功，耗时 {elapsed:.1f}s")
            break

        # 5xx / 429 可重试，其余直接失败返回
        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            last_error = f"HTTP {resp.status_code}: {err_msg}"
            _safe_print(f"{tag} 收到 {resp.status_code}，将重试", file=sys.stderr)
            continue

        try:
            err_detail = json.dumps(resp.json(), indent=2, ensure_ascii=False)[:500]
        except Exception:
            err_detail = resp.text[:500]
        return {"success": False, "error": f"HTTP {resp.status_code}: {err_detail}"}
    else:
        return {"success": False, "error": f"重试 {max_retries} 次仍然失败。最后错误: {last_error}"}

    # 解析响应：优先 url，其次 b64_json。
    # 200 但响应体非 JSON（中转网关 HTML 错误页）或 data 为空/结构异常时，统一失败返回，不裸抛 traceback。
    try:
        data = resp.json()
    except ValueError:
        return {"success": False, "error": f"API 响应体不是有效 JSON: {resp.text[:200]}"}
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return {"success": False, "error": f"API 响应缺少有效的 data 列表: {str(data)[:200]}"}
    image_url = items[0].get("url")
    b64_data = items[0].get("b64_json")

    if image_url:
        _safe_print(f"{tag} 下载图片 from: {image_url}")
        # 保持重定向，同时由 validate_image_response 逐块执行大小与 PNG 校验。
        try:
            validate_image_url(image_url)
            with httpx.stream("GET", image_url, timeout=60, follow_redirects=True) as img_resp:
                img_resp.raise_for_status()
                image_bytes = validate_image_response(img_resp)
        except (httpx.HTTPError, ImageResponseError) as e:
            return {"success": False, "error": f"图片下载校验失败: {e}"}
    elif b64_data:
        _safe_print(f"{tag} 解码 base64 图片...")
        try:
            image_bytes = validate_image_bytes(base64.b64decode(b64_data, validate=True))
        except (binascii.Error, ValueError, ImageResponseError) as e:
            return {"success": False, "error": f"base64 图片校验失败: {e}"}
    else:
        return {"success": False, "error": f"API 响应中未找到图片数据: {data}"}

    try:
        out = atomic_write_image(output_path, image_bytes)
    except (OSError, OutputPathError, ImageResponseError) as e:
        return {"success": False, "error": f"图片写入失败: {e}"}

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
    max_retries: int = 3,
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
    max_retries: int = 3,
) -> list:
    num_tasks = len(tasks)

    if workers <= 0:
        workers = min(num_tasks, 2)
    workers = max(1, min(workers, num_tasks))

    print(f"[heige-image 批量] 共 {num_tasks} 个任务，并发数: {workers}")

    t_start = time.time()
    results = [None] * num_tasks

    def _run_task(index: int, task: dict) -> tuple:
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
        result["index"] = index
        return index, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_task, i, t): i
            for i, t in enumerate(tasks)
        }
        for future in as_completed(futures):
            idx, result = future.result()
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
        description="heige-image - 走任意 OpenAI 兼容图像 API 的生图脚本（规格驱动）",
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
        help="输出文件路径（--spec 模式默认 outimage/同名.png，--prompt 模式默认 outimage/output.png）",
    )

    parser.add_argument(
        "--workers", "-w", type=int, default=0,
        help="批量并发 worker 数（默认: 自动）",
    )

    parser.add_argument(
        "--retry", "-r", type=int, default=3,
        choices=range(0, 11), metavar="0-10",
        help="每个任务的最大重试次数（默认: 3）",
    )

    parser.add_argument(
        "--max-n", type=int, default=DEFAULT_MAX_N, metavar="N",
        help=f"成本护栏：单次最多出几张，超了直接拦（默认: {DEFAULT_MAX_N}）",
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

    # 配置文件读一次复用，省得三个 resolver 各读一遍
    config = _load_config()
    base_url = resolve_base_url(args.base_url, config)
    api_key = resolve_api_key(args.api_key, config)
    model = resolve_model(args.model, config)

    # ---- 批量模式 ----
    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.exists():
            print(f"错误: 批量任务文件不存在: {batch_path}", file=sys.stderr)
            sys.exit(1)

        try:
            tasks = json.loads(batch_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
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

        for i, t in enumerate(tasks):
            if "prompt" not in t or "output" not in t:
                print(f"错误: 任务 #{i + 1} 缺少必填字段 prompt 或 output", file=sys.stderr)
                sys.exit(1)
            ar = t.get("aspect_ratio")
            if ar is not None and ar not in VALID_ASPECT_RATIOS:
                print(
                    f"错误: 任务 #{i + 1} 的 aspect_ratio 非法: {ar!r}，"
                    f"可选值: {', '.join(VALID_ASPECT_RATIOS)}",
                    file=sys.stderr,
                )
                sys.exit(1)

        results = generate_batch(
            tasks=tasks,
            api_key=api_key,
            base_url=base_url,
            model=model,
            workers=args.workers,
            max_retries=args.retry,
        )

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
