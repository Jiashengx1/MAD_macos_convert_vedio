# macOS GUI 打包说明

Windows 打包见 `README_windows_app.md`。

## 运行源码版

```bash
python3.12 gui.py
```

源码版需要当前 Mac 已安装 `ffmpeg` 和 `ffprobe`。

## 生成免安装版

```bash
python3.12 build_macos_app.py
```

生成结果：

- `dist/医务科监控修复助手.app`
- `dist/医务科监控修复助手-macos.zip`

`.app` 内已包含 Python 运行时、GUI、`ffmpeg` 和 `ffprobe`。把 zip 发给别人，对方解压后双击 `医务科监控修复助手.app` 即可。

转换输出为标准 MP4：视频保持原始 H.264/HEVC 码流并重建 MP4 时间戳，音频输出为 AAC。这个版本不再使用 `h264_videotoolbox` 或 `libx264` 重新编码视频，因此不会强制缩放到 1920x1080。

打包脚本会优先使用 `vendor/ffmpeg-macos-arm64/` 中的兼容版 FFmpeg/FFprobe。不要改回 Homebrew 的动态 FFmpeg；Homebrew 版本可能引用较新 macOS 的 AVFoundation 符号，发给旧系统后会在 `ffprobe` 阶段崩溃。

## macOS 安全提示

这个应用没有 Apple 开发者签名和公证。别人第一次打开时，macOS 可能提示无法验证开发者；可在 Finder 中右键应用，选择“打开”，再确认打开。若要彻底消除该提示，需要 Apple Developer ID 证书并做 notarization。

## 架构提示

当前机器打出来的是 Apple Silicon/arm64 版本，适用于 M1/M2/M3/M4 等 Mac。若要支持 Intel Mac，需要在 Intel Mac 上再打一个 Intel 版本，或使用 universal2 Python 和 universal FFmpeg 重新打包。
