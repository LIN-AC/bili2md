"""优先获取 B 站字幕，无字幕且登录有效时使用本地 whisper.cpp 转录。"""

import argparse
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from urllib.parse import parse_qs, urlparse

from ollama_summary import SummaryError, summarize_file
import api_summary


class SubtitleError(Exception):
    """可以直接展示给用户的提取错误。"""


class NoSubtitles(SubtitleError):
    """工具返回空字幕轨道，仍需验证登录状态才能启动 ASR。"""


class InvalidSubtitleCredential(SubtitleError):
    """登录 API 明确返回未登录。"""


def validate_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    hosts = {"bilibili.com", "www.bilibili.com", "m.bilibili.com",
             "b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in hosts:
        raise SubtitleError("链接错误：请输入有效的 B 站视频链接。")
    if parsed.hostname.endswith("bilibili.com"):
        if not re.fullmatch(r"/video/BV[1-9A-HJ-NP-Za-km-z]{10}/?", parsed.path):
            raise SubtitleError("链接错误：请使用包含 BV 号的视频链接。")
    page = parse_qs(parsed.query).get("p")
    if page and (not page[0].isdigit() or int(page[0]) < 1):
        raise SubtitleError("链接错误：分 P 参数 p 必须为正整数。")
    return value


def find_fetcher(explicit: str | None = None) -> str:
    if explicit:
        result = shutil.which(explicit)
        if result:
            return result
        raise SubtitleError(f"依赖错误：找不到指定工具 {explicit}")
    result = shutil.which("bilibili-subtitle-fetch")
    if result:
        return result
    # uv 的 Windows 默认安装目录有时未加入 PATH。
    fallback = Path.home() / ".local" / "bin" / "bilibili-subtitle-fetch.exe"
    if fallback.is_file():
        return str(fallback)
    raise SubtitleError("依赖错误：未找到本机已安装的 bilibili-subtitle-fetch。")


def fetch_subtitle(url: str, executable: str, config: Path | None = None,
                   timeout: float = 120, preferred_lang: str = "zh-CN",
                   subtitle_python: str | None = None) -> str:
    command = [subtitle_interpreter(executable, subtitle_python),
               str(Path(__file__).with_name("chinese_subtitle_fetch.py")),
               "fetch", url, "--no-asr", "--no-clipboard",
               "--output-format", "text", "--preferred-lang", preferred_lang]
    if config:
        command.extend(["--config", str(config.resolve())])
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               BILIBILI_ENABLE_ASR="false")
    last_error = ""
    for attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=timeout, env=env, shell=False)
        except FileNotFoundError as exc:
            raise SubtitleError("依赖错误：字幕工具不存在。") from exc
        except subprocess.TimeoutExpired:
            last_error = f"网络/HTTPS 错误：字幕请求超时（{timeout:g} 秒）。"
        else:
            text = result.stdout.strip()
            for line in result.stderr.splitlines():
                if line.startswith("字幕轨道语言："):
                    print(line, file=sys.stderr)
            if not result.returncode and text.startswith("Info: No subtitles"):
                raise NoSubtitles("无可用中文字幕：可能只有非中文字幕、没有字幕，或 Cookie 无效/过期。")
            if not result.returncode and text.startswith("Info: Subtitle file is empty"):
                last_error = "字幕提取错误：字幕文件为空。"
            elif result.returncode or text.startswith(("Error:", "Error fetching", "An unexpected error")):
                detail = (result.stderr.strip() or text or f"工具退出码 {result.returncode}")
                category = "网络/HTTPS 错误" if re.search(
                    r"HTTP|network|connect|timeout|412|403|SSL", detail, re.I
                ) else "字幕提取错误"
                last_error = f"{category}：{detail}"
            elif text:
                return text
            else:
                last_error = "字幕提取错误：工具未返回字幕文本。"
        if attempt == 0:
            print("提取失败，正在重试一次……", file=sys.stderr)
    raise SubtitleError(f"{last_error}\n已尝试 2 次，未保存字幕文件。")


