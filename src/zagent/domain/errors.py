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


class AgentLimitError(ZAgentError):
    pass

