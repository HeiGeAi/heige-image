#!/usr/bin/env python3
"""heige-image 无 key 版式渲染引擎 - Playwright 把 HTML 模板截成 PNG。

这是 MVP+1 的另一条主干，和 gen.py 分工：
  - gen.py 走 API（gpt-image-2），出插画 / 照片 / 概念图，文字会变形那类不归它。
  - render.py 走 HTML 截图，零 API、免费、文字逐字精确，出公众号封面 / 信息图 / 金句卡那类版式图。

机制学 guizang（种子 HTML + Playwright file:// 截图），但工程惯例对齐 gen.py：
  - 全 Python（gen.py 是 httpx，这里是 playwright），不裂成 py+node 两套。
  - 路径锚定脚本所在工程，不受调用时 cwd 影响。
  - 截图前等字体和纹理加载完再下快门，中文兜底靠系统 PingFang SC 不出豆腐块。
  - 默认 device_scale_factor=2 出高清（简约大字放大更要锐，2x 起步）。

把 guizang 已踩的坑直接焊进实现：
  - headless 下 WebGL / SVG 滤镜要 swiftshader，否则截到空画布。
  - networkidle 之后再多等一会，避开截到 fallback 字体那一瞬。
  - 按节点 bounding box 出图，节点多大就出多大像素，所见即所得，不靠缩放凑。

用法:
    # 截整页（默认抓页面里所有 section.poster 节点，文件名按节点 id）
    python render.py templates/cover-clean.html

    # 只截某个节点，指定输出
    python render.py templates/cover-clean.html --node "#cover" -o outimage/cover.png

    # 出 2 倍高清（默认就是 2x，这里显式写法）
    python render.py templates/cover-clean.html --scale 2

    # 强行截整页视口而不是按节点（兜底，模板里没标 .poster 时用）
    python render.py templates/x.html --full-page -o outimage/x.png

首次使用要装依赖（脚本检测到没装会再提示一遍）:
    python3 -m pip install playwright && python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
import zlib
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    # 照 gen.py 对 httpx 的写法，给一句能直接照抄执行的安装提示
    print(
        "错误: 需要 playwright 库（还要装 chromium 浏览器内核），请执行:\n"
        "  python3 -m pip install playwright && python3 -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 默认输出目录锚到脚本所在工程的 outimage/，和 gen.py 同一个口子，不受 cwd 影响
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outimage"

# 默认抓的版式节点选择器：约定模板里每张图是一个 <section class="poster ...">
DEFAULT_POSTER_SELECTOR = "section.poster"

# headless 下让 WebGL / SVG 滤镜能渲染出来（细线网格、柔影、渐变要靠它）
# 不带这两个 flag，SVG 滤镜在无头模式下会截成空白
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]

# networkidle 之后再多等这么久，确保 webfont 和内联纹理铺完，别截到 fallback 字体那一帧
FONT_SETTLE_MS = 800

# 默认设备像素比，简约大字放大更要锐，2x 起步出高清
DEFAULT_SCALE = 2


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """把节点 id 清成安全的文件名片段（保留中英文数字，其余换成短横）。"""
    cleaned = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", text).strip("-")
    return cleaned or "poster"


def _png_size(path: Path) -> tuple[int, int] | None:
    """直接读 PNG 文件头拿宽高（IHDR），不依赖 sips / Pillow，零额外依赖。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    # PNG 签名 8 字节 + 4 长度 + "IHDR" + 宽 4 + 高 4
    if (
        len(head) < 24
        or head[:8] != b"\x89PNG\r\n\x1a\n"
        or int.from_bytes(head[8:12], "big") != 13
        or head[12:16] != b"IHDR"
    ):
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    if not (0 < width <= 0x7FFFFFFF and 0 < height <= 0x7FFFFFFF):
        return None
    return width, height


