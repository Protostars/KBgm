# -*- coding: utf-8 -*-
"""
从 bangumi Archive 离线数据 (subject.jsonlines / episode.jsonlines)
提取 KikoPlay 所需的五种 TSV 文件，保持现有导入格式兼容。

依赖：bgm-tv-wiki  (pip install bgm-tv-wiki)

用法：
    python extract_local.py <dump_dir> [output_dir] [--since YYYY-MM-DD]

    dump_dir   : 解压后的 Archive 目录（含 subject.jsonlines / episode.jsonlines）
    output_dir : 输出目录，默认 <dump_dir>/../bgm_extracted
    --since    : 仅提取 air_date >= 该日期的动画（含其全部 episode），不传则全量提取。
                 用于增量导入：每周只提取本季度新番，跳过已导入的老番。

输出文件（TSV，制表符分隔）：
    anime_profile.tsv  source_id, name, name_lang, air_date, desc, url, script_data, staff, ts, ep_count, aliases
    anime_info.tsv      nameid, name, ts
    anime_source.tsv    nameid, src_info, ts
    anime_tag.tsv       source_id, tag_name
    pool_info.tsv       poolid, nameid, ts, ep_type, ep_index, ep_name
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import sys

from bgm_tv_wiki import try_parse


# ---------------------------------------------------------------------------
# 纯函数副本（同步自 api/util.py、api/api_anime_profile_ev.py，避免 import flask）
# ---------------------------------------------------------------------------

def _clean_text(value, max_len=None):
    value = (value or "").strip()
    if max_len is not None and len(value) > max_len:
        return value[:max_len]
    return value


LANG_UNKNOWN = 0
LANG_ZH = 1
LANG_JA = 2
LANG_EN = 3

_EP_NAME_CJK_PATTERN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_EP_NAME_KANA_PATTERN = re.compile(r'[\u3040-\u30ff\uff66-\uff9f]')
_EP_NAME_JAPANESE_EP_MARKER_PATTERN = re.compile(r'(?:\u7b2c\s*)?\d+(?:\.\d+)?\s*\u8a71')


def _has_cjk(value):
    return bool(_EP_NAME_CJK_PATTERN.search(value or ""))


def _looks_english(value):
    return bool(value) and bool(re.search(r"[A-Za-z]", value)) and not _has_cjk(value)


def _detect_name_lang(value):
    if _has_cjk(value):
        return LANG_ZH
    if _looks_english(value):
        return LANG_EN
    if value:
        return LANG_JA
    return LANG_UNKNOWN


def get_poolid(anime, ep_type, ep_index):
    # 同步自 api/util.py:get_poolid
    data = "{} {}.{}".format(anime, ep_type, ep_index if not ep_index.is_integer() else int(ep_index))
    md5_obj = hashlib.md5()
    md5_obj.update(data.encode('utf-8'))
    return md5_obj.hexdigest()


def get_nameid(anime):
    # 同步自 api/util.py:get_nameid
    data = "{}".format(anime)
    md5_obj = hashlib.md5()
    md5_obj.update(data.encode('utf-8'))
    return md5_obj.hexdigest()


def _json_dump(value):
    # 同步自 api/api_anime_profile_ev.py:_json_dump
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

_AIR_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_valid_air_date(value):
    value = _clean_text(value)
    if not _AIR_DATE_RE.fullmatch(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# staff 解析（用 bgm-tv-wiki 解析 infobox）
# 从「官方网站」字段开始，到 infobox 结束的所有字段都算 staff 信息
# ---------------------------------------------------------------------------

# 平凡的 staff 角色名，这些出现在「官方网站」之后但通常无意义，不收录
_STAFF_NOISE_KEYS = {
    "在线播放平台", "播放电视台", "其他电视台", "播放结束",
    "链接", "其他", "Copyright",
}


def _parse_staff(infobox_str):
    """从 infobox 字符串解析 staff 信息，返回 {role: name} dict。"""
    return _parse_infobox(infobox_str)[1]


def _parse_infobox(infobox_str):
    """解析 infobox，返回 (ep_count, staff_dict, aliases)。

    ep_count：从「话数」字段提取的整数，提取失败为 0。
    staff：从「官方网站」字段开始（含本字段）到结束的所有字段。
    aliases：从「别名」字段提取的别名列表（去重、去空）。
    infobox 只解析一次，复用结果。
    """
    if not infobox_str:
        return 0, {}, []
    wiki = try_parse(infobox_str)

    ep_count = 0
    staff = {}
    aliases = []
    seen_alias = set()
    collecting = False
    for field in wiki.fields:
        if field.key == "话数":
            ep_count = _parse_ep_count(field.value)
        if field.key == "别名":
            value = field.value
            items = value if isinstance(value, tuple) else ((value,) if value else ())
            for item in items:
                alias_text = item.value if hasattr(item, "value") else str(item)
                alias = _clean_text(alias_text, 256)
                if alias and alias not in seen_alias:
                    seen_alias.add(alias)
                    aliases.append(alias)
        if field.key == "官方网站":
            collecting = True  # 从「官方网站」开始（含本字段）都算 staff
        if not collecting:
            continue
        role = _clean_text(field.key, 128)
        if not role or role in _STAFF_NOISE_KEYS:
            continue
        value = field.value
        if isinstance(value, tuple):
            # 数组型字段，取各 item.value 用「、」连接
            name = "、".join(item.value for item in value if item.value)
        elif value:
            name = value
        else:
            continue
        name = _clean_text(name, 256)
        if not name:
            continue
        staff[role] = name
    return ep_count, staff, aliases


def _parse_ep_count(value):
    """从 infobox 话数字段提取集数，取第一个数字，失败为 0。"""
    if not value:
        return 0
    if isinstance(value, tuple):
        # 数组型，取第一个非空 item
        for item in value:
            n = _parse_ep_count(item.value)
            if n:
                return n
        return 0
    match = re.search(r"\d+", str(value))
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# AnimeTag 过滤：过滤全英文或全数字的平凡标签（如 TV、OVA、WEB、R18）
# ---------------------------------------------------------------------------

_TRIVIAL_TAG_RE = re.compile(r"^[A-Za-z0-9]+$")


def _is_trivial_tag(tag):
    return bool(_TRIVIAL_TAG_RE.fullmatch(tag or ""))


# ---------------------------------------------------------------------------
# episode type 映射：bangumi -> KikoPlay EpType
# ---------------------------------------------------------------------------

# EpType（同步自 pb/service.proto）
EP_TYPE_EP = 1
EP_TYPE_SP = 2

_BGM_EP_TYPE_MAP = {
    0: EP_TYPE_EP,   # 正篇
    1: EP_TYPE_SP,   # 特别篇
}


# ---------------------------------------------------------------------------
# CSV 输出辅助
# ---------------------------------------------------------------------------

class TsvWriter:
    """制表符分隔的 CSV writer，处理字段内特殊字符转义。"""

    def __init__(self, path):
        self._f = open(path, "w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._f, delimiter="\t", quoting=csv.QUOTE_MINIMAL,
                                  escapechar="\\")

    def writerow(self, row):
        self._writer.writerow(row)

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="从 bangumi Archive 离线数据提取 KikoPlay 服务端所需数据")
    parser.add_argument("dump_dir", help="解压后的 Archive 目录（含 subject.jsonlines / episode.jsonlines）")
    parser.add_argument("output_dir", nargs="?", default=None,
                        help="输出目录，默认 <dump_dir>/../bgm_extracted")
    parser.add_argument("--since", default=None,
                        help="仅提取 air_date >= 该日期(YYYY-MM-DD)的动画，用于增量导入。不传则全量提取。")
    args = parser.parse_args()

    dump_dir = args.dump_dir
    output_dir = args.output_dir or os.path.join(
        os.path.dirname(os.path.abspath(dump_dir)), "bgm_extracted")
    since = args.since
    if since and not _is_valid_air_date(since):
        sys.stderr.write("ERROR: --since 日期格式不合法，应为 YYYY-MM-DD: %s\n" % since)
        sys.exit(1)

    subject_path = os.path.join(dump_dir, "subject.jsonlines")
    episode_path = os.path.join(dump_dir, "episode.jsonlines")
    for p in (subject_path, episode_path):
        if not os.path.isfile(p):
            sys.stderr.write("ERROR: 文件不存在: %s\n" % p)
            sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    ts = int(datetime.datetime.now().timestamp() * 1000)
    sys.stdout.write("导入时间戳 ts=%d\n输出目录: %s\n" % (ts, output_dir))
    if since:
        sys.stdout.write("增量模式：仅提取 air_date >= %s 的动画\n" % since)

    subject_count = 0      # 扫描的 type=2 数
    profile_count = 0      # 实际写入 profile 的数（通过 name/air_date 校验）
    tag_count = 0
    skip_no_name = 0
    skip_no_date = 0
    skip_since = 0         # 因 air_date < since 跳过
    # bgm_id -> (name, nameid)
    name_index = {}

    # ---- 第一遍：扫描 subject.jsonlines ----
    with TsvWriter(os.path.join(output_dir, "anime_profile.tsv")) as w_profile, \
         TsvWriter(os.path.join(output_dir, "anime_info.tsv")) as w_info, \
         TsvWriter(os.path.join(output_dir, "anime_source.tsv")) as w_source, \
         TsvWriter(os.path.join(output_dir, "anime_tag.tsv")) as w_tag:

        sys.stdout.write("扫描 subject.jsonlines ...\n")
        with open(subject_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") != 2:
                    continue
                subject_count += 1
                bgm_id = o.get("id")
                if bgm_id is None:
                    continue
                bgm_id = str(bgm_id)

                name_cn = _clean_text(o.get("name_cn"), 256)
                name = _clean_text(o.get("name"), 256)
                name = name_cn or name  # name_cn 优先
                if not name:
                    skip_no_name += 1
                    continue

                air_date = _clean_text(o.get("date") or o.get("airdate"), 10)
                if not _is_valid_air_date(air_date):
                    skip_no_date += 1
                    continue

                # 增量过滤：air_date 早于 since 的跳过（不加入 name_index，
                # 这样第二遍 episode 扫描时也不会提取这些 subject 的集）
                if since and air_date < since:
                    skip_since += 1
                    continue

                desc = _clean_text(o.get("summary"))
                url = _clean_text("https://bgm.tv/subject/{}".format(bgm_id), 512)
                script_data = bgm_id
                ep_count, staff_dict, aliases = _parse_infobox(o.get("infobox") or "")
                staff = _json_dump(staff_dict)
                aliases_json = _json_dump(aliases)  # JSON 数组字符串
                name_lang = _detect_name_lang(name)
                nameid = get_nameid(name)

                name_index[bgm_id] = (name, nameid)

                # anime_profile.tsv: source_id, name, name_lang, air_date, desc, url, script_data, staff, ts, ep_count, aliases
                w_profile.writerow([bgm_id, name, name_lang, air_date, desc, url,
                                     script_data, staff, ts, ep_count, aliases_json])
                # anime_info.tsv
                w_info.writerow([nameid, name, ts])
                # anime_source.tsv
                w_source.writerow([nameid, bgm_id, ts])
                # anime_tag.tsv
                for tag in (o.get("meta_tags") or []):
                    tag = _clean_text(tag, 256)
                    if not tag or _is_trivial_tag(tag):
                        continue
                    w_tag.writerow([bgm_id, tag])
                    tag_count += 1

                profile_count += 1
                if profile_count % 10000 == 0:
                    sys.stdout.write("  已写入 %d 条 profile\n" % profile_count)
                    sys.stdout.flush()

    sys.stdout.write("subject 扫描完成：type=2 共 %d，有效 profile %d，"
                     "跳过(无name) %d，跳过(无date) %d，跳过(since) %d，tag %d\n"
                     % (subject_count, profile_count, skip_no_name, skip_no_date, skip_since, tag_count))

    # ---- 第二遍：扫描 episode.jsonlines ----
    pool_count = 0
    ep_scanned = 0
    ep_skipped_type = 0
    ep_skipped_no_subject = 0

    sys.stdout.write("扫描 episode.jsonlines ...\n")
    with TsvWriter(os.path.join(output_dir, "pool_info.tsv")) as w_pool:
        with open(episode_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ep_scanned += 1
                subject_id = str(o.get("subject_id") or "")
                if subject_id not in name_index:
                    ep_skipped_no_subject += 1
                    continue
                bgm_ep_type = o.get("type")
                if bgm_ep_type not in (0, 1):
                    ep_skipped_type += 1
                    continue
                ep_type = _BGM_EP_TYPE_MAP[bgm_ep_type]

                name, nameid = name_index[subject_id]
                sort = o.get("sort")
                if sort is None:
                    continue
                try:
                    ep_index = float(sort)
                except (TypeError, ValueError):
                    continue

                poolid = get_poolid(name, ep_type, ep_index)
                ep_name_cn = _clean_text(o.get("name_cn"), 256)
                ep_name = _clean_text(o.get("name"), 256)
                ep_name = ep_name_cn or ep_name

                w_pool.writerow([poolid, nameid, ts, ep_type, _format_ep_index(ep_index), ep_name])
                pool_count += 1
                if pool_count % 50000 == 0:
                    sys.stdout.write("  已写入 %d 条 pool_info\n" % pool_count)
                    sys.stdout.flush()

    sys.stdout.write("episode 扫描完成：扫描 %d，写入 pool_info %d，"
                     "跳过(非type0/1) %d，跳过(非动画subject) %d\n"
                     % (ep_scanned, pool_count, ep_skipped_type, ep_skipped_no_subject))

    sys.stdout.write("完成。输出目录：%s\n" % output_dir)


def _format_ep_index(ep_index):
    """格式化 ep_index，与 get_poolid 内部逻辑保持一致：整数不带小数点。"""
    if ep_index.is_integer():
        return str(int(ep_index))
    # 去掉浮点表示的多余尾零，保证 MySQL DOUBLE 精确解析
    return ("%f" % ep_index).rstrip("0").rstrip(".")


if __name__ == "__main__":
    main()
