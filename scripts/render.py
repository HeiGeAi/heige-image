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
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    # 照 gen.py 对 httpx 的写法，给一句能直接照抄执行的安装提示
    print(
        "错误: 需要 playwright 库（还要装 chromium 浏览器内核），请执行:\n"
        "  pip install playwright && playwright install chromium",
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
        head = path.read_bytes()[:24]
    except OSError:
        return None
    # PNG 签名 8 字节 + 4 长度 + "IHDR" + 宽 4 + 高 4
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return width, height


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


# ---------------------------------------------------------------------------
# 核心渲染
# ---------------------------------------------------------------------------

def render(
    html_path: str,
    output_path: str | None = None,
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
    html_file = Path(html_path).resolve()
    if not html_file.exists():
        print(f"错误: HTML 文件不存在: {html_file}", file=sys.stderr)
        sys.exit(1)

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
                print(f"错误: 页面里找不到节点 {node_selector}", file=sys.stderr)
                sys.exit(1)
            out = Path(output_path) if output_path else OUTPUT_DIR / f"{html_file.stem}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            el.screenshot(path=str(out))
            _report(out)
            written.append(str(out))
            browser.close()
            return written

        # ---- 截法 2：整页兜底 ----
        if full_page:
            out = Path(output_path) if output_path else OUTPUT_DIR / f"{html_file.stem}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=True)
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
            out = Path(output_path) if output_path else OUTPUT_DIR / f"{html_file.stem}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=True)
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
        if single_named:
            out_dir = Path(output_path).parent
        elif output_path:
            out_dir = Path(output_path)  # 多节点：-o 是目录
        else:
            out_dir = OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        for i, el in enumerate(posters):
            node_id = el.get_attribute("id") or f"{html_file.stem}-{i + 1}"
            if single_named:
                out = Path(output_path)
            else:
                out = out_dir / f"{_slugify(node_id)}.png"
            el.screenshot(path=str(out))
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

    parser.add_argument(
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

    parser.add_argument(
        "--full-page", action="store_true",
        help="强制截整页视口而不是按 .poster 节点（兜底，模板没标 .poster 时用）",
    )

    parser.add_argument(
        "--settle-ms", type=int, default=FONT_SETTLE_MS, metavar="MS",
        help=f"字体和纹理铺完后的额外等待毫秒（默认: {FONT_SETTLE_MS}，慢网或重纹理可调大）",
    )

    args = parser.parse_args()

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
