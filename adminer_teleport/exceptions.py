"""Custom exceptions for the Adminer orchestrator."""


class OrchestratorError(Exception):
    """Base exception for orchestrator errors."""


class ConfigurationError(OrchestratorError):
    """Raised when configuration is invalid."""


class PortAvailabilityError(OrchestratorError):
    """Raised when required ports are unavailable."""


class ProcessStartupError(OrchestratorError):
    """Raised when processes fail to start."""


class PreflightCheckError(OrchestratorError):
    """Raised when preflight checks fail."""