def _is_complete_png(path: Path) -> bool:
    """流式校验 chunk CRC 和完整 IEND，避免截断截图替换旧文件。"""
    if _png_size(path) is None:
        return False
    try:
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return False

            chunk_index = 0
            seen_idat = False
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) != 8:
                    return False
                chunk_length = int.from_bytes(chunk_header[:4], "big")
                chunk_type = chunk_header[4:]
                if chunk_length > 0x7FFFFFFF:
                    return False
                chunk_index += 1
                if chunk_index == 1 and (
                    chunk_type != b"IHDR" or chunk_length != 13
                ):
                    return False
                if chunk_index > 1 and chunk_type == b"IHDR":
                    return False

                crc = zlib.crc32(chunk_type)
                remaining = chunk_length
                while remaining:
                    part = handle.read(min(remaining, 64 * 1024))
                    if not part:
                        return False
                    crc = zlib.crc32(part, crc)
                    remaining -= len(part)

                stored_crc = handle.read(4)
                if len(stored_crc) != 4:
                    return False
                if int.from_bytes(stored_crc, "big") != crc & 0xFFFFFFFF:
                    return False

                if chunk_type == b"IDAT":
                    seen_idat = True
                if chunk_type == b"IEND":
                    return (
                        chunk_length == 0
                        and seen_idat
                        and handle.read(1) == b""
                    )
    except OSError:
        return False


def _report(path: Path) -> None:
    """截完打印尺寸自检，对齐 gen.py 那句 KB + 路径的打印风格。"""
    size_kb = path.stat().st_size / 1024
    dims = _png_size(path)
    dim_str = f"{dims[0]}x{dims[1]}px" if dims else "尺寸未知"
    print(f"[heige-render] 出图 {dim_str} | {size_kb:.0f}KB -> {path}")


def _output_is_directory(output_path: str | None) -> bool:
    """判断 -o 是目录语义。

    目录尚未存在时，Path 无法单独判断，所以保留 README 示例里末尾
    斜杠的信号。同时兼容 Windows 的反斜杠。
    """
    if not output_path:
        return False
    return output_path.endswith(("/", "\\")) or Path(output_path).is_dir()


