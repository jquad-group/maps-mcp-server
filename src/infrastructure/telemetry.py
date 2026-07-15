"""OpenTelemetry tracing setup — mirrors the sibling MCP servers."""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_telemetry(service_name: str):
    """
    Configures OpenTelemetry tracing for the service.

    Args:
        service_name: The name of the service to appear in traces.
    """
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return

    # Create Resource
    resource = Resource.create(attributes={
        "service.name": service_name,
    })

    # Create TracerProvider
    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    # Configure OTLP Exporter
    otlp_exporter = OTLPSpanExporter()

    # Add BatchSpanProcessor
    span_processor = BatchSpanProcessor(otlp_exporter)
    tracer_provider.add_span_processor(span_processor)

    # Instrument external libraries (HTTPX for outgoing HTTP requests)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)

    # Instrument FastAPI globally
    FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)

    return tracer_provider
