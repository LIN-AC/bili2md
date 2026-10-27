# bili2md：B 站信息、中文字幕、本地 ASR 与视频总结

当前版本：**1.3.1**（Git 标签 `v1.3.1`）。
Python 3.10+ 标准库实现，不需要安装 Python 依赖。

## 使用方式

```powershell
# 链接 → 中文字幕，必要时本地 ASR；不指定模型则不总结
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP"

# 分 P、自定义原文路径、禁止 ASR
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP?p=1" -o "subtitles/demo.txt" --no-asr

# 链接 → 原文 → 本地 Ollama 总结
python bili2md.py "https://www.bilibili.com/video/BV1BgNb6oEnk" --ollama-model "qwen2.5:7b"

# 已有文本 → 总结，不重新访问 B 站或 ASR
python bili2md.py --input-text "subtitles/BV1BgNb6oEnk_p1.txt" --ollama-model "qwen2.5:7b"
```

省略链接时交互输入。支持 BV 视频链接、分 P 和 B 站短链接。
默认原文为 `subtitles/<BV号>_p<分P号>.txt`，保持 UTF-8 纯文本；
同名 `.meta.json` 保存标题、简介、BV/AID/CID、分 P、时长、视频链接、文本来源、字幕状态和原文哈希，
不保存 Cookie 或字幕签名 URL。短链接沿用解析后的 BV 和分 P 命名。
成功重复运行覆盖同名文件；接口、权限或 ASR 失败不会写入新原文或启动总结。

## 直接接口与五种状态

`bilibili_info.py` 直接调用：
- `/x/web-interface/view`：标题、简介、视频分 P/CID 及权限字段。
- `/x/v2/dm/view?aid=...&oid=...&type=1`：字幕轨道，oid 是目标分 P 的 CID。
- 返回的正文 URL：仅使用已验证的 aisubtitle.hdslb.com HTTPS 源。
- `/x/web-interface/nav`：空列表确认及 ASR 下载前的登录检查。

不再依赖旧字幕工具、其独立 Python 环境或 TOML 凭证。
仅服务于旧工具的 `--fetcher`、`--subtitle-python`、`--config` 已移除；其他现有流程参数保留。

| 状态 | 行为 |
| --- | --- |
| chinese | 中文候选正文有效，保存文本，跳过 ASR |
| other_languages | 有其他语言、无中文候选，下载前校验通过后运行 ASR |
| no_subtitles | 字幕字段明确为 null 或 []，且登录有效，下载前校验通过后运行 ASR |
| access_denied | 付费/订阅权限不足、轨道锁定或明确登录限制，报错终止 |
| request_failed | 请求、结构、正文异常或结果不确定，重试一次；仍失败则终止，不进入 ASR |

语言依据每条轨道的 lan，包括 ai-zh、zh 及中文变体。ai_type=0 不等于人工字幕；
顶层空 lan、asr_language 不是字幕存在依据。`--preferred-lang` 仅影响中文候选优先级。
没有中文候选时不下载其他语言正文。

**不再请求播放器字幕接口或做该回退**：此前出现视频标识匹配但正文疑似串视频的问题。
正文校验语言、结构、非空文本、时间范围及可选视频标识；这些检查不能普遍证明语义归属，
主要依据仍是已验证稳定的 dm/view。中文缺少 URL 或正文异常不能按无字幕处理。

专属权限不足依据已验证组合 is_upower_exclusive=true、is_upower_play=false。
其他付费标记若无法确认已购权益，按结果不确定终止。不以通用付费提示、rights.pay=0
或单独的 is_upower_play=false 判定访问权限。

## 统一 Cookie

复制 `config/settings.example.json` 为 `config/settings.json`，配置例如：
```json
{"cookies": "cookies.txt"}
```
将 Netscape Cookie 存放在 config 目录。路径优先级：
`--cookies` > 环境变量 `BILI2MD_COOKIES` > JSON 的 cookies。
相对路径以 JSON 所在目录解析，`--settings` 可指定其他 JSON 文件。