def default_output(url: str) -> Path:
    parsed = urlparse(url)
    bvid = re.search(r"BV[1-9A-HJ-NP-Za-km-z]{10}", parsed.path)
    name = bvid.group() if bvid else re.sub(r"[^a-zA-Z0-9_-]", "_", parsed.path.strip("/"))[:80]
    page = parse_qs(parsed.query).get("p", ["1"])[0]
    return Path("subtitles") / f"{name or 'subtitle'}_p{page}.txt"


def run_external(command: list[str], stage: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                                shell=False)
    except subprocess.TimeoutExpired as exc:
        raise SubtitleError(f"{stage}失败：超过 {timeout:g} 秒。") from exc
    except OSError as exc:
        raise SubtitleError(f"{stage}失败：无法启动程序（{exc}）。") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        raise SubtitleError(f"{stage}失败（退出码 {result.returncode}）：{detail}")
    return result


def require_program(value: str, name: str) -> str:
    executable = shutil.which(value)
    if not executable:
        raise SubtitleError(f"依赖错误：找不到 {name}：{value}")
    return executable


def subtitle_interpreter(fetcher: str, subtitle_python: str | None = None) -> str:
    # uv/pip 的 Windows launcher 内嵌安装时使用的解释器路径。
    interpreter = subtitle_python
    if not interpreter:
        try:
            with zipfile.ZipFile(fetcher) as launcher:
                first_line = launcher.read("__main__.py").decode("utf-8").splitlines()[0]
            if first_line.startswith("#!"):
                interpreter = first_line[2:].strip().strip('"')
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            pass
    if not interpreter:
        interpreter = sys.executable
    return require_program(interpreter, "字幕工具 Python")


def verify_subtitle_login(fetcher: str, config: Path | None, timeout: float,
                          subtitle_python: str | None = None) -> None:
    command = [subtitle_interpreter(fetcher, subtitle_python),
               str(Path(__file__).with_name("check_subtitle_login.py"))]
    if config:
        command.append(str(config.resolve()))
    result = run_external(command, "字幕凭证登录检查（不启动 ASR）", timeout)
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise SubtitleError("字幕凭证登录检查返回异常，未启动 ASR。") from exc
    if payload.get("logged_in") is False:
        raise InvalidSubtitleCredential("字幕凭证未确认登录有效，未启动 ASR。")
    if payload.get("logged_in") is not True:
        raise SubtitleError("字幕凭证未确认登录有效：登录检查返回异常，未启动 ASR。")


def cookie_subtitle_config(cookies: Path, destination: Path) -> Path:
    """将用户提供的 Netscape 凭证临时交给现有字幕工具，不改写原配置。"""
    import http.cookiejar
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookies))
        jar.load(ignore_discard=True, ignore_expires=True)
        values = {c.name: c.value for c in jar if
                  c.domain.lstrip(".") == "bilibili.com" or c.domain.endswith(".bilibili.com")}
        if not values.get("SESSDATA"):
            raise ValueError("缺少 SESSDATA")
    except (OSError, ValueError) as exc:
        raise SubtitleError("字幕 Cookie 已失效，且 Netscape Cookie 文件无可用登录凭证，请更新 Cookie。") from exc
    mapping = {"sessdata": "SESSDATA", "bili_jct": "bili_jct", "buvid3": "buvid3",
               "dedeuserid": "DedeUserID"}
    content = "[credential]\n" + "\n".join(
        f"{key} = {json.dumps(values[name])}" for key, name in mapping.items() if values.get(name))
    destination.write_text(content + "\n", encoding="utf-8")
    return destination


def prepare_audio(audio: Path, folder: Path, ffmpeg: str, timeout: float) -> Path:
    if audio.suffix.lower() in {".flac", ".mp3", ".ogg", ".wav"}:
        print(f"音频格式 {audio.suffix} 可直接转录。", file=sys.stderr)
        return audio
    converted = folder / "converted.wav"
    print(f"将 {audio.suffix} 转为 16 kHz 单声道 PCM WAV……", file=sys.stderr)
    run_external([require_program(ffmpeg, "FFmpeg"), "-nostdin", "-hide_banner",
                  "-loglevel", "error", "-y", "-i", str(audio), "-vn", "-ar", "16000",
                  "-ac", "1", "-c:a", "pcm_s16le", str(converted)], "FFmpeg 音频转换", timeout)
    if not converted.is_file() or converted.stat().st_size <= 44:
        raise SubtitleError("FFmpeg 音频转换失败：未生成有效 WAV 文件。")
    return converted


