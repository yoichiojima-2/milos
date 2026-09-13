"""milos: a secure agent platform on Google Cloud.

Scripts drive sessions through `Client`; see `milos.client` and the README.
"""

from .client import ApiError, Client

__version__ = "0.2.0"

__all__ = ["ApiError", "Client", "__version__"]
