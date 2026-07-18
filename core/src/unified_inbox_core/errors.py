from __future__ import annotations


class DeliveryError(RuntimeError):
    """A delivery failure with retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class PermanentDeliveryError(DeliveryError):
    """A delivery failure that cannot succeed without changing the payload."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
