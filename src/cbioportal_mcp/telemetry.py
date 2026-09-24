"""OpenTelemetry + Datadog LLM Observability middleware for cBioPortal MCP."""

from __future__ import annotations

import atexit
import json
import logging
import os
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_tracer_provider: TracerProvider | None = None


def configure_telemetry() -> TracerProvider | None:
    """Configure OpenTelemetry with OTLP gRPC exporter toward the Datadog node agent.

    Environment variables (in priority order):
      OTEL_EXPORTER_OTLP_ENDPOINT  Full gRPC endpoint URL, e.g. "http://1.2.3.4:4317"
      DD_AGENT_HOST                Node IP injected via Kubernetes Downward API
                                   (falls back to "localhost")
      OTEL_SERVICE_NAME            Service name reported to Datadog (default: "cbioportal-mcp")

    Returns the configured TracerProvider, or None if setup failed (server keeps running).
    """
    global _tracer_provider

    service_name = os.getenv("OTEL_SERVICE_NAME", "cbioportal-mcp")

    # OTEL_EXPORTER_OTLP_ENDPOINT takes precedence; otherwise derive from DD_AGENT_HOST.
    # Uses OTLP/HTTP on port 4318 (lighter than gRPC — no grpcio dependency).
    # Note: OTLPSpanExporter only appends /v1/traces when reading from the env var,
    # not when the endpoint is passed directly — so we include the full path here.
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        agent_host = os.getenv("DD_AGENT_HOST", "localhost")
        endpoint = f"http://{agent_host}:4318/v1/traces"

    try:
        resource = Resource.create({SERVICE_NAME: service_name})
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        atexit.register(shutdown_telemetry)
        logger.info(
            "✅ OpenTelemetry configured: service=%s endpoint=%s", service_name, endpoint
        )
        return provider
    except Exception as exc:
        logger.warning("⚠️  Failed to configure OpenTelemetry (%s). Telemetry disabled.", exc)
        return None


def shutdown_telemetry() -> None:
    """Flush pending spans and shut down the tracer provider gracefully.

    Registered via atexit so it runs on SIGTERM→SystemExit during pod termination.
    """
    global _tracer_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_millis=5000)
            _tracer_provider.shutdown()
            logger.info("OpenTelemetry tracer provider shut down.")
        except Exception as exc:
            logger.warning("Error during telemetry shutdown: %s", exc)
        finally:
            _tracer_provider = None


def _extract_user_identity() -> tuple[str | None, str | None]:
    """Read the current request's identity (user_id, user_email), either may be None.

    Delegates to the same header resolution used for study authorization
    (``cbioportal_mcp.authentication.study_access.extract_request_identity``),
    which checks LibreChat's x-user-id/x-user-email convention first, then
    falls back to trusted-proxy headers (x-auth-request-user/email,
    x-forwarded-user/email, x-keycloak-sub/user/email). This keeps identity
    resolution consistent everywhere: a Keycloak/oauth2-proxy-fronted deployment
    now shows up with a real usr.id in traces even when the caller isn't
    LibreChat, instead of only counting LibreChat's own header convention.
    """
    try:
        from cbioportal_mcp.authentication.study_access import extract_request_identity

        identity = extract_request_identity()
        return identity.user_id, identity.user_email
    except Exception as exc:
        logger.debug("_extract_user_identity failed: %s", exc)
        return None, None


