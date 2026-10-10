#!/usr/bin/env python3

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

HOST = "http://epg.dtcmc.com:11004"
UA = "okhttp/3.12.1"


def http_get_json(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Request failed: {url} -> {last}")


def fetch_channels(endpoint, column_id, page_size=200, max_pages=50):
    """Fetch channel directory. column_id may be an int or a list of column ids (merged, de-duplicated)."""
    col_ids = column_id if isinstance(column_id, (list, tuple)) else [column_id]
    seen, channels = set(), []
    for cid in col_ids:
        page = 0
        while page < max_pages:
            q = urllib.parse.urlencode({
                "columnId": cid, "pageIndex": page,
                "category": "ALL", "pageSize": page_size, "lang": "ja",
            })
            data = http_get_json(f"{HOST}/epgs/{endpoint}/media/get?{q}")
            recs = (data.get("result") or {}).get("records") or []
            if not recs:
                break
            for r in recs:
                rid = r.get("id")
                if rid and rid not in seen:
                    seen.add(rid)
                    channels.append(r)
            if len(recs) < page_size:
                break
            page += 1
    return channels


def fetch_programs(cid, date, tz):
    q = urllib.parse.urlencode({"id": cid, "date": date, "timezone": tz})
    data = http_get_json(f"{HOST}/epgs/channel/epg/get?{q}")
    return data.get("result") or []


def fmt_xmltv_time(epoch_ms, tz_hours):
    tzinfo = timezone(timedelta(hours=tz_hours))
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).astimezone(tzinfo)
    sign = "+" if tz_hours >= 0 else "-"
    return f"{dt.strftime('%Y%m%d%H%M%S')} {sign}{abs(tz_hours):02d}00"


def build_xmltv(channels, dates, tz, limit=None):
    root = ET.Element("tv", {"generator-info-name": "kaiteki-epg2xmltv"})
    chans = channels[:limit] if limit else channels
    for ch in chans:
        cid = ch.get("id")
        if not cid:
            continue
        el = ET.SubElement(root, "channel", {"id": cid})
        ET.SubElement(el, "display-name").text = ch.get("title") or cid
    total = 0
    total_dates = len(dates)
    for idx, ch in enumerate(chans, 1):
        cid = ch.get("id")
        if not cid:
            continue
        print(f"[{idx}/{len(chans)}] {cid} {ch.get('title', '')} ({total_dates} days)", file=sys.stderr)
        for date in dates:
            try:
                progs = fetch_programs(cid, date, tz)
            except Exception as e:
                print(f"  ! {cid} {date} failed: {e}", file=sys.stderr)
                continue
            for p in progs:
                start, end = p.get("startUtc"), p.get("endUtc")
                if not start or not end:
                    continue
                pe = ET.SubElement(root, "programme", {
                    "start": fmt_xmltv_time(start, tz),
                    "stop": fmt_xmltv_time(end, tz),
                    "channel": cid,
                })
                ET.SubElement(pe, "title").text = p.get("title") or ""
                ET.SubElement(pe, "desc").text = p.get("description") or ""
                total += 1
            time.sleep(0.15)
    return root, total


def main():
    ap = argparse.ArgumentParser(description="Kaiteki EPG to XMLTV")
    ap.add_argument("--endpoint", default="zyepg", choices=["zyepg", "mbepg"],
                    help="Channel-list endpoint (default zyepg full set)")
    ap.add_argument("--column-id", default="101,102,103",
                    help="Category/column IDs, comma-separated (default zyepg 101,102,103; mbepg uses 201)")
    ap.add_argument("--date", default=None,
                    help="Anchor date YYYY-MM-DD for single-day mode (default today)")
    ap.add_argument("--days", type=int, default=7,
                    help="Days to cover (default 7). >0 = next N days from today; <0 = past |N| days")
    ap.add_argument("--back", type=int, default=0,
                    help="Extra lookback of N days (default 0), combined with --days>0")
    ap.add_argument("--tz", type=int, default=9, help="Output timezone in hours (9=JST, 8=CST)")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N channels (debug)")
    ap.add_argument("-o", "--out", default="epg.xml", help="Output XMLTV file")
    a = ap.parse_args()

    # Build the date list (ascending, de-duplicated)
    today = datetime.now()
    if a.date:
        today = datetime.strptime(a.date, "%Y-%m-%d")
    offsets = set()
    if a.days >= 1:
        offsets.update(range(0, a.days))          # next N days from today (incl. today)
    elif a.days == 0:
        offsets.add(0)
    else:
        n = abs(a.days)
        offsets.update(range(-(n - 1), 1))        # past n days (incl. today); e.g. -7 -> -6..0
    if a.back > 0:
        offsets.update(range(-a.back, 0))         # extra lookback
    dates = sorted({(today + timedelta(days=off)).strftime("%Y-%m-%d") for off in offsets})

    print(f"Fetching channel list (endpoint={a.endpoint} columnId={a.column_id}) ...", file=sys.stderr)
    col_ids = [int(x) for x in str(a.column_id).split(",") if x.strip()]
    channels = fetch_channels(a.endpoint, col_ids)
    print(f"{len(channels)} channels; date range {dates[0]} ~ {dates[-1]} "
          f"({len(dates)} days, tz={a.tz})", file=sys.stderr)

    root, total = build_xmltv(channels, dates, a.tz, a.limit)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(a.out, encoding="utf-8", xml_declaration=True)

    n_chan = len(root.findall("channel"))
    print(f"\nDone: {n_chan} <channel>, {total} <programme> -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()