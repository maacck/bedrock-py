# Bedrock Framework

Bedrock is a modular application framework whose contrib capabilities can be supplied by built-in or third-party implementations. This glossary defines the canonical language for that provider ecosystem.

## Provider Ecosystem

**Provider**:
A runtime implementation that supplies one Bedrock capability. A capability determines whether one Provider is selected or several Providers operate together.
_Avoid_: Backend, Adapter, plugin

**Provider Spec**:
A lightweight description that makes a Provider available by name without activating it.
_Avoid_: BackendSpec, AdapterSpec, provider registration record

**Provider Factory**:
The activation mechanism that creates a Provider from capability-specific configuration.
_Avoid_: Backend constructor, provider class loader

**Provider Catalog**:
The collection of available Provider Specs. A Catalog describes availability and never owns active Provider instances.
_Avoid_: ProviderRegistry, BackendRegistry, ClassRegistry

**Active Provider**:
A Provider instance currently owned and used by a capability.
_Avoid_: Loaded backend, resolved spec

**Selected Provider**:
The sole Active Provider for a single-provider capability such as cache or storage.
_Avoid_: Current backend, default backend

**Provider Set**:
The ordered Active Providers for a fan-out capability such as metrics.
_Avoid_: Backend list, provider registry

**DI Provider**:
The dependency-injection decorator that registers a service binding. It is distinct from a contrib Provider even though both use the word “provider”.
_Avoid_: Treating DI bindings as contrib Provider Specs

## Capability Ownership

**Manager**:
A stateful capability facade that owns Active Providers, configuration, and lifecycle. A contrib capability normally exposes one Manager singleton named after the capability.
_Avoid_: Service for long-lived Provider ownership

**Service**:
Stateless or request-scoped domain operations composed from caller-supplied dependencies. A Service does not own long-lived Providers or global capability state.
_Avoid_: Manager for pure operation functions

**Capability Singleton**:
The module-level Manager instance named after its capability, such as `cache`, `storage`, or `metrics`.
_Avoid_: Service singleton

