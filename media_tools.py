"""yt-dlp、FFmpeg 和 whisper.cpp 调用；保留下载、转换和转录的局部清理。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from bilibili_info import VideoInfo, BilibiliClient
from cookie_debug import cookie_operation
from exception import SubtitleError, external_error, check_external_result


def run_external(command: list[str], stage: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                                shell=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        external_error(exc, stage, timeout)
    check_external_result(result, stage)
    return result


def require_program(value: str, name: str) -> str:
    executable = shutil.which(value)
    if not executable:
        raise SubtitleError(f"依赖错误：找不到 {name}：{value}")
    return executable


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


def transcribe_audio(info: VideoInfo, client: BilibiliClient, yt_dlp: str, ffmpeg: str,
                     whisper: str, model: Path, timeout: float) -> str:
    client.require_download_cookie()
    downloader = require_program(yt_dlp, "yt-dlp")
    whisper = require_program(whisper, "whisper-cli")
    if not model.is_file() or model.stat().st_size == 0:
        raise SubtitleError(f"ASR 模型不存在或为空：{model}")
    with tempfile.TemporaryDirectory(prefix="bili2md-asr-") as temp:
        folder = Path(temp).resolve()
        # 导出实际通过登录检查的 jar；yt-dlp 回写时不会影响用户原文件。
        cookie_copy = folder / "cookies.txt"
        cookie_operation("media_tools.transcribe_audio", "export_for_ytdlp")
        client.jar.save(str(cookie_copy), ignore_discard=True, ignore_expires=False)
        manifest = folder / "download-path.txt"
        print("登录检查通过，下载音频……", file=sys.stderr)
        # 固定只选请求的分 P（默认第一 P），不下载整个合集。
        cookie_operation("media_tools.transcribe_audio", "external_cookie_file_input")
        run_external([downloader, "--ignore-config", "--no-progress", "--no-warnings",
                      "--cookies", str(cookie_copy), "--playlist-items", str(info.page),
                      "-f", "bestaudio", "-o", str(folder / "audio.%(ext)s"),
                      "--print-to-file", "after_move:filepath", str(manifest),
                      "--", info.url], "yt-dlp 音频下载", timeout)
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
