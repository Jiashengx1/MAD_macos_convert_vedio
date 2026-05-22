# macOS GUI 打包说明

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

- `dist/MacVideoConverter.app`
- `dist/MacVideoConverter-macos.zip`

`.app` 内已包含 Python 运行时、GUI、`ffmpeg` 和 `ffprobe`。把 zip 发给别人，对方解压后双击 `MacVideoConverter.app` 即可。

转换输出固定为标准 MP4：H.264 视频、AAC 音频、1920x1080 画面。

## macOS 安全提示

这个应用没有 Apple 开发者签名和公证。别人第一次打开时，macOS 可能提示无法验证开发者；可在 Finder 中右键应用，选择“打开”，再确认打开。若要彻底消除该提示，需要 Apple Developer ID 证书并做 notarization。

## 架构提示

当前机器打出来的是 Apple Silicon/arm64 版本，适用于 M1/M2/M3/M4 等 Mac。若要支持 Intel Mac，需要在 Intel Mac 上再打一个 Intel 版本，或使用 universal2 Python 和 universal FFmpeg 重新打包。
