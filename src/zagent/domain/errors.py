class ZAgentError(Exception):
    """Base error for errors safe to classify at the API boundary."""


class NotFoundError(ZAgentError):
    pass


class ValidationError(ZAgentError):
    pass


class ModelProtocolError(ZAgentError):
    pass


class ModelTransportError(ZAgentError):
    pass


class ToolExecutionError(ZAgentError):
    pass


class PermissionRequiredError(ToolExecutionError):
    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class AgentLimitError(ZAgentError):
    def __init__(self, message: str, checkpoint: dict | None = None) -> None:
        super().__init__(message)
        self.checkpoint = checkpoint
