"""自然语言时间解析：一句中文 → Reminder 需要的结构化字段。

纯函数：不读时钟、不读库、不调网络；`now` 由调用方注入。
只用标准库 `re` + `datetime`。

老板定的截止点语义：上午→12:00、下午→18:00、晚上/今天→22:30。
一句话里多个时间词，日期取全句最后一个；**时刻只认句首时间前缀**里的最后一个，
正文中间提到的时间既不剥进标题、也不参与时刻解析。
标题只抠**句首连续的时间前缀**（正文中间提到的时间不动），
「提前X」是提醒修饰语，出现在哪都抠；时间词之间隔的空格一律容错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

LIST_DEFAULT = "GTD"
LIST_WAITING = "Waiting"
DEFAULT_AT = time(22, 30)          # 「今天」没别的时刻词 → 当天截止 22:30

_CUTOFF = {
    "上午": time(12, 0),
    "中午": time(12, 0),
    "中午前": time(12, 0),
    "下午": time(18, 0),
    "晚上": time(22, 30),
    "傍晚": time(19, 0),
}
# 「今早/明早/今夜/明晚」既是日期也是时段：值 = (日偏移, 时段词)
_COMPOUND_PERIOD = {
    "今早": (0, "早上"), "今晨": (0, "早上"), "明早": (1, "早上"), "明晨": (1, "早上"),
    "今晚": (0, "晚上"), "今夜": (0, "晚上"), "明晚": (1, "晚上"), "明夜": (1, "晚上"),
}
_CLOCK_QUALS = ("凌晨", "早上", "早晨", "上午", "中午", "下午", "晚上", "傍晚", "夜里")
_QUAL_ALT = "|".join(list(_COMPOUND_PERIOD) + list(_CLOCK_QUALS))
_PERIOD_ALT = "|".join(list(_COMPOUND_PERIOD)
                       + ["中午前", "中午", "上午", "下午", "晚上", "傍晚"])
_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_REL_DAYS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
_PM_QUALIFIERS = {"下午", "傍晚", "晚上", "夜里"}

_TOKEN = re.compile(
    r"""
    (?:在\s*)?
    (?:
        (?P<adv>提前\s*(?P<a_num>\d{1,3}|半|[一二两三四五六七八九十]{1,3})\s*(?:个)?\s*
                 (?P<a_unit>小时|钟头|分钟|分))
      | (?P<rel>大后天|后天|明天|明日|今天|今日)
      | (?P<week>(?:下下|下|本|这)?周[一二三四五六日天])
      | (?P<md>(?P<mon>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号])
      | (?P<md2>(?P<mon2>\d{1,2})[-/](?P<day2>\d{1,2})(?:[日号])?)
      | (?P<dom>\d{1,2})\s*号
      | (?P<clock>(?:(?P<qual>QUAL_ALT)\s*)?
                 (?P<hh>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*
                 (?:(?P<csep>[:：])(?P<mi>\d{2})|点(?:\s*(?:半|(?P<mi2>\d{1,2})\s*分?))?)
                 (?:之前|以前|前)?)
      | (?P<period>PERIOD_ALT)(?:之前|以前|前)?
    )
    """.replace("QUAL_ALT", _QUAL_ALT).replace("PERIOD_ALT", _PERIOD_ALT),
    re.VERBOSE,
)
_WAITING = re.compile(r"等\s*(?:对方|回复|人)")
_FILLERS = ("提醒我一下", "提醒一下", "提醒我", "记得", "别忘了", "帮我",
            "麻烦帮我", "麻烦你", "麻烦")
_PUNCT = " \t\u3000，。、；：！？,.;:!?…·—～~「」『』“”\"'()（）【】[]"


@dataclass(frozen=True)
class Parsed:
    title: str                          # 去掉时间表达后的任务标题
    due: date | None = None             # 到期日
    at: time | None = None              # 精确时刻（None = 当天不定时）
    advance_minutes: int | None = None  # 提前量（可选）
    matched: str = ""                   # 命中的时间原文，便于回显与调试
    list: str = LIST_DEFAULT            # GTD / Waiting


@dataclass(frozen=True)
class _Hit:
    kind: str
    start: int
    end: int
    text: str
    data: dict


def _cn_number(raw: str) -> int | None:
    """中文/阿拉伯数字 → int；解析不了给 None。"""
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        head, _, tail = raw.partition("十")
        if head and head not in _DIGITS:
            return None
        if tail and tail not in _DIGITS:
            return None
        return (_DIGITS[head] if head else 1) * 10 + (_DIGITS[tail] if tail else 0)
    return _DIGITS.get(raw) if len(raw) == 1 else None


def _advance_minutes(num: str, unit: str) -> int | None:
    if num == "半":
        return 30          # 「半小时」= 30 分钟，不是 0.5 小时×60
    value = _cn_number(num)
    if value is None:
        return None
    return value * 60 if unit in ("小时", "钟头") else value


def _clock_time(hour: int | None, minute: int, qual: str | None) -> time | None:
    """时刻归一：默认按下午理解，除非带「凌晨/早上/上午」。"""
    if hour is None or minute > 59:
        return None
    if qual in _PM_QUALIFIERS and hour < 12:
        hour += 12
    elif qual == "中午" and 0 < hour < 12:
        hour += 12
    elif qual is None and hour < 12 and hour != 0:
        hour += 12
    if hour > 23:
        return None
    return time(hour, minute)


def _clock_minute(hit: re.Match, text: str) -> int:
    if hit.group("mi"):
        return int(hit.group("mi"))
    if hit.group("mi2"):
        return int(hit.group("mi2"))
    return 30 if "半" in text else 0


def _split_qual(qual: str | None) -> tuple[str | None, int | None]:
    """「今早/今晚」这类词同时给日偏移和时段限定，拆成 (时段词, 日偏移)。"""
    if qual in _COMPOUND_PERIOD:
        offset, base = _COMPOUND_PERIOD[qual]
        return base, offset
    return qual, None


def _make_hit(match: re.Match) -> _Hit | None:
    g = match.groupdict()
    raw = match.group(0)
    if g.get("adv"):
        minutes = _advance_minutes(g["a_num"], g["a_unit"])
        kind, data = "advance", {"minutes": minutes}
    elif g.get("rel"):
        kind, data = "date", {"rel": g["rel"]}
    elif g.get("week"):
        kind, data = "date", {"week": g["week"]}
    elif g.get("md"):
        kind, data = "date", {"month": int(g["mon"]), "day": int(g["day"])}
    elif g.get("md2"):
        kind, data = "date", {"month": int(g["mon2"]), "day": int(g["day2"])}
    elif g.get("dom"):
        kind, data = "date", {"dom": int(g["dom"])}
    elif g.get("clock"):
        qual, offset = _split_qual(g.get("qual"))
        hour = _cn_number(g["hh"])
        kind = "at"
        data = {"value": _clock_time(hour, _clock_minute(match, raw), qual),
                "day_offset": offset}
    elif g.get("period"):
        offset, base = _COMPOUND_PERIOD.get(g["period"], (None, g["period"]))
        kind, data = "at", {"value": _CUTOFF.get(base), "day_offset": offset}
    else:
        return None
    return _Hit(kind, match.start(), match.end(), raw, data)


def _scan(text: str) -> list[_Hit]:
    hits = [_make_hit(match) for match in _TOKEN.finditer(text)]
    return [hit for hit in hits if hit is not None]


def _week_date(expr: str, today: date) -> date | None:
    found = re.fullmatch(r"(?P<pre>下下|下|本|这)?周(?P<day>[一二三四五六日天])", expr)
    if found is None:
        return None
    index, pre = _WEEKDAY[found.group("day")], found.group("pre")
    monday = today - timedelta(days=today.weekday())
    if pre == "下下":
        return monday + timedelta(days=14 + index)
    if pre == "下":
        return monday + timedelta(days=7 + index)
    candidate = monday + timedelta(days=index)
    return candidate if candidate >= today else candidate + timedelta(days=7)


def _month_day(month: int, day: int, today: date) -> date | None:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def _day_of_month(day: int, today: date) -> date | None:
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= today:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _date_from_data(data: dict, today: date) -> date | None:
    if "rel" in data:
        return today + timedelta(days=_REL_DAYS[data["rel"]])
    if "week" in data:
        return _week_date(data["week"], today)
    if "month" in data:
        return _month_day(data["month"], data["day"], today)
    if "dom" in data:
        return _day_of_month(data["dom"], today)
    return None


def _resolve_due(hits: list[_Hit], today: date) -> date | None:
    """日期取最后一个能解析的；「今早/明晚」这类词自带日偏移也算日期。"""
    due = None
    for hit in hits:
        offset = hit.data.get("day_offset")
        if offset is not None:
            due = today + timedelta(days=offset)
        elif hit.kind == "date":
            resolved = _date_from_data(hit.data, today)
            if resolved is not None:
                due = resolved
    return due


def _has_date(hits: list[_Hit]) -> bool:
    """前缀里是否给了日期（含「今早/明晚」这类自带日偏移的复合词）。"""
    return any(hit.kind == "date" or hit.data.get("day_offset") is not None
               for hit in hits)


def _resolve_at(hits: list[_Hit], due: date | None) -> time | None:
    """时刻只认**句首时间前缀**里的最后一个；只给日期没给时刻 → 当天 22:30 兜底。

    正文里提到的时间（「…顺便看看 3 点的会」）不参与，否则前面给了截止点
    （中午前=12:00）却会被正文的钟点覆盖掉。兜底只在日期真的解析出来时给，
    否则「13月45日交材料」这种坏日期会从「进收件箱」变成「排到今天 22:30」。
    """
    values = [hit.data.get("value") for hit in hits
              if hit.kind == "at" and hit.data.get("value") is not None]
    if values:
        return values[-1]
    if due is not None and _has_date(hits):
        return DEFAULT_AT
    return None


def _resolve_advance(hits: list[_Hit]) -> int | None:
    values = [hit.data.get("minutes") for hit in hits
              if hit.kind == "advance" and hit.data.get("minutes") is not None]
    return values[-1] if values else None


def _cut_title(text: str, spans: list[tuple[int, int]]) -> str:
    pieces, cursor = [], 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return _clean_title("".join(pieces))


def _skippable(gap: str) -> bool:
    """两个时间词之间只隔空格/标点 → 视为同一段时间前缀。"""
    return all(ch in _PUNCT or ch.isspace() for ch in gap)


def _prefix_clear(text: str, upto: int) -> bool:
    """句首（扣掉填充词与标点）就是时间词 → 可以开始剥。"""
    head = text[:upto]
    for filler in _FILLERS:
        head = head.replace(filler, " ")
    return _skippable(head)


def _strip_hits(text: str, hits: list[_Hit]) -> list[_Hit]:
    """标题里要抠掉的时间词。

    只抠**句首连续的时间前缀**（后面紧跟的仍是时间词就继续吃，中间只允许空格/标点），
    正文中间提到的时间（如「…顺便看看 3 点的会」）不抠，否则标题会缺字。
    「提前半小时」是提醒修饰语，出现在哪都抠（旧用例要求）。
    """
    kept: list[_Hit] = []
    cursor = 0
    started = False
    for hit in hits:
        if hit.kind == "advance" or hit.start < cursor:
            continue
        gap = text[cursor:hit.start]
        ready = _skippable(gap) if started else _prefix_clear(text, hit.start)
        if not ready:
            break
        kept.append(hit)
        cursor = hit.end
        started = True
    return kept + [hit for hit in hits if hit.kind == "advance"]


def _echo(text: str, hits: list[_Hit]) -> str:
    """命中的时间原文：时间词之间只隔空格/标点就原样保留，隔了正文用空格分开。"""
    if not hits:
        return ""
    pieces = [hits[0].text]
    for prev, hit in zip(hits, hits[1:]):
        gap = text[prev.end:hit.start]
        if not gap:
            sep = ""
        else:
            sep = gap if _skippable(gap) else " "
        pieces.append(sep + hit.text)
    return "".join(pieces)


def _clean_title(raw: str) -> str:
    title = re.sub(r"[\s\u3000]+", " ", raw).strip()
    stripping = True
    while stripping:
        stripping = False
        for filler in _FILLERS:
            if title.startswith(filler) and len(title) > len(filler):
                title = title[len(filler):].strip()
                stripping = True
            elif title.endswith(filler) and len(title) > len(filler):
                title = title[:-len(filler)].strip()
                stripping = True
    title = title.strip(_PUNCT)
    return "" if title in ("一下", "提醒我", "提醒一下") else title


def _resolve_list(text: str) -> tuple[str, list[tuple[int, int]]]:
    found = _WAITING.search(text)
    if found is None:
        return LIST_DEFAULT, []
    return LIST_WAITING, [(found.start(), found.end())]


def parse(text: str, *, now: datetime) -> Parsed:
    """把一句中文解析成 Reminder 需要的字段；解析不出来不算错。"""
    hits = _scan(text)
    list_name, extra_spans = _resolve_list(text)
    cut = _strip_hits(text, hits)
    due = _resolve_due(hits, now.date())
    return Parsed(
        title=_cut_title(text, [(hit.start, hit.end) for hit in cut] + extra_spans),
        due=due,
        at=_resolve_at(cut, due),
        advance_minutes=_resolve_advance(hits),
        matched=_echo(text, hits),
        list=list_name,
    )
