#!/usr/bin/env python3
"""heige-image 图生图 / 图片编辑脚本 - 走任意 OpenAI 兼容图像编辑 API。

上传一张或多张本地图片 + 一段文字编辑描述，模型按描述改图生成新图。
多图时图1 是基底图，图2 及之后是人脸 / 风格参考图，prompt 里按「图1/图2」指代。
和 gen.py（文生图）配套：gen.py 打 /images/generations，本脚本打 /images/edits。

API 通用化设计（与 gen.py 完全一致的配置约定）：
  - 对接任意 OpenAI 兼容的图像编辑接口（官方 https://api.openai.com/v1 或任意中转）
  - base_url / api_key / model 三项可配，优先级都是 CLI > 环境变量 > 配置文件 > 默认值
  - 配置文件在 ~/.heige-image/config.json，存 base_url + api_key + model
  - 国内想直接改图可以用 gptx.cc 这个渠道（OpenAI 兼容且支持 /images/edits）

脚本特性：
  - ASPECT_SIZE_MAP 三尺寸映射（方 / 横 / 竖），扩散模型实际只出这三种
  - multipart/form-data 上传原图，图片 < 10MB，支持 jpg/jpeg/png/webp/gif
  - 下载加 follow_redirects=True，绕开 301 跳转掉图的坑（和 gen.py 一致）
  - --input + --prompt：单张编辑；--input 可重复传入实现多图输入
  - 单图走 image 字段，多图走多个 image[] 字段（两种 multipart 形态服务端都认）
  - --batch：并发批量编辑，JSON 任务格式，input 字段接受字符串或数组

配置 API（任选一种）：
    # 1. 配置文件 ~/.heige-image/config.json
    {"base_url": "https://api.gptx.cc/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}

    # 2. 环境变量
    export HEIGE_IMAGE_BASE_URL=https://api.gptx.cc/v1
    export HEIGE_IMAGE_API_KEY=sk-xxx
    export HEIGE_IMAGE_MODEL=gpt-image-2

    # 3. 命令行参数（优先级最高）
    python edit.py --base-url https://api.gptx.cc/v1 --api-key sk-xxx ...

用法:
    # 单张编辑
    python edit.py --input photo.jpg --prompt "把背景换成雪景，人物姿态不变" \
                   [-ar 1:1] [-o ./outimage/edited.png] [--retry 3]

    # 多图输入（--input 可重复，图1=基底，图2+=人脸/风格参考）
    python edit.py --input scene.jpg --input face.jpg \
                   --prompt "把图2的人物自然融入图1的场景" [-o ./outimage/merged.png]

    # 并发批量编辑（batch JSON 里 input 接受字符串或数组）
    python edit.py --batch tasks.json [--workers 2] [--retry 3]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
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

# ---------------------------------------------------------------------------
# 渠道配置（通用：对接任意 OpenAI 兼容图像编辑 API，与 gen.py 同一套约定）
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-2"
CONFIG_DIR = Path.home() / ".heige-image"
CONFIG_FILE = CONFIG_DIR / "config.json"
# 默认输出目录锚到脚本所在工程的 outimage/，不受调用时 cwd 影响
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

SUPPORTED_IMAGE_FORMATS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE_MB = 10
TIMEOUT_SECONDS = 600
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

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
        "{\"base_url\": \"https://api.gptx.cc/v1\", \"api_key\": \"sk-xxx\", \"model\": \"gpt-image-2\"}\n"
        "  2. 设置环境变量 HEIGE_IMAGE_API_KEY=sk-xxx\n"
        "  3. 使用 --api-key sk-xxx 命令行参数",
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
# 图片读取
# ---------------------------------------------------------------------------

def read_image_bytes(image_path: str) -> tuple[bytes, str]:
    path = Path(image_path)

    if not path.exists():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_FORMATS:
        raise ValueError(
            f"不支持的图片格式 '{suffix}'，支持: {', '.join(sorted(SUPPORTED_IMAGE_FORMATS))}"
        )

    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_IMAGE_SIZE_MB:
        raise ValueError(f"图片过大 ({size_mb:.1f}MB)，建议 < {MAX_IMAGE_SIZE_MB}MB")

    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"

    return path.read_bytes(), mime_type


def _suffix_from_mime(mime_type: str) -> str:
    map_ = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return map_.get(mime_type, ".png")


# ---------------------------------------------------------------------------
# 请求（multipart/form-data，打 /images/edits）
# ---------------------------------------------------------------------------

def _request_once(
    prompt: str,
    images: list,
    timeout: int,
    api_key: str,
    base_url: str,
    model: str,
    size: str,
) -> httpx.Response:
    url = f"{base_url}/images/edits"

    parts = []
    for idx, (image_bytes, mime_type) in enumerate(images):
        filename = f"image{idx}{_suffix_from_mime(mime_type)}"
        parts.append((filename, io.BytesIO(image_bytes), mime_type))

    # 单图沿用 image 字段（与旧版请求完全一致），多图走 image[] 列表
    if len(parts) == 1:
        files = {"image": parts[0]}
    else:
        files = [("image[]", part) for part in parts]

    with httpx.Client(timeout=timeout) as client:
        data = {
            "model": model,
            "prompt": prompt,
            "size": size,
        }
        return client.post(
            url,
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {api_key}"},
        )


# ---------------------------------------------------------------------------
# 核心编辑逻辑
# ---------------------------------------------------------------------------

def _edit_core(
    input_image,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    max_retries: int = 3,
    task_label: str = "",
) -> dict:
    tag = f"[heige-image 编辑{' ' + task_label if task_label else ''}]"
    if output_path is None:
        output_path = str(OUTPUT_DIR / "output.png")

    # input_image 接受字符串（单图）或路径列表（多图，图1=基底，图2+=参考）
    input_paths = [input_image] if isinstance(input_image, str) else list(input_image)
    if not input_paths:
        return {"success": False, "error": "input 不能为空"}

    images = []
    try:
        for i, p in enumerate(input_paths):
            image_bytes, mime_type = read_image_bytes(p)
            images.append((image_bytes, mime_type))
            role = "" if len(input_paths) == 1 else f"（图{i + 1}{'·基底' if i == 0 else '·参考'}）"
            _safe_print(f"{tag} 输入图片{role}: {p} ({mime_type})")
    except (FileNotFoundError, ValueError) as e:
        return {"success": False, "error": str(e)}

    resolved_size = ASPECT_SIZE_MAP.get(aspect_ratio, "1024x1024")

    _safe_print(f"{tag} 正在编辑图片...")
    _safe_print(f"{tag}   编辑描述: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    _safe_print(f"{tag}   宽高比: {aspect_ratio} -> {resolved_size} | 超时: {TIMEOUT_SECONDS}s")

    resp = None
    last_error = None
    elapsed = 0.0

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = min(2 ** attempt, 60)
            _safe_print(f"{tag} 第 {attempt}/{max_retries} 次重试，等待 {delay}s ...")
            time.sleep(delay)

        _safe_print(f"{tag} 发送请求 (attempt {attempt + 1})")

        t0 = time.time()
        try:
            resp = _request_once(
                prompt, images,
                TIMEOUT_SECONDS, api_key, base_url, model, resolved_size,
            )
        except httpx.TimeoutException:
            last_error = "请求超时"
            _safe_print(f"{tag} 请求超时", file=sys.stderr)
            continue
        except httpx.ConnectError as e:
            last_error = f"连接失败: {e}"
            _safe_print(f"{tag} 连接失败: {e}", file=sys.stderr)
            continue

        elapsed = time.time() - t0

        if resp.status_code == 200:
            _safe_print(f"{tag} API 响应成功，耗时 {elapsed:.1f}s")
            break

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

    # 解析响应：优先 url，其次 b64_json
    data = resp.json()
    image_url = data.get("data", [{}])[0].get("url")
    b64_data = data.get("data", [{}])[0].get("b64_json")

    if image_url:
        _safe_print(f"{tag} 下载图片 from: {image_url}")
        # follow_redirects=True 是 gen.py 修过的 301 掉图坑，别去掉
        img_resp = httpx.get(image_url, timeout=60, follow_redirects=True)
        img_resp.raise_for_status()
        out_bytes = img_resp.content
    elif b64_data:
        _safe_print(f"{tag} 解码 base64 图片...")
        out_bytes = base64.b64decode(b64_data)
    else:
        return {"success": False, "error": f"API 响应中未找到图片数据: {data}"}

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(out_bytes)

    size_kb = len(out_bytes) / 1024
    _safe_print(f"{tag} 编辑完成，大小 {size_kb:.0f}KB -> {out}")

    return {
        "success": True,
        "path": str(out),
        "size_kb": round(size_kb, 1),
        "elapsed": round(elapsed, 1),
    }


def edit(
    input_image,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    aspect_ratio: str = "1:1",
    output_path: str | None = None,
    max_retries: int = 3,
) -> str:
    result = _edit_core(
        input_image=input_image,
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

def edit_batch(
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

    print(f"[heige-image 批量编辑] 共 {num_tasks} 个任务，并发数: {workers}")

    t_start = time.time()
    results = [None] * num_tasks

    def _run_task(index: int, task: dict) -> tuple:
        result = _edit_core(
            input_image=task["input"],
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
            _safe_print(f"[heige-image 批量编辑] 任务 #{idx + 1} {status}")

    t_total = time.time() - t_start
    ok = sum(1 for r in results if r and r["success"])
    print(f"\n[heige-image 批量编辑] 全部完成: {ok}/{num_tasks} 成功，总耗时 {t_total:.1f}s")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="heige-image - 走任意 OpenAI 兼容图像编辑 API 的图生图脚本",
        epilog="上传本地图片 + 文字描述，模型按描述改图生成新图。",
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

    parser.add_argument(
        "--input", "-i", action="append", default=None,
        help="输入图片路径，可重复传入多张（图1=基底图，图2+=人脸/风格参考图；单图模式必填）",
    )
    parser.add_argument(
        "--prompt", "-p", default=None,
        help="编辑描述提示词（单图模式，必填）",
    )
    parser.add_argument(
        "--aspect-ratio", "-ar", default="1:1",
        choices=VALID_ASPECT_RATIOS, help="输出图片宽高比（默认: 1:1）",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出文件路径（默认 outimage/output.png）",
    )

    parser.add_argument(
        "--batch", "-b", default=None, metavar="JSON_FILE",
        help="批量任务 JSON 文件路径（与 --input/--prompt 互斥）",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=0,
        help="并发 worker 数（默认: 自动）",
    )

    parser.add_argument(
        "--retry", "-r", type=int, default=3,
        choices=range(0, 11), metavar="0-10",
        help="每个任务的最大重试次数（默认: 3）",
    )

    args = parser.parse_args()

    if args.batch and (args.input or args.prompt):
        parser.error("--batch 和 --input/--prompt 不能同时使用")
    if not args.batch and (not args.input or not args.prompt):
        parser.error("单图模式必须同时指定 --input 和 --prompt，或使用 --batch 批量模式")

    # 配置文件读一次复用
    config = _load_config()
    base_url = resolve_base_url(args.base_url, config)
    api_key = resolve_api_key(args.api_key, config)
    model = resolve_model(args.model, config)

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

        for i, t in enumerate(tasks):
            for field in ("input", "prompt", "output"):
                if field not in t:
                    print(f"错误: 任务 #{i + 1} 缺少必填字段 '{field}'", file=sys.stderr)
                    sys.exit(1)
            inp = t["input"]
            input_ok = isinstance(inp, str) or (
                isinstance(inp, list) and inp and all(isinstance(p, str) for p in inp)
            )
            if not input_ok:
                print(
                    f"错误: 任务 #{i + 1} 的 'input' 必须是图片路径字符串或非空路径数组",
                    file=sys.stderr,
                )
                sys.exit(1)
            ar = t.get("aspect_ratio")
            if ar is not None and ar not in VALID_ASPECT_RATIOS:
                print(
                    f"错误: 任务 #{i + 1} 的 aspect_ratio 非法: {ar!r}，"
                    f"可选值: {', '.join(VALID_ASPECT_RATIOS)}",
                    file=sys.stderr,
                )
                sys.exit(1)

        results = edit_batch(
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
    else:
        edit(
            input_image=args.input,
            prompt=args.prompt,
            api_key=api_key,
            base_url=base_url,
            model=model,
            aspect_ratio=args.aspect_ratio,
            output_path=args.output,
            max_retries=args.retry,
        )


if __name__ == "__main__":
    main()
