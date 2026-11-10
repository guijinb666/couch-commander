"""验证 winrt SMTC 是否能读到真实播放进度（本轮 m01602 专项取证）。

只读，不控制任何播放器。
"""
import asyncio

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Mgr,
)


async def main() -> int:
    manager = await Mgr.request_async()
    sessions = manager.get_sessions()
    print("SESSIONS", len(sessions))
    current = manager.get_current_session()
    print("CURRENT", getattr(current, "source_app_user_model_id", None))
    readable = 0
    for session in sessions:
        print("---")
        try:
            info = await session.try_get_media_properties_async()
            title = info.title
            artist = info.artist
        except Exception as exc:  # noqa: BLE001
            title = f"<{type(exc).__name__}>"
            artist = ""
        try:
            timeline = session.get_timeline_properties()
            position = timeline.position
            duration = timeline.end_time
            start = timeline.start_time
            timeline_ok = True
        except Exception as exc:  # noqa: BLE001
            position = duration = start = f"<{type(exc).__name__}>"
            timeline_ok = False
        try:
            playback = session.get_playback_info()
            status = playback.playback_status
            status_name = getattr(status, "name", str(status))
            rate = playback.playback_rate
        except Exception as exc:  # noqa: BLE001
            status_name = f"<{type(exc).__name__}>"
            rate = None
        print("  app   :", session.source_app_user_model_id)
        print("  title :", title, "| artist:", artist)
        print("  status:", status_name, "| rate:", rate)
        print("  pos   :", position, "dur:", duration, "start:", start)
        if timeline_ok and title:
            readable += 1
    print("PROGRESS_READABLE", readable)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