def _extract_mcp_client_info(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> tuple[str | None, str | None]:
    """Read the MCP client's self-reported identity from the initialize handshake.

    Every MCP client (LibreChat, Claude Code, Codex, Claude Desktop, ...) sends
    a ``clientInfo`` block (``name``/``version``) as part of ``initialize``. This
    is the one signal that reliably distinguishes *which application* is on the
    other end of the connection, independent of the LibreChat-specific
    x-user-id/x-user-email header convention (which only LibreChat sends, so a
    direct connector otherwise looks identical to an anonymous LibreChat call).

    Returns (client_name, client_version), either may be None.
    """
    try:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            return None, None
        client_params = fastmcp_context.session.client_params
        if client_params is None:
            return None, None
        client_info = client_params.clientInfo
        return client_info.name or None, client_info.version or None
    except Exception as exc:
        logger.debug("_extract_mcp_client_info failed: %s", exc)
        return None, None


def _extract_session_id(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> str | None:
    """Read the MCP transport session ID (Streamable HTTP / SSE) for this call.

    Stable for the lifetime of one client connection, then a fresh session ID
    is issued on reconnect. Unlike network.client.ip (shared behind NAT/VPN,
    unstable on dynamic IPs) or usr.id (only present when a trusted proxy or
    LibreChat injects identity headers), this gives a reliable count of
    distinct *connections* even for anonymous direct-connector traffic —
    e.g. "how many separate Claude Code sessions hit the server today",
    independent of whether any user identity was ever attached. It is not a
    persistent user identity: the same human reconnecting gets a new ID.

    Returns None for stdio/in-memory transports, which have no session ID.
    """
    try:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            return None
        return fastmcp_context.session_id
    except Exception as exc:
        logger.debug("_extract_session_id failed: %s", exc)
        return None


def _extract_request_source() -> str:
    """Classify the request as coming through a trusted, header-injecting proxy
    or arriving direct (e.g. a connector plugged straight into the MCP endpoint).

    Reuses the broader identity-header detection already used for study
    authorization, so this stays consistent with what authz considers
    "authenticated" rather than re-deriving its own header list.
    """
    try:
        from cbioportal_mcp.authentication.study_access import extract_request_identity

        return extract_request_identity().client
    except Exception as exc:
        logger.debug("_extract_request_source failed: %s", exc)
        return "unknown"


def _extract_client_ip() -> str | None:
    """Extract the original client IP from the active HTTP request.

    Reads X-Forwarded-For (populated by Traefik from the ELB/NLB) and returns
    the leftmost entry, which is the true client IP when trusted-proxy mode is
    configured on Traefik.  Falls back to the direct connection host.

    Returns None when no HTTP request context is active (e.g. stdio transport).
    """
    try:
        from fastmcp.server.dependencies import get_http_headers
        from fastmcp.server.http import _current_http_request

        headers = get_http_headers(include_all=True)
        forwarded_for = headers.get("x-forwarded-for", "").strip()
        if forwarded_for:
            # "client, proxy1, proxy2" — take the leftmost (original client)
            return forwarded_for.split(",")[0].strip()

        # Fallback: direct connection host
        request = _current_http_request.get()
        if request and request.client:
            return request.client.host
    except Exception:
        pass
    return None


def _llmobs_tool_span(
    tool_name: str,
    arguments: dict,
    user_id: str | None,
    user_email: str | None,
    client_name: str | None,
    client_version: str | None,
    request_source: str,
    session_id: str | None,
):
    """Start a Datadog LLMObs tool span for an MCP tool call.

    Returns None if LLMObs is not initialized (e.g. no DD_API_KEY).
    """
    try:
        from ddtrace.llmobs import LLMObs

        span = LLMObs.start_span(span_kind="tool", name=f"mcp.tool.{tool_name}")
        if user_id:
            span.set_tag("usr.id", user_id)
        if client_name:
            span.set_tag("mcp.client.name", client_name)
        span.set_tag("mcp.request.source", request_source)
        if session_id:
            span.set_tag("mcp.session.id", session_id)

        metadata: dict = {}
        if user_email:
            metadata["user_email"] = user_email
        if client_name:
            metadata["mcp_client_name"] = client_name
        if client_version:
            metadata["mcp_client_version"] = client_version
        if session_id:
            metadata["mcp_session_id"] = session_id
        metadata["request_source"] = request_source

        LLMObs.annotate(
            span=span,
            input_data=json.dumps(arguments, default=str),
            metadata=metadata if metadata else None,
        )
        return span
    except Exception:
        return None


def _llmobs_finish(span, result, *, error: bool) -> None:
    """Annotate and finish a LLMObs span returned by _llmobs_tool_span."""
    if span is None:
        return
    try:
        from ddtrace.llmobs import LLMObs

        output: str
        if error:
            output = "error"
        else:
            # Serialize the MCP result content to a compact string for the output field.
            try:
                if hasattr(result, "content"):
                    output = json.dumps(
                        [c.model_dump() if hasattr(c, "model_dump") else str(c) for c in result.content],
                        default=str,
                    )
                else:
                    output = str(result)
            except Exception:
                output = str(result)

        LLMObs.annotate(span=span, output_data=output)
        span.finish()
    except Exception:
        try:
            span.finish()
        except Exception:
            pass


class TelemetryMiddleware(Middleware):
    """FastMCP middleware that emits both an OTel span and a Datadog LLMObs tool span
    for every MCP tool call.

    OTel span name : ``mcp.tool/<tool_name>``
    OTel attributes:
      mcp.tool.name        Tool name
      network.client.ip    Original client IP from X-Forwarded-For (HTTP only)
      mcp.tool.success     True on success, False when an exception propagates
      error.type           Exception class name on failure
      mcp.client.name      Client app name from the MCP initialize handshake
                           (e.g. "librechat", "claude-code", "codex") — the
                           reliable signal for which surface is calling in,
                           independent of the LibreChat-only x-user-id header.
      mcp.client.version   Client app version from the same handshake.
      mcp.request.source   "authenticated-proxy" when trusted identity headers
                           were present, "direct" otherwise (mirrors the
                           classification used for study authorization).
      mcp.session.id       MCP transport session ID (HTTP/SSE only) — a stable
                           per-connection ID usable to count distinct sessions
                           even when no user identity is attached.

    The LLMObs tool span populates the Datadog LLM Observability dashboard widgets
    (Trace Success Rate, Total Number of Traces, Estimated Total Cost).
    """

    def __init__(self) -> None:
        self._tracer = trace.get_tracer(__name__)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, mt.CallToolResult],
    ) -> mt.CallToolResult:
        tool_name = getattr(context.message, "name", None) or "unknown"
        arguments = getattr(context.message, "arguments", {}) or {}
        user_id, user_email = _extract_user_identity()
        client_name, client_version = _extract_mcp_client_info(context)
        request_source = _extract_request_source()
        session_id = _extract_session_id(context)

        llmobs_span = _llmobs_tool_span(
            tool_name,
            arguments,
            user_id,
            user_email,
            client_name,
            client_version,
            request_source,
            session_id,
        )

        with self._tracer.start_as_current_span(f"mcp.tool/{tool_name}") as span:
            span.set_attribute("mcp.tool.name", tool_name)
            span.set_attribute("mcp.user_id.present", user_id is not None)
            if user_id:
                span.set_attribute("enduser.id", user_id)

            span.set_attribute("mcp.request.source", request_source)
            if client_name:
                span.set_attribute("mcp.client.name", client_name)
            if client_version:
                span.set_attribute("mcp.client.version", client_version)
            if session_id:
                span.set_attribute("mcp.session.id", session_id)

            client_ip = _extract_client_ip()
            if client_ip:
                span.set_attribute("network.client.ip", client_ip)

            try:
                result = await call_next(context)
                span.set_attribute("mcp.tool.success", True)
                _llmobs_finish(llmobs_span, result, error=False)
                return result
            except Exception as exc:
                span.set_attribute("mcp.tool.success", False)
                span.set_attribute("error.type", type(exc).__name__)
                span.record_exception(exc)
                _llmobs_finish(llmobs_span, None, error=True)
                raise
