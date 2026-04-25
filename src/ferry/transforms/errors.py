class UnknownTransformError(Exception):
    """User config references a transform name that isn't registered."""


class TransformError(Exception):
    """A transform failed — bad input, refused content, etc."""
