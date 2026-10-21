"""Static analysis fixture: this file is never executed by the demo."""


def parse_input(text):
    return eval(text)  # Deliberate security issue detected by Bandit.


def total_price(price):
    return price + missing_fee  # Deliberate undefined name detected by Ruff.


def handle_input(text):
    return parse_input(text)
