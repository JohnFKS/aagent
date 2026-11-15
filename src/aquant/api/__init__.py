"""API 层:FastAPI 应用与真实回测适配器。

- adapter:RealRunnerAdapter(延迟 import aquant.runner,注入 agents 层 Runner 协议);
- main:FastAPI app(/api/health、/api/validate、/api/chat)。
"""
