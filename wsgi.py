"""Gunicorn entrypoint for cloud deployments."""

from server import app, start_board_scheduler, _bg_refresh_sectors

# 云容器里没有 __main__ 分支，入口在这里拉起后台调度与板块预热。
start_board_scheduler()
_bg_refresh_sectors()
