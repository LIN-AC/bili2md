"""保守的纯文本清洗与独立副本保存；不调用模型、不猜测语义。"""
from collections.abc import Mapping
from pathlib import Path
import re


def clean_text(text: str, source: str = "unknown", *, terms: Mapping[str, str] | None = None) -> str:
    """字幕去空片段，ASR/未知来源保留段落；重复及非空行顺序不变。

    terms 仅由调用方明确提供，区分大小写、按 Unicode 词边界匹配，
    一次替换、不级联；不匹配嵌在中文或其他词内部的子串。
    """
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n\n")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        if line:
            lines.append(line)
        elif source != "subtitle" and lines and lines[-1]:
            lines.append("")
    result = "\n".join(lines).rstrip("\n")
    if terms:
        for old, new in terms.items():
            if (not isinstance(old, str) or not isinstance(new, str) or
                    not old.strip() or not new.strip() or
                    any(c in old + new for c in "\r\n\u2028\u2029")):
                raise ValueError("术语映射必须为非空的单行字符串。")
        pattern = r"(?<!\w)(?:" + "|".join(re.escape(key) for key in sorted(terms, key=len, reverse=True)) + r")(?!\w)"
        result = re.sub(pattern, lambda match: terms[match[0]], result)
    return result


def save_cleaned_text(source: Path, text: str, *, reserved_paths=()) -> Path:
    """保存 UTF-8 清洗副本；独占创建，保留原文及先前的清洗结果。"""
    reserved = {path.resolve() for path in (source, *reserved_paths)}
    number = 1
    while True:
        suffix = "" if number == 1 else f" ({number})"
        target = source.with_name(f"{source.stem}.cleaned{suffix}.txt")
        number += 1
        if target.resolve() in reserved:
            continue
        try:
            handle = target.open("xb")
        except FileExistsError:
            continue
        with handle:
            handle.write((text + "\n" if text else "").encode("utf-8"))
        return target
