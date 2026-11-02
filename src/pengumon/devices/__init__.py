"""Devices: a driver (pengumon.drivers), the schedule it is polled on, and a monitor that keeps its snapshot.

The devices come from the `devices:` list in config.yaml (devices/settings.py); devices/registry.py builds them. This
file re-exports nothing: import from the module that defines a name, so importing the settings never loads the rest.
"""
