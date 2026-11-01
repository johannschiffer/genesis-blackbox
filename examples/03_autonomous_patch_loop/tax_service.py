"""
Showcase 3: Tax Calculation Service
Provides business logic for autonomous 1-shot repair loop demonstration.
"""


def calculate_tax(amount: float, rate: float) -> float:
    """
    Calculates tax amount.
    Unfixed bug: Negative rates trigger an unhandled ValueError instead of clamping.
    """
    if rate < 0:
        raise ValueError(f"Invalid tax rate: {rate}")
    return amount * rate

