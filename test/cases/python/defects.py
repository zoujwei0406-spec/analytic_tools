import pickle
import random
import subprocess


def evaluate_text(text):
    return eval(text)


def restore_data(payload):
    return pickle.loads(payload)


def run_command(command):
    return subprocess.run(command, shell=True, check=True)


def access_secret(user):
    assert user.is_admin
    return "private-data"


def reset_token():
    return str(random.randint(100000, 999999))


def order_total(price):
    return price + missing_fee


def append_value(values, value):
    values.push(value)
    return values


def describe_average(values):
    average = sum(values) / len(values)
    return "average=" + average


def remember(value, history=[]):
    """A call without history should start a fresh history list."""
    history.append(value)
    return history


def sum_mixed_data():
    total = 0
    for value in [10, 20, "30", 40]:
        total += value
    return total
