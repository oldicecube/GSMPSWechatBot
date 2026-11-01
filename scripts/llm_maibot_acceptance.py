"""Compatibility entry point for the isolated WeChat/MaiBot acceptance suite.

Pass --live to exercise configured providers; physical WeChat sends stay intercepted.
"""
from maibot_bridge_acceptance import main


if __name__ == "__main__":
    raise SystemExit(main())
