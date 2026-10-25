# 当前可运行版本：中文字幕、本地 ASR、Ollama 及云端 API 总结

已验证版本：**1.2**（Git 标签 `v1.2`）。

使用 Python 3.10+ 标准库调用本机已安装的 `bilibili-subtitle-fetch`，无需为本项目安装 Python 依赖。
优先获取视频已有中文字幕（包括可访问的中文 AI 字幕），保存 UTF-8 纯文本。仍向字幕工具明确传入 `--no-asr`，由本项目控制无中文字幕时的本地 whisper.cpp 兜底。可选本机 Ollama 或 OpenAI-compatible API 生成中文 Markdown 总结；不指定 `--ollama-model` 或 `--api-model` 时保持仅保存字幕的原行为。

```powershell
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP"
# 或交互输入链接
python bili2md.py
# 自定义保存路径
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP" -o "subtitles/demo.txt"
```

默认保存为当前目录的 `subtitles/<BV号>_p<分P号>.txt`。短链接使用短链接路径作为文件名。
支持标准 BV 视频链接、`?p=2` 分 P 链接及工具支持的 B 站短链接。通过字幕工具自己的 Python 环境运行薄包装层 `chinese_subtitle_fetch.py`，在候选字幕阶段按 `lan` 标识过滤，只保留 `zh`（含 `zh-CN`、`zh-Hans`、`zh-Hant`）、`cmn`、`yue` 及对应 `ai-` 前缀轨道，然后复用原工具的选择、下载和格式化逻辑。只有英文字幕或没有字幕时进入 ASR；不凭字幕文本内容猜测语言。
重复运行会覆盖同名文件；提取或转录失败不写入输出文件。字幕工具报错、空内容或请求超时会重试一次；仅当过滤后没有中文字幕，并确认字幕凭证登录有效后，才进入 ASR。请求失败直接保留原有错误处理，不转换成“没有中文字幕”。
网络/HTTPS 错误会单独标注，并保留工具错误信息（例如 HTTP 412）；其他提取失败标注为字幕提取错误。成功退出码为 0，失败为 1，手动取消为 130。

工具应在 PATH 中，也会检查 Windows 下的 `~/.local/bin/bilibili-subtitle-fetch.exe`。可手动指定：

```powershell
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP" --fetcher "C:\Users\Lin\.local\bin\bilibili-subtitle-fetch.exe"
```

默认复用字幕工具现有 cookie 配置，Windows 路径为 `%APPDATA%\bilibili-subtitle-fetch\config.toml`。
如未配置或 cookie 过期，在本机运行 `bilibili-subtitle-fetch init`；不要将 cookie 提交到仓库。
也可通过 `--config <路径>` 使用指定配置。AI 字幕通常需要有效登录 Cookie。字幕工具返回“无字幕”时无法区分真正无字幕与 Cookie 失效，因此本项目另外使用相同凭证调用登录检查 API。若默认字幕 Cookie 已失效，会从项目中的 Netscape Cookie 创建临时字幕配置，先验证登录，再重新调用原字幕工具；仍无字幕才进入 ASR。显式指定 `--config` 时不替换失效配置。
网络错误、登录检查失败、字幕提取失败、空字幕文本都不会触发 ASR。登录有效且字幕列表经过语言过滤后没有中文轨道，是当前“无可用中文字幕”的判断依据，不能排除平台侧隐藏字幕等情况。
`--timeout 120` 设置每次工具调用超时秒数；`--preferred-lang zh-CN` 设置中文候选轨道中的首选语言，不会允许回退到英文字幕。

## 验证

中文字幕过滤及配置迁移后，18 项 Python 测试通过，覆盖仅英文 AI 字幕、中文与英文混合、空轨道、请求失败传播、Cookie 路径优先级及原 ASR 分支。再次使用用户提供的两分钟视频 `BV1BgNb6oEnk` 跑通完整 Python 调用链，迁移后的 Cookie 路径生效，成功保存 650 字符。仅英文等语言组合由模拟元数据验证，未扩大联网视频测试范围。

