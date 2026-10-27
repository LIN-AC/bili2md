将 Netscape 格式的 B 站 Cookie 放在此目录，例如 `cookies.txt`。
首次使用复制 `settings.example.json` 为 `settings.json`，将 `cookies` 设为文件路径。
相对路径以 settings.json 所在目录为基准，也支持绝对路径。

路径优先级：命令行 `--cookies` > 环境变量 `BILI2MD_COOKIES` > JSON 配置的 `cookies`。
`--settings` 可指定其他 JSON 配置文件。B 站接口和 yt-dlp 统一使用这份 Netscape Cookie，不再读取其他工具的 TOML 凭证。
未配置文件或文件不存在时可以匿名尝试字幕；空列表需有效登录才能确认。文件仅在启动时加载，ASR 下载使用已通过登录检查的内存 Cookie 导出的临时副本。
Cookie 和本地配置被忽略，不提交真实凭证。
