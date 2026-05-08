class BedrockExc(Exception):
    """Base exception class for Bedrock runtime errors."""

    detail: str = "Bedrock runtime error."

    def __init__(self, msg: str | None = None):
        """Initialize the exception with an optional detail override.

        Args:
            msg: Optional detail message.
        """
        if msg is not None:
            self.detail = msg
        super().__init__(self.detail)


class ImproperlyConfigured(BedrockExc):
    """Raised when Bedrock runtime configuration is invalid."""

    detail: str = "Improperly configured."


class FilterError(BedrockExc):
    """Base exception for all filter-related errors."""

    detail: str = "Filter error."


class BadFilterFormatError(FilterError):
    """Raised when a filter spec is malformed or uses an invalid operator/field."""

    detail: str = "Bad filter format."


class FilterDepthExceededError(FilterError):
    """Raised when the recursive depth of a filter tree exceeds BEDROCK_MAX_FILTER_DEPTH."""

    detail: str = "Filter depth exceeded."
