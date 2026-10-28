# bili2md：B 站信息、中文字幕、本地 ASR 与视频总结

当前版本：**2.0**。新增独立基础文本清洗与可选本地 Qwen 清洗，保留原文、清洗副本和独立模型配置；保留已有字幕、ASR 及总结功能。
Git 标签 `v2.0` 对应本次发布，旧版本标签保留历史快照。
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
分 P 参数仅接受单个 ASCII 正整数；空值、重复参数、正负号或非法端口会明确报错。
默认原文为 `subtitles/<安全处理后的视频标题>.txt`，保持 UTF-8 纯文本；
非法文件名字符替换为下划线，处理 Windows 保留名与尾部空格/句点，并限制标题长度。
空标题或处理后无有效标题时回退为 `<BV号>_p<分P号>`。重名使用 `标题 (2).txt` 等序号，
通过独占创建避免覆盖已有默认输出，也避开已有同名元数据和默认总结。显式 `-o` 保留原有覆盖行为。
同名 `.meta.json` 保存标题、简介、BV/AID/CID、分 P、时长、视频链接、文本来源、字幕状态和原文哈希，
不保存 Cookie 或字幕签名 URL。标题来自已获取的视频信息，不增加信息请求。
接口、权限或 ASR 失败不会写入新原文或启动总结。

## 基础文本清洗

调用链为字幕或 ASR → 保存原文及元数据 → 清洗并另存副本 → 可选模型总结。
`text_cleaning.py` 提供纯函数 `clean_text(text, source="unknown", terms=None)`
（`terms` 是仅限关键字参数）和 `save_cleaned_text()`；清洗不访问网络、不调用模型。
未启用总结时也保存清洗副本；`--input-text` 通过共用的 `summarize_file()` 接入同一流程，
Ollama 与 API 都使用清洗后的正文，标题、简介和模型接口保持不变。

原始 `.txt` 和 `.meta.json` 不被清洗修改，哈希始终校验原始文件字节。
清洗副本保存在原文目录，例如 `标题.cleaned.txt`，重名使用 `标题.cleaned (2).txt`，
独占创建以避免覆盖。每次总结生成新的清洗副本，便于与原文比较；模型失败时也保留副本。
原有显式 `-o` 覆盖原文的行为不变。清洗副本保存失败会停止任务，不继续调用模型。

规则与边界：

- 去除开头的 UTF-8 BOM；统一 CRLF、CR、Unicode 行/段分隔符为 LF。
- 行内连续空白（含制表符、不换行空格）变为单个空格，去除行首尾空白；不删除词间空格。
- CC/AI 已由字幕模块转换为按片段换行的文本，`source="subtitle"` 删除空片段行。
- ASR 与未知来源保留段落间一个空行；不猜测句子边界，不拼接非空行。
- 不删除任何重复句子，不改变非空行顺序，不改写数字、否定词、标点或专有名词。
- 术语表默认为空，只供调用方明确传入；区分大小写、匹配完整词项，长词优先且不级联替换。
  中文内部子串不会匹配，避免无分词依据的替换。本轮不增加术语配置文件或 CLI 参数。

独立调用示例：

```python
from text_cleaning import clean_text

cleaned = clean_text("  使用\t PyTorch  2.5\n\n不要删除。\n不要删除。", "subtitle")
# 使用 PyTorch 2.5\n不要删除。\n不要删除。
```

当前不处理 SRT/VTT 解析、时间戳删除、HTML 标签剥离、口头语删除、重复片段推断、
标点恢复、代码缩进还原或 ASR 语义纠错；这些内容缺少可靠依据时原样保留。
现有字幕接口/ASR 返回的是纯文本，本轮未增加原始 HTTP JSON 或音频保存功能。

专用回归：`python -B -m unittest test_text_cleaning -q`，包含 16 个小样例与流程测试，
覆盖空白、重复、来源差异、术语边界、原文和元数据保护、重名、模型/写入失败及两种总结入口。

## 可选的本地 Qwen 清洗

使用 `--llm-clean` 显式启用；不传此参数时仍只有基础清洗。
配置由 `text_cleaner.CleanerConfig` 独立管理，可通过 `--cleaner-config` 指定 JSON 文件，
参考 `config/text_cleaner.example.json`。未指定配置文件时使用如下默认值，
完全不读取 API Key、总结模型、总结 URL 或总结生成参数。

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| model | qwen3:14b-q4_K_M | 本机安装的模型标签 |
| base_url | http://localhost:11434 | 本机 Ollama 根地址，只允许回环地址 |
| timeout | 600 | 每个分块请求的超时秒数 |
| num_ctx | 8192 | 上下文长度 |
| num_predict | 4096 | 每块最大输出 token 数 |
| temperature | 0 | 生成温度 |
| top_p | 0.9 | 采样参数 |
| seed | 42 | 随机种子，不保证跨设备确定性 |

```powershell
# 获取视频 → 基础清洗 → Qwen 清洗，不生成总结
python bili2md.py "https://www.bilibili.com/video/BV1YRnW6LESu" --llm-clean

# 已有文本 → 基础清洗 → 独立 Qwen 清洗 → 原有总结模型
python bili2md.py --input-text "subtitles/demo.txt" --llm-clean --cleaner-config "config/text_cleaner.example.json" --ollama-model "qwen2.5:7b"
```