def transcribe_audio(url: str, cookies: Path, yt_dlp: str, ffmpeg: str,
                     whisper: str, model: Path, timeout: float) -> str:
    downloader = require_program(yt_dlp, "yt-dlp")
    whisper = require_program(whisper, "whisper-cli")
    if not model.is_file() or model.stat().st_size == 0:
        raise SubtitleError(f"ASR 模型不存在或为空：{model}")
    if not cookies.is_file():
        raise SubtitleError(f"yt-dlp Cookie 文件不存在：{cookies}")
    # 只在内存中验证 Netscape 格式；Cookie 由 yt-dlp 自行读取。
    import http.cookiejar
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookies))
        jar.load(ignore_discard=True, ignore_expires=True)
        if not any(c.domain.lstrip(".") == "bilibili.com" or
                   c.domain.endswith(".bilibili.com") for c in jar):
            raise ValueError("没有 B 站 Cookie")
    except (OSError, ValueError, http.cookiejar.LoadError) as exc:
        raise SubtitleError("yt-dlp Cookie 文件无有效 B 站 Netscape Cookie，请重新导出。") from exc
    with tempfile.TemporaryDirectory(prefix="bili2md-asr-") as temp:
        folder = Path(temp).resolve()
        # yt-dlp 会回写 Cookie jar，使用临时副本保护用户原文件。
        cookie_copy = folder / "cookies.txt"
        shutil.copyfile(cookies, cookie_copy)
        manifest = folder / "download-path.txt"
        print("登录检查通过，下载音频……", file=sys.stderr)
        # 固定只选请求的分 P（默认第一 P），不下载整个合集。
        part = parse_qs(urlparse(url).query).get("p", ["1"])[0]
        run_external([downloader, "--ignore-config", "--no-progress", "--no-warnings",
                      "--cookies", str(cookie_copy), "--playlist-items", part,
                      "-f", "bestaudio", "-o", str(folder / "audio.%(ext)s"),
                      "--print-to-file", "after_move:filepath", str(manifest),
                      "--", url], "yt-dlp 音频下载", timeout)
        paths = manifest.read_text(encoding="utf-8").splitlines() if manifest.is_file() else []
        if len(paths) != 1:
            raise SubtitleError("yt-dlp 音频下载失败：未返回唯一的音频文件。")
        audio = Path(paths[0]).resolve()
        if audio.parent != folder or not audio.is_file() or not audio.stat().st_size:
            raise SubtitleError("yt-dlp 音频下载失败：输出文件无效。")
        audio = prepare_audio(audio, folder, ffmpeg, timeout)
        prefix = folder / "transcript"
        print("正在使用 whisper.cpp 中文转录……", file=sys.stderr)
        run_external([whisper, "-m", str(model.resolve()), "-f", str(audio),
                      "-l", "zh", "-otxt", "-of", str(prefix)], "whisper-cli 中文转录", timeout)
        transcript = prefix.with_suffix(".txt")
        text = transcript.read_text(encoding="utf-8-sig").strip() if transcript.is_file() else ""
        if not text:
            raise SubtitleError("whisper-cli 中文转录失败：没有生成非空转录文本。")
        return text


