"""在字幕工具自己的环境中运行，仅限制候选字幕语言，不重写提取逻辑。"""
import sys


def is_chinese(language: str) -> bool:
    language = language.lower().replace("_", "-")
    if language.startswith("ai-"):
        language = language[3:]
    return language.split("-")[0] in {"zh", "cmn", "yue"}


def install_chinese_filter(server):
    original = server.fetch_available_subtitles

    async def chinese_tracks(*args, **kwargs):
        tracks = await original(*args, **kwargs)
        # 请求异常直接向上传播；不把失败转换为空列表。
        languages = [str(track.get("lan", "")) for track in tracks]
        print("字幕轨道语言：" + (", ".join(languages) or "无"), file=sys.stderr)
        return [track for track in tracks if is_chinese(str(track.get("lan", "")))]

    server.fetch_available_subtitles = chinese_tracks


if __name__ == "__main__":
    from bilibili_subtitle_fetch import server

    install_chinese_filter(server)
    server.main()
