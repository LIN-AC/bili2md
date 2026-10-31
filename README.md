# bili2md

## 外部依赖

| 依赖 | 什么时候需要 |
| --- | --- |
| Python 3.10+ | 所有运行方式都需要；项目仅使用 Python 标准库，无需安装 Python 库依赖。 |
| yt-dlp | 无可用中文字幕、需要下载音频交给 Whisper 时需要。已有字幕或使用已有文本时不需要。 |
| FFmpeg | 下载的音频需要转换为 Whisper 可读格式时需要；当前 `.flac`、`.mp3`、`.ogg`、`.wav` 直接转写。 |
| whisper.cpp | 无可用中文字幕时需要其 `whisper-cli` 和本地 GGML 模型；已有字幕或已有文本时不需要。 |

## CLI 参数与命令

输入支持 B 站 BV 视频链接、短链接、`?p=2` 等分 P，以及 `--input-text` 指定的 UTF-8 文本。省略链接且未指定文本时，会交互询问链接。

提取字幕不要求指定总结模型。生成 Markdown 时，必须在 `--ollama-model` 和 `--api-model` 中选择一个，二者互斥。本地模式需要已启动的 Ollama 服务及已安装模型；API 模式需要服务地址、模型名称及凭证环境变量。

| 参数 | 默认值 / 要求 |
| --- | --- |
| `url` | 可选位置参数；未提供时交互输入。 |
| `-o`, `--output` | 默认 `subtitles/<视频标题>.txt`；默认重名自动编号，显式路径会覆盖已有文件。 |
| `--input-text` | 默认未设置；与视频链接、`-o` 互斥，必须指定总结模型。 |
| `--timeout` | 每次字幕提取请求 120 秒。 |
| `--preferred-lang` | `zh-CN`，影响中文字幕候选优先级。 |
| `--cookies` | Netscape Cookie 文件；优先于环境变量 `BILI2MD_COOKIES` 和配置文件。无 Cookie 可尝试获取字幕，确认空字幕及下载音频需要有效登录。 |
| `--settings` | 项目目录下 `config/settings.json`，读取其中的 `cookies` 路径；相对路径以配置文件目录为基准。 |
| `--no-asr` | 默认关闭；传入后不使用 Whisper，无中文字幕时停止。 |
| `--yt-dlp` | `yt-dlp`，通过 PATH 查找，也可指定完整路径。 |
| `--ffmpeg` | `ffmpeg`，通过 PATH 查找，也可指定完整路径。 |
| `--whisper-cli` | `D:\whisper.cpp\build-vs\bin\Release\whisper-cli.exe`；ASR 时须指向可运行文件。 |
| `--model` | `D:\whisper.cpp\ggml-large-v3-turbo.bin`；这是 ASR 模型路径，不是总结模型。 |
| `--asr-timeout` | 下载、转换、转写每个阶段各 3600 秒。 |
| `--ollama-model` | 无默认值；本地总结时必填，模型需已安装。 |
| `--ollama-url` | `http://127.0.0.1:11434`，仅允许本机服务地址。 |
| `--ollama-timeout` | 本地总结请求 600 秒。 |
| `--api-model` | 无默认值；API 总结时必填。 |
| `--api-base-url` | 无默认值；API 总结时必填，通常包含 `/v1`，不要包含 `/chat/completions`。 |
| `--api-key-env` | `BILI2MD_API_KEY`，保存 API Key 的环境变量名称。 |
| `--api-timeout` | API 总结请求 600 秒。 |
| `--summary-output` | 默认 `summaries/<文本文件名>.md`；设置时必须指定总结模型，不能与原始文本路径相同。 |
| `-h`, `--help` | 查看帮助。 |

以下命令在项目目录的 PowerShell 中运行；总结示例需本机已安装 `qwen2.5:7b`，Cookie、音频工具及模型路径按实际环境配置。

```powershell
# 仅获取中文字幕，禁止下载音频和转写
python bili2md.py "https://www.bilibili.com/video/BV1YRnW6LESu" --no-asr

# 获取字幕；必要时使用 Whisper；最后生成 Markdown
python bili2md.py "https://www.bilibili.com/video/BV1YRnW6LESu" --ollama-model "qwen2.5:7b"

# 明确指定原文和总结位置
python bili2md.py "https://www.bilibili.com/video/BV1YRnW6LESu?p=1" -o "subtitles/demo.txt" --ollama-model "qwen2.5:7b" --summary-output "summaries/demo.md"

# 总结上一步保存的文件，不重新获取视频
python bili2md.py --input-text "subtitles/demo.txt" --ollama-model "qwen2.5:7b"

# 查看所有参数
python bili2md.py --help
```

## 处理流程

1. 读取视频信息和目标分 P，优先获取可用中文字幕，包括 CC 和 AI 中文字幕。
2. 确认没有中文字幕或仅有其他语言字幕时，校验登录状态，使用 yt-dlp 下载音频，必要时由 FFmpeg 转换，再用 whisper.cpp 转写。`--no-asr` 可关闭此步骤。请求失败、权限不足或字幕状态不确定时明确报错，不误判为无字幕。
3. 将标题、简介和字幕/转写正文保存到 UTF-8 文本，采用“标题 / 简介 / 正文”三段结构，另存元数据及完整文件哈希。
4. 指定总结模型时，直接读取保存的文本，调用本地 Ollama 或兼容 API 的 LLM 生成 Markdown。标题、简介只作为热词和上下文参考，以正文为事实依据。不指定模型则保存文本后结束。

`--input-text` 从已有文本直接进入总结，跳过视频获取与 ASR。总结前验证存在的元数据哈希；旧版纯正文文本也可读取。模型失败时保留输入文件和已有总结。
