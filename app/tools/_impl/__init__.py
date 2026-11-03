"""tool 的底层实现。

约定：app/tools/*.py 只放对 LLM 暴露的 tool，实现一律收在本包下；
本包不反向 import app.tools.*，避免出现循环依赖。
"""