```powershell
python -m unittest -v
python bili2md.py "https://www.bilibili.com/video/BV1zvnb6eEMP"
```

2026-10-24 已使用上述视频和本机已安装的工具完成实际联网提取，输出 5,312 字符到
`subtitles/BV1zvnb6eEMP_p1.txt`。测试覆盖文本保存、禁用 ASR、重试成功、超时、无字幕保护已有文件、错误文本和链接验证。
联网测试依赖视频状态、网络及本机 cookie；单元测试使用模拟工具响应，不依赖网络。

## 本地 ASR 使用方式

```powershell
python bili2md.py "https://www.bilibili.com/video/BV1BgNb6oEnk"
# 仅提取已有字幕，恢复第一版行为
python bili2md.py "https://www.bilibili.com/video/BV1BgNb6oEnk" --no-asr
```

无中文字幕时调用 `yt-dlp -f bestaudio`。Cookie 路径优先级为 `--cookies <路径>` > 环境变量 `BILI2MD_COOKIES` > `config/settings.json` 中的 `cookies` 字段。首次使用复制 `config/settings.example.json` 为 `config/settings.json`；配置示例为 `{"cookies": "cookies.txt"}`，相对路径以配置所在目录解析，也支持绝对路径。可用 `--settings <JSON路径>` 更换配置。Cookie 文件名不写死在 Python 中；下载时使用临时副本，避免 yt-dlp 回写原文件。

当前项目原 `www.bilibili.com_cookies.txt` 已移至 `config/cookies.txt` 并配置引用；原根目录 `cookies.txt` 保留为 `config/legacy_cookies.txt`，未更改内容。真实 Cookie 和本地配置均由 `.gitignore` 忽略，只提交无敏感信息的示例和说明。`--config` 继续专指字幕工具的 TOML 凭证配置，与本项目的 `--settings` 分开。
默认只下载请求的分 P，未指定分 P 则取第一 P。下载文件为 flac、mp3、ogg 或 wav 时直接传给 whisper-cli；其他格式使用 FFmpeg 转为 16 kHz、单声道、16-bit PCM WAV。
调用 whisper-cli 的 `-l zh -otxt -of <临时路径>`，读取生成的非空文本后，按现有命名规则写入 `subtitles`。音频和中间转录文件自动清理；Cookie 文件已加入 `.gitignore`。

默认资源：

- `yt-dlp`、`ffmpeg`：从 PATH 查找。
- whisper-cli：`D:\whisper.cpp\build-vs\bin\Release\whisper-cli.exe`。
- 模型：`D:\whisper.cpp\ggml-large-v3-turbo.bin`。

可用 `--yt-dlp`、`--ffmpeg`、`--whisper-cli`、`--model` 指定路径；这些资源只在进入 ASR 时检查，有字幕流程不依赖它们。
`--asr-timeout 3600` 设置下载、转换、转录各阶段超时秒数。每个外部程序均检查退出码和必需输出文件，退出成功但缺少转录文本也视为失败。
登录检查需使用字幕工具所属的 Python 环境，Windows 下自动从启动器中读取解释器路径；其他安装方式可用 `--subtitle-python <路径>` 指定。

2026-10-25 新增调用链路实测：用户提供的两分钟视频 `BV1BgNb6oEnk`，默认字幕 Cookie 失效时被正确识别，随后用项目 Netscape Cookie 验证登录并重新确认无字幕，完成 yt-dlp 下载、FFmpeg WAV 转换、本地 large-v3-turbo 中文转录，保存到 `subtitles/BV1BgNb6oEnk_p1.txt`（650 字符）。原有有字幕视频 `BV1zvnb6eEMP` 仍成功提取 5,312 字符，未启动 ASR。Python 单元测试还覆盖登录失效不启动 ASR、凭证更新后重新找到字幕、外部调用失败及空转录文件等分支；这些失败分支使用模拟响应。

