# Bedrock Metrics

Application metrics instrumentation: counter/gauge/timer handles with decorators, a hardcoded logger, and pluggable providers (StatsD, Sentry, Prometheus pushgateway). Emission is synchronous and never raises into business code.

## Quick Reference

| Concern | Import |
|---------|--------|
| Metrics singleton | `from bedrock.contrib.metric import metrics` |
| Register a provider | `from bedrock.contrib.metric import register_provider` |
| List providers | `from bedrock.contrib.metric import list_providers` |

## Setup

```python
from bedrock.contrib.metric import metrics, StatsDProvider, StatsDSettings

metrics.register_provider(StatsDProvider(settings=StatsDSettings(host="127.0.0.1")))
```

Every emit writes a log record to the `bedrock.contrib.metric` logger (`INFO` by default), then fans out to all registered providers. There is no `configure()` and no default provider.

## Handles

```python
# Counter — inc() or decorator (sync/async), fractional deltas preserved
@metrics.counter("user.login.count", tags={"realm": "cn"})
def login(...): ...

# Gauge — set() only (no decorator)
metrics.gauge("db.connections").set(12)

# Timer — decorator, context manager, or observe(seconds)
with metrics.timer("db.query.duration"):
    ...
```

All handles copy `tags` at creation time. `set_log_level(level)` changes the severity of each metric log record (default `logging.INFO`).

## Providers

| Provider | Extra | Notes |
|----------|-------|-------|
| `StatsDProvider` | none | Zero-dependency UDP; Datadog tag syntax. |
| `SentryProvider` | `metric-sentry` | Forwards via `sentry_sdk.metrics`. |
| `PrometheusPushProvider` | `metric-prometheus` | Pushgateway; throttled pushes. |

StatsD settings (`STATSD_`): `host`, `port` (8125), `prefix`. Prometheus settings (`METRIC_PROMETHEUS_`): `gateway_url` (required), `job` (`bedrock-app`), `push_interval` (1.0).

## Mapping

| Handle | StatsD | Sentry | Prometheus |
|--------|--------|--------|------------|
| counter | `c` | `count` | `Counter` |
| gauge | `g` | `gauge` | `Gauge` |
| timer | `ms` | `distribution` (second) | `Histogram` |

Prometheus fixes a metric's type and label-key schema at first observation; later mismatches raise `ValueError` (isolated by the manager).

## Isolation Guarantee

- Provider `emit()` failures are caught (`except Exception`, never `BaseException`) and logged as rate-limited warnings.
- Emission is synchronous — providers run I/O inline on the caller's thread (StatsD `sendto`, Prometheus HTTP POST), so a slow sink can block the caller.
- Failing providers are not auto-disabled.

## Exceptions

`MetricError` (a `BedrockExc`) → `MetricConfigurationError`, `MetricProviderError`. Raised only at provider construction time; emission never raises.

## Anti-Patterns

- Don't wrap `inc()`/`set()`/`observe()` in try/except — emits never raise.
- Don't call `close()` in normal operation; the framework never calls it.
- Don't change a Prometheus metric's type or tag keys after first observation.
- Don't emit from inside a custom provider's `emit()` — it recurses.

## See Also

- Guide: `docs-web/content/docs/en/(bedrock)/guides/metric.mdx`
