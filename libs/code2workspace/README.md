# Code2Workspace SDK

This package contains the runtime used by `code2workspace`.

Its current core capabilities include:

- graph assembly on top of LangGraph
- tool-driven agent execution on top of LangChain
- filesystem, memory, skills, and summarization middleware
- local and sandbox-oriented backend abstractions
- subagent orchestration

Main entry point:

```python
from code2workspace import create_workspace_agent
```
