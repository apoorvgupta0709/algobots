"""Platform exception hierarchy."""


class AlgobotError(Exception):
    """Base class for all platform errors."""


class BrokerError(AlgobotError):
    """Order placement / broker API failure."""


class AuthError(BrokerError):
    """Broker login/token failure."""


class DataError(AlgobotError):
    """Missing/invalid market data."""


class RiskRejection(AlgobotError):
    """Order rejected by the risk engine (caps, kill switch, sizing)."""


class GateError(AlgobotError):
    """Promotion attempted without a passing gate."""


class ConfigError(AlgobotError):
    """Bad or missing configuration."""
