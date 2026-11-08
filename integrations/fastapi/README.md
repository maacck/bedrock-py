# bedrock-fastapi

FastAPI integration for [Bedrock](https://bedrock-py.com). Your application creates and owns the FastAPI app;
this package supplies pieces you add as needed:

- `RequestScopeMiddleware`: one Bedrock DI scope per request.
- `DbSessionMiddleware` / `AsyncDbSessionMiddleware`: each request runs inside `db.session_scope()` /
  `db.asession_scope()`. Services commit; uncommitted work is rolled back when the request ends.
- `bedrock_exception_handler(status_codes)`: answers mapped `BedrockExc` subclasses with `{"detail": ...}`.

```python
from bedrock.exc import BedrockExc
from bedrock_fastapi import AsyncDbSessionMiddleware, RequestScopeMiddleware, bedrock_exception_handler

app.add_middleware(AsyncDbSessionMiddleware)
app.add_middleware(RequestScopeMiddleware)
app.add_exception_handler(BedrockExc, bedrock_exception_handler({ItemNotFound: 404}))
```

See the FastAPI integration docs at https://bedrock-py.com/docs/integrations/fastapi for installation, the
transaction convention, and a complete example.
