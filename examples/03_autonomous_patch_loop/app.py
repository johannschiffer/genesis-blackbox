"""
Showcase 3: Target Application Entrypoint
Simulates an order checkout service invoking tax_service.
"""

import sys
from tax_service import calculate_tax


def main():
    rate = float(sys.argv[1]) if len(sys.argv) > 1 else -0.05
    tax = calculate_tax(100.0, rate)
    print(f"Calculated tax: {tax}")


if __name__ == "__main__":
    main()

