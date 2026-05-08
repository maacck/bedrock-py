"""String utility functions for the Bedrock framework.

This module provides helper functions for string manipulation and generation,
with a focus on security-sensitive operations like password generation.

The :func:`random_password` function is the primary export and should be
used whenever a secure random password is needed (e.g. temporary passwords,
initial user setup, password reset flows).
"""

from __future__ import annotations

import secrets
import string


class StringHelpers:
    """Utility class for string operations with security-focused methods.

    This class provides both instance and static methods for common string
    operations. The primary use case is secure random string generation.

    Example:
        >>> from bedrock.utils.string_helpers import StringHelpers
        >>>
        >>> helpers = StringHelpers()
        >>> password = helpers.random_password(length=16)
        >>> print(password)  # e.g. "K7mP9xL2vQ8nR4tY"
    """

    # Default character sets for password generation
    DEFAULT_CHARS = string.ascii_letters + string.digits
    SECURE_CHARS = string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]{}|;:,.<>?"

    def random_password(
        self,
        length: int = 12,
        chars: str | None = None,
        min_upper: int = 1,
        min_lower: int = 1,
        min_digit: int = 1,
        min_special: int = 0,
    ) -> str:
        """Generate a cryptographically secure random password.

        Uses Python's :mod:`secrets` module (not :mod:`random`) to ensure
        cryptographic security suitable for passwords, API keys, and tokens.

        Args:
            length: Total length of the password. Must be >= 8.
                   Default is 12 characters (industry standard minimum).
            chars: Character set to draw from. If None, uses a balanced set
                  of uppercase, lowercase, and digits. For maximum security,
                  consider using :attr:`SECURE_CHARS`.
            min_upper: Minimum number of uppercase letters required.
            min_lower: Minimum number of lowercase letters required.
            min_digit: Minimum number of digits required.
            min_special: Minimum number of special characters required.

        Returns:
            A randomly generated password string meeting the specified criteria.

        Raises:
            ValueError: If length < 8, or if the minimum requirements cannot
                       be satisfied with the given character set.

        Security Notes:
            - Uses ``secrets.choice()`` for cryptographically secure randomness
            - Default settings ensure a mix of character types
            - Length of 12+ is recommended for production use
            - Consider using ``min_special=1`` for maximum security
        """
        if length < 8:
            raise ValueError("Password length must be at least 8 characters.")

        if chars is None:
            chars = self.DEFAULT_CHARS

        if not chars:
            raise ValueError("Character set cannot be empty.")

        # Calculate minimum required characters
        min_required = min_upper + min_lower + min_digit + min_special
        if min_required > length:
            raise ValueError(f"Minimum requirements ({min_required}) exceed password length ({length})")

        # Generate password ensuring minimum character requirements
        password = self._generate_with_requirements(
            length=length,
            chars=chars,
            min_upper=min_upper,
            min_lower=min_lower,
            min_digit=min_digit,
            min_special=min_special,
        )

        return password

    def _generate_with_requirements(
        self,
        length: int,
        chars: str,
        min_upper: int,
        min_lower: int,
        min_digit: int,
        min_special: int,
    ) -> str:
        """Internal method to generate password with character type requirements.

        This ensures the generated password meets minimum complexity requirements
        by first placing required characters, then filling the rest randomly.
        """
        password_list: list[str] = []

        # Add required uppercase letters
        if min_upper > 0:
            uppercase = string.ascii_uppercase
            password_list.extend(secrets.choice(uppercase) for _ in range(min_upper))

        # Add required lowercase letters
        if min_lower > 0:
            lowercase = string.ascii_lowercase
            password_list.extend(secrets.choice(lowercase) for _ in range(min_lower))

        # Add required digits
        if min_digit > 0:
            digits = string.digits
            password_list.extend(secrets.choice(digits) for _ in range(min_digit))

        # Add required special characters
        if min_special > 0:
            special = "!@#$%^&*()_+-=[]{}|;:,.<>?"
            password_list.extend(secrets.choice(special) for _ in range(min_special))

        # Fill remaining characters from the full character set
        remaining = length - len(password_list)
        if remaining > 0:
            password_list.extend(secrets.choice(chars) for _ in range(remaining))

        # Shuffle the password to avoid predictable patterns
        # (secrets.SystemRandom provides cryptographically secure shuffling)
        secrets.SystemRandom().shuffle(password_list)

        return "".join(password_list)

    @staticmethod
    def generate_token(length: int = 32) -> str:
        """Generate a cryptographically secure random token.

        This is a convenience method for generating API keys, session tokens,
        and other non-password random strings. Uses URL-safe base64 encoding.

        Args:
            length: Number of random bytes to generate before encoding.
                   The resulting string will be longer due to base64 encoding.

        Returns:
            A URL-safe base64 encoded random token.
        """
        return secrets.token_urlsafe(length)

    @staticmethod
    def is_strong_password(password: str) -> bool:
        """Check if a password meets basic strength requirements.

        This is a simple validation helper. For production use, consider
        implementing more sophisticated password strength estimation.

        Args:
            password: The password to evaluate.

        Returns:
            True if the password meets minimum strength criteria.
        """
        if len(password) < 8:
            return False

        has_upper = any(c.isupper() for c in password)
        has_lower = any(c.islower() for c in password)
        has_digit = any(c.isdigit() for c in password)

        return has_upper and has_lower and has_digit


# --------------------------------------------------------------------------- #
# Module-level convenience API                                                  #
# --------------------------------------------------------------------------- #

# Default instance for module-level functions
_default_helpers: StringHelpers | None = None


def _get_default() -> StringHelpers:
    """Return (or lazily create) the module-level StringHelpers singleton."""
    global _default_helpers
    if _default_helpers is None:
        _default_helpers = StringHelpers()
    return _default_helpers


def random_password(
    length: int = 12,
    chars: str | None = None,
    min_upper: int = 1,
    min_lower: int = 1,
    min_digit: int = 1,
    min_special: int = 0,
) -> str:
    """Generate a cryptographically secure random password (module-level).

    Convenience function that uses the default :class:`StringHelpers` instance.
    See :meth:`StringHelpers.random_password` for full documentation.

    Args:
        length: Total length of the password. Must be >= 8. Default is 12.
        chars: Character set to draw from. If None, uses letters and digits.
        min_upper: Minimum uppercase letters required.
        min_lower: Minimum lowercase letters required.
        min_digit: Minimum digits required.
        min_special: Minimum special characters required.

    Returns:
        A randomly generated secure password.

    Example:
        >>> from bedrock.utils.string_helpers import random_password
        >>> pwd = random_password(length=16, min_special=2)
        >>> print(len(pwd))  # 16
    """
    return _get_default().random_password(
        length=length,
        chars=chars,
        min_upper=min_upper,
        min_lower=min_lower,
        min_digit=min_digit,
        min_special=min_special,
    )


def generate_token(length: int = 32) -> str:
    """Generate a cryptographically secure random token (module-level).

    Convenience function that uses :meth:`StringHelpers.generate_token`.

    Args:
        length: Number of random bytes before base64 encoding.

    Returns:
        A URL-safe base64 encoded random token.
    """
    return StringHelpers.generate_token(length)


def is_strong_password(password: str) -> bool:
    """Check password strength (module-level).

    Convenience function that uses :meth:`StringHelpers.is_strong_password`.
    """
    return StringHelpers.is_strong_password(password)


__all__ = [
    "StringHelpers",
    "random_password",
    "generate_token",
    "is_strong_password",
]
