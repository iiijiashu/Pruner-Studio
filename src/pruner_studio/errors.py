class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class UpstreamError(AppError):
    def __init__(self, code: str, message: str, status: int | None = None):
        if status is None:
            status = 400 if code == "UPSTREAM_REJECTED" else 502
        super().__init__(code, message, status)


def propagate_risk_control(exc):
    if isinstance(exc, AppError) and exc.code in {"UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN"}:
        raise exc