## 本地 Ollama 中文总结

先启动本机 Ollama，并准备好要使用的模型。程序不会下载模型，也不会写死模型名。可用 `ollama list` 查看本机模型；下面的 `qwen2.5:7b` 仅为本轮实测示例，请替换为已安装的模型名称。

```powershell
# 读取已有 UTF-8 字幕或 ASR 文本，不访问 B 站、不重新转录
python bili2md.py --input-text "subtitles/BV1BgNb6oEnk_p1.txt" --ollama-model "qwen2.5:7b"

# 完整流程：字幕提取 / ASR → 保存原文 → 本地 Ollama → Markdown
python bili2md.py "https://www.bilibili.com/video/BV1BgNb6oEnk" --ollama-model "qwen2.5:7b"

# 自定义总结路径和请求超时
python bili2md.py --input-text "subtitles/BV1BgNb6oEnk_p1.txt" --ollama-model "qwen2.5:7b" --summary-output "summaries/demo.md" --ollama-timeout 600
```

- `--ollama-model`：指定模型并启用总结。已有的 `--model` 仍表示 whisper.cpp 模型文件，含义不变。
- `--input-text`：读取已有文本，必须指定总结模型；不能同时传视频链接或 `--output`。
- `--summary-output`：Markdown 输出路径，默认 `summaries/<字幕文件名>.md`。`--output` 仍只控制原字幕的路径。
- `--ollama-url`：本机服务地址，默认 `http://127.0.0.1:11434`，仅支持本机回环地址；请求绕过代理。
- `--ollama-timeout`：单次总结请求超时秒数，默认 600。

