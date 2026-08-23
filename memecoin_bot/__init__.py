"""A risk-first Solana meme coin trading bot.

The package defaults to paper trading. Live execution is a deliberate,
separately implemented seam (see :mod:`memecoin_bot.broker`) so that no
code path can spend real funds by accident.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
