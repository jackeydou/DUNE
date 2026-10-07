"""The error every case check raises."""


class CaseError(Exception):
    """A case directory cannot be loaded. The message names the file, the field, and the fix."""
