"""Test setup: the crawler modules live in the repo root; every test runs offline (no network, no browser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
