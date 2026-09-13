# Changelog

## 2.0.1

### Fixed

- 输出路径校验不再误拒符号链接父目录，macOS 的 /tmp、/var 下 -o 输出恢复可用（父目录解析为真实路径落盘，最终落点文件仍为 symlink 时依旧拒绝）。
- gen.py / edit.py 的 200 响应解析加防护：响应体非 JSON 或 data 为空/结构异常时统一返回失败信息，不再裸抛 traceback。
- 重试循环覆盖全部 httpx 网络异常（ReadError、RemoteProtocolError、DecodeError 等），批量模式下不再被单任务连接重置中断整批。
- 图片下载 URL 加 SSRF 防护：仅允许 https，拒绝字面内网地址与本机名。
- 日志打印下载 URL 时剥离 query 与 fragment，签名取图凭证不再落入会话或 CI 日志。

## 2.0.0

- 去掉固定画风预设，改为需求驱动：用户指定 > 配套文件推断 > 风格库默认三级决策链。
- 内置设计总监 agent（references/design-director.md）：出图前产《设计方案》，出图后按方案逐项验收，含无 Agent 工具环境的降级跑法。
- 新增风格库 references/style-library.md，十个风格词条备好 prompt 词汇与禁忌项，原粘土 3D 签名降为普通词条。
- 删除 references/heige-visual-signature.md，决策矩阵与反 AI 体检去品牌化。
- 模板默认色板换中性编辑黑，新增 custom 色板位供设计方案填入；修正注释中失实的「render.py 注入」说法。
- 三桶比例表修正：2.35:1 换成脚本实际支持的 5:4。

## 1.1.1

- 对 API 图片启用 20 MiB 流式上限，并验证完整 PNG 容器、CRC 与像素流。
- 在任何付费请求前拒绝 JPEG 扩展名错配、输出 symlink 及父目录 symlink，写入时再次校验并保留原子替换。
- CI 增加真实 Chromium 版式渲染与 2160×2880 PNG 门禁。
