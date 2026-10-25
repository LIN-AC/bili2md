将 Netscape 格式的 B 站 Cookie 放在此目录，例如 `cookies.txt`。
首次使用复制 `settings.example.json` 为 `settings.json`，将 `cookies` 设为文件路径。
相对路径以 settings.json 所在目录为基准，也支持绝对路径。

路径优先级：命令行 `--cookies` > 环境变量 `BILI2MD_COOKIES` > JSON 配置的 `cookies`。
`--settings` 可指定其他 JSON 配置文件。`--config` 仍专指字幕工具的 TOML 凭证文件。
Cookie 和本地配置被忽略，不提交真实凭证。
