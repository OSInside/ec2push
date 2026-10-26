class EC2PushError(Exception):
    """
    Base class of all errors raised by ec2push
    """


class EC2PushImageError(EC2PushError):
    """
    The local image file can't be used for the upload
    """


class EC2PushRemoteError(EC2PushError):
    """
    Connecting to or running a command on the helper instance failed
    """


class EC2PushAWSError(EC2PushError):
    """
    An AWS resource could not be set up or did not reach the
    expected state
    """