def positive_seconds(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("超时必须是有限正数")
    return number


def cookie_path(explicit: Path | None, settings: Path) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    if os.environ.get("BILI2MD_COOKIES"):
        return Path(os.environ["BILI2MD_COOKIES"]).expanduser().resolve()
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        value = data["cookies"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("cookies 必须是非空路径字符串")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SubtitleError(f"Cookie 路径配置错误：请检查 {settings} 的 cookies 字段，或使用 --cookies 指定。") from exc
    path = Path(value).expanduser()
    return (path if path.is_absolute() else settings.resolve().parent / path).resolve()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="B 站视频链接；省略则交互输入")
    parser.add_argument("-o", "--output", type=Path, help="输出 .txt 路径（已有文件会覆盖）")
    parser.add_argument("--config", type=Path, help="字幕工具的 cookie 配置文件路径")
    parser.add_argument("--fetcher", help="本机 bilibili-subtitle-fetch 可执行文件路径")
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
    parser.add_argument("--subtitle-python", help="字幕工具所属 Python，默认从 Windows 启动器识别")
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
    args = parser.parse_args()
    if args.input_text and (args.url or args.output):
        parser.error("--input-text 不能与视频链接或 --output 同时使用")
    if (args.input_text or args.summary_output) and not (args.ollama_model or args.api_model):
        parser.error("生成总结需要 --ollama-model 或 --api-model 指定模型名称")
    if args.api_base_url and args.api_model is None:
        parser.error("--api-base-url 需要同时指定 --api-model")
    if args.ollama_model is not None and not args.ollama_model.strip():
        parser.error("--ollama-model 不能为空")
    try:
        summary_model = args.ollama_model or args.api_model
        summary_url = args.ollama_url
        summary_timeout = args.ollama_timeout
        summary_options = {}
        summary_label = "Ollama"
        if args.api_model is not None:
            api_summary.api_config(args.api_model, args.api_base_url, args.api_key_env)
            summary_url, summary_timeout, summary_label = args.api_base_url, args.api_timeout, "API"
            summary_options["generator"] = partial(api_summary.generate_summary, api_key_env=args.api_key_env)
        if args.input_text:
            print(f"正在使用 {summary_label} 总结……", file=sys.stderr)
            summary = summarize_file(args.input_text, summary_model, args.summary_output,
                                     summary_url, summary_timeout, **summary_options)
            print(f"总结已保存：{summary.resolve()}")
            return 0
        url = validate_url(args.url or input("请输入 B 站视频链接："))
        if args.config and not args.config.is_file():
            raise SubtitleError("配置错误：指定的 cookie 配置文件不存在。")
        fetcher = find_fetcher(args.fetcher)
        try:
            text = fetch_subtitle(url, fetcher, args.config, args.timeout, args.preferred_lang,
                                  args.subtitle_python)
        except NoSubtitles:
            if args.no_asr:
                raise
            cookies = cookie_path(args.cookies, args.settings)
            try:
                verify_subtitle_login(fetcher, args.config, args.timeout, args.subtitle_python)
            except InvalidSubtitleCredential:
                if args.config:
                    raise  # 显式选择的配置失效时不替换用户选择。
                print("字幕工具 Cookie 已失效，使用 Netscape Cookie 重新检查字幕……", file=sys.stderr)
                with tempfile.TemporaryDirectory(prefix="bili2md-cookie-") as temp:
                    config = cookie_subtitle_config(cookies, Path(temp) / "config.toml")
                    verify_subtitle_login(fetcher, config, args.timeout, args.subtitle_python)
                    try:
                        text = fetch_subtitle(url, fetcher, config, args.timeout, args.preferred_lang,
                                              args.subtitle_python)
                    except NoSubtitles:
                        text = transcribe_audio(url, cookies, args.yt_dlp, args.ffmpeg,
                                                args.whisper_cli, args.model, args.asr_timeout)
            else:
                text = transcribe_audio(url, cookies, args.yt_dlp, args.ffmpeg,
                                        args.whisper_cli, args.model, args.asr_timeout)
        output = args.output or default_output(url)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"字幕已保存：{output.resolve()}（{len(text)} 字符）")
        if summary_model:
            print(f"正在使用 {summary_label} 总结……", file=sys.stderr)
            summary = summarize_file(output, summary_model, args.summary_output,
                                     summary_url, summary_timeout, video_url=url, **summary_options)
            print(f"总结已保存：{summary.resolve()}")
        return 0
    except (SubtitleError, SummaryError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