清洗输出为 `demo.qwen.cleaned.txt`，基础副本为 `demo.cleaned.txt`，两者均独占创建，重名自动编号。
原文及元数据哈希不变；原有总结模型选择逻辑不变。总结共用入口只接受一个可选清洗回调，
不负责选择清洗模型；`text_cleaner.py` 不导入总结模块。

接口为 `clean_text(text, config) -> str`，输入应先经过 `text_cleaning.clean_text()`。
通过 Ollama `/api/generate` 请求非流式 JSON 正文，关闭 thinking。
按保守 UTF-8 字节预算分块，优先在句末/换行处分割，保留边界空白并按顺序合并。
预算为提示词、JSON 和输出留空间，但不是精确的模型 tokenizer；不支持跨块上下文纠错。
每块最多请求一次，失败即停止，不回退为“清洗成功”，所有块通过后才保存 Qwen 副本。
超时、连接失败、HTTP 错误、JSON 异常、截断及可疑输出均报错，CLI 返回 1，不继续总结。
基础文本与原文保留；失败时已有总结不变。日志只记录模型标签、处理块数、耗时和固定错误原因，
不记录提示词、原始服务错误正文或凭证。

输出检查会比较数字、英文标识符及中文否定/限定词的顺序与次数，并限制长度和字符改动幅度。
因此部分合理的术语改动也可能被拒绝；它不能证明语义等价，尤其不能可靠识别所有中文专有名词、
小幅语义反转或上下文遗漏。需对照保留的文件抽查，不能把通过规则检查视作事实核验。

离线回归：`python -X utf8 -B -m unittest test_text_cleaner -v`。

2.0 发布验证：108 项完整回归通过；本机 Qwen 短样例测试通过，用户普通视频测试通过。
已知待查问题：用户测试科技类视频时，LLM 清洗被阻止，具体原因尚未定位；本版本保留当前检查与失败策略。

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
| request_failed | 请求失败、结构异常或结果不确定时终止，不进入 ASR；仅暂时性网络故障重试目标请求一次 |

语言依据每条轨道的 lan，包括 ai-zh、zh 及中文变体。ai_type=0 不等于人工字幕；
顶层空 lan、asr_language 不是字幕存在依据。`--preferred-lang` 仅影响中文候选优先级。
首选语言比较忽略大小写，并将下划线统一为连字符；保留人工/AI 前缀的区别。
没有中文候选时不下载其他语言正文。

轨道 `type=0` 表示 CC，不受 AI 的 `ai_status=2` 要求限制；`type=1` 的 AI 轨道仍需状态为 2。
CC 正文可能不含 `lang`，此时依据目标 AID/CID 对应的 dm/view 轨道语言；正文若明确携带语言或视频标识，仍必须通过校验。
不识别的轨道类型、锁定、非就绪状态或正文异常不会被当作无字幕。

**不再请求播放器字幕接口或做该回退**：此前出现视频标识匹配但正文疑似串视频的问题。
正文校验语言、结构、非空文本、时间范围及可选视频标识；这些检查不能普遍证明语义归属，
主要依据仍是已验证稳定的 dm/view。中文缺少 URL 或正文异常不能按无字幕处理。

专属权限不足依据已验证组合 is_upower_exclusive=true、is_upower_play=false。
其他付费标记若无法确认已购权益，按结果不确定终止。不以通用付费提示、rights.pay=0
或单独的 is_upower_play=false 判定访问权限。

## 异常边界与请求重试

`bili_cli.parse_args()` 只处理 CLI 选项，`read_input()` 统一命令行与交互输入。
`bili2md.run_task(args)` 执行单次任务并向调用方抛出异常，`main()` 使用公共错误映射返回退出码。
最外层保留 `raise SystemExit(main())`：成功为 0，配置/任务失败为 1，CLI 语法错误为 2，手动取消为 130。
必要配置和初始化失败抛出 `ConfigurationError`；错误链接抛出 `InvalidVideoURL`，权限错误抛出 `AccessDenied`，
其他业务错误抛出 `RequestFailed` 或已有的 `SubtitleError` / `SummaryError`。调用业务函数不会退出进程。

输入 URL 只在提取入口解析一次；短链接的每个目标在访问前单独校验，并复用解析出的 BV 和整数分 P。
超时、连接中断、临时 DNS 故障及 HTTP 408/500/502/503/504 最多尝试两次（首次加一次重试）。
重试限定在失败的 JSON 请求或短链接当前跳转，不重新执行已经成功的视频信息、字幕列表或输入解析。
业务错误、JSON/正文结构错误、权限错误、证书错误及其他 HTTP 错误不自动重试；超过上限明确抛出异常。
短链接另有循环检查和最多 10 跳限制。模型生成及收费 API 不加入此重试流程。

## 统一 Cookie

