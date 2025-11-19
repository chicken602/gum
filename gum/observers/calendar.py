from __future__ import annotations
import os
import aiohttp
import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List
from ics import Calendar as IcsCalendar

from .observer import Observer
from ..schemas import Update


###############################################################################
# Calendar Observer (persistent cache + batching + polling)
###############################################################################

class Calendar(Observer):
    """
    Observer that monitors an ICS calendar feed for event additions,
    deletions, or modifications and emits *batched, chronologically
    ordered* updates.

    ⚠️  TODO (TZ semantics)
        Outlook ICS feeds often export *local wall-clock times* labeled as UTC ("Z").
        This observer therefore treats all event times as local via
        `.replace(tzinfo=self.local_tz)`. If you use a properly
        timezone-aware ICS (e.g., Google), you may remove this substitution.
    """

    def __init__(
        self,
        ics_url: Optional[str] = None,
        polling_interval: int = 60,          # poll every 60 s
        snapshot_interval: int = 24 * 3600,  # daily summary
        timezone: Optional[str] = None,      # auto-detect if not provided
        debug: bool = False,
    ) -> None:
        self.ics_url = ics_url or os.getenv("CALENDAR_ICS")
        if not self.ics_url:
            raise ValueError(
                "No ICS URL provided. Pass via constructor or CALENDAR_ICS environment variable.\n"
                "See https://learn.microsoft.com/en-us/answers/questions/4617072/how-to-export-outlook-calendar-to-ics-link for more information."
            )

        self.polling_interval = polling_interval
        self.snapshot_interval = snapshot_interval

        # auto-detect system timezone if not specified
        if timezone is None:
            try:
                detected_tz = datetime.now().astimezone().tzinfo
                self.local_tz = ZoneInfo(str(detected_tz))
                if debug:
                    print(f"[Calendar] Auto-detected local timezone: {self.local_tz}")
            except Exception:
                self.local_tz = ZoneInfo("America/Los_Angeles")
                if debug:
                    print("[Calendar] Failed to detect local timezone, defaulting to America/Los_Angeles")
        else:
            self.local_tz = ZoneInfo(timezone)

        self.debug = debug

        # persistent cache on disk
        self.cache_dir = os.path.expanduser("~/.cache/gum/calendar")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_path = os.path.join(self.cache_dir, "calendar_cache.json")
        self._cache: Dict[str, Dict] = self._load_cache()

        self._last_snapshot_time = datetime.now(self.local_tz)

        super().__init__()

    # ─────────────────────────────── cache helpers
    def _load_cache(self) -> Dict[str, Dict]:
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, "r") as f:
                    raw = json.load(f)
                for uid, info in raw.items():
                    for k in ("start", "end"):
                        if info.get(k):
                            info[k] = datetime.fromisoformat(info[k])
                if self.debug:
                    print(f"[Calendar] Loaded {len(raw)} cached events.")
                return raw
        except Exception as e:
            if self.debug:
                print(f"[Calendar] Failed to load cache: {e}")
        return {}

    def _save_cache(self) -> None:
        try:
            serializable = {
                uid: {
                    **info,
                    "start": info["start"].isoformat() if info.get("start") else None,
                    "end": info["end"].isoformat() if info.get("end") else None,
                }
                for uid, info in self._cache.items()
            }
            with open(self.cache_path, "w") as f:
                json.dump(serializable, f, indent=2)
            if self.debug:
                print(f"[Calendar] Cache saved ({len(serializable)} events).")
        except Exception as e:
            if self.debug:
                print(f"[Calendar] Failed to save cache: {e}")

    # ─────────────────────────────── background worker
    async def _worker(self) -> None:
        while self._running:
            changed = await self._poll_once()
            if changed:
                self._save_cache()

            now = datetime.now(self.local_tz)
            if (now - self._last_snapshot_time).total_seconds() > self.snapshot_interval:
                await self._emit_snapshot()
                self._last_snapshot_time = now

            await asyncio.sleep(self.polling_interval)

    # ─────────────────────────────── ICS fetch + parse
    async def _fetch_calendar(self) -> Optional[List]:
        async with aiohttp.ClientSession() as session:
            async with session.get(self.ics_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Bad status: {resp.status}")
                body = await resp.text()
        cal = IcsCalendar(body)
        return list(cal.events)

    # ─────────────────────────────── poll & diff
    async def _poll_once(self) -> bool:
        """Fetch ICS, diff with cache (last 7 days), and emit one batched update."""
        try:
            events = await self._fetch_calendar()
        except Exception as e:
            if self.debug:
                print(f"[Calendar] Fetch failed: {e}")
            return False
        if not events:
            return False

        now = datetime.now(self.local_tz)
        one_week_ago = now - timedelta(days=7)
        new_state: Dict[str, Dict] = {}

        for ev in events:
            if not ev.begin:
                continue
            start = ev.begin.datetime.replace(tzinfo=self.local_tz)
            end = ev.end.datetime.replace(tzinfo=self.local_tz) if ev.end else None
            if start < one_week_ago:
                continue

            new_state[ev.uid] = {
                "uid": ev.uid,
                "title": ev.name or "<no title>",
                "start": start,
                "end": end,
                "desc": (ev.description or "").strip(),
                "loc": ev.location or "<no location>",
            }

        added, removed, modified = [], [], []

        for uid, info in new_state.items():
            if uid not in self._cache:
                added.append(info)
            else:
                old = self._cache[uid]
                if any(info[k] != old.get(k) for k in ("title", "start", "end", "desc", "loc")):
                    modified.append(info)
        for uid in self._cache:
            if uid not in new_state:
                removed.append(self._cache[uid])

        self._cache = new_state

        if not (added or removed or modified):
            if self.debug:
                print("[Calendar] No changes detected.")
            return False

        # always batch chronologically
        changes = []
        for group, kind in ((added, "NEW"), (modified, "MODIFIED"), (removed, "DELETED")):
            for ev in group:
                changes.append((ev["start"], kind, ev))
        changes.sort(key=lambda x: x[0])

        content = self._format_batch_update(changes, now)
        await self.update_queue.put(Update(content=content, content_type="input_text"))

        if self.debug:
            print(f"[Calendar] Emitted batch update ({len(changes)} events).")
        return True

    # ─────────────────────────────── format batched updates
    def _format_batch_update(self, sorted_changes: List[tuple], current_time: datetime) -> str:
        lines = [f"Current Time: {current_time.strftime('%Y-%m-%d %H:%M %Z')}"]
        for start, kind, ev in sorted_changes:
            delta = ev["start"] - current_time
            total = int(delta.total_seconds())
            neg = total < 0
            total = abs(total)
            days, rem = divmod(total, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            delta_str = f"{days}d {hours}h {minutes}m"
            if neg:
                delta_str += " ago"

            lines += [
                f"{kind} calendar event:",
                f"  Title      : {ev['title']}",
                f"  When       : {ev['start']} → {ev['end']}",
                f"  Location   : {ev['loc']}",
                f"  Starts In  : {delta_str}",
            ]
            if ev["desc"]:
                lines.append("  Description:")
                for line in ev["desc"].splitlines():
                    lines.append(f"    {line}")
            # event delimiter
            lines.append("\n----------\n")
        return "\n".join(lines).strip()

    # ─────────────────────────────── daily snapshot
    async def _emit_snapshot(self) -> None:
        try:
            events = await self._fetch_calendar()
        except Exception as e:
            if self.debug:
                print(f"[Calendar] Snapshot fetch failed: {e}")
            return
        if not events:
            return

        now = datetime.now(self.local_tz)
        week_ahead = now + timedelta(days=7)
        future_events = [
            {
                "title": ev.name or "<no title>",
                "start": ev.begin.datetime.replace(tzinfo=self.local_tz),
                "end": ev.end.datetime.replace(tzinfo=self.local_tz) if ev.end else None,
                "loc": ev.location or "<no location>",
                "desc": (ev.description or "").strip(),
            }
            for ev in events
            if ev.begin and now <= ev.begin.datetime.replace(tzinfo=self.local_tz) <= week_ahead
        ]
        future_events.sort(key=lambda e: e["start"])

        lines = [
            f"Current Time: {now.strftime('%Y-%m-%d %H:%M %Z')}",
            "Weekly Calendar Snapshot:",
        ]
        for ev in future_events:
            lines += [
                f"  Title    : {ev['title']}",
                f"  When     : {ev['start']} → {ev['end']}",
                f"  Location : {ev['loc']}",
            ]
            if ev["desc"]:
                lines.append("  Description:")
                for line in ev["desc"].splitlines():
                    lines.append(f"    {line}")
            lines.append("")

        await self.update_queue.put(Update(content="\n".join(lines), content_type="input_text"))
        if self.debug:
            print("[Calendar] Snapshot emitted.")

    # ─────────────────────────────── public query API
    def query(
        self,
        start_delta: timedelta = timedelta(seconds=0),
        end_delta: timedelta = timedelta(days=1),
    ) -> List[Dict]:
        """
        Query cached calendar events within a time window relative to now.

        Args:
            start_delta (timedelta, optional): Offset from now for window start.
                Defaults to 0 (i.e., now).
            end_delta (timedelta, optional): Offset from now for window end.
                Defaults to 1 day.

        Returns:
            List[Dict]: List of events (dicts) in the requested timeframe.
        """
        if not self._cache:
            if self.debug:
                print("[Calendar] Cache is empty; nothing to query.")
            return []

        now = datetime.now(self.local_tz)
        start_time = now + start_delta
        end_time = now + end_delta

        results = []
        for ev in self._cache.values():
            ev_start = ev["start"]
            ev_end = ev["end"] or ev_start
            if ev_end >= start_time and ev_start <= end_time:
                results.append(ev)

        results.sort(key=lambda e: e["start"])
        if self.debug:
            print(f"[Calendar] Query returned {len(results)} events from {start_time} to {end_time}.")
        return results

    def query_str(
        self,
        start_delta: timedelta = timedelta(seconds=0),
        end_delta: timedelta = timedelta(days=1),
    ) -> str:
        """
        Return a formatted string representation of cached events
        within a time window relative to now.

        Uses the same formatting style as the batched updates emitted
        by the observer, for human readability.

        Args:
            start_delta (timedelta, optional): Offset from now for window start.
                Defaults to 0 (now).
            end_delta (timedelta, optional): Offset from now for window end.
                Defaults to 1 day.

        Returns:
            str: Readable text of calendar events in the time range.
        """
        events = self.query(start_delta=start_delta, end_delta=end_delta)
        now = datetime.now(self.local_tz)
        if not events:
            return f"Current Time: {now.strftime('%Y-%m-%d %H:%M %Z')}\nNo events found in this range."

        lines = [
            f"Current Time: {now.strftime('%Y-%m-%d %H:%M %Z')}",
            f"Calendar Events ({len(events)} found):",
        ]

        for ev in events:
            delta = ev["start"] - now
            total = int(delta.total_seconds())
            neg = total < 0
            total = abs(total)
            days, rem = divmod(total, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            delta_str = f"{days}d {hours}h {minutes}m"
            if neg:
                delta_str += " ago"

            lines += [
                f"  Title      : {ev['title']}",
                f"  When       : {ev['start']} → {ev['end']}",
                f"  Location   : {ev['loc']}",
                f"  Starts In  : {delta_str}",
            ]
            if ev["desc"]:
                lines.append("  Description:")
                for line in ev["desc"].splitlines():
                    lines.append(f"    {line}")
            # visual delimiter between events
            lines.append("\n----------\n")

        return "\n".join(lines).strip()