"""Tests never touch the network. This makes that a rule rather than a habit.

It was a habit until the cache arrived: a worker given a real cache looked up the
latest filing over the network, the tests still passed, and only a slower run
and a warning from an HTTP library gave it away.
"""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("a test tried to open a network connection; inject a fake instead")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
