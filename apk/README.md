# TVBox / 影视仓客户端安装包

由于 Android APK 文件体积较大，本仓库默认通过 `.gitignore` 忽略 `*.apk`。

### 推荐客户端

- **影视仓 (TVBox 多仓版)**：支持多仓订阅、多线路智能聚合与播放。
  - 架构：`arm64-v8a` / `armeabi-v7a`
- 本机机顶盒调试已测试通过，支持直接通过 ADB 一键推送到局域网设备：
  ```bash
  adb connect 192.168.0.245:5555
  adb install -r apk/tvbox.apk
  ```