复制 `config/settings.example.json` 为 `config/settings.json`，配置例如：
```json
{"cookies": "cookies.txt"}
```
将 Netscape Cookie 存放在 config 目录。路径优先级：
`--cookies` > 环境变量 `BILI2MD_COOKIES` > JSON 的 cookies。
相对路径以 JSON 所在目录解析，`--settings` 可指定其他 JSON 文件。

Cookie 在客户端初始化时加载一次，并过滤过期条目。没有文件时可匿名尝试字幕，取得有效中文不强制登录；
匿名或失效登录下的空列表不能确认无字幕。ASR 下载前检查内存中的有效 B 站 Cookie 和登录状态。
同次提取中，一分钟内的成功登录检查可复用；新提取或验证超时后重新检查。
正文请求不携带账号 Cookie，日志不输出完整签名 URL。yt-dlp 使用通过登录检查的内存 Cookie 导出的临时副本，
不重新读取或回写原 Cookie 文件；运行中替换原文件需重新运行才生效。
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

### 模块职责整理（1.3.3）

- `bili_cli.py`：参数、选项组合校验及交互输入；不包含认证或视频业务校验。
- `bili2md.py`：应用编排、Cookie 路径配置、标题输出命名及保存、简洁 CLI 入口。
- `bilibili_info.py`：视频地址/分 P 校验、Cookie 加载与登录复用、视频信息、字幕与请求级重试。
- `media_tools.py`：yt-dlp、FFmpeg、whisper.cpp 参数与执行、临时文件及结果校验。
- `exception.py`：公共异常类型和其状态枚举、HTTP/网络错误转换、云端脱敏、外部工具错误及退出码映射；不反向依赖业务模块。
- `ollama_summary.py` / `api_summary.py`：保留总结协议、响应校验和 Markdown 保存；公共错误转换委托给 `exception.py`。
- `cookie_debug.py`：仅记录检查/操作及次数，不承担 Cookie 校验实现。

调用方向为入口 → CLI / 应用编排 → 视频、媒体和总结模块 → 公共异常模块。
异常类型在原模块继续以导入名称可用，类身份及继承语义保持一致；未引入新框架。
URL/BV/分 P 和跳转白名单仍属于业务校验，供 CLI 与未来服务器共同使用，不搬入 CLI 造成反向依赖。

1.3.3 发布时 81 项回归通过。重构时基于原版本临时副本与工作区分别执行模拟日志：
中文字幕前后均 2 次检查；其他语言转 ASR、空列表转 ASR 前后均 4 次检查（各 1 次服务端登录）。
四项分别为文件存在、格式解析/过期过滤、有效 B 站域和服务端认证，语义不同，不机械合并。
文件只加载一次；一分钟内的成功登录结果复用，新提取或超时后重新检查，下载导出已验证的内存 jar。
本轮验证为离线样本、Mock 外部工具/模型和本机 HTTP 服务；没有重新联网提取 CC、实际转录或调用收费 API。

空字幕列表的登录检查直接传播认证异常，保留错误类型与原因；认证失败不会重试、误判为无字幕或启动 ASR。
补充 Cookie 过期过滤与登录请求携带凭证、登录失败退出码及请求次数回归。

```powershell
python -m unittest -q
```

本轮 Mock 覆盖五态、匿名中文、登录失效、异常重试、分 P/CID、正文异常、付费阻断、
ASR 调度、标题简介到 Markdown 及原文哈希保护。保留 ASR、Ollama 和 API 回归，
不在回归中调用真实转录或模型服务。

后续优化新增 Cookie 登录复用与过期、下载凭证快照、URL 边界、语言优先级、
空字幕元数据及单次读盘校验的回归测试。总结前先检查字幕非空，正文解码与哈希使用同一份文件字节。

2026-10-28 Code Review 修复后，71 项完整回归通过。新增连续字幕请求失败、正文/登录请求重试边界、
不可重试错误、短链接循环、配置/任务异常传播及 CC 多分 P 归属检查。
真实回归样本 [BV1YRnW6LESu](https://www.bilibili.com/video/BV1YRnW6LESu) 第 1 P：
CC 轨道 `type=0, lan=zh, ai_status=0`，正文无 `lang`；成功保存 118 段、985 字符，未运行 ASR。
输入解析、视频信息、字幕列表、CC 正文各执行一次。脱敏字段结构保存在 `test_fixtures/cc_BV1YRnW6LESu.json`，
离线回归使用占位正文，不保存 Cookie、签名参数或完整视频字幕。

2026-10-26 新模块真实查询四个已有样本：
- BV1zvnb6eEMP：有效中文字幕 5,312 字符，原文及元数据保存通过。
- BV1nLH46YEig：dm/view 只有 ai-en，不使用异常播放器返回的中文。
- BV1BgNb6oEnk：空字幕且登录有效。
- BV1nF4JzuEUU：订阅权限不足，停止后续访问及下载。

真实中文配合模拟生成器验证标题、简介到 Markdown 的流程；
本轮没有下载音频、运行 Whisper、调用 Ollama 或 DeepSeek。
此前 1.2 已完成用户授权的 DeepSeek 官方 API 单次实测，模型 deepseek-flash，
凭证环境变量名 DEEPSEEK_API_KEY，本轮未重复收费请求。
