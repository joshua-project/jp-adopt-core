"""Worker task modules: each task exposes an ARQ function and may also expose
an inline coroutine (``..._inline``) that the API can hand off via FastAPI
BackgroundTasks when ARQ is not configured (dev / tests).
"""