def _fail(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def _path_text(value, *, label: str) -> str:
    """把公开 Python 入口的路径统一为文本，避免启动浏览器后才报类型错误。"""
    try:
        value = os.fspath(value)
    except TypeError:
        _fail(f"{label}必须是字符串或文本 PathLike")
    if not isinstance(value, str):
        _fail(f"{label}必须是字符串或文本 PathLike")
    if "\x00" in value:
        _fail(f"{label}不能包含 NUL 字符")
    return value


def _prepare_output_parent(path: Path, *, create: bool) -> None:
    """验证最近现存父节点，并在需要时于启动浏览器前创建目标目录。"""
    parent = Path(os.path.abspath(str(path.parent)))
    existing_parent = parent
    while not os.path.lexists(existing_parent):
        next_parent = existing_parent.parent
        if next_parent == existing_parent:
            break
        existing_parent = next_parent
    if not existing_parent.is_dir():
        _fail(f"输出父路径不是目录: {existing_parent}")
    if not os.access(existing_parent, os.W_OK | os.X_OK):
        _fail(f"输出的最近现存父目录不可写: {existing_parent}")
    if not create:
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _fail(f"输出父目录无法创建: {parent}（{error}）")
    if not parent.is_dir():
        _fail(f"输出父路径不是目录: {parent}")
    if not os.access(parent, os.W_OK | os.X_OK):
        _fail(f"输出父目录不可写: {parent}")


def _validate_png_output(path: Path) -> None:
    """校验最终截图目标，防止误写非 PNG 或跟随符号链接。"""
    if path.suffix.lower() != ".png":
        _fail(f"输出文件必须使用 .png 扩展名: {path}")
    if path.is_symlink():
        _fail(f"输出文件不能是符号链接: {path}")
    if path.exists() and not path.is_file():
        _fail(f"输出目标已存在但不是普通文件: {path}")


def _prepare_png_output(path: Path) -> None:
    _validate_png_output(path)
    _prepare_output_parent(path, create=True)


def _create_atomic_temp(path: Path) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        temp_path = path.parent / f".heige-render-{secrets.token_hex(8)}.png"
        try:
            return os.open(temp_path, flags, 0o666), temp_path
        except FileExistsError:
            continue
    raise FileExistsError("无法创建唯一的截图临时文件")


def _screenshot_atomically(path: Path, screenshot, **kwargs) -> None:
    """先把截图写到同目录临时 PNG，成功后再原子替换目标。"""
    _prepare_png_output(path)

    fd = None
    temp_path = None
    try:
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            existing_mode = None

        fd, temp_path = _create_atomic_temp(path)
        os.close(fd)
        fd = None
        screenshot(path=str(temp_path), **kwargs)
        if not temp_path.is_file() or temp_path.is_symlink():
            raise OSError(f"截图没有产生普通文件: {temp_path}")
        if temp_path.stat().st_size == 0 or not _is_complete_png(temp_path):
            raise OSError(f"截图没有产生有效 PNG: {temp_path}")
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        if path.is_symlink():
            raise OSError(f"输出目标在截图期间变成了符号链接: {path}")
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


# ---------------------------------------------------------------------------
# 核心渲染
# ---------------------------------------------------------------------------

def render(
    html_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str] | None = None,
    node_selector: str | None = None,
    scale: int = DEFAULT_SCALE,
    full_page: bool = False,
    settle_ms: int = FONT_SETTLE_MS,
) -> list[str]:
    """把一个 HTML 文件渲染成 PNG。

    三种截法，优先级从高到低：
      1. node_selector 指定了某个节点 -> 只截这一个节点的 bounding box。
      2. full_page=True -> 截整页（兜底，模板里没标 .poster 时用）。
      3. 默认 -> 抓页面里所有 section.poster 节点，逐个按 id 出图。

    返回: 实际写出的 PNG 路径列表。
    """
    html_path = _path_text(html_path, label="HTML 输入")
    if output_path is not None:
        output_path = os.path.expanduser(
            _path_text(output_path, label="输出路径")
        )
    html_file = Path(html_path).expanduser().resolve()
    if not html_file.is_file():
        _fail(f"HTML 输入必须是普通文件: {html_file}")
    if isinstance(settle_ms, bool) or not isinstance(settle_ms, int) or settle_ms < 0:
        _fail("settle_ms 必须是非负整数")
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not 1 <= scale <= 4
    ):
        _fail("scale 必须是 1 至 4 的数值")
    if node_selector and full_page:
        _fail("--node 与 --full-page 不能同时使用")
    if output_path is not None and Path(output_path).is_symlink():
        _fail(f"输出路径不能是符号链接: {output_path}")
    if output_path is not None:
        # 单节点时它可能是文件，多节点时可能是目录；
        # 两种语义都要求它的父链可创建，这一点可在启动浏览器前确定。
        _prepare_output_parent(Path(output_path), create=False)

    # 节点和整页模式在启动浏览器前就能确定最终文件。
    if node_selector or full_page:
        explicit_out = (
            Path(output_path)
            if output_path
            else OUTPUT_DIR / f"{html_file.stem}.png"
        )
        _prepare_png_output(explicit_out)

    url = html_file.as_uri()  # file:// 绝对路径，Windows / macOS 都对
    written: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        # 给一个大点的视口 + 高像素比，版式 CSS 里尺寸是焊死的，视口只要够大别裁掉就行
        page = browser.new_page(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=scale,
        )

        print(f"[heige-render] 载入 {url} | 像素比 {scale}x")
        # networkidle 等远程 webfont 拉完；拉不到也无所谓，字体栈里有 PingFang SC 兜底
        page.goto(url, wait_until="networkidle")
        # 关键一步：确保浏览器拿到的字体真的就绪，再等一会铺纹理，别截到 fallback 那一帧
        try:
            page.evaluate("document.fonts && document.fonts.ready")
        except Exception:
            pass
        page.wait_for_timeout(settle_ms)

        # ---- 截法 1：指定了单个节点 ----
        if node_selector:
            el = page.query_selector(node_selector)
            if el is None:
                browser.close()
                _fail(f"页面里找不到节点 {node_selector}")
            out = Path(output_path) if output_path else OUTPUT_DIR / f"{html_file.stem}.png"
            _validate_png_output(out)
            _screenshot_atomically(out, el.screenshot)
            _report(out)
            written.append(str(out))
            browser.close()
            return written

        # ---- 截法 2：整页兜底 ----
        if full_page:
            out = Path(output_path) if output_path else OUTPUT_DIR / f"{html_file.stem}.png"
            _validate_png_output(out)
            _screenshot_atomically(out, page.screenshot, full_page=True)
            _report(out)
            written.append(str(out))
            browser.close()
            return written

        # ---- 截法 3：默认抓所有 section.poster 逐个出 ----
        posters = page.query_selector_all(DEFAULT_POSTER_SELECTOR)
        if not posters:
            # 模板里没标 .poster，给清晰提示并回退到整页，免得 agent 一脸懵
            print(
                f"[heige-render] 没找到 {DEFAULT_POSTER_SELECTOR} 节点，回退截整页。\n"
                "  约定：每张版式图包一个 <section class=\"poster ...\"> 才能逐张出图。",
                file=sys.stderr,
            )
            if output_path and _output_is_directory(output_path):
                out = Path(output_path) / f"{html_file.stem}.png"
            else:
                out = (
                    Path(output_path)
                    if output_path
                    else OUTPUT_DIR / f"{html_file.stem}.png"
                )
            _validate_png_output(out)
            _screenshot_atomically(out, page.screenshot, full_page=True)
            _report(out)
            written.append(str(out))
            browser.close()
            return written

        # -o 怎么解释：只有一个节点时当成文件名；多个节点时当成目录（每张按 id 命名）
        single_named = (
            output_path is not None
            and len(posters) == 1
            and not _output_is_directory(output_path)
        )
        if (
            len(posters) > 1
            and output_path is not None
            and Path(output_path).is_file()
        ):
            browser.close()
            _fail("多节点模式的 -o 必须是目录，不能指向现存普通文件")
        if single_named:
            out_dir = Path(output_path).parent
        elif output_path:
            out_dir = Path(output_path)  # 多节点：-o 是目录
        else:
            out_dir = OUTPUT_DIR
        node_ids = [
            el.get_attribute("id") or f"{html_file.stem}-{i + 1}"
            for i, el in enumerate(posters)
        ]
        if single_named:
            outputs = [Path(output_path)]
        else:
            slug_groups: dict[str, list[str]] = {}
            slugs = []
            for node_id in node_ids:
                slug = _slugify(node_id)
                slugs.append(slug)
                collision_key = (
                    slug.casefold()
                    if sys.platform == "darwin" or os.name == "nt"
                    else slug
                )
                slug_groups.setdefault(collision_key, []).append(node_id)
            collisions = {
                slug: ids for slug, ids in slug_groups.items() if len(ids) > 1
            }
            if collisions:
                details = "; ".join(
                    f"{slug}.png <- {ids!r}"
                    for slug, ids in collisions.items()
                )
                browser.close()
                _fail(f"节点 id 清洗后的输出文件名冲突，未执行任何截图: {details}")
            outputs = [out_dir / f"{slug}.png" for slug in slugs]

        # 在第一张截图前校验全部最终目标，避免半批交付。
        for out in outputs:
            _prepare_png_output(out)

        for el, out in zip(posters, outputs):
            _screenshot_atomically(out, el.screenshot)
            _report(out)
            written.append(str(out))

        browser.close()

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="heige-image 无 key 版式渲染 - Playwright 把 HTML 模板截成 PNG（零 API、文字精确）",
    )

    parser.add_argument(
        "html", metavar="HTML_FILE",
        help="要渲染的 HTML 模板路径，如 templates/cover-clean.html",
    )

    capture_mode = parser.add_mutually_exclusive_group()
    capture_mode.add_argument(
        "--node", "-n", default=None, metavar="CSS_SELECTOR",
        help="只截这一个节点（CSS 选择器，如 '#cover'）；不给则抓所有 section.poster 逐张出",
    )

    parser.add_argument(
        "--output", "-o", default=None,
        help="输出路径。单节点时是文件名；多节点时当目录用（默认 outimage/，文件名按节点 id）",
    )

    parser.add_argument(
        "--scale", "-s", type=int, default=DEFAULT_SCALE,
        choices=range(1, 5), metavar="1-4",
        help=f"设备像素比，出高清图（默认: {DEFAULT_SCALE}x，简约大字放大更要锐）",
    )

    capture_mode.add_argument(
        "--full-page", action="store_true",
        help="强制截整页视口而不是按 .poster 节点（兜底，模板没标 .poster 时用）",
    )

    parser.add_argument(
        "--settle-ms", type=int, default=FONT_SETTLE_MS, metavar="MS",
        help=f"字体和纹理铺完后的额外等待毫秒（默认: {FONT_SETTLE_MS}，慢网或重纹理可调大）",
    )

    args = parser.parse_args()

    if args.settle_ms < 0:
        parser.error("--settle-ms 必须是非负整数")

    paths = render(
        html_path=args.html,
        output_path=args.output,
        node_selector=args.node,
        scale=args.scale,
        full_page=args.full_page,
        settle_ms=args.settle_ms,
    )

    print(f"\n[heige-render] 全部完成，共出 {len(paths)} 张图")


if __name__ == "__main__":
    main()
