"""直接获取 B 站信息和中文字幕；无中文字幕时使用本地 ASR，可选生成总结。"""
import argparse
from pathlib import Path


def positive_seconds(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("超时必须是有限正数")
    return number


def parse_args(argv=None):
    """仅负责 CLI 语法与选项组合检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="B 站视频链接；省略则交互输入")
    parser.add_argument("-o", "--output", type=Path, help="输出 .txt 路径（已有文件会覆盖）")
    parser.add_argument("--timeout", type=positive_seconds, default=120, help="每次提取超时秒数，默认 120")
    parser.add_argument("--preferred-lang", default="zh-CN", help="首选字幕语言，默认 zh-CN")
    parser.add_argument("--cookies", type=Path, help="Netscape Cookie 路径，覆盖环境变量和配置")
    parser.add_argument("--settings", type=Path, default=Path(__file__).parent / "config" / "settings.json",
                        help="项目路径配置文件，cookies 相对该文件所在目录解析")
    parser.add_argument("--yt-dlp", default="yt-dlp", help="yt-dlp 路径")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg 路径")
    parser.add_argument("--whisper-cli", default=r"D:\whisper.cpp\build-vs\bin\Release\whisper-cli.exe")
    parser.add_argument("--model", type=Path, default=Path(r"D:\whisper.cpp\ggml-large-v3-turbo.bin"))
    parser.add_argument("--asr-timeout", type=positive_seconds, default=3600,
                        help="下载、转换、转录各阶段超时秒数，默认 3600")
    parser.add_argument("--no-asr", action="store_true", help="关闭本项目 ASR 兜底，保持仅提取字幕")
    parser.add_argument("--input-text", type=Path, help="读取已有 UTF-8 字幕/ASR 文本，只生成总结")
    summaries = parser.add_mutually_exclusive_group()
    summaries.add_argument("--ollama-model", help="启用总结，并指定本机 Ollama 模型名")
    summaries.add_argument("--api-model", help="启用云端 API 总结，指定兼容接口的模型名")
    parser.add_argument("--api-base-url", help="OpenAI-compatible API 根地址，通常包含 /v1")
    parser.add_argument("--api-key-env", default="BILI2MD_API_KEY", help="保存 API Key 的环境变量名称")
    parser.add_argument("--api-timeout", type=positive_seconds, default=600, help="API 总结请求超时秒数")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="本机 Ollama 服务地址")
    parser.add_argument("--ollama-timeout", type=positive_seconds, default=600, help="总结请求超时秒数")
    parser.add_argument("--summary-output", type=Path, help="总结 Markdown 路径，默认 summaries/<字幕文件名>.md")
    args = parser.parse_args(argv)
    if args.input_text and (args.url or args.output):
        parser.error("--input-text 不能与视频链接或 --output 同时使用")
    if (args.input_text or args.summary_output) and not (args.ollama_model or args.api_model):
        parser.error("生成总结需要 --ollama-model 或 --api-model 指定模型名称")
    if args.api_base_url and args.api_model is None:
        parser.error("--api-base-url 需要同时指定 --api-model")
    if args.ollama_model is not None and not args.ollama_model.strip():
        parser.error("--ollama-model 不能为空")
    return args


def read_input(argv=None):
    """命令行和交互输入合流；视频真实性、地址和分 P 校验由业务入口执行一次。"""
    args = parse_args(argv)
    if not args.input_text and not args.url:
        args.url = input("请输入 B 站视频链接：")
    return args