实现复用 Ollama 官方 [`POST /api/generate`](https://docs.ollama.com/api/generate)，使用 `stream: false`，读取完成响应中的 `response`。总结包含模型和原字幕文件名；从链接运行时也记录视频来源。原始字幕不被总结覆盖，生成结果目录已加入 `.gitignore`。

服务无法连接、超时、模型不存在、HTTP/API 错误、无效响应或空总结都会明确报错并返回退出码 1；失败时不创建新总结、不覆盖已有总结。链接流程中已保存的字幕仍会保留，可稍后用 `--input-text` 重试。

当前最小版本将整份字幕一次性发送给模型，不分块处理长视频；输入和输出受所选模型的上下文及生成能力限制。总结基于字幕表述，可能继承转录或模型错误，不能替代事实核验。

2026-10-25 第三轮实测：先用已有 `subtitles/BV1BgNb6oEnk_p1.txt` 和本机 `qwen2.5:7b` 成功生成 Markdown，再使用同一两分钟视频验证从链接经 ASR 到总结的完整流程。原文仍保存为 650 字符，总结输出到 `summaries/BV1BgNb6oEnk_p1.md`。26 项 Python 测试通过；服务不可连接、模型不存在、超时、无效响应和失败保护等分支使用模拟响应验证，没有关闭或改动本机 Ollama 服务。

## OpenAI-compatible 云端 API 总结（第四轮）

第一版仅实现非流式 Chat Completions：向 `<base_url>/chat/completions` 发送 `model`、`messages` 和 `stream: false`，读取 `choices[0].message.content`。协议参考 [OpenAI 官方 Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。不自动选择模型、下载模型、调用付费接口试探或在失败时切换供应商。

先在运行程序的环境中配置 API Key，默认环境变量名为 `BILI2MD_API_KEY`。若凭证已存放在其他环境变量，用 `--api-key-env` 传入**变量名称**。不要把 Key 写入源码、命令参数或配置文件。程序不读取 `.env`；如使用该文件，应由你自己的运行环境加载，文件已被 Git 忽略。

下面的地址和模型均为占位示例，使用时替换为自己提供的配置，示例不包含认证值：

```powershell
# 已有文本 → API → Markdown；运行前已设置相应环境变量
python bili2md.py --input-text "subtitles/BV1BgNb6oEnk_p1.txt" --api-base-url "https://api.example.com/v1" --api-model "YOUR_MODEL" --api-key-env "BILI2MD_API_KEY"

# 视频链接 → 原有字幕/ASR → API → Markdown
python bili2md.py "https://www.bilibili.com/video/BV1BgNb6oEnk" --api-base-url "https://api.example.com/v1" --api-model "YOUR_MODEL" --summary-output "summaries/api-summary.md"
```

- `--api-model`：指定模型并选择云端总结；与 `--ollama-model` 互斥。
- `--api-base-url`：必填根地址，按服务要求包含 `/v1` 或其他前缀，不包含 `/chat/completions`；禁止在 URL 中放认证信息。云端使用 HTTPS，本机模拟允许 HTTP。
- `--api-key-env`：环境变量名，默认 `BILI2MD_API_KEY`，认证值只用于 Bearer 请求头。
- `--api-timeout`：单次请求超时秒数，默认 600；不会自动重试，避免额外请求。

API 和 Ollama 复用相同总结提示词、`--input-text`、`--summary-output`、Markdown 文件命名及原子保存流程，不改变字幕、ASR 或 whisper.cpp 的 `--model` 参数。默认总结输出到 `summaries`，原文保留。

缺少凭证、模型或地址会在访问视频前报错；401/403、404、429、服务错误、超时及格式错误均有明确提示。失败时不保存新总结、不覆盖已有总结；链接流程已生成的字幕仍保留。错误提示不输出 Key、响应正文、认证头或请求 URL；重定向被拒绝，避免向其他地址转发认证信息。仅接受正常完成且非空的文本，不保存被截断的结果。

测试范围：已验证本机伪 API HTTP 链路、参数及认证传递、错误脱敏、Markdown 保存和原文保护。2026-10-26 使用用户提供的 DeepSeek 官方地址、`deepseek-flash` 模型及 `DEEPSEEK_API_KEY` 环境变量，基于已保存的短字幕完成一次真实 API 请求，成功生成 3,482 字节 Markdown，原字幕哈希未变；未重复运行字幕提取或 ASR，未查看、输出或记录 Key 实际值。`v1.1` 保留为第三轮版本，`v1.2` 包含第四轮云端 API 功能。

以下保留原始项目规划，HTML 报告尚未实现。

# 组织结构图
## b站视频自动总结项目
- 输入链接
  - 测试连通性
    - 连通
      - 判断视频是否有ai字幕
        - 有:提取ai字幕并存储
          - 有字幕但是提取失败，再次重新提取，再次尝试失败后返回错误信息，需要标注是字幕提取错误
        - 没有ai字幕，优先使用yt-dlp尝试只提取音频，失败则下载视频至本地，使用ffmeg转为音频
          - 调用本地asr进行处理\(whisper.cpp\)
            - 将处理后的文本数据进行储存
              - 提示选择api或者本地语言模型
                - 将文本数据进行精简处理
                  - 输出处理后的数据
    - 连通失败
      - 返回https错误信息
- 初始配置
  - cookies，避免b站412风控
- 关于如何下载视频，我还没有想法
- 后续优化输出可以参考该项目https://github.com/imexlovery/video-report-agent?utm\_source=chatgpt.com
  - 项目描述：该项目主要是输入文本，将文本进行总结输出HTML总结

### TODO：统一 Cookie 配置（后续考虑）

**当前状态：**
- `bilibili-subtitle-fetch`：使用工具自身的 Cookie 配置，已验证正常。
- `yt-dlp`：使用 Netscape 格式 Cookie 文件，需要支持用户自定义路径。

**后续考虑：**

如果未来需要统一管理认证配置，再研究两个工具的 Cookie 格式、配置方式及兼容性。

**当前不实施统一集成，不修改已验证的字幕提取流程。**
