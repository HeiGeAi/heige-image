# Changelog

## 1.1.1

- 对 API 图片启用 20 MiB 流式上限，并验证完整 PNG 容器、CRC 与像素流。
- 在任何付费请求前拒绝 JPEG 扩展名错配、输出 symlink 及父目录 symlink，写入时再次校验并保留原子替换。
- CI 增加真实 Chromium 版式渲染与 2160×2880 PNG 门禁。