直接接口和 yt-dlp 使用同一配置。没有文件时可匿名尝试字幕，取得有效中文不强制登录；
匿名或失效登录下的空列表不能确认无字幕。ASR 下载前必须有有效文件并通过登录检查。
正文请求不携带账号 Cookie，日志不输出完整签名 URL。yt-dlp 使用临时 Cookie 副本；
真实凭证、本地配置、原文、元数据及总结目录均由 .gitignore 排除。

## 本地 ASR

保留 yt-dlp 下载目标分 P 的 bestaudio 流程。flac、mp3、ogg、wav 直接转录；
其他格式用 FFmpeg 转成 16 kHz、单声道、16-bit PCM WAV，再调用 whisper-cli 中文转录。
中间文件自动清理，不重新下载或编译工具。

- `--yt-dlp`、`--ffmpeg`：默认从 PATH 查找。
- `--whisper-cli`：默认 D:\whisper.cpp\build-vs\bin\Release\whisper-cli.exe。
- `--model`：默认 D:\whisper.cpp\ggml-large-v3-turbo.bin，仅指 ASR 模型。
- `--timeout`：B 站单次 HTTP 请求超时，默认 120 秒。
- `--asr-timeout`：下载、转换、转录各阶段超时，默认 3600 秒。
- `--no-asr`：无中文字幕时停止。

## Ollama 和 OpenAI-compatible API

两者复用提示词、文本输入和 Markdown 原子保存流程。默认 `summaries/<原文文件名>.md`，
可用 `--summary-output` 指定。链接流程和带 .meta.json 的 `--input-text`
会把标题、简介加入总结上下文及 Markdown。元数据需与原文哈希一致；
旧文本没有元数据时仍可总结。原文始终保留。

Ollama 使用 `--ollama-model` 指定模型，`--ollama-url` 默认 http://127.0.0.1:11434，
仅支持本机地址；`--ollama-timeout` 默认 600 秒。不会自动下载或选择模型。
协议参考 [Ollama Generate API](https://docs.ollama.com/api/generate)。

云端使用与 Ollama 互斥的 `--api-model`，`--api-base-url` 指定根地址并自动追加 /chat/completions。
Key 只从环境变量读取，默认 BILI2MD_API_KEY，`--api-key-env` 传入变量名，不是 Key。
不要将 Key 写入源码、命令参数或配置。程序不自动加载 .env。
以下地址和模型仅为占位示例，运行前在本机环境中配置凭证：

```powershell
python bili2md.py --input-text "subtitles/BV1BgNb6oEnk_p1.txt" --api-base-url "https://api.example.com/v1" --api-model "YOUR_MODEL" --api-key-env "BILI2MD_API_KEY"
```

`--api-timeout` 默认 600 秒。云端要求 HTTPS，本机模拟允许 HTTP。
使用非流式 [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，
不跟随重定向、不自动重试收费请求，不输出认证头、Key 或云端原始错误正文。
总结失败保留已有 Markdown 和已保存原文。整份文本单次发送，不自动分段；
受模型上下文限制，模型及转录内容仍可能有误，不能代替事实核验。

## 验证

```powershell
python -m unittest -q
```

本轮 Mock 覆盖五态、匿名中文、登录失效、异常重试、分 P/CID、正文异常、付费阻断、
ASR 调度、标题简介到 Markdown 及原文哈希保护。保留 ASR、Ollama 和 API 回归，
不在回归中调用真实转录或模型服务。

2026-10-26 新模块真实查询四个已有样本：
- BV1zvnb6eEMP：有效中文字幕 5,312 字符，原文及元数据保存通过。
- BV1nLH46YEig：dm/view 只有 ai-en，不使用异常播放器返回的中文。
- BV1BgNb6oEnk：空字幕且登录有效。
- BV1nF4JzuEUU：订阅权限不足，停止后续访问及下载。

真实中文配合模拟生成器验证标题、简介到 Markdown 的流程；
本轮没有下载音频、运行 Whisper、调用 Ollama 或 DeepSeek。
此前 1.2 已完成用户授权的 DeepSeek 官方 API 单次实测，模型 deepseek-flash，
凭证环境变量名 DEEPSEEK_API_KEY，本轮未重复收费请求。
