"""直接获取 B 站信息和中文字幕；无中文字幕时使用本地 ASR，可选生成总结。"""

from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from ollama_summary import summarize_file, format_source
from exception import SubtitleError, SummaryError, TASK_ERRORS, cli_error
from bili_cli import parse_args, read_input
from media_tools import transcribe_audio
import api_summary
from bilibili_info import BilibiliClient, BilibiliError, SubtitleState, VideoInfo, ConfigurationError
from cookie_debug import cookie_operation, reset_cookie_debug


def default_output(info: VideoInfo) -> Path:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', info.title).strip(' .')[:80].rstrip(' .')
    if not title.strip('_. '):
        title = f"{info.bvid}_p{info.page}"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", title.split('.')[0], re.I):
        title = '_' + title
    return Path("subtitles") / f"{title}.txt"


def save_text(info: VideoInfo, raw_text: bytes, explicit: Path | None = None) -> Path:
    """默认标题名用独占创建防止重名覆盖；显式 -o 保留原覆盖约定。"""
    raw_text = format_source(info.title, info.desc, raw_text.decode('utf-8')).encode('utf-8')
    base = explicit or default_output(info)
    base.parent.mkdir(parents=True, exist_ok=True)
    if explicit is not None:
        base.write_bytes(raw_text)
        return base
    number = 1
    while True:
        output = base if number == 1 else base.with_name(f"{base.stem} ({number}).txt")
        number += 1
        # 同名元数据或默认总结也可能属于先前任务。
        if output.with_suffix('.meta.json').exists() or (Path('summaries') / (output.stem + '.md')).exists():
            continue
        try:
            handle = output.open('xb')
        except FileExistsError:
            continue
        with handle:
            handle.write(raw_text)
        return output


def cookie_path(explicit: Path | None, settings: Path, optional: bool = False) -> Path | None:
    cookie_operation("bili2md.cookie_path", "resolve_config_path")
    if explicit:
        return explicit.expanduser().resolve()
    if os.environ.get("BILI2MD_COOKIES"):
        return Path(os.environ["BILI2MD_COOKIES"]).expanduser().resolve()
    if optional and not settings.exists():
        return None
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        value = data["cookies"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("cookies 必须是非空路径字符串")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError(f"Cookie 路径配置错误：请检查 {settings} 的 cookies 字段，或使用 --cookies 指定。") from exc
    path = Path(value).expanduser()
    return (path if path.is_absolute() else settings.resolve().parent / path).resolve()


def run_task(args) -> int:
    """执行一次任务；配置或业务错误向调用方抛出，不读取交互输入或退出进程。"""
    summary_model = args.ollama_model or args.api_model
    summary_url = args.ollama_url
    summary_timeout = args.ollama_timeout
    summary_options = {}
    summary_label = "Ollama"
    if args.api_model is not None:
        try:
            api_summary.api_config(args.api_model, args.api_base_url, args.api_key_env)
        except SummaryError as exc:
            raise ConfigurationError(str(exc)) from None
        summary_url, summary_timeout, summary_label = args.api_base_url, args.api_timeout, "API"
        summary_options["generator"] = partial(api_summary.generate_summary, api_key_env=args.api_key_env)
    if args.input_text:
        print(f"正在使用 {summary_label} 总结……", file=sys.stderr)
        summary = summarize_file(args.input_text, summary_model, args.summary_output,
                                 summary_url, summary_timeout, **summary_options)
        print(f"总结已保存：{summary.resolve()}")
        return 0
    url = args.url
    cookies = cookie_path(args.cookies, args.settings, optional=True)
    client = BilibiliClient(cookies, args.timeout)
    result = client.fetch(url, args.preferred_lang)
    metadata = result.info.metadata()
    url = result.info.url
    print(f"视频标题：{result.info.title}")
    print(f"字幕状态：{result.state.value}")
    if result.state is SubtitleState.CHINESE:
        text = result.text
    elif result.state in {SubtitleState.OTHER_LANGUAGES, SubtitleState.NO_SUBTITLES}:
        if args.no_asr:
            raise SubtitleError("无可用中文字幕，已按 --no-asr 停止。")
        text = transcribe_audio(result.info, client, args.yt_dlp, args.ffmpeg,
                                args.whisper_cli, args.model, args.asr_timeout)
    else:
        raise SubtitleError("无法确认字幕状态，未启动 ASR。")
    raw_text = (text + "\n").encode("utf-8")
    output = save_text(result.info, raw_text, args.output)
    metadata["text_source"] = "subtitle" if result.state is SubtitleState.CHINESE else "asr"
    metadata["subtitle_state"] = result.state.value
    metadata["text_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"字幕已保存：{output.resolve()}（{len(text)} 字符）")
    if summary_model:
        print(f"正在使用 {summary_label} 总结……", file=sys.stderr)
        summary = summarize_file(output, summary_model, args.summary_output,
                                 summary_url, summary_timeout, video_url=url, metadata=metadata, **summary_options)
        print(f"总结已保存：{summary.resolve()}")
    return 0


def main(argv=None) -> int:
    reset_cookie_debug()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        try:
            args = read_input(argv)
        except SystemExit as exc:
            return exc.code
        return run_task(args)
    except (*TASK_ERRORS, KeyboardInterrupt) as exc:
        message, code = cli_error(exc)
        print(message, file=sys.stderr)
        return code



if __name__ == "__main__":
    raise SystemExit(main())
